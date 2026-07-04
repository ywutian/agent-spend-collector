"""CLI for the read-only cross-rail agent spend collector."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .adapters import from_llm_usage, from_stripe_events, from_usdc_transfers, from_x402_settlements
from .detectors import run_all
from .gateway import (
    GuardRequest,
    audit_config as build_audit_config,
    cap_for_request,
    decide,
    record_forwarded_spend,
    record_target_spend,
    rate_cap_for_request,
    record_x402_settlement,
    require_valid_policy,
    validate_policy as validate_policy_data,
)
from .providers import llm_provider
from .report import render
from .store import SpendStore

_ROOT = Path(__file__).resolve().parent.parent
_FIXTURES = _ROOT / "fixtures"


def _load_fixture(name: str):
    with open(_FIXTURES / name, encoding="utf-8") as f:
        return json.load(f)


def _load_json_file(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _load_config(path: str | Path | None) -> dict:
    if not path:
        return {}
    return _load_json_file(path)


def _load_wallet_map(path: str | Path | None = None, config: dict | None = None,
                     base_dir: str | Path | None = None) -> dict:
    data: dict = {}
    config = config or {}
    def resolve(value: str | Path) -> Path:
        p = Path(value)
        return p if p.is_absolute() or base_dir is None else Path(base_dir) / p
    if path:
        data = _load_json_file(resolve(path))
    elif config.get("wallet_map_file"):
        data = _load_json_file(resolve(config["wallet_map_file"]))
    elif isinstance(config.get("wallets"), dict):
        data = config["wallets"]
    elif isinstance(config.get("wallet_map"), dict):
        data = config["wallet_map"]
    if isinstance(data.get("wallets"), dict):
        data = data["wallets"]
    return {str(k).lower(): v for k, v in data.items()}


def _load_budgets(default: dict[str, float] | None = None) -> dict[str, float]:
    path = os.environ.get("SPEND_BUDGETS_FILE")
    if not path:
        return dict(default or {})
    data = _load_json_file(path)
    return {str(k): float(v) for k, v in data.items()}


def _budgets_from_config(config: dict, default: dict[str, float] | None = None) -> dict[str, float]:
    budgets = config.get("budgets")
    if isinstance(budgets, dict):
        return {str(k): float(v) for k, v in budgets.items()}
    return _load_budgets(default)


def _rail_config(config: dict, name: str) -> dict:
    rails = config.get("rails")
    if isinstance(rails, dict) and isinstance(rails.get(name), dict):
        return rails[name]
    if isinstance(config.get(name), dict):
        return config[name]
    return {}


def _enabled(cfg: dict, default: bool = True) -> bool:
    return bool(cfg.get("enabled", default))


def _load_id_list(path: str | Path | None = None, values: list[str] | None = None) -> list[str]:
    out = [str(v).strip() for v in (values or []) if str(v).strip()]
    if not path:
        return out
    p = Path(path)
    text = p.read_text(encoding="utf-8").strip()
    if not text:
        return out
    if p.suffix.lower() == ".json":
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("generation_ids") or data.get("ids") or []
        out.extend(str(v).strip() for v in data if str(v).strip())
    else:
        out.extend(line.strip() for line in text.splitlines() if line.strip())
    return out


def _with_stream_usage(raw: bytes) -> bytes:
    """Streamed chat responses omit token usage, so streamed spend can't be priced.
    For a `stream: true` body, ask the provider to emit a final usage chunk
    (OpenAI-compatible: stream_options.include_usage). Non-stream bodies pass through.
    """
    try:
        data = json.loads(raw or b"{}")
    except (ValueError, TypeError):
        return raw
    if not isinstance(data, dict) or not data.get("stream"):
        return raw
    opts = dict(data["stream_options"]) if isinstance(data.get("stream_options"), dict) else {}
    if opts.get("include_usage"):
        return raw
    opts["include_usage"] = True
    data["stream_options"] = opts
    return json.dumps(data).encode()


def _usage_body_from_sse(tail: bytes) -> bytes | None:
    """Pull the final usage-bearing SSE chunk from a streamed response tail and return
    a synthetic non-stream body {id, model, usage} that record_forwarded_spend can
    price. Returns None if the stream carried no usage (nothing to record).
    """
    found = None
    for line in tail.decode("utf-8", "ignore").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        chunk = line[len("data:"):].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            obj = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("usage"):
            found = obj
    if found is None:
        return None
    return json.dumps({"id": found.get("id"), "model": found.get("model"),
                       "usage": found["usage"]}).encode()


def _is_event_stream(content_type: str) -> bool:
    """A forwarded response needs SSE usage-teeing only if it is a real event
    stream. Chunked transfer-encoding is just HTTP framing (OpenAI sends ordinary
    JSON chunked too) and must NOT trigger stream handling, or the response gets
    SSE-parsed, finds no usage, and the spend goes unrecorded.
    """
    return "text/event-stream" in (content_type or "").lower()


def _print_summary(store: SpendStore) -> None:
    print(f"\nTotal agent spend: ${store.total():.4f}   (one ledger, all rails)\n")
    print("By agent x rail:")
    for r in store.by("x_agent_id", "rail"):
        print(f"  {r['x_agent_id']:<13} {r['rail']:<10} ${r['spend']:.4f}  ({r['events']} events)")


def _alert_row(alert) -> dict:
    return {
        "kind": alert.kind,
        "subject": alert.subject,
        "detail": alert.detail,
        "severity": alert.severity,
        "value": alert.value,
    }


def _run_summary(store: SpendStore, alerts: list, budgets: dict[str, float]) -> dict:
    return {
        "total_spend": store.total(),
        "events": store.db.execute("SELECT COUNT(*) FROM spend_events").fetchone()[0],
        "agents": store.db.execute("SELECT COUNT(DISTINCT x_agent_id) FROM spend_events").fetchone()[0],
        "rails": [r["rail"] for r in store.by("rail")],
        "budgets": budgets,
        "alerts": {
            "total": len(alerts),
            "high": sum(1 for a in alerts if a.severity == "high"),
            "warn": sum(1 for a in alerts if a.severity == "warn"),
        },
    }


def _write_json_artifact(path: str | Path, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def _decode_x402_header(value: str) -> dict:
    import base64
    raw = (value or "").strip()
    if not raw:
        raise ValueError("missing payment payload")
    try:
        data = json.loads(raw)
    except ValueError:
        padded = raw + "=" * (-len(raw) % 4)
        data = json.loads(base64.b64decode(padded).decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("payment payload must be an object")
    return data


def _x402_amount_units(resource: dict) -> str:
    if resource.get("amount_units") is not None:
        return str(resource["amount_units"])
    decimals = int(resource.get("asset_decimals", 6))
    return str(int(round(float(resource["amount"]) * (10 ** decimals))))


def _x402_payment_requirements(resource: dict) -> dict:
    return {
        "scheme": str(resource.get("scheme", "exact")),
        "network": str(resource.get("network", "eip155:8453")),
        "asset": str(resource["asset"]),
        "amount": _x402_amount_units(resource),
        "payTo": str(resource["pay_to"]),
        "maxTimeoutSeconds": int(resource.get("max_timeout_seconds", 60)),
        "extra": {
            "name": str(resource.get("asset_name", "USDC")),
            "version": str(resource.get("asset_version", "2")),
        },
    }


def _x402_public_requirements(resource_id: str, resource: dict, requirements: dict) -> dict:
    return {
        "x402Version": int(resource.get("x402_version", 2)),
        "resource": resource_id,
        "paymentRequirements": requirements,
        "description": str(resource.get("description") or resource.get("service") or resource_id),
        "mimeType": str(resource.get("mime_type", "application/json")),
    }


def _x402_payment_binding_errors(payment_payload: dict, requirements: dict, resource: dict) -> list[str]:
    accepted = payment_payload.get("accepted")
    if not isinstance(accepted, dict):
        return ["payment payload missing accepted requirements"]
    errors: list[str] = []
    for key in ("scheme", "network", "asset", "amount", "payTo"):
        if str(accepted.get(key, "")) != str(requirements.get(key, "")):
            errors.append(f"payment accepted.{key} does not match requirements")

    resource_payload = payment_payload.get("resource")
    if isinstance(resource_payload, dict):
        signed_url = str(resource_payload.get("url") or "")
        required_url = str(resource.get("resource_url") or resource.get("url") or "")
        if signed_url and required_url and signed_url != required_url:
            errors.append("payment resource.url does not match configured resource")
    else:
        errors.append("payment payload missing resource binding")
    return errors


def _facilitator_request_json(url: str, payload: dict, resource: dict) -> dict:
    headers = {"content-type": "application/json"}
    auth_env = resource.get("facilitator_auth_env")
    if auth_env and os.environ.get(str(auth_env)):
        headers["authorization"] = f"Bearer {os.environ[str(auth_env)]}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=float(resource.get("timeout", 30))) as resp:
        data = json.load(resp)
    if not isinstance(data, dict):
        raise ValueError("facilitator response must be a JSON object")
    return data


def _alert_payload(alerts: list, summary: dict) -> dict | None:
    """Slack-compatible / generic JSON for the high-severity alerts, or None."""
    high = [a for a in alerts if a.severity == "high"]
    if not high:
        return None
    lines = "\n".join(f"[{a.severity}] {a.kind} {a.subject}: {a.detail}" for a in high)
    return {"text": f"agent-spend: {len(high)} high alert(s)\n{lines}",
            "alerts": [_alert_row(a) for a in high], "summary": summary}


_ALERT_HOSTS = (  # webhook host substring -> platform envelope
    ("hooks.slack.com", "slack"),
    ("discord", "discord"),
    ("feishu", "feishu"), ("larksuite", "feishu"), ("larkoffice", "feishu"),
    ("office.com", "teams"),
)


def _alert_platform(url: str) -> str:
    """Notification platform for a webhook URL. SPEND_ALERT_FORMAT overrides; else
    auto-detect from the host; else a generic JSON POST.
    """
    override = os.environ.get("SPEND_ALERT_FORMAT", "").strip().lower()
    if override:
        return override
    host = urllib.parse.urlsplit(url).netloc.lower()
    for needle, platform in _ALERT_HOSTS:
        if needle in host:
            return platform
    return "generic"


def _format_alert(platform: str, text: str, structured: dict) -> dict:
    """Wrap the alert text in each platform's expected envelope."""
    if platform == "slack":
        return {"text": text}
    if platform == "discord":
        return {"content": text[:1900]}  # Discord caps content at 2000 chars
    if platform == "feishu":  # Feishu / Lark
        return {"msg_type": "text", "content": {"text": text}}
    if platform == "teams":
        return {"@type": "MessageCard", "@context": "http://schema.org/extensions",
                "title": "agent-spend alerts", "text": text}
    return structured  # generic: full {text, alerts, summary}


