"""Live, read-only pulls from provider cost APIs and settlement rails.

- LLM token: Anthropic Cost & Usage API (admin key).
- Stripe: succeeded PaymentIntents via the Events API (restricted read key).
- x402: USDC Transfer events into a merchant address on Base, via public RPC.
"""
from __future__ import annotations

import json
import os
import hashlib
import hmac
import csv
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


# --- LLM token cost (OpenRouter generation metadata) ---
_OPENROUTER_GENERATION_URL = "https://openrouter.ai/api/v1/generation"


def env_openrouter_key() -> str | None:
    return os.environ.get("OPENROUTER_API_KEY")


def fetch_openrouter_generations(api_key: str, generation_ids: list[str]) -> list[dict]:
    """Pull OpenRouter generation metadata by id.

    OpenRouter exposes generation cost/token metadata per generation id rather
    than as a broad account export. For live gateway traffic, prefer recording
    the `usage` object returned with the response.
    """
    rows: list[dict] = []
    for generation_id in generation_ids:
        params = urllib.parse.urlencode({"id": generation_id})
        req = urllib.request.Request(
            f"{_OPENROUTER_GENERATION_URL}?{params}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        data = _request_json(req)
        row = data.get("data", data)
        if isinstance(row, dict):
            rows.append(row)
    return rows


def from_openrouter_generation_rows(rows, key_to_agent=None, key_to_budget=None) -> list[SpendEvent]:
    """OpenRouter /generation rows -> SpendEvents."""
    key_to_agent = key_to_agent or {}
    key_to_budget = key_to_budget or {}
    out = []
    for r in rows:
        generation_id = str(r.get("id") or r.get("upstream_id") or r.get("request_id"))
        model = str(r.get("model") or r.get("router") or "openrouter")
        external_user = str(r.get("external_user") or r.get("session_id") or "openrouter")
        prompt = int(r.get("tokens_prompt", r.get("native_tokens_prompt", 0)) or 0)
        completion = int(r.get("tokens_completion", r.get("native_tokens_completion", 0)) or 0)
        total = prompt + completion
        out.append(SpendEvent(
            event_id=f"openrouter:{generation_id}",
            event_time=str(r.get("created_at") or datetime.now(tz=timezone.utc).isoformat()),
            rail="llm_token",
            provider_name="openrouter",
            service_name=model,
            billed_cost=float(r.get("total_cost", r.get("usage", 0)) or 0),
            billing_currency="USD",
            consumed_quantity=total,
            pricing_unit="token",
            x_agent_id=key_to_agent.get(external_user, external_user),
            x_budget_id=key_to_budget.get(external_user, "default"),
            x_session_id=str(r.get("session_id") or ""),
            x_merchant_id=str(r.get("provider_name") or "openrouter"),
            x_receipt_ref=generation_id,
            x_source_event=source_ref("openrouter", r),
        ))
    return out


# --- Cloud cost (AWS Cost Explorer) ---
_AWS_CE_URL = "https://ce.us-east-1.amazonaws.com"
_AWS_CE_TARGET = "AWSInsightsIndexService.GetCostAndUsage"


def env_aws_access_key_id() -> str | None:
    return os.environ.get("AWS_ACCESS_KEY_ID")


def env_aws_secret_access_key() -> str | None:
    return os.environ.get("AWS_SECRET_ACCESS_KEY")


def env_aws_session_token() -> str | None:
    return os.environ.get("AWS_SESSION_TOKEN")


def _aws_signing_key(secret_key: str, date_stamp: str, region: str, service: str) -> bytes:
    key = ("AWS4" + secret_key).encode()
    for value in (date_stamp, region, service, "aws4_request"):
        key = hmac.new(key, value.encode(), hashlib.sha256).digest()
    return key


def _aws_sigv4_headers(
    *,
    url: str,
    body: bytes,
    access_key: str,
    secret_key: str,
    region: str,
    service: str,
    target: str,
    session_token: str | None = None,
    now: datetime | None = None,
) -> dict[str, str]:
    now = now or datetime.now(tz=timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    parsed = urllib.parse.urlsplit(url)
    headers = {
        "content-type": "application/x-amz-json-1.1",
        "host": parsed.netloc,
        "x-amz-date": amz_date,
        "x-amz-target": target,
    }
    if session_token:
        headers["x-amz-security-token"] = session_token
    signed_headers = ";".join(sorted(k.lower() for k in headers))
    canonical_headers = "".join(f"{k.lower()}:{headers[k].strip()}\n" for k in sorted(headers, key=str.lower))
    canonical_request = "\n".join([
        "POST",
        parsed.path or "/",
        parsed.query,
        canonical_headers,
        signed_headers,
        hashlib.sha256(body).hexdigest(),
    ])
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])
    signature = hmac.new(
        _aws_signing_key(secret_key, date_stamp, region, service),
        string_to_sign.encode(),
        hashlib.sha256,
    ).hexdigest()
    headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return headers


