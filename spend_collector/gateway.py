"""Pre-spend gateway decisions.

The collector is read-only after money moves. The gateway is the small inline
piece agents can call before they spend: it checks policy + ledger history and
returns allow/deny.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from .adapters import _price
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


def decide(store: SpendStore, policy: dict, req: GuardRequest) -> GuardDecision:
    """Return a pre-spend allow/deny decision.

    Policy intentionally stays simple JSON so agents can generate and review it:
    global allow lists, optional per-agent overrides, budget caps, and amount caps.
    """
    reasons: list[str] = []
    deny: list[str] = []
    agent = _agent_policy(policy, req.x_agent_id)

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

    merchant_key = req.merchant_key()
    allowed_merchants = set(_list(agent.get("merchants")) or _list(policy.get("merchants")))
    if allowed_merchants and merchant_key not in allowed_merchants:
        deny.append(f"merchant {merchant_key} is not allowed")
    elif policy.get("deny_new_merchants") and not _merchant_seen(store, req):
        deny.append(f"merchant {merchant_key} has no ledger history")
    elif policy.get("warn_new_merchants") and not _merchant_seen(store, req):
        reasons.append(f"merchant {merchant_key} has no ledger history")

    if deny:
        return GuardDecision("deny", deny, asdict(req))
    if not reasons:
        reasons.append("policy checks passed")
    return GuardDecision("allow", reasons, asdict(req))


def validate_policy(policy: dict) -> list[str]:
    errors: list[str] = []
    root_keys = {
        "agents_allowed", "rails", "budgets", "max_amount", "max_amount_by_rail",
        "warn_new_merchants", "deny_new_merchants", "agents", "merchants",
        "targets", "providers", "gateway_tokens", "reservation_ttl_seconds",
    }
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
        if "base_url" not in provider:
            errors.append(f"provider {name} missing base_url")
        if "api_key_env" not in provider:
            errors.append(f"provider {name} missing api_key_env")
        if "amount" not in provider and "max_estimated_amount" not in provider:
            errors.append(f"provider {name} missing amount or max_estimated_amount")

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
    usage = (data.get("usage") if isinstance(data, dict) else None) or {}
    if not usage:
        return None
    pname = str(provider.get("provider", "openai"))
    model = str(data.get("model") or guard_payload.get("service") or pname)
    prompt = int(usage.get("prompt_tokens", 0) or 0)
    completion = int(usage.get("completion_tokens", 0) or 0)
    rid = str(data.get("id") or f"{guard_payload.get('agent', 'agent')}:{prompt}:{completion}")
    event = SpendEvent(
        event_id=f"gw:{rid}",
        event_time=datetime.now(tz=timezone.utc).isoformat(),
        rail="llm_token",
        provider_name=pname,
        service_name=model,
        billed_cost=_price(model, prompt, completion),
        billing_currency="USD",
        consumed_quantity=prompt + completion,
        pricing_unit="token",
        x_agent_id=str(guard_payload.get("agent", "unknown")),
        x_budget_id=str(guard_payload.get("budget", "default")),
        x_session_id=str(guard_payload.get("session", "")),
        x_merchant_id=pname,
        x_receipt_ref=str(data.get("id", "")),
        x_source_event=source_ref(pname, {"id": data.get("id"), "model": model, "usage": usage}),
    )
    store.ingest([event])
    return event
