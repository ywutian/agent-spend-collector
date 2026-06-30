"""Pre-spend gateway decisions.

The collector is read-only after money moves. The gateway is the small inline
piece agents can call before they spend: it checks policy + ledger history and
returns allow/deny.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

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


def _spent_for_budget(store: SpendStore, budget: str) -> float:
    row = store.db.execute(
        "SELECT COALESCE(SUM(billed_cost), 0) AS spent FROM spend_events WHERE x_budget_id = ?",
        (budget,),
    ).fetchone()
    return float(row["spent"] or 0)


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

    budgets = dict(policy.get("budgets", {}))
    budgets.update(agent.get("budgets_caps", {}))
    cap = budgets.get(req.x_budget_id)
    if cap is not None:
        spent = _spent_for_budget(store, req.x_budget_id)
        if spent + req.amount > float(cap):
            deny.append(
                f"budget {req.x_budget_id} would exceed cap "
                f"{spent + req.amount:.2f}/{float(cap):.2f}"
            )
        else:
            reasons.append(f"budget {req.x_budget_id} remaining {float(cap) - spent - req.amount:.2f}")

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