def _aws_tag_value(value: str, tag_key: str | None = None) -> str:
    value = str(value or "")
    if "$" in value:
        key, val = value.split("$", 1)
        if tag_key is None or key == tag_key:
            return val or "unknown"
    if value.lower().startswith("no tag"):
        return "unknown"
    return value or "unknown"


def fetch_aws_cost_and_usage(
    access_key: str,
    secret_key: str,
    *,
    session_token: str | None = None,
    days: int = 7,
    region: str = "us-east-1",
    tag_agent: str = "agent_id",
    tag_budget: str = "budget_id",
) -> list[dict]:
    """Pull AWS Cost Explorer daily unblended cost grouped by agent/budget tags."""
    end = date.today()
    start = end - timedelta(days=days)
    group_by = []
    if tag_agent:
        group_by.append({"Type": "TAG", "Key": tag_agent})
    if tag_budget:
        group_by.append({"Type": "TAG", "Key": tag_budget})
    if not group_by:
        group_by.append({"Type": "DIMENSION", "Key": "SERVICE"})
    payload = {
        "TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()},
        "Granularity": "DAILY",
        "Metrics": ["UnblendedCost"],
        "GroupBy": group_by[:2],
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = _aws_sigv4_headers(
        url=_AWS_CE_URL,
        body=body,
        access_key=access_key,
        secret_key=secret_key,
        region=region,
        service="ce",
        target=_AWS_CE_TARGET,
        session_token=session_token,
    )
    req = urllib.request.Request(_AWS_CE_URL, data=body, headers=headers, method="POST")
    data = _request_json(req)

    rows: list[dict] = []
    for bucket in data.get("ResultsByTime", []):
        start_at = bucket.get("TimePeriod", {}).get("Start", start.isoformat())
        end_at = bucket.get("TimePeriod", {}).get("End", end.isoformat())
        groups = bucket.get("Groups") or []
        if not groups:
            amount = (bucket.get("Total", {}).get("UnblendedCost") or {}).get("Amount", 0)
            unit = (bucket.get("Total", {}).get("UnblendedCost") or {}).get("Unit", "USD")
            rows.append({
                "start": start_at, "end": end_at, "amount": float(amount or 0),
                "currency": unit, "service": "aws", "agent_id": "unknown",
                "budget_id": "default",
            })
            continue
        for group in groups:
            metrics = group.get("Metrics", {}).get("UnblendedCost") or {}
            keys = list(group.get("Keys") or [])
            agent = _aws_tag_value(keys[0], tag_agent) if tag_agent and keys else "unknown"
            budget_idx = 1 if tag_agent else 0
            budget = (
                _aws_tag_value(keys[budget_idx], tag_budget)
                if tag_budget and len(keys) > budget_idx else "default"
            )
            rows.append({
                "start": start_at,
                "end": end_at,
                "amount": float(metrics.get("Amount", 0) or 0),
                "currency": metrics.get("Unit", "USD"),
                "service": "aws",
                "agent_id": agent,
                "budget_id": budget,
                "group_keys": keys,
            })
    return rows


def from_aws_cost_rows(rows) -> list[SpendEvent]:
    """AWS Cost Explorer rows -> SpendEvents."""
    out = []
    for r in rows:
        receipt = f"{r.get('start')}:{r.get('end')}:{r.get('service')}:{r.get('agent_id')}:{r.get('budget_id')}"
        out.append(SpendEvent(
            event_id="aws:" + hashlib.sha256(receipt.encode()).hexdigest(),
            event_time=str(r.get("start") or datetime.now(tz=timezone.utc).date().isoformat()),
            rail="cloud",
            provider_name="aws",
            service_name=str(r.get("service") or "aws"),
            billed_cost=float(r.get("amount", 0) or 0),
            billing_currency=str(r.get("currency") or "USD"),
            consumed_quantity=0,
            pricing_unit="usd",
            x_agent_id=str(r.get("agent_id") or "unknown"),
            x_budget_id=str(r.get("budget_id") or "default"),
            x_receipt_ref=receipt,
            x_source_event=source_ref("aws", r),
        ))
    return out


# --- Cloud cost (GCP Billing Export) ---
def _gcp_label_value(labels, key: str) -> str:
    if isinstance(labels, dict):
        return str(labels.get(key) or "")
    if isinstance(labels, list):
        for item in labels:
            if isinstance(item, dict) and item.get("key") == key:
                return str(item.get("value") or "")
    return ""


def _gcp_nested(row: dict, path: str, default=""):
    cur = row
    for part in path.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(part)
    return cur if cur is not None else default


def load_gcp_billing_export(path: str | os.PathLike) -> list[dict]:
    """Load GCP billing export rows from JSON, NDJSON, or CSV.

    This expects rows exported from BigQuery's Cloud Billing export. JSON/NDJSON
    preserve nested project/service/sku/labels fields best; CSV is supported for
    simple flattened exports.
    """
    p = os.fspath(path)
    if p.lower().endswith(".csv"):
        with open(p, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    with open(p, encoding="utf-8") as f:
        text = f.read().strip()
    if not text:
        return []
    if text.startswith("["):
        data = json.loads(text)
        if not isinstance(data, list):
            raise ValueError(f"{path} must contain a JSON array")
        return data
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def from_gcp_billing_rows(rows, *, label_agent: str = "agent_id",
                          label_budget: str = "budget_id") -> list[SpendEvent]:
    """GCP Cloud Billing export rows -> SpendEvents."""
    out = []
    for r in rows:
        labels = r.get("labels") or r.get("project.labels") or []
        project = r.get("project") if isinstance(r.get("project"), dict) else {}
        service = r.get("service") if isinstance(r.get("service"), dict) else {}
        sku = r.get("sku") if isinstance(r.get("sku"), dict) else {}
        cost = float(r.get("cost") or 0)
        currency = str(r.get("currency") or "USD")
        service_name = (
            service.get("description") or r.get("service.description") or
            service.get("id") or r.get("service.id") or "gcp"
        )
        sku_name = sku.get("description") or r.get("sku.description") or ""
        project_id = project.get("id") or r.get("project.id") or r.get("project_id") or "unknown-project"
        start = (
            r.get("usage_start_time") or r.get("usage_start") or
            r.get("usage_start_time.value") or datetime.now(tz=timezone.utc).isoformat()
        )
        agent = _gcp_label_value(labels, label_agent) or r.get(label_agent) or "unknown"
        budget = _gcp_label_value(labels, label_budget) or r.get(label_budget) or "default"
        receipt = f"{start}:{project_id}:{service_name}:{sku_name}:{agent}:{budget}:{cost}"
        out.append(SpendEvent(
            event_id="gcp:" + hashlib.sha256(receipt.encode()).hexdigest(),
            event_time=str(start),
            rail="cloud",
            provider_name="gcp",
            service_name=str(service_name),
            billed_cost=cost,
            billing_currency=currency,
            consumed_quantity=float(_gcp_nested(r, "usage.amount", r.get("usage.amount", 0)) or 0),
            pricing_unit=str(_gcp_nested(r, "usage.unit", r.get("usage.unit", "usage")) or "usage"),
            x_agent_id=str(agent),
            x_budget_id=str(budget),
            x_merchant_id=str(project_id),
            x_receipt_ref=receipt,
            x_source_event=source_ref("gcp", r),
        ))
    return out


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