def _triage_alerts(alerts: list, summary: dict) -> str | None:
    """Opt-in AI triage: ask an LLM for the likely cause + one recommended action for
    the high-severity alerts. Enabled by SPEND_TRIAGE_MODEL; uses an OpenAI-compatible
    endpoint (SPEND_TRIAGE_BASE_URL, default OpenAI) with SPEND_TRIAGE_API_KEY or
    OPENAI_API_KEY. Point the base URL at your own gateway to route (and record) the
    triage call itself. Sends only alert metadata; best-effort, None on any failure.
    """
    model = os.environ.get("SPEND_TRIAGE_MODEL")
    key = os.environ.get("SPEND_TRIAGE_API_KEY") or os.environ.get("OPENAI_API_KEY")
    high = [a for a in alerts if a.severity == "high"]
    if not model or not key or not high:
        return None
    base = os.environ.get("SPEND_TRIAGE_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    facts = {"alerts": [_alert_row(a) for a in high],
             "total_spend": summary.get("total_spend"), "budgets": summary.get("budgets")}
    body = {
        "model": model, "max_tokens": 160, "temperature": 0,
        "messages": [
            {"role": "system", "content": "You are an agent-spend security analyst. Given "
             "anomaly alerts (metadata only), reply in 2-3 sentences: the most likely cause "
             "and one concrete recommended action. No preamble."},
            {"role": "user", "content": json.dumps(facts)},
        ],
    }
    req = urllib.request.Request(
        f"{base}/chat/completions", data=json.dumps(body).encode(),
        headers={"content-type": "application/json", "authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        return (data["choices"][0]["message"]["content"] or "").strip() or None
    except (urllib.error.URLError, OSError, KeyError, ValueError, IndexError):
        return None


def _notify_alerts(alerts: list, summary: dict) -> bool:
    """POST high-severity alerts to SPEND_ALERT_WEBHOOK (opt-in). Formats for Slack,
    Discord, Feishu/Lark, Teams, or a generic JSON body (auto-detected from the URL,
    or SPEND_ALERT_FORMAT), and appends opt-in AI triage. Metadata only, never breaks a run.
    """
    url = os.environ.get("SPEND_ALERT_WEBHOOK")
    payload = _alert_payload(alerts, summary)
    if not url or payload is None:
        return False
    triage = _triage_alerts(alerts, summary)
    if triage:
        payload = {**payload, "text": f"{payload['text']}\n\n\U0001f50e {triage}", "triage": triage}
    body = _format_alert(_alert_platform(url), payload["text"], payload)
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"content-type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except (urllib.error.URLError, OSError):
        return False


def _finish_run(store: SpendStore, budgets: dict[str, float], out_dir: str | Path = ".") -> None:
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    _print_summary(store)
    alerts = run_all(store, budgets)
    print("\nAlerts:")
    if alerts:
        for a in alerts:
            print(f"  [{a.severity:<4}] {a.kind:<22} {a.subject:<13} {a.detail}")
    else:
        print("  none")

    report_path = out_path / "report.html"
    alerts_path = out_path / "alerts.json"
    summary_path = out_path / "run-summary.json"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(render(store, budgets, alerts))
    print(f"\nWrote {report_path}  (open in a browser)")
    summary = _run_summary(store, alerts, budgets)
    _write_json_artifact(alerts_path, [_alert_row(a) for a in alerts])
    _write_json_artifact(summary_path, summary)
    print(f"Wrote {alerts_path} and {summary_path}")
    if _notify_alerts(alerts, summary):
        print("Sent high-severity alerts to SPEND_ALERT_WEBHOOK")


def demo(out_dir: str | Path = ".") -> None:
    """Run the product demo: LLM + x402 + USDC + Stripe -> ledger -> security signals."""
    llm = _load_fixture("llm_usage.json")
    x402 = _load_fixture("x402_settlements.json")
    usdc = _load_fixture("usdc_transfers.json")
    stripe = _load_fixture("stripe_events.json")
    budgets = _load_budgets(_load_fixture("budgets.json"))

    with SpendStore() as store:
        store.ingest(from_llm_usage(llm))
        store.ingest(from_x402_settlements(x402))
        store.ingest(from_usdc_transfers(usdc))
        store.ingest(from_stripe_events(stripe))
        _finish_run(store, budgets, out_dir)

        alerts = run_all(store, budgets)
        kinds = {a.kind for a in alerts}
        expected = {
            "spend_spike",
            "budget_burn",
            "budget_burn_rate",
            "spend_per_task",
            "new_key_spike",
            "new_merchant_provider",
        }
        assert 36.10 < store.total() < 36.12, store.total()
        assert expected <= kinds, kinds
        assert any(a.kind == "spend_spike" and a.subject == "research-bot" for a in alerts), alerts
        assert any(a.kind == "new_key_spike" and a.subject == "new-key-bot" for a in alerts), alerts
        assert any(a.kind == "new_merchant_provider" and a.subject == "support-bot" for a in alerts), alerts
    print("[self-check] cross-rail ledger + Phase-0 security demo -- OK")

    from .sources import decode_transfer_log
    log = {"topics": ["0x" + "d" * 64, "0x" + "0" * 24 + "11" * 20, "0x" + "0" * 24 + "22" * 20],
           "data": "0x" + format(2_500_000, "064x"), "transactionHash": "0xabc", "blockNumber": "0x10"}
    decoded = decode_transfer_log(log)
    assert decoded["to"] == "0x" + "22" * 20 and decoded["amount_raw"] == 2_500_000
    print("[self-check] x402 Transfer decoder -- OK")

    event = {"id": "evt_1", "created": 1781740800, "type": "payment_intent.succeeded",
             "data": {"object": {"id": "pi_1", "amount_received": 4200, "currency": "usd",
                                  "metadata": {"agent_id": "ops-bot", "budget_id": "team-ops"}}}}
    stripe_event = from_stripe_events([event])[0]
    assert stripe_event.billed_cost == 42.0 and stripe_event.rail == "card"
    print("[self-check] Stripe payment mapping -- OK")


def pull(db_path: str | Path = "spend.db", out_dir: str | Path = ".", days: int = 7,
         provider: str = "anthropic") -> None:
    """Pull real LLM cost data (read-only admin key). provider: anthropic | openai."""
    from .sources import (env_admin_key, env_openai_key, fetch_anthropic_cost_report,
                          fetch_openai_costs, from_llm_cost_rows)
    if provider == "openai":
        key = env_openai_key()
        if not key:
            print("Set OPENAI_ADMIN_KEY to pull OpenAI cost data:\n"
                  "  export OPENAI_ADMIN_KEY=sk-...\n"
                  "  python3 -m spend_collector pull --provider openai")
            sys.exit(1)
        rows = fetch_openai_costs(key, days=days)
    else:
        key = env_admin_key()
        if not key:
            print("Set ANTHROPIC_ADMIN_KEY to pull real cost data:\n"
                  "  export ANTHROPIC_ADMIN_KEY=sk-ant-admin01-...\n"
                  "  python3 -m spend_collector pull")
            sys.exit(1)
        rows = fetch_anthropic_cost_report(key, days=days)
    with SpendStore(str(db_path)) as store:
        n = store.ingest(from_llm_cost_rows(rows))
        print(f"ingested {n} {provider} cost rows -> {db_path}")
        _finish_run(store, _load_budgets(), out_dir)


def pull_openrouter(db_path: str | Path = "spend.db", out_dir: str | Path = ".",
                    generation_ids: list[str] | None = None,
                    generation_ids_file: str | Path | None = None) -> None:
    """Pull OpenRouter generation metadata by generation id."""
    from .sources import env_openrouter_key, fetch_openrouter_generations, from_openrouter_generation_rows
    key = env_openrouter_key()
    if not key:
        print("Set OPENROUTER_API_KEY to pull OpenRouter generation metadata:\n"
              "  export OPENROUTER_API_KEY=sk-or-...\n"
              "  python3 -m spend_collector pull-openrouter --generation-id gen_...")
        sys.exit(1)
    ids = _load_id_list(generation_ids_file, generation_ids)
    if not ids:
        print("Pass at least one OpenRouter generation id:\n"
              "  python3 -m spend_collector pull-openrouter --generation-id gen_...\n"
              "  python3 -m spend_collector pull-openrouter --generation-ids-file generations.txt")
        sys.exit(1)
    with SpendStore(str(db_path)) as store:
        n = store.ingest(from_openrouter_generation_rows(fetch_openrouter_generations(key, ids)))
        print(f"ingested {n} OpenRouter generations -> {db_path}")
        _finish_run(store, _load_budgets(), out_dir)


def pull_aws(db_path: str | Path = "spend.db", out_dir: str | Path = ".", days: int = 7,
             tag_agent: str = "agent_id", tag_budget: str = "budget_id") -> None:
    """Pull AWS Cost Explorer spend grouped by agent/budget cost allocation tags."""
    from .sources import (
        env_aws_access_key_id, env_aws_secret_access_key, env_aws_session_token,
        fetch_aws_cost_and_usage, from_aws_cost_rows,
    )
    access_key = env_aws_access_key_id()
    secret_key = env_aws_secret_access_key()
    if not access_key or not secret_key:
        print("Set AWS read-only Cost Explorer credentials:\n"
              "  export AWS_ACCESS_KEY_ID=...\n"
              "  export AWS_SECRET_ACCESS_KEY=...\n"
              "  python3 -m spend_collector pull-aws")
        sys.exit(1)
    with SpendStore(str(db_path)) as store:
        n = store.ingest(from_aws_cost_rows(fetch_aws_cost_and_usage(
            access_key,
            secret_key,
            session_token=env_aws_session_token(),
            days=days,
            tag_agent=tag_agent,
            tag_budget=tag_budget,
        )))
        print(f"ingested {n} AWS cost rows -> {db_path}")
        _finish_run(store, _load_budgets(), out_dir)


def pull_gcp_billing_file(db_path: str | Path = "spend.db", out_dir: str | Path = ".",
                          billing_export_file: str | Path | None = None,
                          label_agent: str = "agent_id",
                          label_budget: str = "budget_id") -> None:
    """Pull GCP Cloud Billing export rows from a JSON/NDJSON/CSV file."""
    from .sources import load_gcp_billing_export, from_gcp_billing_rows
    if not billing_export_file:
        print("Pass a GCP Billing Export file:\n"
              "  python3 -m spend_collector pull-gcp-billing-file --billing-export-file gcp-billing.ndjson")
        sys.exit(1)
    with SpendStore(str(db_path)) as store:
        rows = load_gcp_billing_export(billing_export_file)
        n = store.ingest(from_gcp_billing_rows(rows, label_agent=label_agent, label_budget=label_budget))
        print(f"ingested {n} GCP billing rows -> {db_path}")
        _finish_run(store, _load_budgets(), out_dir)


def _azure_token_from_env_or_sp(env: dict | None = None) -> str | None:
    from .sources import fetch_azure_access_token
    env = env or os.environ
    token = env.get("AZURE_ACCESS_TOKEN")
    if token:
        return token
    tenant_id = env.get("AZURE_TENANT_ID")
    client_id = env.get("AZURE_CLIENT_ID")
    client_secret = env.get("AZURE_CLIENT_SECRET")
    if tenant_id and client_id and client_secret:
        return fetch_azure_access_token(tenant_id, client_id, client_secret)
    return None


def pull_azure(db_path: str | Path = "spend.db", out_dir: str | Path = ".", days: int = 7,
               scope: str | None = None, tag_agent: str = "agent_id",
               tag_budget: str = "budget_id") -> None:
    """Pull Azure Cost Management spend grouped by agent/budget tags."""
    from .sources import fetch_azure_cost_usage, from_azure_cost_rows
    scope = scope or os.environ.get("AZURE_COST_SCOPE")
    if not scope:
        print("Set an Azure Cost Management scope:\n"
              "  export AZURE_COST_SCOPE=/subscriptions/00000000-0000-0000-0000-000000000000\n"
              "  python3 -m spend_collector pull-azure --scope \"$AZURE_COST_SCOPE\"")
        sys.exit(1)
    token = _azure_token_from_env_or_sp()
    if not token:
        print("Set Azure read-only Cost Management credentials:\n"
              "  export AZURE_ACCESS_TOKEN=$(az account get-access-token --resource https://management.azure.com/ --query accessToken -o tsv)\n"
              "  # or set AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET for service principal auth\n"
              "  python3 -m spend_collector pull-azure --scope \"$AZURE_COST_SCOPE\"")
        sys.exit(1)
    with SpendStore(str(db_path)) as store:
        n = store.ingest(from_azure_cost_rows(fetch_azure_cost_usage(
            token,
            scope,
            days=days,
            tag_agent=tag_agent,
            tag_budget=tag_budget,
        )))
        print(f"ingested {n} Azure cost rows -> {db_path}")
        _finish_run(store, _load_budgets(), out_dir)


def pull_x402(db_path: str | Path = "spend.db", out_dir: str | Path = ".",
              pay_to: str | None = None, lookback_blocks: int = 2000,
              wallet_map_path: str | Path | None = None) -> None:
    """Pull real x402 settlements (USDC into a merchant address on Base, read-only RPC)."""
    from .sources import env_pay_to, fetch_base_usdc_transfers
    pay_to = pay_to or env_pay_to()
    if not pay_to:
        print("Pass an x402 receiving address (Base USDC):\n"
              "  X402_PAY_TO=0x... python3 -m spend_collector pull-x402\n"
              "  python3 -m spend_collector pull-x402 --pay-to 0x...")
        sys.exit(1)
    with SpendStore(str(db_path)) as store:
        n = store.ingest(from_x402_settlements(
            fetch_base_usdc_transfers(pay_to, lookback_blocks=lookback_blocks),
            wallet_map=_load_wallet_map(wallet_map_path),
        ))
        print(f"ingested {n} x402 settlements -> {db_path}")
        _finish_run(store, _load_budgets(), out_dir)


def pull_usdc(db_path: str | Path = "spend.db", out_dir: str | Path = ".",
              pay_to: str | None = None, lookback_blocks: int = 2000,
              wallet_map_path: str | Path | None = None) -> None:
    """Pull direct USDC transfers into a wallet on Base (read-only public RPC)."""
    from .sources import env_usdc_pay_to, fetch_base_usdc_transfers
    pay_to = pay_to or env_usdc_pay_to()
    if not pay_to:
        print("Pass a USDC receiving address on Base:\n"
              "  USDC_PAY_TO=0x... python3 -m spend_collector pull-usdc\n"
              "  python3 -m spend_collector pull-usdc --pay-to 0x...")
        sys.exit(1)
    with SpendStore(str(db_path)) as store:
        n = store.ingest(from_usdc_transfers(
            fetch_base_usdc_transfers(pay_to, lookback_blocks=lookback_blocks),
            wallet_map=_load_wallet_map(wallet_map_path),
        ))
        print(f"ingested {n} USDC transfers -> {db_path}")
        _finish_run(store, _load_budgets(), out_dir)


def pull_stripe(db_path: str | Path = "spend.db", out_dir: str | Path = ".",
                days: int = 7, limit: int = 100) -> None:
    """Pull real card payments via the Stripe Events API (read-only, restricted key)."""
    from .sources import env_stripe_key, fetch_stripe_payment_intent_events
    key = env_stripe_key()
    if not key:
        print("Set STRIPE_SECRET_KEY (restricted read key) to pull card payments:\n"
              "  export STRIPE_SECRET_KEY=rk_live_...\n"
              "  python3 -m spend_collector pull-stripe")
        sys.exit(1)
    with SpendStore(str(db_path)) as store:
        n = store.ingest(from_stripe_events(fetch_stripe_payment_intent_events(key, days=days, limit=limit)))
        print(f"ingested {n} Stripe payments -> {db_path}")
        _finish_run(store, _load_budgets(), out_dir)


def _configured_env(cfg: dict, default_name: str) -> str | None:
    name = cfg.get("api_key_env") or default_name
    return os.environ.get(str(name))


def pull_all(config_path: str | Path | None = None, db_path: str | Path | None = None,
             out_dir: str | Path | None = None) -> None:
    """Pull every configured rail into one ledger, then render once."""
    from .sources import (
        env_aws_access_key_id, env_aws_secret_access_key, env_aws_session_token,
        env_pay_to, env_usdc_pay_to, fetch_anthropic_cost_report,
        fetch_aws_cost_and_usage, fetch_azure_access_token, fetch_azure_cost_usage,
        fetch_base_usdc_transfers, fetch_openai_costs, fetch_openrouter_generations,
        fetch_stripe_payment_intent_events, from_aws_cost_rows, from_azure_cost_rows,
        from_gcp_billing_rows, from_llm_cost_rows, from_openrouter_generation_rows,
        load_gcp_billing_export,
    )
    config = _load_config(config_path)
    config_base = Path(config_path).resolve().parent if config_path else None
    db_path = db_path or config.get("db", "spend.db")
    out_dir = out_dir or config.get("out_dir", "artifacts")
    wallet_map = _load_wallet_map(config=config, base_dir=config_base)
    budgets = _budgets_from_config(config)

    with SpendStore(str(db_path)) as store:
        llm_cfg = _rail_config(config, "llm") or _rail_config(config, "llm_token")
        if _enabled(llm_cfg):
            provider = str(llm_cfg.get("provider", config.get("llm_provider", "anthropic")))
            days = int(llm_cfg.get("days", config.get("days", 7)))
            if provider == "openrouter":
                key = _configured_env(llm_cfg, "OPENROUTER_API_KEY")
                ids = _load_id_list(llm_cfg.get("generation_ids_file"), llm_cfg.get("generation_ids") or [])
                if key and ids:
                    n = store.ingest(from_openrouter_generation_rows(fetch_openrouter_generations(key, ids)))
                    print(f"ingested {n} OpenRouter generations -> {db_path}")
                elif not key:
                    print("skipped llm/openrouter: set OPENROUTER_API_KEY or rails.llm.api_key_env")
                else:
                    print("skipped llm/openrouter: set rails.llm.generation_ids or generation_ids_file")
            elif provider == "openai":
                key = _configured_env(llm_cfg, "OPENAI_ADMIN_KEY")
                if key:
                    n = store.ingest(from_llm_cost_rows(fetch_openai_costs(key, days=days)))
                    print(f"ingested {n} openai cost rows -> {db_path}")
                else:
                    print("skipped llm/openai: set OPENAI_ADMIN_KEY or rails.llm.api_key_env")
            else:
                key = _configured_env(llm_cfg, "ANTHROPIC_ADMIN_KEY")
                if key:
                    n = store.ingest(from_llm_cost_rows(fetch_anthropic_cost_report(key, days=days)))
                    print(f"ingested {n} anthropic cost rows -> {db_path}")
                else:
                    print("skipped llm/anthropic: set ANTHROPIC_ADMIN_KEY or rails.llm.api_key_env")

        openrouter_cfg = _rail_config(config, "openrouter")
        if _enabled(openrouter_cfg, default=False):
            key = _configured_env(openrouter_cfg, "OPENROUTER_API_KEY")
            ids = _load_id_list(
                openrouter_cfg.get("generation_ids_file"),
                openrouter_cfg.get("generation_ids") or [],
            )
            if key and ids:
                n = store.ingest(from_openrouter_generation_rows(fetch_openrouter_generations(key, ids)))
                print(f"ingested {n} OpenRouter generations -> {db_path}")
            elif not key:
                print("skipped openrouter: set OPENROUTER_API_KEY or rails.openrouter.api_key_env")
            else:
                print("skipped openrouter: set rails.openrouter.generation_ids or generation_ids_file")

        aws_cfg = _rail_config(config, "aws") or _rail_config(config, "cloud")
        if _enabled(aws_cfg, default=False):
            access_key = os.environ.get(str(aws_cfg.get("access_key_env", "AWS_ACCESS_KEY_ID")))
            secret_key = os.environ.get(str(aws_cfg.get("secret_key_env", "AWS_SECRET_ACCESS_KEY")))
            session_token = os.environ.get(str(aws_cfg.get("session_token_env", "AWS_SESSION_TOKEN")))
            if access_key and secret_key:
                n = store.ingest(from_aws_cost_rows(fetch_aws_cost_and_usage(
                    access_key,
                    secret_key,
                    session_token=session_token or env_aws_session_token(),
                    days=int(aws_cfg.get("days", config.get("days", 7))),
                    tag_agent=str(aws_cfg.get("tag_agent", "agent_id")),
                    tag_budget=str(aws_cfg.get("tag_budget", "budget_id")),
                )))
                print(f"ingested {n} AWS cost rows -> {db_path}")
            else:
                print("skipped aws: set AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY or rails.aws *_env")

        gcp_cfg = _rail_config(config, "gcp")
        if _enabled(gcp_cfg, default=False):
            export_file = gcp_cfg.get("billing_export_file")
            if export_file:
                export_path = Path(export_file)
                if config_base is not None and not export_path.is_absolute():
                    export_path = config_base / export_path
                n = store.ingest(from_gcp_billing_rows(
                    load_gcp_billing_export(export_path),
                    label_agent=str(gcp_cfg.get("label_agent", "agent_id")),
                    label_budget=str(gcp_cfg.get("label_budget", "budget_id")),
                ))
                print(f"ingested {n} GCP billing rows -> {db_path}")
            else:
                print("skipped gcp: set rails.gcp.billing_export_file")

        azure_cfg = _rail_config(config, "azure")
        if _enabled(azure_cfg, default=False):
            scope = azure_cfg.get("scope") or os.environ.get("AZURE_COST_SCOPE")
            token = os.environ.get(str(azure_cfg.get("access_token_env", "AZURE_ACCESS_TOKEN")))
            if not token:
                tenant_id = os.environ.get(str(azure_cfg.get("tenant_id_env", "AZURE_TENANT_ID")))
                client_id = os.environ.get(str(azure_cfg.get("client_id_env", "AZURE_CLIENT_ID")))
                client_secret = os.environ.get(str(azure_cfg.get("client_secret_env", "AZURE_CLIENT_SECRET")))
                if tenant_id and client_id and client_secret:
                    token = fetch_azure_access_token(tenant_id, client_id, client_secret)
            if scope and token:
                n = store.ingest(from_azure_cost_rows(fetch_azure_cost_usage(
                    token,
                    str(scope),
                    days=int(azure_cfg.get("days", config.get("days", 7))),
                    tag_agent=str(azure_cfg.get("tag_agent", "agent_id")),
                    tag_budget=str(azure_cfg.get("tag_budget", "budget_id")),
                )))
                print(f"ingested {n} Azure cost rows -> {db_path}")
            elif not scope:
                print("skipped azure: set rails.azure.scope or AZURE_COST_SCOPE")
            else:
                print("skipped azure: set AZURE_ACCESS_TOKEN or service principal env vars")

        x402_cfg = _rail_config(config, "x402")
        if _enabled(x402_cfg):
            pay_to = x402_cfg.get("pay_to") or env_pay_to()
            if pay_to:
                rows = fetch_base_usdc_transfers(
                    str(pay_to),
                    lookback_blocks=int(x402_cfg.get("lookback_blocks", config.get("lookback_blocks", 2000))),
                )
                n = store.ingest(from_x402_settlements(rows, wallet_map=wallet_map))
                print(f"ingested {n} x402 settlements -> {db_path}")
            else:
                print("skipped x402: set rails.x402.pay_to or X402_PAY_TO")

        usdc_cfg = _rail_config(config, "usdc") or _rail_config(config, "stablecoin")
        if _enabled(usdc_cfg):
            pay_to = usdc_cfg.get("pay_to") or env_usdc_pay_to()
            if pay_to:
                rows = fetch_base_usdc_transfers(
                    str(pay_to),
                    lookback_blocks=int(usdc_cfg.get("lookback_blocks", config.get("lookback_blocks", 2000))),
                )
                n = store.ingest(from_usdc_transfers(rows, wallet_map=wallet_map))
                print(f"ingested {n} USDC transfers -> {db_path}")
            else:
                print("skipped usdc: set rails.usdc.pay_to, USDC_PAY_TO, or X402_PAY_TO")

        stripe_cfg = _rail_config(config, "stripe") or _rail_config(config, "card")
        if _enabled(stripe_cfg):
            key = _configured_env(stripe_cfg, "STRIPE_SECRET_KEY")
            if key:
                n = store.ingest(from_stripe_events(fetch_stripe_payment_intent_events(
                    key,
                    days=int(stripe_cfg.get("days", config.get("days", 7))),
                    limit=int(stripe_cfg.get("limit", 100)),
                )))
                print(f"ingested {n} Stripe payments -> {db_path}")
            else:
                print("skipped stripe: set STRIPE_SECRET_KEY or rails.stripe.api_key_env")

        _finish_run(store, budgets, out_dir)


def report(db_path: str | Path = "spend.db", out_dir: str | Path = ".") -> None:
    """Regenerate report.html from an existing SQLite ledger."""
    if not Path(db_path).exists():
        print(f"ledger not found: {db_path}")
        sys.exit(1)
    with SpendStore(str(db_path)) as store:
        if store.total() == 0:
            print(f"{db_path} is empty -- run a pull first (pull / pull-x402 / pull-usdc / pull-stripe).")
            sys.exit(1)
        print(f"loaded ledger {db_path}")
        _finish_run(store, _load_budgets(), out_dir)


def _load_policy(path: str | Path | None) -> dict:
    if not path:
        path = os.environ.get("SPEND_POLICY_FILE")
    if not path:
        return {}
    return _load_json_file(path)


def _request_id(value: str | None = None) -> str:
    return value or f"req:{uuid.uuid4().hex}"


def _decide_and_record(store: SpendStore, policy: dict, req: GuardRequest, *,
                       request_id: str | None = None, route_type: str = "guard",
                       route_id: str = "") -> dict:
    request_id = _request_id(request_id)
    existing = store.gateway_decision_as_dict(request_id)
    if existing:
        return existing
    decision = decide(store, policy, req)
    ttl = int(policy.get("reservation_ttl_seconds", 900))
    cap = cap_for_request(policy, req)
    rate_cap = rate_cap_for_request(policy, req)
    store.reserve_and_record_gateway_decision(
        request_id=request_id,
        req=req,
        decision=decision.decision,
        reasons=decision.reasons,
        route_type=route_type,
        route_id=route_id,
        ttl_seconds=ttl,
        cap=cap,
        rate_cap=rate_cap,
    )
    return store.gateway_decision_as_dict(request_id) or decision.as_dict()


def guard(args) -> dict:
    policy = _load_policy(args.policy)
    require_valid_policy(policy, env_token=os.environ.get("SPEND_GATEWAY_TOKEN"))
    req = GuardRequest(
        x_agent_id=args.agent,
        rail=args.rail,
        amount=args.amount,
        x_budget_id=args.budget,
        provider_name=args.provider or "",
        service_name=args.service or "",
        x_merchant_id=args.merchant or "",
        x_session_id=args.session or "",
    )
    with SpendStore(args.db) as store:
        decision = _decide_and_record(store, policy, req, request_id=args.request_id)
    print(json.dumps(decision, indent=2, sort_keys=True))
    if not decision["allowed"] and args.enforce_exit_code:
        sys.exit(2)
    return decision


def make_gateway_server(db_path: str | Path = "spend.db", policy_path: str | Path | None = None,
                        host: str = "127.0.0.1", port: int = 8787) -> ThreadingHTTPServer:
    startup_policy = _load_policy(policy_path)
    require_valid_policy(startup_policy, env_token=os.environ.get("SPEND_GATEWAY_TOKEN"))

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: dict, headers: dict | None = None) -> None:
            body = json.dumps(payload, indent=2, sort_keys=True).encode()
            self.send_response(code)
            sent_content_type = False
            for key, value in (headers or {}).items():
                if key.lower() == "content-type":
                    sent_content_type = True
                self.send_header(str(key), str(value))
            if not sent_content_type:
                self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_bytes(self, code: int, body: bytes, headers: dict[str, str]) -> None:
            self.send_response(code)
            for key, value in headers.items():
                if key.lower() in {"connection", "content-length", "transfer-encoding"}:
                    continue
                self.send_header(key, value)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_upstream(self, resp, extra_headers: dict | None = None) -> bytes | None:
            headers = dict(resp.headers.items())
            content_type = headers.get("Content-Type", headers.get("content-type", ""))
            is_stream = _is_event_stream(content_type)
            self.send_response(resp.status)
            for key, value in headers.items():
                if key.lower() in {"connection", "content-length", "transfer-encoding"}:
                    continue
                self.send_header(key, value)
            for key, value in (extra_headers or {}).items():
                self.send_header(str(key), str(value))
            if not is_stream:
                body = resp.read()
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return body
            self.end_headers()
            tail = bytearray()  # ponytail: keep last 64KB; the usage chunk is small and last
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                tail += chunk
                if len(tail) > 65536:
                    del tail[:-65536]
            return _usage_body_from_sse(bytes(tail))

        def _read_json(self) -> dict:
            length = int(self.headers.get("content-length", "0"))
            self._raw_body = self.rfile.read(length)
            payload = json.loads(self._raw_body or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            return payload

        def _read_raw(self) -> bytes:
            length = int(self.headers.get("content-length", "0"))
            self._raw_body = self.rfile.read(length)
            return self._raw_body

        def _authorized(self, policy: dict, token: str = "") -> bool:
            tokens = policy.get("gateway_tokens")
            env_token = os.environ.get("SPEND_GATEWAY_TOKEN")
            if env_token:
                tokens = list(tokens or []) + [env_token]
            if not tokens:
                return True
            auth = self.headers.get("authorization", "")
            bearer = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
            supplied = token or bearer or self.headers.get("x-gateway-token", "")
            return supplied in set(str(t) for t in tokens)

        def _guard_request(self, payload: dict) -> GuardRequest:
            return GuardRequest(
                x_agent_id=str(payload["agent"]),
                rail=str(payload["rail"]),
                amount=float(payload["amount"]),
                x_budget_id=str(payload["budget"]),
                provider_name=str(payload.get("provider", "")),
                service_name=str(payload.get("service", "")),
                x_merchant_id=str(payload.get("merchant", "")),
                x_session_id=str(payload.get("session", "")),
            )

        def _request_id(self, payload: dict) -> str:
            return _request_id(self.headers.get("x-request-id") or payload.get("request_id"))

        def _guard_payload(self, payload: dict, *, route_type: str = "guard",
                           route_id: str = "") -> dict:
            req = self._guard_request(payload)
            with SpendStore(str(db_path)) as store:
                return _decide_and_record(
                    store,
                    _load_policy(policy_path),
                    req,
                    request_id=self._request_id(payload),
                    route_type=route_type,
                    route_id=route_id,
                )

        def _target_request(self, payload: dict, policy: dict) -> tuple[dict, dict]:
            target_id = str(payload["target"])
            target = policy.get("targets", {}).get(target_id)
            if not isinstance(target, dict):
                raise ValueError(f"target {target_id} is not configured")
            guard_payload = {
                "agent": payload["agent"],
                "rail": target["rail"],
                "amount": target["amount"],
                "budget": payload.get("budget", target.get("budget", "default")),
                "provider": target.get("provider", ""),
                "service": target.get("service", ""),
                "merchant": target.get("merchant", ""),
                "session": payload.get("session", ""),
            }
            return target, guard_payload

        def _x402_route(self, policy: dict) -> tuple[str, dict] | None:
            parsed = urllib.parse.urlsplit(self.path)
            parts = parsed.path.strip("/").split("/", 1)
            if len(parts) < 2 or parts[0] != "x402":
                return None
            resource_id = parts[1].split("/", 1)[0]
            resource = policy.get("x402_resources", {}).get(resource_id)
            if not isinstance(resource, dict):
                return None
            return resource_id, resource

        def _x402_guard_payload(self, resource_id: str, resource: dict) -> dict:
            agent = self.headers.get("x-agent-id") or resource.get("agent")
            if not agent:
                raise ValueError("x402 calls require X-Agent-ID or x402 resource agent")
            return {
                "agent": agent,
                "rail": "api_x402",
                "amount": float(resource["amount"]),
                "budget": self.headers.get("x-budget-id") or resource.get("budget", "default"),
                "provider": "x402",
                "service": resource.get("service", resource_id),
                "merchant": resource.get("merchant") or resource.get("pay_to", ""),
                "session": self.headers.get("x-session-id", ""),
                "asset": resource.get("asset_name", "USDC"),
                "asset_decimals": int(resource.get("asset_decimals", 6)),
                "network": resource.get("network", "eip155:8453"),
            }

        def _x402_headers(self, resource: dict) -> dict[str, str]:
            merged_headers = dict(resource.get("headers", {}))
            for header, env_name in resource.get("headers_env", {}).items():
                value = os.environ.get(str(env_name))
                if value:
                    merged_headers[str(header)] = value
            passthrough = {
                str(k): str(v)
                for k, v in self.headers.items()
                if k.lower() not in {
                    "host", "content-length", "connection", "authorization",
                    "payment-required", "payment-signature", "payment-response",
                    "x-payment", "x-payment-response", "x-agent-id", "x-budget-id",
                    "x-session-id", "x-gateway-token",
                }
            }
            passthrough.update({str(k): str(v) for k, v in merged_headers.items()})
            return passthrough

        def _send_x402_required(self, resource_id: str, resource: dict, requirements: dict,
                                status: int = 402, error: dict | None = None) -> None:
            payload = _x402_public_requirements(resource_id, resource, requirements)
            if error:
                payload["error"] = error
            encoded = json.dumps(payload, separators=(",", ":"))
            self._send(status, payload, headers={
                "content-type": "application/json",
                "payment-required": encoded,
            })

        def _forward_x402_resource(self, resource: dict, payment_response: dict) -> None:
            body = getattr(self, "_raw_body", b"")
            method = str(resource.get("method", self.command)).upper()
            data = None if method == "GET" else body
            req = urllib.request.Request(
                str(resource["url"]),
                data=data,
                headers=self._x402_headers(resource),
                method=method,
            )
            payment_header = json.dumps(payment_response, separators=(",", ":"))
            with urllib.request.urlopen(req, timeout=float(resource.get("timeout", 30))) as resp:
                self._send_upstream(resp, extra_headers={
                    "payment-response": payment_header,
                    "x-payment-response": payment_header,
                })

        def _handle_x402(self, policy: dict) -> bool:
            route = self._x402_route(policy)
            if not route:
                return False
            resource_id, resource = route
            body = self._read_raw()
            requirements = _x402_payment_requirements(resource)
            guard_payload = self._x402_guard_payload(resource_id, resource)
            payment_header = self.headers.get("payment-signature") or self.headers.get("x-payment")
            if not payment_header:
                with SpendStore(str(db_path)) as store:
                    decision = decide(store, policy, self._guard_request(guard_payload)).as_dict()
                if not decision["allowed"]:
                    self._send(403, decision)
                else:
                    self._send_x402_required(resource_id, resource, requirements)
                return True

            request_id = _request_id(self.headers.get("x-request-id"))
            with SpendStore(str(db_path)) as store:
                if store.gateway_decision_by_request(request_id):
                    self._send(409, {
                        "error": "duplicate_x402_request_id",
                        "request_id": request_id,
                        "detail": "Use a fresh X-Request-ID and payment payload for each x402 settlement.",
                    })
                    return True
            with SpendStore(str(db_path)) as store:
                decision = _decide_and_record(
                    store,
                    policy,
                    self._guard_request(guard_payload),
                    request_id=request_id,
                    route_type="x402",
                    route_id=resource_id,
                )
            if not decision["allowed"]:
                self._send(403, decision)
                return True

            payment_payload = _decode_x402_header(payment_header)
            binding_errors = _x402_payment_binding_errors(payment_payload, requirements, resource)
            if binding_errors:
                with SpendStore(str(db_path)) as store:
                    store.release_reservation(request_id)
                self._send_x402_required(resource_id, resource, requirements, error={
                    "reason": "payment_binding_mismatch",
                    "message": "; ".join(binding_errors),
                })
                return True
            facilitator = str(resource.get("facilitator_url", ""))
            if not facilitator:
                raise ValueError("x402 resource missing facilitator_url")
            version = int(resource.get("x402_version", payment_payload.get("x402Version", 2)))
            facilitator_payload = {
                "x402Version": version,
                "paymentPayload": payment_payload,
                "paymentRequirements": requirements,
            }
            verify_result = _facilitator_request_json(
                facilitator.rstrip("/") + "/verify",
                facilitator_payload,
                resource,
            )
            if not verify_result.get("isValid"):
                with SpendStore(str(db_path)) as store:
                    store.release_reservation(request_id)
                self._send_x402_required(resource_id, resource, requirements, error={
                    "reason": verify_result.get("invalidReason", "invalid_payment"),
                    "message": verify_result.get("invalidMessage", "payment verification failed"),
                })
                return True

            settle_result = _facilitator_request_json(
                facilitator.rstrip("/") + "/settle",
                facilitator_payload,
                resource,
            )
            if settle_result.get("success") is False:
                with SpendStore(str(db_path)) as store:
                    store.release_reservation(request_id)
                self._send_x402_required(resource_id, resource, requirements, error={
                    "reason": settle_result.get("errorReason", "settlement_failed"),
                    "message": settle_result.get("errorMessage", "payment settlement failed"),
                })
                return True

            with SpendStore(str(db_path)) as store:
                record_x402_settlement(
                    store,
                    guard_payload,
                    request_id,
                    requirements,
                    verify_result,
                    settle_result,
                )
            self._raw_body = body
            self._forward_x402_resource(resource, settle_result)
            return True

        def _forward(self, target: dict, payload: dict,
                     guard_payload: dict | None = None, request_id: str = "") -> None:
            merged_headers = dict(target.get("headers", {}))
            for header, env_name in target.get("headers_env", {}).items():
                value = os.environ.get(str(env_name))
                if value:
                    merged_headers[str(header)] = value
            merged_headers.update(payload.get("headers", {}) or {})
            headers = {
                str(k): str(v)
                for k, v in merged_headers.items()
                if str(k).lower() not in {"host", "content-length", "connection"}
            }
            body = payload.get("body", b"")
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
                headers.setdefault("content-type", "application/json")
            elif isinstance(body, str):
                body = body.encode()
            elif body is None:
                body = b""
            method = str(target.get("method", "POST")).upper()
            req = urllib.request.Request(str(target["url"]), data=body, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=float(target.get("timeout", 30))) as resp:
                    self._send_upstream(resp)
                if guard_payload is not None:  # record flat per-call spend, release the hold
                    with SpendStore(str(db_path)) as store:
                        record_target_spend(store, guard_payload, request_id)
            except urllib.error.HTTPError as exc:
                self._send_bytes(exc.code, exc.read(), dict(exc.headers.items()))

        def _provider_route(self, policy: dict) -> tuple[str, dict, str] | None:
            parsed = urllib.parse.urlsplit(self.path)
            parts = parsed.path.strip("/").split("/", 1)
            if not parts or not parts[0]:
                return None
            provider_id = parts[0]
            provider = policy.get("providers", {}).get(provider_id)
            if not isinstance(provider, dict):
                return None
            known = llm_provider(provider_id)  # fill base_url/api_key_env from the catalog
            if known:
                provider = {**known, **provider}  # policy overrides catalog defaults
            suffix = "/" + parts[1] if len(parts) > 1 else "/"
            if parsed.query:
                suffix += "?" + parsed.query
            return provider_id, provider, suffix

        def _provider_guard_payload(self, provider_id: str, provider: dict,
                                    suffix: str, payload: dict) -> dict:
            service_key = provider.get("service_from_body")
            service = payload.get(service_key) if service_key else None
            agent = self.headers.get("x-agent-id") or provider.get("agent")
            budget = self.headers.get("x-budget-id") or provider.get("budget", "default")
            if not agent:
                raise ValueError("provider-compatible calls require X-Agent-ID or provider.agent")
            return {
                "agent": agent,
                "rail": provider.get("rail", "llm_token"),
                "amount": provider.get("amount", provider.get("max_estimated_amount", 0)),
                "budget": budget,
                "provider": provider.get("provider", provider_id),
                "service": service or provider.get("service", suffix),
                "merchant": provider.get("merchant", provider_id),
                "session": self.headers.get("x-session-id", ""),
            }

        def _provider_headers(self, provider: dict) -> dict[str, str]:
            headers = {
                str(k): str(v)
                for k, v in self.headers.items()
                if k.lower() not in {
                    "host", "content-length", "connection", "authorization",
                    "x-agent-id", "x-budget-id", "x-session-id", "x-gateway-token",
                }
            }
            provider_key = provider.get("api_key")
            if provider.get("api_key_env"):
                provider_key = os.environ.get(str(provider["api_key_env"]))
            if not provider_key:
                raise ValueError("provider API key is not configured")
            auth_header = str(provider.get("auth_header", "Authorization"))
            auth_prefix = str(provider.get("auth_prefix", "Bearer"))
            headers[auth_header] = f"{auth_prefix} {provider_key}".strip()
            return headers

        def _forward_provider(self, provider: dict, suffix: str,
                              guard_payload: dict | None = None, request_id: str = "") -> None:
            url = str(provider["base_url"]).rstrip("/") + suffix
            method = str(provider.get("method", "POST")).upper()
            body = _with_stream_usage(getattr(self, "_raw_body", b""))
            req = urllib.request.Request(
                url,
                data=body,
                headers=self._provider_headers(provider),
                method=method,
            )
            try:
                with urllib.request.urlopen(req, timeout=float(provider.get("timeout", 30))) as resp:
                    raw = self._send_upstream(resp)
                # Close the loop: record the actual spend, then release the pre-spend
                # reservation so the budget reflects real usage, not the estimate.
                if raw is not None and guard_payload is not None:
                    with SpendStore(str(db_path)) as store:
                        recorded = record_forwarded_spend(store, raw, provider, guard_payload)
                        if recorded is not None and request_id:
                            store.release_reservation(request_id)
            except urllib.error.HTTPError as exc:
                self._send_bytes(exc.code, exc.read(), dict(exc.headers.items()))

        def do_GET(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path == "/health":
                self._send(200, {"ok": True})
                return
            if parsed.path in ("/", "/dashboard"):
                policy = _load_policy(policy_path)
                token = urllib.parse.parse_qs(parsed.query).get("token", [""])[0]
                if not self._authorized(policy, token):
                    self._send(401, {"error": "unauthorized"})
                    return
                budgets = _load_budgets(policy.get("budgets") or {})
                with SpendStore(str(db_path)) as store:
                    html = render(store, budgets, run_all(store, budgets), refresh_seconds=30)
                self._send_bytes(200, html.encode("utf-8"), {"content-type": "text/html; charset=utf-8"})
                return
            policy = _load_policy(policy_path)
            if self._handle_x402(policy):
                return
            self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            try:
                policy = _load_policy(policy_path)
                if self._handle_x402(policy):
                    return
                payload = self._read_json()
                if not self._authorized(policy):
                    self._send(401, {"error": "unauthorized"})
                    return
                if self.path == "/reservations/release":
                    request_id = str(payload["request_id"])
                    with SpendStore(str(db_path)) as store:
                        released = store.release_reservation(request_id)
                    self._send(200, {"request_id": request_id, "released": released})
                    return
                if self.path == "/guard":
                    decision = self._guard_payload(payload)
                    self._send(200 if decision["allowed"] else 403, decision)
                    return
                if self.path == "/forward":
                    target, guard_payload = self._target_request(payload, policy)
                    if "request_id" in payload:
                        guard_payload["request_id"] = payload["request_id"]
                    decision = self._guard_payload(guard_payload, route_type="target", route_id=str(payload["target"]))
                    if not decision["allowed"]:
                        self._send(403, decision)
                        return
                    self._forward(target, payload, guard_payload, decision.get("request_id", ""))
                    return
                provider_route = self._provider_route(policy)
                if provider_route:
                    provider_id, provider, suffix = provider_route
                    guard_payload = self._provider_guard_payload(provider_id, provider, suffix, payload)
                    decision = self._guard_payload(guard_payload, route_type="provider", route_id=provider_id)
                    if not decision["allowed"]:
                        self._send(403, decision)
                        return
                    self._forward_provider(provider, suffix, guard_payload, decision.get("request_id", ""))
                    return
                self._send(404, {"error": "not found"})
            except urllib.error.URLError as exc:
                self._send(502, {"error": str(exc)})
            except (KeyError, TypeError, ValueError) as exc:
                self._send(400, {"error": str(exc)})

        def log_message(self, fmt, *args):
            return

    return ThreadingHTTPServer((host, port), Handler)


def gateway(db_path: str | Path = "spend.db", policy_path: str | Path | None = None,
            host: str = "127.0.0.1", port: int = 8787) -> None:
    """Run a tiny local HTTP gateway with POST /guard and POST /forward."""
    server = make_gateway_server(db_path, policy_path, host, port)
    print(f"spend gateway listening on http://{host}:{port}")
    print("POST /guard for decisions; POST /forward to guard then proxy an allowlisted target")
    server.serve_forever()


def validate_policy_cmd(policy_path: str) -> None:
    policy = _load_policy(policy_path)
    check = dict(policy)
    if os.environ.get("SPEND_GATEWAY_TOKEN") and not check.get("gateway_tokens"):
        check["gateway_tokens"] = [os.environ["SPEND_GATEWAY_TOKEN"]]
    errors = validate_policy_data(check)
    if errors:
        for error in errors:
            print(f"error: {error}")
        sys.exit(1)
    print("policy OK")


def audit_config_cmd(policy_path: str, db_path: str = "spend.db", out_dir: str = "artifacts") -> None:
    policy = _load_policy(policy_path)
    report = build_audit_config(
        policy,
        db_path=db_path,
        out_dir=out_dir,
        env_token_configured=bool(os.environ.get("SPEND_GATEWAY_TOKEN")),
    )
    print(json.dumps(report, indent=2, sort_keys=True))


def release_reservation_cmd(db_path: str, request_id: str) -> None:
    with SpendStore(db_path) as store:
        released = store.release_reservation(request_id)
    print(json.dumps({"request_id": request_id, "released": released}, indent=2, sort_keys=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spend-collector",
        description="Read-only cross-rail agent spend collector.",
    )
    parser.add_argument("--out-dir", default=".", help="directory for report.html and JSON artifacts")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--out-dir", default=argparse.SUPPRESS,
                        help="directory for report.html and JSON artifacts")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("demo", parents=[common], help="run the fixture-backed product demo")

    pull_p = sub.add_parser("pull", parents=[common], help="pull LLM cost rows (Anthropic or OpenAI)")
    pull_p.add_argument("--db", default="spend.db", help="SQLite ledger path")
    pull_p.add_argument("--days", type=int, default=7, help="days of provider history to request")
    pull_p.add_argument("--provider", default="anthropic", choices=["anthropic", "openai"],
                        help="LLM cost provider")

    openrouter_p = sub.add_parser("pull-openrouter", parents=[common],
                                  help="pull OpenRouter generation metadata by id")
    openrouter_p.add_argument("--db", default="spend.db", help="SQLite ledger path")
    openrouter_p.add_argument("--generation-id", action="append", default=[],
                              help="OpenRouter generation id; may be repeated")
    openrouter_p.add_argument("--generation-ids-file",
                              help="text file or JSON file containing generation ids")

    aws_p = sub.add_parser("pull-aws", parents=[common],
                           help="pull AWS Cost Explorer spend grouped by tags")
    aws_p.add_argument("--db", default="spend.db", help="SQLite ledger path")
    aws_p.add_argument("--days", type=int, default=7, help="days of AWS cost history to request")
    aws_p.add_argument("--tag-agent", default="agent_id", help="AWS cost allocation tag for agent id")
    aws_p.add_argument("--tag-budget", default="budget_id", help="AWS cost allocation tag for budget id")

    gcp_p = sub.add_parser("pull-gcp-billing-file", parents=[common],
                           help="pull GCP Cloud Billing export rows from a file")
    gcp_p.add_argument("--db", default="spend.db", help="SQLite ledger path")
    gcp_p.add_argument("--billing-export-file", required=True,
                       help="BigQuery billing export rows as JSON, NDJSON, or CSV")
    gcp_p.add_argument("--label-agent", default="agent_id", help="GCP label for agent id")
    gcp_p.add_argument("--label-budget", default="budget_id", help="GCP label for budget id")

    azure_p = sub.add_parser("pull-azure", parents=[common],
                             help="pull Azure Cost Management spend grouped by tags")
    azure_p.add_argument("--db", default="spend.db", help="SQLite ledger path")
    azure_p.add_argument("--days", type=int, default=7, help="days of Azure cost history to request")
    azure_p.add_argument("--scope", help="Azure Cost Management scope; defaults to AZURE_COST_SCOPE")
    azure_p.add_argument("--tag-agent", default="agent_id", help="Azure tag for agent id")
    azure_p.add_argument("--tag-budget", default="budget_id", help="Azure tag for budget id")

    all_p = sub.add_parser("pull-all", parents=[common],
                           help="pull every configured rail from one JSON config")
    all_p.add_argument("--config", default="spend.config.json",
                       help="collector config path; defaults to spend.config.json")
    all_p.add_argument("--db", help="override SQLite ledger path from config")

    x402_p = sub.add_parser("pull-x402", parents=[common],
                            help="pull Base USDC settlements into an x402 address")
    x402_p.add_argument("--db", default="spend.db", help="SQLite ledger path")
    x402_p.add_argument("--pay-to", help="merchant receiving address; defaults to X402_PAY_TO")
    x402_p.add_argument("--lookback-blocks", type=int, default=2000, help="Base blocks to scan")
    x402_p.add_argument("--wallet-map", help="JSON wallet address -> agent/budget map")

    usdc_p = sub.add_parser("pull-usdc", parents=[common],
                            help="pull direct Base USDC transfers into an address")
    usdc_p.add_argument("--db", default="spend.db", help="SQLite ledger path")
    usdc_p.add_argument("--pay-to", help="receiving address; defaults to USDC_PAY_TO or X402_PAY_TO")
    usdc_p.add_argument("--lookback-blocks", type=int, default=2000, help="Base blocks to scan")
    usdc_p.add_argument("--wallet-map", help="JSON wallet address -> agent/budget map")

    stripe_p = sub.add_parser("pull-stripe", parents=[common],
                              help="pull Stripe succeeded PaymentIntent events")
    stripe_p.add_argument("--db", default="spend.db", help="SQLite ledger path")
    stripe_p.add_argument("--days", type=int, default=7, help="days of Stripe event history to request")
    stripe_p.add_argument("--limit", type=int, default=100, help="Stripe page size, 1-100")

    report_p = sub.add_parser("report", parents=[common],
                              help="regenerate dashboard from an existing ledger")
    report_p.add_argument("--db", default="spend.db", help="SQLite ledger path")

    guard_p = sub.add_parser("guard", help="pre-spend allow/deny decision")
    guard_p.add_argument("--db", default="spend.db", help="SQLite ledger path")
    guard_p.add_argument("--policy", help="gateway policy JSON; defaults to SPEND_POLICY_FILE")
    guard_p.add_argument("--agent", required=True, help="agent id asking to spend")
    guard_p.add_argument("--rail", required=True, help="rail, e.g. llm_token, api_x402, stablecoin, card")
    guard_p.add_argument("--amount", type=float, required=True, help="requested spend amount")
    guard_p.add_argument("--budget", required=True, help="budget id")
    guard_p.add_argument("--provider", help="provider, e.g. openai, x402, stripe")
    guard_p.add_argument("--service", help="model, endpoint, or merchant service")
    guard_p.add_argument("--merchant", help="merchant id/address")
    guard_p.add_argument("--session", help="task/session id")
    guard_p.add_argument("--request-id", help="idempotency key for audit/reservation")
    guard_p.add_argument("--enforce-exit-code", action="store_true",
                         help="exit 2 on deny so callers can block the spend")

    gateway_p = sub.add_parser("gateway", help="run local HTTP pre-spend gateway")
    gateway_p.add_argument("--db", default="spend.db", help="SQLite ledger path")
    gateway_p.add_argument("--policy", help="gateway policy JSON; defaults to SPEND_POLICY_FILE")
    gateway_p.add_argument("--host", default="127.0.0.1", help="bind host")
    gateway_p.add_argument("--port", type=int, default=8787, help="bind port")

    validate_p = sub.add_parser("validate-policy", help="strictly validate gateway policy JSON")
    validate_p.add_argument("--policy", required=True, help="gateway policy JSON")

    audit_p = sub.add_parser("audit-config", help="show env vars, outbound hosts, and local files")
    audit_p.add_argument("--policy", required=True, help="gateway policy JSON")
    audit_p.add_argument("--db", default="spend.db", help="SQLite ledger path")
    audit_p.add_argument("--out-dir", default="artifacts", help="artifact directory")

    release_p = sub.add_parser("release-reservation", help="release an active gateway reservation")
    release_p.add_argument("--db", default="spend.db", help="SQLite ledger path")
    release_p.add_argument("--request-id", required=True, help="gateway request id")

    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    cmd = args.cmd or "demo"
    if cmd == "demo":
        demo(args.out_dir)
    elif cmd == "pull":
        pull(args.db, args.out_dir, args.days, args.provider)
    elif cmd == "pull-openrouter":
        pull_openrouter(args.db, args.out_dir, args.generation_id, args.generation_ids_file)
    elif cmd == "pull-aws":
        pull_aws(args.db, args.out_dir, args.days, args.tag_agent, args.tag_budget)
    elif cmd == "pull-gcp-billing-file":
        pull_gcp_billing_file(
            args.db, args.out_dir, args.billing_export_file, args.label_agent, args.label_budget,
        )
    elif cmd == "pull-azure":
        pull_azure(args.db, args.out_dir, args.days, args.scope, args.tag_agent, args.tag_budget)
    elif cmd == "pull-all":
        pull_all(args.config, args.db, args.out_dir)
    elif cmd == "pull-x402":
        pull_x402(args.db, args.out_dir, args.pay_to, args.lookback_blocks, args.wallet_map)
    elif cmd == "pull-usdc":
        pull_usdc(args.db, args.out_dir, args.pay_to, args.lookback_blocks, args.wallet_map)
    elif cmd == "pull-stripe":
        pull_stripe(args.db, args.out_dir, args.days, args.limit)
    elif cmd == "report":
        report(args.db, args.out_dir)
    elif cmd == "guard":
        guard(args)
    elif cmd == "gateway":
        gateway(args.db, args.policy, args.host, args.port)
    elif cmd == "validate-policy":
        validate_policy_cmd(args.policy)
    elif cmd == "audit-config":
        audit_config_cmd(args.policy, args.db, args.out_dir)
    elif cmd == "release-reservation":
        release_reservation_cmd(args.db, args.request_id)


if __name__ == "__main__":
    main()
