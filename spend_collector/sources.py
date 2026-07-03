"""Live, read-only pulls from provider cost APIs and settlement rails.

- LLM token: Anthropic Cost & Usage API (admin key).
- Stripe: succeeded PaymentIntents via the Events API (restricted read key).
- x402: USDC Transfer events into a merchant address on Base, via public RPC.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

from .schema import SpendEvent, source_ref


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _request_json(req: urllib.request.Request, *, timeout: int | None = None,
                  retries: int | None = None) -> object:
    timeout = timeout if timeout is not None else _env_int("SPEND_HTTP_TIMEOUT", 30)
    retries = retries if retries is not None else _env_int("SPEND_HTTP_RETRIES", 3)
    last_error: BaseException | None = None

    for attempt in range(max(1, retries)):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in {429, 500, 502, 503, 504} or attempt == retries - 1:
                raise
        except (TimeoutError, urllib.error.URLError) as exc:
            last_error = exc
            if attempt == retries - 1:
                raise
        time.sleep(min(2 ** attempt, 8))

    raise RuntimeError(f"request failed after {retries} attempts") from last_error

# --- LLM token cost (Anthropic) ---
_ANTHROPIC_URL = "https://api.anthropic.com/v1/organizations/cost_report"


def env_admin_key() -> str | None:
    return os.environ.get("ANTHROPIC_ADMIN_KEY")


def fetch_anthropic_cost_report(admin_key: str, days: int = 7) -> list[dict]:
    """Pull daily cost buckets grouped by api_key_id + model."""
    start = (date.today() - timedelta(days=days)).isoformat()
    url = f"{_ANTHROPIC_URL}?starting_at={start}&group_by[]=api_key_id&group_by[]=model"
    req = urllib.request.Request(url, headers={
        "x-api-key": admin_key,
        "anthropic-version": "2023-06-01",
    })
    data = _request_json(req)

    rows: list[dict] = []
    for bucket in data.get("data", []):
        ts = bucket.get("starting_at", start)
        for item in bucket.get("results", []):
            # cost_report returns `amount` in lowest currency units (cents) as a string,
            # e.g. "123.45" == $1.2345. Convert to USD. (Verify against a real response.)
            cents = float(item.get("amount", item.get("cost", 0)) or 0)
            rows.append({
                "amount_usd": cents / 100,
                # cost_report groups by workspace/description; api_key_id grouping may be
                # ignored (it's on the usage report). Fall back to workspace for attribution.
                "api_key_id": item.get("api_key_id") or item.get("workspace_id") or "unknown",
                "model": item.get("model") or "anthropic",
                "event_time": ts,
            })
    return rows


def from_llm_cost_rows(rows, key_to_agent=None, key_to_budget=None) -> list[SpendEvent]:
    """Cost-API rows (cost already USD) -> SpendEvents. Row may carry `provider`."""
    key_to_agent = key_to_agent or {}
    key_to_budget = key_to_budget or {}
    out = []
    for r in rows:
        key = r["api_key_id"]
        provider = r.get("provider", "anthropic")
        out.append(SpendEvent(
            event_id=f"llmcost:{provider}:{key}:{r['model']}:{r['event_time']}",
            event_time=r["event_time"],
            rail="llm_token",
            provider_name=provider,
            service_name=r["model"],
            billed_cost=r["amount_usd"],
            billing_currency="USD",
            consumed_quantity=0,
            pricing_unit="usd",
            x_agent_id=key_to_agent.get(key, key),
            x_budget_id=key_to_budget.get(key, "default"),
            x_receipt_ref=key,
            x_source_event=source_ref(provider, r),
        ))
    return out


# --- LLM token cost (OpenAI) ---
_OPENAI_COSTS_URL = "https://api.openai.com/v1/organization/costs"


def env_openai_key() -> str | None:
    return os.environ.get("OPENAI_ADMIN_KEY")


def fetch_openai_costs(admin_key: str, days: int = 7) -> list[dict]:
    """Pull daily cost buckets from the OpenAI Costs API, grouped by line_item + api_key_id.

    OpenAI's `amount` is an object {value, currency} with value already in the main
    unit (USD dollars, unlike Anthropic's cents). Verify against a real response.
    """
    start_time = int((datetime.now(tz=timezone.utc) - timedelta(days=days)).timestamp())
    params = {"start_time": str(start_time), "bucket_width": "1d",
              "group_by[]": ["line_item", "api_key_id"], "limit": "180"}
    url = f"{_OPENAI_COSTS_URL}?{urllib.parse.urlencode(params, doseq=True)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {admin_key}"})
    data = _request_json(req)

    rows: list[dict] = []
    for bucket in data.get("data", []):
        ts = datetime.fromtimestamp(
            int(bucket.get("start_time") or start_time), tz=timezone.utc).isoformat()
        for item in bucket.get("results", []):
            amount = item.get("amount") or {}
            rows.append({
                "amount_usd": float(amount.get("value", 0) or 0),
                "api_key_id": item.get("api_key_id") or item.get("project_id") or "unknown",
                "model": item.get("line_item") or "openai",
                "event_time": ts,
                "provider": "openai",
            })
    return rows


# --- Stripe card / PaymentIntent rail (read-only Events API) ---
_STRIPE_EVENTS_URL = "https://api.stripe.com/v1/events"


def env_stripe_key() -> str | None:
    return os.environ.get("STRIPE_SECRET_KEY") or os.environ.get("STRIPE_API_KEY")


def fetch_stripe_payment_intent_events(secret_key: str, days: int = 7, limit: int = 100) -> list[dict]:
    """Pull successful PaymentIntent events from Stripe's read-only Events API.

    Stripe Events have limited retention, so production use should poll frequently
    and persist rows locally. Use a restricted read key when possible.
    """
    created_gte = int((datetime.now(tz=timezone.utc) - timedelta(days=days)).timestamp())
    params = {
        "type": "payment_intent.succeeded",
        "created[gte]": str(created_gte),
        "limit": str(min(max(limit, 1), 100)),
    }
    rows: list[dict] = []
    starting_after = None
    while True:
        if starting_after:
            params["starting_after"] = starting_after
        url = f"{_STRIPE_EVENTS_URL}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {secret_key}"})
        data = _request_json(req)
        batch = data.get("data", [])
        rows.extend(batch)
        if not data.get("has_more") or not batch:
            return rows
        starting_after = batch[-1]["id"]


# --- x402 / on-chain USDC settlements (Base), read-only via public RPC ---
BASE_RPC = "https://mainnet.base.org"
USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def env_pay_to() -> str | None:
    return os.environ.get("X402_PAY_TO")


def env_usdc_pay_to() -> str | None:
    return os.environ.get("USDC_PAY_TO") or os.environ.get("X402_PAY_TO")


def _rpc(method: str, params: list, rpc_url: str) -> object:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(rpc_url, data=body, headers={"content-type": "application/json"})
    out = _request_json(req)
    if out.get("error"):
        raise RuntimeError(out["error"])
    return out["result"]


def decode_transfer_log(log: dict) -> dict:
    """Decode an ERC-20 Transfer log -> {from, to, amount_raw, tx, block}."""
    topics = log["topics"]
    return {
        "from": "0x" + topics[1][-40:],
        "to": "0x" + topics[2][-40:],
        "amount_raw": int(log["data"], 16),
        "tx": log["transactionHash"],
        "block": int(log["blockNumber"], 16),
    }


def fetch_base_usdc_transfers(pay_to: str, *, rpc_url: str = BASE_RPC, usdc: str = USDC_BASE,
                              lookback_blocks: int = 2000, decimals: int = 6) -> list[dict]:
    """Read-only: USDC Transfer events into `pay_to` on Base."""
    latest = int(_rpc("eth_blockNumber", [], rpc_url), 16)
    from_block = max(0, latest - lookback_blocks)
    pad_to = "0x" + "0" * 24 + pay_to.lower().replace("0x", "")
    logs = _rpc("eth_getLogs", [{
        "address": usdc, "fromBlock": hex(from_block), "toBlock": "latest",
        "topics": [_TRANSFER_TOPIC, None, pad_to],
    }], rpc_url)

    ts_cache: dict[int, str] = {}
    rows = []
    for log in logs:
        d = decode_transfer_log(log)
        if d["block"] not in ts_cache:
            blk = _rpc("eth_getBlockByNumber", [hex(d["block"]), False], rpc_url)
            ts_cache[d["block"]] = datetime.fromtimestamp(
                int(blk["timestamp"], 16), tz=timezone.utc).isoformat()
        rows.append({
            "transaction": d["tx"], "amount": f"{d['amount_raw'] / 10 ** decimals:.6f}",
            "asset": "USDC", "network": "base", "payer": d["from"], "pay_to": d["to"],
            "resource": "onchain:transfer", "event_time": ts_cache[d["block"]],
            "agent_id": d["from"], "budget_id": "default",
        })
    return rows
