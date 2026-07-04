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

from .adapters import from_llm_usage, from_stripe_events, from_x402_settlements
from .detectors import run_all
from .gateway import (
    GuardRequest,
    audit_config as build_audit_config,
    cap_for_request,
    decide,
    record_forwarded_spend,
    record_target_spend,
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


def _load_budgets(default: dict[str, float] | None = None) -> dict[str, float]:
    path = os.environ.get("SPEND_BUDGETS_FILE")
    if not path:
        return dict(default or {})
    data = _load_json_file(path)
    return {str(k): float(v) for k, v in data.items()}


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


def _notify_alerts(alerts: list, summary: dict) -> bool:
    """POST high-severity alerts to SPEND_ALERT_WEBHOOK (opt-in). Formats for Slack,
    Discord, Feishu/Lark, Teams, or a generic JSON body (auto-detected from the URL,
    or SPEND_ALERT_FORMAT). Best-effort: metadata only, never breaks a run.
    """
    url = os.environ.get("SPEND_ALERT_WEBHOOK")
    payload = _alert_payload(alerts, summary)
    if not url or payload is None:
        return False
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
    """Run the product demo: LLM + x402 + Stripe -> ledger -> security signals."""
    llm = _load_fixture("llm_usage.json")
    x402 = _load_fixture("x402_settlements.json")
    stripe = _load_fixture("stripe_events.json")
    budgets = _load_budgets(_load_fixture("budgets.json"))

    with SpendStore() as store:
        store.ingest(from_llm_usage(llm))
        store.ingest(from_x402_settlements(x402))
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
        assert 27.10 < store.total() < 27.12, store.total()
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


def pull_x402(db_path: str | Path = "spend.db", out_dir: str | Path = ".",
              pay_to: str | None = None, lookback_blocks: int = 2000) -> None:
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
            fetch_base_usdc_transfers(pay_to, lookback_blocks=lookback_blocks)
        ))
        print(f"ingested {n} x402 settlements -> {db_path}")
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


def report(db_path: str | Path = "spend.db", out_dir: str | Path = ".") -> None:
    """Regenerate report.html from an existing SQLite ledger."""
    if not Path(db_path).exists():
        print(f"ledger not found: {db_path}")
        sys.exit(1)
    with SpendStore(str(db_path)) as store:
        if store.total() == 0:
            print(f"{db_path} is empty -- run a pull first (pull / pull-x402 / pull-stripe).")
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
    store.reserve_and_record_gateway_decision(
        request_id=request_id,
        req=req,
        decision=decision.decision,
        reasons=decision.reasons,
        route_type=route_type,
        route_id=route_id,
        ttl_seconds=ttl,
        cap=cap,
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
        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, indent=2, sort_keys=True).encode()
            self.send_response(code)
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

        def _send_upstream(self, resp) -> bytes | None:
            headers = dict(resp.headers.items())
            content_type = headers.get("Content-Type", headers.get("content-type", ""))
            is_stream = _is_event_stream(content_type)
            self.send_response(resp.status)
            for key, value in headers.items():
                if key.lower() in {"connection", "content-length", "transfer-encoding"}:
                    continue
                self.send_header(key, value)
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
            self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            try:
                payload = self._read_json()
                policy = _load_policy(policy_path)
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

    x402_p = sub.add_parser("pull-x402", parents=[common],
                            help="pull Base USDC settlements into an x402 address")
    x402_p.add_argument("--db", default="spend.db", help="SQLite ledger path")
    x402_p.add_argument("--pay-to", help="merchant receiving address; defaults to X402_PAY_TO")
    x402_p.add_argument("--lookback-blocks", type=int, default=2000, help="Base blocks to scan")

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
    guard_p.add_argument("--rail", required=True, help="rail, e.g. llm_token, api_x402, card")
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
    elif cmd == "pull-x402":
        pull_x402(args.db, args.out_dir, args.pay_to, args.lookback_blocks)
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
