"""Pre-spend gateway decisions.

The collector is read-only after money moves. The gateway is the small inline
piece agents can call before they spend: it checks policy + ledger history and
returns allow/deny.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from .adapters import _price
from .providers import llm_provider, usage_tokens
from .schema import SpendEvent, source_ref
from .store import SpendStore


@dataclass(frozen=True)
class GuardRequest:
    x_agent_id: str
    rail: str
    amount: float
    x_budget_id: str
    provider_name: str = ""
    service_name: str = ""
    x_merchant_id: str = ""
    x_session_id: str = ""

    def merchant_key(self) -> str:
        merchant = self.x_merchant_id or self.service_name or "unknown"
        provider = self.provider_name or self.rail
        return f"{self.rail}:{provider}:{merchant}"


@dataclass(frozen=True)
class GuardDecision:
    decision: str
    reasons: list[str]
    request: dict[str, Any]
    request_id: str = ""
    reservation_id: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["allowed"] = self.allowed
        return out


def _list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _agent_policy(policy: dict, agent: str) -> dict:
    return policy.get("agents", {}).get(agent, {})


class PolicyError(ValueError):
    pass


def _spent_for_budget(store: SpendStore, budget: str) -> float:
    row = store.db.execute(
        "SELECT COALESCE(SUM(billed_cost), 0) AS spent FROM spend_events WHERE x_budget_id = ?",
        (budget,),
    ).fetchone()
    return float(row["spent"] or 0)


def _spent_since(store: SpendStore, budget: str, since_iso: str) -> float:
    # ponytail: string compare of same-format ISO timestamps = chronological; gateway
    # events all use datetime.now(utc).isoformat(), so this is correct for live spend.
    row = store.db.execute(
        "SELECT COALESCE(SUM(billed_cost), 0) AS spent FROM spend_events "
        "WHERE x_budget_id = ? AND event_time > ?",
        (budget, since_iso),
    ).fetchone()
    return float(row["spent"] or 0)


def _hourly_cap(policy: dict, budget: str) -> float | None:
    limits = policy.get("max_amount_per_hour")
    if isinstance(limits, dict):
        cap = limits.get(budget)
    else:
        cap = limits  # a bare number applies to every budget
    return float(cap) if cap is not None else None


def _budget_cap(policy: dict, agent: str, budget: str) -> float | None:
    agent_policy = _agent_policy(policy, agent)
    budgets = dict(policy.get("budgets", {}))
    budgets.update(agent_policy.get("budgets_caps", {}))
    cap = budgets.get(budget)
    return float(cap) if cap is not None else None


def _merchant_seen(store: SpendStore, req: GuardRequest) -> bool:
    row = store.db.execute(
        "SELECT 1 FROM spend_events WHERE x_agent_id = ? AND rail = ? "
        "AND provider_name = ? AND (x_merchant_id = ? OR service_name = ?) LIMIT 1",
        (
            req.x_agent_id,
            req.rail,
            req.provider_name,
            req.x_merchant_id or req.service_name,
            req.service_name or req.x_merchant_id,
        ),
    ).fetchone()
    return row is not None


def _parse_alert_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        if len(value) == 10:
            value = f"{value}T00:00:00+00:00"
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _latest_event_time(store: SpendStore) -> datetime | None:
    row = store.db.execute("SELECT MAX(event_time) AS event_time FROM spend_events").fetchone()
    return _parse_alert_time(row["event_time"] if row else "")


def _alert_within_lookback(alert, anchor: datetime | None, lookback_hours: float) -> bool:
    event_time = _parse_alert_time(getattr(alert, "event_time", ""))
    if event_time is None:
        return False
    if anchor is None:
        anchor = datetime.now(timezone.utc)
    return event_time >= anchor - timedelta(hours=lookback_hours)


_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),        # OpenAI / compatible
    re.compile(r"sk-ant-[A-Za-z0-9-]{20,}"),     # Anthropic
    re.compile(r"AKIA[0-9A-Z]{16}"),             # AWS access key id
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),   # GitHub token
)


def inspect_content(body: bytes, policy: dict) -> list[str]:
    """Deterministic request-content checks for the forwarding gateway: an oversized
    payload (token bomb), configured deny patterns (e.g. prompt-injection markers),
    and secrets/keys being sent outbound. Returns deny reasons (empty = clean).

    Opt-in via a `content_guard` policy block. Reads the body in memory for the
    decision only -- content is never stored, matching the gateway's audit stance.
    """
    guard = policy.get("content_guard")
    if not guard or not body:
        return []
    reasons: list[str] = []
    max_bytes = guard.get("max_bytes")
    if max_bytes is not None and len(body) > int(max_bytes):
        reasons.append(f"request body {len(body)} bytes exceeds max_bytes {int(max_bytes)}")
    text = body.decode("utf-8", "ignore")
    lowered = text.lower()
    for pat in _list(guard.get("deny_patterns")):
        if str(pat).lower() in lowered:
            reasons.append(f"content matched deny pattern: {pat}")
    if guard.get("deny_secrets") and any(rx.search(text) for rx in _SECRET_PATTERNS):
        reasons.append("request appears to contain a secret/key being sent outbound")
    return reasons


def decide(store: SpendStore, policy: dict, req: GuardRequest) -> GuardDecision:
    """Return a pre-spend allow/deny decision.

    Policy intentionally stays simple JSON so agents can generate and review it:
    global allow lists, optional per-agent overrides, budget caps, and amount caps.
    """
    reasons: list[str] = []
    deny: list[str] = []
    agent = _agent_policy(policy, req.x_agent_id)

    # Kill-switch: a frozen agent or budget is denied outright (break-glass).
    if req.x_agent_id in set(_list(policy.get("frozen_agents"))):
        deny.append(f"agent {req.x_agent_id} is frozen (kill-switch)")
    if req.x_budget_id in set(_list(policy.get("frozen_budgets"))):
        deny.append(f"budget {req.x_budget_id} is frozen (kill-switch)")

    allowed_agents = set(_list(policy.get("agents_allowed")))
    if allowed_agents and req.x_agent_id not in allowed_agents:
        deny.append(f"agent {req.x_agent_id} is not allowed")

    allowed_rails = set(_list(agent.get("rails")) or _list(policy.get("rails")))
    if allowed_rails and req.rail not in allowed_rails:
        deny.append(f"rail {req.rail} is not allowed for {req.x_agent_id}")

    allowed_budgets = set(_list(agent.get("budgets")))
    if allowed_budgets and req.x_budget_id not in allowed_budgets:
        deny.append(f"budget {req.x_budget_id} is not allowed for {req.x_agent_id}")

    max_amount = agent.get("max_amount", policy.get("max_amount"))
    rail_max = policy.get("max_amount_by_rail", {}).get(req.rail)
    if rail_max is not None:
        max_amount = min(float(max_amount), float(rail_max)) if max_amount is not None else rail_max
    if max_amount is not None and req.amount > float(max_amount):
        deny.append(f"amount {req.amount:.2f} exceeds max {float(max_amount):.2f}")

    cap = _budget_cap(policy, req.x_agent_id, req.x_budget_id)
    if cap is not None:
        spent = _spent_for_budget(store, req.x_budget_id)
        reserved = store.active_reservations_total(req.x_budget_id)
        if spent + reserved + req.amount > float(cap):
            deny.append(
                f"budget {req.x_budget_id} would exceed cap "
                f"{spent + reserved + req.amount:.2f}/{float(cap):.2f}"
            )
        else:
            reasons.append(f"budget {req.x_budget_id} remaining {float(cap) - spent - reserved - req.amount:.2f}")

    rate_cap = _hourly_cap(policy, req.x_budget_id)  # velocity: catch runaway loops fast
    if rate_cap is not None:
        since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        recent = _spent_since(store, req.x_budget_id, since)
        if recent + req.amount > rate_cap:
            deny.append(f"budget {req.x_budget_id} hourly rate {recent + req.amount:.2f}/{rate_cap:.2f} exceeded")
        else:
            reasons.append(f"budget {req.x_budget_id} hourly {recent + req.amount:.2f}/{rate_cap:.2f}")

    merchant_key = req.merchant_key()
    allowed_merchants = set(_list(agent.get("merchants")) or _list(policy.get("merchants")))
    if allowed_merchants and merchant_key not in allowed_merchants:
        deny.append(f"merchant {merchant_key} is not allowed")
    elif policy.get("deny_new_merchants") and not _merchant_seen(store, req):
        deny.append(f"merchant {merchant_key} has no ledger history")
    elif policy.get("warn_new_merchants") and not _merchant_seen(store, req):
        reasons.append(f"merchant {merchant_key} has no ledger history")

    # Behavioral: block a call while its agent is recently flagged by a detector,
    # turning after-the-fact alerts into in-flight interception. `block_on_anomaly`
    # is true (block on any high-severity alert) or a list of kinds to block on.
    # ponytail: runs the detectors per guarded request; cache/precompute if the
    # added latency matters at volume. Skipped when a cheaper check already denied.
    block = policy.get("block_on_anomaly")
    if block and not deny:
        from .detectors import run_all
        kinds = None if block is True else set(_list(block))
        budgets = {str(k): float(v) for k, v in (policy.get("budgets") or {}).items()}
        lookback_hours = float(policy.get("block_on_anomaly_lookback_hours", 24))
        anchor = _latest_event_time(store)
        for a in run_all(store, budgets):
            if a.subject != req.x_agent_id:
                continue
            if not _alert_within_lookback(a, anchor, lookback_hours):
                continue
            if (kinds is None and a.severity == "high") or (kinds is not None and a.kind in kinds):
                deny.append(f"agent {req.x_agent_id} blocked on anomaly: {a.kind} ({a.detail})")
                break

    if deny:
        return GuardDecision("deny", deny, asdict(req))
    if not reasons:
        reasons.append("policy checks passed")
    return GuardDecision("allow", reasons, asdict(req))


def validate_policy(policy: dict) -> list[str]:
    errors: list[str] = []
    root_keys = {
        "agents_allowed", "rails", "budgets", "max_amount", "max_amount_by_rail",
        "max_amount_per_hour", "warn_new_merchants", "deny_new_merchants", "agents",
        "merchants", "targets", "providers", "gateway_tokens", "reservation_ttl_seconds",
        "x402_resources", "content_guard", "frozen_agents", "frozen_budgets",
        "block_on_anomaly", "block_on_anomaly_lookback_hours",
    }
    content_guard_keys = {"max_bytes", "deny_patterns", "deny_secrets"}
    agent_keys = {"budgets", "rails", "max_amount", "budgets_caps", "merchants"}
    target_keys = {
        "url", "method", "rail", "provider", "merchant", "service", "amount",
        "budget", "headers", "headers_env", "timeout",
    }
    provider_keys = {
        "base_url", "api_key_env", "rail", "provider", "merchant",
        "service_from_body", "amount", "budget", "auth_header", "auth_prefix",
        "method", "timeout", "agent", "max_estimated_amount",
    }
    x402_resource_keys = {
        "url", "resource_url", "method", "amount", "amount_units", "asset", "asset_decimals",
        "asset_name", "asset_version", "x402_version",
        "pay_to", "network", "scheme", "max_timeout_seconds", "description",
        "mime_type", "budget", "merchant", "service", "facilitator_url",
        "facilitator_auth_env", "headers", "headers_env", "timeout", "agent",
    }

    if not isinstance(policy, dict):
        return ["policy must be a JSON object"]
    for key in policy:
        if key not in root_keys:
            errors.append(f"unknown policy key: {key}")
    def has_raw_api_key(obj) -> bool:
        if isinstance(obj, dict):
            return any(k == "api_key" or has_raw_api_key(v) for k, v in obj.items())
        if isinstance(obj, list):
            return any(has_raw_api_key(v) for v in obj)
        return False

    if has_raw_api_key(policy):
        errors.append("policy must not contain raw api_key values; use api_key_env")

    if "budgets" in policy and not isinstance(policy["budgets"], dict):
        errors.append("budgets must be an object")
    if "reservation_ttl_seconds" in policy and int(policy["reservation_ttl_seconds"]) <= 0:
        errors.append("reservation_ttl_seconds must be positive")
    if "block_on_anomaly_lookback_hours" in policy and float(policy["block_on_anomaly_lookback_hours"]) <= 0:
        errors.append("block_on_anomaly_lookback_hours must be positive")

    guard = policy.get("content_guard")
    if guard is not None:
        if not isinstance(guard, dict):
            errors.append("content_guard must be an object")
        else:
            for key in guard:
                if key not in content_guard_keys:
                    errors.append(f"unknown content_guard key: {key}")

    for agent, cfg in policy.get("agents", {}).items():
        if not isinstance(cfg, dict):
            errors.append(f"agent {agent} must be an object")
            continue
        for key in cfg:
            if key not in agent_keys:
                errors.append(f"unknown agent key for {agent}: {key}")

    has_forwarding = bool(policy.get("providers") or policy.get("targets"))
    if has_forwarding and not policy.get("gateway_tokens"):
        errors.append("providers or targets require gateway_tokens or SPEND_GATEWAY_TOKEN")

    for name, target in policy.get("targets", {}).items():
        if not isinstance(target, dict):
            errors.append(f"target {name} must be an object")
            continue
        for key in target:
            if key not in target_keys:
                errors.append(f"unknown target key for {name}: {key}")
        for required in ("url", "rail", "amount"):
            if required not in target:
                errors.append(f"target {name} missing {required}")
        for key, value in target.get("headers", {}).items():
            if "authorization" in key.lower() or str(value).startswith(("sk-", "rk_")):
                errors.append(f"target {name} header {key} looks like a raw secret; use headers_env")

    for name, provider in policy.get("providers", {}).items():
        if not isinstance(provider, dict):
            errors.append(f"provider {name} must be an object")
            continue
        for key in provider:
            if key not in provider_keys:
                errors.append(f"unknown provider key for {name}: {key}")
        known = llm_provider(name)  # base_url/api_key_env optional for a known provider name
        if "base_url" not in provider and not (known and known.get("base_url")):
            errors.append(f"provider {name} missing base_url (or use a known provider name)")
        if "api_key_env" not in provider and not (known and known.get("api_key_env")):
            errors.append(f"provider {name} missing api_key_env (or use a known provider name)")
        if "amount" not in provider and "max_estimated_amount" not in provider:
            errors.append(f"provider {name} missing amount or max_estimated_amount")

    for name, resource in policy.get("x402_resources", {}).items():
        if not isinstance(resource, dict):
            errors.append(f"x402 resource {name} must be an object")
            continue
        for key in resource:
            if key not in x402_resource_keys:
                errors.append(f"unknown x402 resource key for {name}: {key}")
        for required in ("url", "amount", "pay_to", "asset", "facilitator_url"):
            if required not in resource:
                errors.append(f"x402 resource {name} missing {required}")
        for key, value in resource.get("headers", {}).items():
            if "authorization" in key.lower() or str(value).startswith(("sk-", "rk_")):
                errors.append(f"x402 resource {name} header {key} looks like a raw secret; use headers_env")

    return errors


def require_valid_policy(policy: dict, *, env_token: str | None = None) -> None:
    check = dict(policy)
    if env_token and not check.get("gateway_tokens"):
        check["gateway_tokens"] = [env_token]
    errors = validate_policy(check)
    if errors:
        raise PolicyError("; ".join(errors))


def audit_config(policy: dict, *, db_path: str = "spend.db", out_dir: str = "artifacts",
                 env_token_configured: bool = False) -> dict:
    env_vars = set()
    hosts = set()
    for provider in policy.get("providers", {}).values():
        if provider.get("api_key_env"):
            env_vars.add(provider["api_key_env"])
        if provider.get("base_url"):
            hosts.add(urlsplit(provider["base_url"]).netloc)
    for target in policy.get("targets", {}).values():
        if target.get("url"):
            hosts.add(urlsplit(target["url"]).netloc)
        for value in target.get("headers_env", {}).values():
            env_vars.add(value)
    for resource in policy.get("x402_resources", {}).values():
        if resource.get("url"):
            hosts.add(urlsplit(resource["url"]).netloc)
        if resource.get("facilitator_url"):
            hosts.add(urlsplit(resource["facilitator_url"]).netloc)
        if resource.get("facilitator_auth_env"):
            env_vars.add(resource["facilitator_auth_env"])
        for value in resource.get("headers_env", {}).values():
            env_vars.add(value)
    if policy.get("providers") or policy.get("targets"):
        env_vars.add("SPEND_GATEWAY_TOKEN")
    return {
        "env_vars_read": sorted(env_vars),
        "outbound_hosts": sorted(h for h in hosts if h),
        "files_written": [db_path, f"{out_dir}/report.html", f"{out_dir}/alerts.json", f"{out_dir}/run-summary.json"],
        "will_not_store": ["prompts", "responses", "request bodies", "provider keys", "gateway tokens"],
        "gateway_token_configured": bool(policy.get("gateway_tokens") or env_token_configured),
    }


def cap_for_request(policy: dict, req: GuardRequest) -> float | None:
    return _budget_cap(policy, req.x_agent_id, req.x_budget_id)


def rate_cap_for_request(policy: dict, req: GuardRequest) -> float | None:
    return _hourly_cap(policy, req.x_budget_id)


def record_forwarded_spend(store: SpendStore, raw: bytes, provider: dict, guard_payload: dict):
    """Record a forwarded LLM call as a spend event from the response's token usage.

    Closes the loop: a successful forward lands in the ledger with per-agent / budget
    attribution. Only billing metadata (id, model, usage) is read -- no prompt or
    response content is stored. Returns the event, or None when there is no usage
    (e.g. a streamed response without usage stats).
    """
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    prompt, completion = usage_tokens(data)  # OpenAI / Anthropic / Gemini / Cohere shapes
    if not prompt and not completion:
        return None
    pname = str(provider.get("provider", "openai"))
    model = str(data.get("model") or data.get("modelVersion") or guard_payload.get("service") or pname)
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    total = int(usage.get("total_tokens", prompt + completion) or prompt + completion)
    billed_cost = (
        float(usage["cost"])
        if pname == "openrouter" and usage.get("cost") is not None
        else _price(model, prompt, completion)
    )
    rid = str(data.get("id") or f"{guard_payload.get('agent', 'agent')}:{prompt}:{completion}")
    event = SpendEvent(
        event_id=f"gw:{rid}",
        event_time=datetime.now(tz=timezone.utc).isoformat(),
        rail="llm_token",
        provider_name=pname,
        service_name=model,
        billed_cost=billed_cost,
        billing_currency="USD",
        consumed_quantity=total,
        pricing_unit="token",
        x_agent_id=str(guard_payload.get("agent", "unknown")),
        x_budget_id=str(guard_payload.get("budget", "default")),
        x_session_id=str(guard_payload.get("session", "")),
        x_merchant_id=pname,
        x_receipt_ref=str(data.get("id", "")),
        x_source_event=source_ref(pname, {"id": data.get("id"), "model": model, "tokens": [prompt, completion]}),
    )
    store.ingest([event])
    return event


def record_target_spend(store: SpendStore, guard_payload: dict, request_id: str):
    """Record a forwarded non-LLM tool/API call. These meter differently per vendor
    (audio seconds, characters, pages, per-call), so there is no universal usage to
    parse -- we record the policy's flat per-call `amount` and release the hold.
    For exact metered cost, add a per-tool extractor or reconcile from billing.
    """
    amount = float(guard_payload.get("amount", 0) or 0)
    if amount <= 0 or not request_id:
        return None
    provider = str(guard_payload.get("provider", "tool"))
    service = str(guard_payload.get("service") or guard_payload.get("merchant") or "tool")
    event = SpendEvent(
        event_id=f"gw:target:{request_id}",
        event_time=datetime.now(tz=timezone.utc).isoformat(),
        rail=str(guard_payload.get("rail", "api")),
        provider_name=provider,
        service_name=service,
        billed_cost=amount,
        billing_currency="USD",
        consumed_quantity=1,
        pricing_unit="call",
        x_agent_id=str(guard_payload.get("agent", "unknown")),
        x_budget_id=str(guard_payload.get("budget", "default")),
        x_session_id=str(guard_payload.get("session", "")),
        x_merchant_id=str(guard_payload.get("merchant", "")),
        x_receipt_ref=request_id,
        x_source_event=source_ref(provider, {"target": service, "amount": amount, "request_id": request_id}),
        charge_category="Purchase",
    )
    store.ingest([event])
    store.release_reservation(request_id)
    return event


def record_x402_settlement(
    store: SpendStore,
    guard_payload: dict,
    request_id: str,
    requirements: dict,
    verify_result: dict,
    settle_result: dict,
):
    """Record an x402 middleware settlement without storing the signed payment payload."""
    amount_units = float(
        settle_result.get("amount")
        or (settle_result.get("extra") or {}).get("chargedAmount")
        or requirements.get("amount")
        or 0
    )
    decimals = int(guard_payload.get("asset_decimals", 6) or 6)
    amount = amount_units / (10 ** decimals)
    if amount <= 0:
        amount = float(guard_payload.get("amount", 0) or 0)
    transaction = str(
        settle_result.get("transaction")
        or settle_result.get("txHash")
        or settle_result.get("tx_hash")
        or request_id
    )
    asset_name = str((requirements.get("extra") or {}).get("name") or guard_payload.get("asset", "USDC"))
    network = str(settle_result.get("network") or requirements.get("network") or guard_payload.get("network", ""))
    payer = str(verify_result.get("payer") or settle_result.get("payer") or "unknown")
    service = str(guard_payload.get("service") or "x402")
    event = SpendEvent(
        event_id=f"x402:{transaction}",
        event_time=datetime.now(tz=timezone.utc).isoformat(),
        rail="api_x402",
        provider_name="x402",
        service_name=service,
        billed_cost=amount,
        billing_currency=asset_name,
        consumed_quantity=1,
        pricing_unit="call",
        x_agent_id=str(guard_payload.get("agent", payer)),
        x_budget_id=str(guard_payload.get("budget", "default")),
        x_session_id=str(guard_payload.get("session", "")),
        x_merchant_id=str(requirements.get("payTo") or guard_payload.get("merchant", "")),
        x_receipt_ref=transaction,
        x_source_event=source_ref("x402", {
            "transaction": transaction,
            "payer": payer,
            "network": network,
            "amount": requirements.get("amount"),
            "payTo": requirements.get("payTo"),
            "resource": service,
            "request_id": request_id,
            "success": settle_result.get("success"),
        }),
        charge_category="Purchase",
    )
    store.ingest([event])
    store.release_reservation(request_id)
    return event
