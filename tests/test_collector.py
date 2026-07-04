from __future__ import annotations

import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.request
import unittest
from datetime import datetime, timezone
from pathlib import Path

from spend_collector.__main__ import (
    _alert_payload, _alert_platform, _alert_row, _format_alert, _is_event_stream,
    _load_budgets, _run_summary, _triage_alerts, _usage_body_from_sse, _with_stream_usage,
    main, make_gateway_server,
)
from spend_collector.adapters import (
    _price, _tokencost_price, from_llm_usage, from_stripe_events,
    from_usdc_transfers, from_x402_settlements,
)
from spend_collector.detectors import Alert, off_hours_activity, run_all
from spend_collector.gateway import (
    GuardRequest, decide, record_forwarded_spend, record_target_spend, validate_policy,
)
from spend_collector.providers import KNOWN_PROVIDERS, llm_provider, usage_tokens
from spend_collector.report import _money, render
from spend_collector.schema import COLUMNS, SpendEvent
from spend_collector.sources import (
    _aws_sigv4_headers, _env_int, _request_json, decode_transfer_log,
    fetch_aws_cost_and_usage, fetch_azure_access_token, fetch_azure_cost_usage,
    fetch_openai_costs, fetch_openrouter_generations, from_aws_cost_rows,
    from_azure_cost_rows, from_gcp_billing_rows, from_llm_cost_rows,
    from_openrouter_generation_rows, load_gcp_billing_export,
)
from spend_collector.store import SpendStore

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"


def load_fixture(name: str):
    with open(FIXTURES / name, encoding="utf-8") as f:
        return json.load(f)


class CollectorTest(unittest.TestCase):
    def wait_for_http(self, url: str) -> None:
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=0.2):
                    return
            except OSError:
                time.sleep(0.05)
        self.fail(f"server did not start: {url}")

    def build_demo_store(self) -> tuple[SpendStore, dict]:
        store = SpendStore()
        self.addCleanup(store.close)
        store.ingest(from_llm_usage(load_fixture("llm_usage.json")))
        store.ingest(from_x402_settlements(load_fixture("x402_settlements.json")))
        store.ingest(from_usdc_transfers(load_fixture("usdc_transfers.json")))
        store.ingest(from_stripe_events(load_fixture("stripe_events.json")))
        return store, load_fixture("budgets.json")

    def test_demo_fixtures_prove_cross_rail_security_story(self) -> None:
        store, budgets = self.build_demo_store()
        alerts = run_all(store, budgets)
        kinds = {a.kind for a in alerts}

        self.assertAlmostEqual(store.total(), 36.112, places=3)
        self.assertEqual(
            {"api_x402", "card", "llm_token", "stablecoin"},
            {r["rail"] for r in store.by("rail")},
        )
        self.assertTrue({
            "spend_spike",
            "budget_burn",
            "budget_burn_rate",
            "spend_per_task",
            "new_key_spike",
            "new_merchant_provider",
        } <= kinds)
        self.assertTrue(any(a.kind == "new_key_spike" and a.subject == "new-key-bot" for a in alerts))

    def test_demo_events_keep_source_evidence_hashes(self) -> None:
        store, _ = self.build_demo_store()
        rows = store.db.execute(
            "SELECT rail, x_receipt_ref, x_source_event FROM spend_events"
        ).fetchall()

        self.assertEqual(len(rows), 17)
        for row in rows:
            self.assertTrue(row["x_receipt_ref"])
            self.assertIn(":sha256:", row["x_source_event"])
            self.assertEqual(len(row["x_source_event"].rsplit(":", 1)[-1]), 64)

    def test_ingest_is_idempotent_and_counts_inserted_rows(self) -> None:
        store = SpendStore()
        self.addCleanup(store.close)
        event = SpendEvent(
            "evt-1", "2026-06-29T01:00:00Z", "card", "stripe", "checkout",
            1.23, "USD", 1, "payment", "agent-a", "budget-a",
        )

        self.assertEqual(store.ingest([event]), 1)
        self.assertEqual(store.ingest([event]), 0)
        self.assertEqual(store.total(), 1.23)

    def test_stripe_adapter_maps_payment_intent_metadata(self) -> None:
        event = {
            "id": "evt_test",
            "type": "payment_intent.succeeded",
            "created": 1781740800,
            "data": {"object": {
                "id": "pi_test",
                "amount_received": 4200,
                "currency": "usd",
                "metadata": {
                    "agent_id": "ops-bot",
                    "budget_id": "team-ops",
                    "merchant_id": "vendor-a",
                    "session_id": "task-1",
                },
            }},
        }

        [row] = from_stripe_events([event])
        self.assertEqual(row.rail, "card")
        self.assertEqual(row.billed_cost, 42.0)
        self.assertEqual(row.x_agent_id, "ops-bot")
        self.assertEqual(row.x_budget_id, "team-ops")
        self.assertEqual(row.x_merchant_id, "vendor-a")

    def test_x402_log_decoder(self) -> None:
        log = {
            "topics": [
                "0x" + "d" * 64,
                "0x" + "0" * 24 + "11" * 20,
                "0x" + "0" * 24 + "22" * 20,
            ],
            "data": "0x" + format(2_500_000, "064x"),
            "transactionHash": "0xabc",
            "blockNumber": "0x10",
        }

        decoded = decode_transfer_log(log)
        self.assertEqual(decoded["to"], "0x" + "22" * 20)
        self.assertEqual(decoded["amount_raw"], 2_500_000)
        self.assertEqual(decoded["block"], 16)

    def test_usdc_adapter_maps_direct_transfers_to_stablecoin_rail(self) -> None:
        rows = from_usdc_transfers(load_fixture("usdc_transfers.json"))

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].rail, "stablecoin")
        self.assertEqual(rows[0].provider_name, "usdc:base")
        self.assertEqual(rows[0].billing_currency, "USDC")
        self.assertEqual(rows[0].billed_cost, 7.25)
        self.assertEqual(rows[0].consumed_quantity, 7.25)
        self.assertEqual(rows[0].pricing_unit, "usdc")
        self.assertEqual(rows[0].x_agent_id, "ops-bot")
        self.assertEqual(rows[0].x_budget_id, "team-ops")
        self.assertEqual(rows[0].x_merchant_id, "data-vendor-wallet")
        self.assertTrue(rows[0].x_receipt_ref.startswith("0xusdc"))

    def test_wallet_map_overrides_onchain_payer_attribution(self) -> None:
        transfer = {
            "transaction": "0xwalletmap",
            "amount": "2.50",
            "asset": "USDC",
            "network": "base",
            "payer": "0xabc",
            "pay_to": "0xmerchant",
            "event_time": "2026-06-29T04:30:00Z",
        }
        wallet_map = {"0xabc": {"agent_id": "mapped-bot", "budget_id": "team-mapped"}}

        [row] = from_usdc_transfers([transfer], wallet_map=wallet_map)

        self.assertEqual(row.x_agent_id, "mapped-bot")
        self.assertEqual(row.x_budget_id, "team-mapped")

    def test_report_contains_console_sections(self) -> None:
        store, budgets = self.build_demo_store()
        html = render(store, budgets, run_all(store, budgets))

        self.assertIn("Agent Spend Console", html)
        self.assertIn("Rail Mix", html)
        self.assertIn("Budget Burn", html)
        self.assertIn("Security Signals", html)
        self.assertIn("Recent Ledger Events", html)
        self.assertIn("Evidence", html)
        self.assertIn("new merchant provider", html)

    def test_cli_demo_writes_artifacts_to_out_dir(self) -> None:
        with tempfile.TemporaryDirectory() as out_dir:
            with contextlib.redirect_stdout(io.StringIO()):
                main(["demo", "--out-dir", out_dir])
            self.assertTrue((Path(out_dir) / "report.html").exists())
            self.assertTrue((Path(out_dir) / "alerts.json").exists())
            self.assertTrue((Path(out_dir) / "run-summary.json").exists())

    def test_cli_report_requires_existing_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.db"
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    main(["report", "--db", str(missing), "--out-dir", tmp])
            self.assertFalse(missing.exists())

    def test_cli_pull_usdc_requires_receiving_address(self) -> None:
        old_usdc = os.environ.pop("USDC_PAY_TO", None)
        old_x402 = os.environ.pop("X402_PAY_TO", None)
        self.addCleanup(lambda: os.environ.__setitem__("USDC_PAY_TO", old_usdc) if old_usdc is not None else None)
        self.addCleanup(lambda: os.environ.__setitem__("X402_PAY_TO", old_x402) if old_x402 is not None else None)

        with tempfile.TemporaryDirectory() as tmp:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                with self.assertRaises(SystemExit):
                    main(["pull-usdc", "--db", str(Path(tmp) / "spend.db"), "--out-dir", tmp])

        self.assertIn("USDC receiving address", stdout.getvalue())

    def test_cli_pull_all_uses_config_and_wallet_map(self) -> None:
        import spend_collector.sources as sources
        original_fetch = sources.fetch_base_usdc_transfers

        def fake_fetch(pay_to, **kwargs):
            return [{
                "transaction": "0x" + str(pay_to)[-4:],
                "amount": "2.00",
                "asset": "USDC",
                "network": "base",
                "payer": "0xabc",
                "pay_to": str(pay_to),
                "resource": "/paid",
                "event_time": "2026-06-29T04:30:00Z",
            }]

        sources.fetch_base_usdc_transfers = fake_fetch
        self.addCleanup(lambda: setattr(sources, "fetch_base_usdc_transfers", original_fetch))

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "spend.config.json"
            db_path = tmp_path / "spend.db"
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump({
                    "db": str(db_path),
                    "out_dir": str(tmp_path / "artifacts"),
                    "budgets": {"team-mapped": 10.0},
                    "wallets": {"0xabc": {"agent_id": "mapped-bot", "budget_id": "team-mapped"}},
                    "rails": {
                        "llm": {"enabled": False},
                        "stripe": {"enabled": False},
                        "x402": {"enabled": True, "pay_to": "0xx402"},
                        "usdc": {"enabled": True, "pay_to": "0xusdc"},
                    },
                }, f)

            with contextlib.redirect_stdout(io.StringIO()):
                main(["pull-all", "--config", str(config_path)])

            with SpendStore(str(db_path)) as store:
                self.addCleanup(store.close)
                rows = store.db.execute(
                    "SELECT rail, x_agent_id, x_budget_id FROM spend_events ORDER BY rail"
                ).fetchall()

        self.assertEqual([r["rail"] for r in rows], ["api_x402", "stablecoin"])
        self.assertEqual({r["x_agent_id"] for r in rows}, {"mapped-bot"})
        self.assertEqual({r["x_budget_id"] for r in rows}, {"team-mapped"})

    def test_gateway_denies_spend_that_would_exceed_budget(self) -> None:
        store = SpendStore()
        self.addCleanup(store.close)
        store.ingest([SpendEvent(
            "evt-budget", "2026-06-29T01:00:00Z", "api_x402", "x402", "/feed",
            1.50, "USDC", 1, "call", "research-bot", "team-research",
            x_merchant_id="0xfeed",
        )])
        decision = decide(store, {"budgets": {"team-research": 2.0}}, GuardRequest(
            x_agent_id="research-bot",
            rail="api_x402",
            provider_name="x402",
            service_name="/scrape",
            x_merchant_id="0xtool",
            amount=0.75,
            x_budget_id="team-research",
        ))

        self.assertEqual(decision.decision, "deny")
        self.assertTrue(any("would exceed cap" in r for r in decision.reasons))

    def test_gateway_checks_agent_rail_amount_and_new_merchant_policy(self) -> None:
        store = SpendStore()
        self.addCleanup(store.close)
        request = GuardRequest(
            x_agent_id="research-bot",
            rail="card",
            provider_name="stripe",
            service_name="checkout",
            x_merchant_id="new-vendor",
            amount=12.0,
            x_budget_id="team-research",
        )
        decision = decide(store, {
            "deny_new_merchants": True,
            "agents": {"research-bot": {"rails": ["llm_token", "api_x402"], "max_amount": 5.0}},
        }, request)

        self.assertEqual(decision.decision, "deny")
        self.assertTrue(any("rail card is not allowed" in r for r in decision.reasons))
        self.assertTrue(any("exceeds max" in r for r in decision.reasons))
        self.assertTrue(any("has no ledger history" in r for r in decision.reasons))

    def test_cli_guard_outputs_machine_readable_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "spend.db"
            policy_path = Path(tmp) / "policy.json"
            with open(policy_path, "w", encoding="utf-8") as f:
                json.dump({"max_amount": 1.0}, f)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                main([
                    "guard",
                    "--db", str(db_path),
                    "--policy", str(policy_path),
                    "--agent", "research-bot",
                    "--rail", "api_x402",
                    "--provider", "x402",
                    "--merchant", "0xtool",
                    "--service", "/scrape",
                    "--amount", "3.50",
                    "--budget", "team-research",
                ])
            payload = json.loads(stdout.getvalue())

        self.assertFalse(payload["allowed"])
        self.assertEqual(payload["decision"], "deny")
        self.assertTrue(any("exceeds max" in r for r in payload["reasons"]))

    def test_http_gateway_forward_allows_or_denies_without_prompts(self) -> None:
        calls: list[dict] = []

        class Upstream(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length", "0")))
                calls.append(json.loads(body))
                out = b'{"upstream": true}'
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, fmt, *args):
                return

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()
        self.addCleanup(lambda: (upstream.shutdown(), upstream_thread.join(1), upstream.server_close()))

        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "policy.json"
            db_path = Path(tmp) / "spend.db"
            policy = {
                "max_amount": 1.0,
                "gateway_tokens": ["test-gateway-token"],
                "content_guard": {"deny_patterns": ["ignore previous instructions"]},
                "targets": {
                    "ok": {
                        "url": f"http://127.0.0.1:{upstream.server_port}/ok",
                        "rail": "api_x402",
                        "provider": "x402",
                        "merchant": "0xtool",
                        "service": "/ok",
                        "amount": 0.5,
                        "budget": "team-research",
                    },
                    "blocked": {
                        "url": f"http://127.0.0.1:{upstream.server_port}/blocked",
                        "rail": "api_x402",
                        "provider": "x402",
                        "merchant": "0xtool",
                        "service": "/blocked",
                        "amount": 2.5,
                        "budget": "team-research",
                    },
                },
            }
            with open(policy_path, "w", encoding="utf-8") as f:
                json.dump(policy, f)

            gateway_server = make_gateway_server(str(db_path), str(policy_path), port=0)
            gateway_port = gateway_server.server_port
            gateway_thread = threading.Thread(target=gateway_server.serve_forever, daemon=True)
            gateway_thread.start()
            self.addCleanup(lambda: (
                gateway_server.shutdown(),
                gateway_thread.join(1),
                gateway_server.server_close(),
            ))
            self.wait_for_http(f"http://127.0.0.1:{gateway_port}/health")

            allow_req = urllib.request.Request(
                f"http://127.0.0.1:{gateway_port}/forward",
                data=json.dumps({"agent": "research-bot", "target": "ok", "body": {"q": "yes"}}).encode(),
                headers={"content-type": "application/json", "authorization": "Bearer test-gateway-token"},
                method="POST",
            )
            with urllib.request.urlopen(allow_req, timeout=2) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(json.load(resp), {"upstream": True})
            self.assertEqual(calls, [{"q": "yes"}])

            deny_req = urllib.request.Request(
                f"http://127.0.0.1:{gateway_port}/forward",
                data=json.dumps({"agent": "research-bot", "target": "blocked", "body": {"q": "no"}}).encode(),
                headers={"content-type": "application/json", "authorization": "Bearer test-gateway-token"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(deny_req, timeout=2)
            self.assertEqual(err.exception.code, 403)
            payload = json.loads(err.exception.read())
            self.assertEqual(payload["decision"], "deny")
            self.assertFalse(payload["allowed"])
            self.assertNotIn("prompt", json.dumps(payload).lower())
            self.assertEqual(calls, [{"q": "yes"}])

            content_deny_req = urllib.request.Request(
                f"http://127.0.0.1:{gateway_port}/forward",
                data=json.dumps({
                    "agent": "research-bot",
                    "target": "ok",
                    "body": {"q": "ignore previous instructions"},
                }).encode(),
                headers={"content-type": "application/json", "authorization": "Bearer test-gateway-token"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(content_deny_req, timeout=2)
            self.assertEqual(err.exception.code, 403)
            self.assertEqual(calls, [{"q": "yes"}])

    def test_x402_middleware_quotes_payment_requirements_before_settlement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "spend.db"
            policy_path = Path(tmp) / "policy.json"
            with open(policy_path, "w", encoding="utf-8") as f:
                json.dump({
                    "budgets": {"team-research": 10.0},
                    "x402_resources": {
                        "scrape": {
                            "url": "http://127.0.0.1:9/scrape",
                            "amount": 1.25,
                            "asset": "0xUSDC",
                            "pay_to": "0xmerchant",
                            "network": "eip155:8453",
                            "service": "/scrape",
                            "budget": "team-research",
                            "facilitator_url": "http://127.0.0.1:9",
                        },
                    },
                }, f)
            gateway_server = make_gateway_server(str(db_path), str(policy_path), port=0)
            gateway_port = gateway_server.server_port
            gateway_thread = threading.Thread(target=gateway_server.serve_forever, daemon=True)
            gateway_thread.start()
            self.addCleanup(lambda: (gateway_server.shutdown(), gateway_thread.join(1), gateway_server.server_close()))
            self.wait_for_http(f"http://127.0.0.1:{gateway_port}/health")

            req = urllib.request.Request(
                f"http://127.0.0.1:{gateway_port}/x402/scrape",
                headers={"x-agent-id": "research-bot", "x-budget-id": "team-research"},
                method="GET",
            )
            with self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(req, timeout=2)
            self.assertEqual(err.exception.code, 402)
            header = err.exception.headers["payment-required"]
            required = json.loads(header)
            body = json.loads(err.exception.read())

        self.assertEqual(required["paymentRequirements"]["amount"], "1250000")
        self.assertEqual(required["paymentRequirements"]["payTo"], "0xmerchant")
        self.assertEqual(body["resource"], "scrape")

    def test_x402_middleware_verifies_settles_forwards_and_records(self) -> None:
        upstream_calls: list[dict] = []
        facilitator_calls: list[dict] = []

        class Upstream(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length", "0")))
                upstream_calls.append({
                    "path": self.path,
                    "payment": self.headers.get("payment-signature"),
                    "body": json.loads(body),
                })
                out = b'{"paid": true}'
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, fmt, *args):
                return

        class Facilitator(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", "0"))))
                facilitator_calls.append({"path": self.path, "body": body})
                if self.path.endswith("/verify"):
                    out = {"isValid": True, "payer": "0xpayer", "extra": {}}
                else:
                    out = {
                        "success": True,
                        "payer": "0xpayer",
                        "transaction": "0xsettled",
                        "network": "eip155:8453",
                        "amount": "2500000",
                    }
                raw = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, fmt, *args):
                return

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()
        self.addCleanup(lambda: (upstream.shutdown(), upstream_thread.join(1), upstream.server_close()))

        facilitator = ThreadingHTTPServer(("127.0.0.1", 0), Facilitator)
        facilitator_thread = threading.Thread(target=facilitator.serve_forever, daemon=True)
        facilitator_thread.start()
        self.addCleanup(lambda: (facilitator.shutdown(), facilitator_thread.join(1), facilitator.server_close()))

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "spend.db"
            policy_path = Path(tmp) / "policy.json"
            with open(policy_path, "w", encoding="utf-8") as f:
                json.dump({
                    "budgets": {"team-research": 10.0},
                    "content_guard": {"deny_patterns": ["blocked payload"]},
                    "reservation_ttl_seconds": 900,
                    "x402_resources": {
                        "scrape": {
                            "url": f"http://127.0.0.1:{upstream.server_port}/paid",
                            "resource_url": "https://seller.example/x402/scrape",
                            "method": "POST",
                            "amount": 2.50,
                            "asset": "0xUSDC",
                            "pay_to": "0xmerchant",
                            "network": "eip155:8453",
                            "service": "/paid",
                            "budget": "team-research",
                            "facilitator_url": f"http://127.0.0.1:{facilitator.server_port}",
                        },
                    },
                }, f)
            gateway_server = make_gateway_server(str(db_path), str(policy_path), port=0)
            gateway_port = gateway_server.server_port
            gateway_thread = threading.Thread(target=gateway_server.serve_forever, daemon=True)
            gateway_thread.start()
            self.addCleanup(lambda: (gateway_server.shutdown(), gateway_thread.join(1), gateway_server.server_close()))
            self.wait_for_http(f"http://127.0.0.1:{gateway_port}/health")

            payment = {
                "x402Version": 2,
                "accepted": {
                    "scheme": "exact",
                    "network": "eip155:8453",
                    "asset": "0xUSDC",
                    "amount": "2500000",
                    "payTo": "0xmerchant",
                },
                "payload": {"authorization": {"from": "0xpayer"}, "signature": "0xsig"},
                "resource": {"url": "https://seller.example/x402/scrape"},
            }
            mismatched = {
                **payment,
                "accepted": {**payment["accepted"], "amount": "9999999"},
                "resource": {"url": "https://seller.example/x402/other"},
            }
            bad_req = urllib.request.Request(
                f"http://127.0.0.1:{gateway_port}/x402/scrape",
                data=json.dumps({"q": "no"}).encode(),
                headers={
                    "content-type": "application/json",
                    "payment-signature": json.dumps(mismatched),
                    "x-agent-id": "research-bot",
                    "x-budget-id": "team-research",
                    "x-request-id": "x402-bad-binding",
                },
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(bad_req, timeout=2)
            self.assertEqual(err.exception.code, 402)
            mismatch_body = json.loads(err.exception.read())
            self.assertEqual(mismatch_body["error"]["reason"], "payment_binding_mismatch")
            self.assertIn("accepted.amount", mismatch_body["error"]["message"])
            self.assertIn("resource.url", mismatch_body["error"]["message"])

            content_blocked_req = urllib.request.Request(
                f"http://127.0.0.1:{gateway_port}/x402/scrape",
                data=json.dumps({"q": "blocked payload"}).encode(),
                headers={
                    "content-type": "application/json",
                    "payment-signature": json.dumps(payment),
                    "x-agent-id": "research-bot",
                    "x-budget-id": "team-research",
                    "x-request-id": "x402-content-blocked",
                },
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(content_blocked_req, timeout=2)
            self.assertEqual(err.exception.code, 403)
            self.assertEqual(facilitator_calls, [])

            req = urllib.request.Request(
                f"http://127.0.0.1:{gateway_port}/x402/scrape",
                data=json.dumps({"q": "yes"}).encode(),
                headers={
                    "content-type": "application/json",
                    "payment-signature": json.dumps(payment),
                    "x-agent-id": "research-bot",
                    "x-budget-id": "team-research",
                    "x-request-id": "x402-req-1",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(json.load(resp), {"paid": True})
                payment_response = json.loads(resp.headers["payment-response"])

            duplicate_req = urllib.request.Request(
                f"http://127.0.0.1:{gateway_port}/x402/scrape",
                data=json.dumps({"q": "yes"}).encode(),
                headers={
                    "content-type": "application/json",
                    "payment-signature": json.dumps(payment),
                    "x-agent-id": "research-bot",
                    "x-budget-id": "team-research",
                    "x-request-id": "x402-req-1",
                },
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(duplicate_req, timeout=2)
            self.assertEqual(err.exception.code, 409)

            with SpendStore(str(db_path)) as store:
                self.addCleanup(store.close)
                row = store.db.execute(
                    "SELECT rail, provider_name, billed_cost, billing_currency, x_agent_id, "
                    "x_budget_id, x_receipt_ref FROM spend_events"
                ).fetchone()
                active = store.db.execute(
                    "SELECT COUNT(*) FROM spend_reservations WHERE status = 'active'"
                ).fetchone()[0]

        self.assertEqual([c["path"] for c in facilitator_calls], ["/verify", "/settle"])
        self.assertEqual(facilitator_calls[0]["body"]["paymentRequirements"]["amount"], "2500000")
        self.assertEqual(len(upstream_calls), 1)
        self.assertIsNone(upstream_calls[0]["payment"])
        self.assertEqual(upstream_calls[0]["body"], {"q": "yes"})
        self.assertEqual(payment_response["transaction"], "0xsettled")
        self.assertEqual(row["rail"], "api_x402")
        self.assertEqual(row["provider_name"], "x402")
        self.assertEqual(row["billed_cost"], 2.5)
        self.assertEqual(row["billing_currency"], "USDC")
        self.assertEqual(row["x_agent_id"], "research-bot")
        self.assertEqual(row["x_budget_id"], "team-research")
        self.assertEqual(row["x_receipt_ref"], "0xsettled")
        self.assertEqual(active, 0)

    def test_provider_compatible_gateway_swaps_agent_token_for_provider_key(self) -> None:
        calls: list[dict] = []

        class Provider(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length", "0")))
                calls.append({
                    "path": self.path,
                    "auth": self.headers.get("authorization"),
                    "agent": self.headers.get("x-agent-id"),
                    "body": json.loads(body),
                })
                out = b'{"id":"chatcmpl_test","choices":[]}'
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, fmt, *args):
                return

        provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
        provider_thread.start()
        self.addCleanup(lambda: (provider.shutdown(), provider_thread.join(1), provider.server_close()))

        old_key = os.environ.get("FAKE_OPENAI_KEY")
        os.environ["FAKE_OPENAI_KEY"] = "sk-provider-secret"
        if old_key is None:
            self.addCleanup(lambda: os.environ.pop("FAKE_OPENAI_KEY", None))
        else:
            self.addCleanup(lambda: os.environ.__setitem__("FAKE_OPENAI_KEY", old_key))

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "spend.db"
            policy_path = Path(tmp) / "policy.json"
            policy = {
                "max_amount": 1.0,
                "gateway_tokens": ["agent-gateway-token"],
                "providers": {
                    "openai": {
                        "base_url": f"http://127.0.0.1:{provider.server_port}",
                        "api_key_env": "FAKE_OPENAI_KEY",
                        "rail": "llm_token",
                        "provider": "openai",
                        "merchant": "openai",
                        "service_from_body": "model",
                        "amount": 0.5,
                        "budget": "team-research",
                    },
                    "openai_blocked": {
                        "base_url": f"http://127.0.0.1:{provider.server_port}",
                        "api_key_env": "FAKE_OPENAI_KEY",
                        "rail": "llm_token",
                        "provider": "openai",
                        "merchant": "openai",
                        "service_from_body": "model",
                        "amount": 2.5,
                        "budget": "team-research",
                    },
                },
            }
            with open(policy_path, "w", encoding="utf-8") as f:
                json.dump(policy, f)

            gateway_server = make_gateway_server(str(db_path), str(policy_path), port=0)
            gateway_port = gateway_server.server_port
            gateway_thread = threading.Thread(target=gateway_server.serve_forever, daemon=True)
            gateway_thread.start()
            self.addCleanup(lambda: (
                gateway_server.shutdown(),
                gateway_thread.join(1),
                gateway_server.server_close(),
            ))
            self.wait_for_http(f"http://127.0.0.1:{gateway_port}/health")

            body = {"model": "gpt-test", "messages": [{"role": "user", "content": "hi"}]}
            allow_req = urllib.request.Request(
                f"http://127.0.0.1:{gateway_port}/openai/v1/chat/completions",
                data=json.dumps(body).encode(),
                headers={
                    "content-type": "application/json",
                    "authorization": "Bearer agent-gateway-token",
                    "x-agent-id": "research-bot",
                    "x-budget-id": "team-research",
                },
                method="POST",
            )
            with urllib.request.urlopen(allow_req, timeout=2) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(json.load(resp)["id"], "chatcmpl_test")

            self.assertEqual(calls[0]["path"], "/v1/chat/completions")
            self.assertEqual(calls[0]["auth"], "Bearer sk-provider-secret")
            self.assertEqual(calls[0]["agent"], None)
            self.assertEqual(calls[0]["body"], body)

            deny_req = urllib.request.Request(
                f"http://127.0.0.1:{gateway_port}/openai_blocked/v1/chat/completions",
                data=json.dumps(body).encode(),
                headers={
                    "content-type": "application/json",
                    "authorization": "Bearer agent-gateway-token",
                    "x-agent-id": "research-bot",
                    "x-budget-id": "team-research",
                },
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(deny_req, timeout=2)
            self.assertEqual(err.exception.code, 403)
            payload = json.loads(err.exception.read())
            self.assertEqual(payload["decision"], "deny")
            self.assertEqual(len(calls), 1)

    def test_policy_validation_and_audit_config_are_secret_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            valid = Path(tmp) / "valid.json"
            typo = Path(tmp) / "typo.json"
            raw = Path(tmp) / "raw.json"
            no_token = Path(tmp) / "no-token.json"
            with open(valid, "w", encoding="utf-8") as f:
                json.dump({
                    "gateway_tokens": ["agent-token"],
                    "providers": {
                        "openai": {
                            "base_url": "https://api.openai.com",
                            "api_key_env": "OPENAI_API_KEY",
                            "amount": 0.1,
                        },
                    },
                }, f)
            with open(typo, "w", encoding="utf-8") as f:
                json.dump({"max_amunt": 1}, f)
            with open(raw, "w", encoding="utf-8") as f:
                json.dump({
                    "gateway_tokens": ["agent-token"],
                    "providers": {"openai": {
                        "base_url": "https://api.openai.com",
                        "api_key": "sk-secret",
                        "amount": 0.1,
                    }},
                }, f)
            with open(no_token, "w", encoding="utf-8") as f:
                json.dump({
                    "providers": {"openai": {
                        "base_url": "https://api.openai.com",
                        "api_key_env": "OPENAI_API_KEY",
                        "amount": 0.1,
                    }},
                }, f)

            with contextlib.redirect_stdout(io.StringIO()) as out:
                main(["validate-policy", "--policy", str(valid)])
            self.assertIn("policy OK", out.getvalue())
            for bad in (typo, raw, no_token):
                with contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        main(["validate-policy", "--policy", str(bad)])

            with contextlib.redirect_stdout(io.StringIO()) as out:
                main(["audit-config", "--policy", str(valid), "--db", "spend.db", "--out-dir", "artifacts"])
            audit = json.loads(out.getvalue())
            self.assertIn("OPENAI_API_KEY", audit["env_vars_read"])
            self.assertIn("api.openai.com", audit["outbound_hosts"])
            self.assertNotIn("sk-secret", json.dumps(audit))
            self.assertIn("prompts", audit["will_not_store"])

    def test_gateway_requires_token_for_forwarding_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "spend.db"
            policy_path = Path(tmp) / "policy.json"
            with open(policy_path, "w", encoding="utf-8") as f:
                json.dump({
                    "gateway_tokens": ["agent-token"],
                    "providers": {
                        "openai": {
                            "base_url": "https://example.com",
                            "api_key_env": "FAKE_OPENAI_KEY",
                            "amount": 0.1,
                        },
                    },
                }, f)
            gateway_server = make_gateway_server(str(db_path), str(policy_path), port=0)
            gateway_port = gateway_server.server_port
            gateway_thread = threading.Thread(target=gateway_server.serve_forever, daemon=True)
            gateway_thread.start()
            self.addCleanup(lambda: (gateway_server.shutdown(), gateway_thread.join(1), gateway_server.server_close()))
            self.wait_for_http(f"http://127.0.0.1:{gateway_port}/health")

            req = urllib.request.Request(
                f"http://127.0.0.1:{gateway_port}/openai/v1/chat/completions",
                data=json.dumps({"model": "gpt-test"}).encode(),
                headers={"content-type": "application/json", "x-agent-id": "research-bot"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(req, timeout=2)
            self.assertEqual(err.exception.code, 401)

    def test_gateway_audits_and_reserves_then_releases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "spend.db"
            policy_path = Path(tmp) / "policy.json"
            with open(policy_path, "w", encoding="utf-8") as f:
                json.dump({"budgets": {"team-research": 1.0}, "reservation_ttl_seconds": 900}, f)

            with contextlib.redirect_stdout(io.StringIO()) as out:
                main([
                    "guard", "--db", str(db_path), "--policy", str(policy_path),
                    "--agent", "research-bot", "--rail", "api_x402", "--amount", "0.75",
                    "--budget", "team-research", "--request-id", "req-a",
                ])
            first = json.loads(out.getvalue())
            self.assertTrue(first["allowed"])
            self.assertTrue(first["reservation_id"])

            with contextlib.redirect_stdout(io.StringIO()) as out:
                main([
                    "guard", "--db", str(db_path), "--policy", str(policy_path),
                    "--agent", "research-bot", "--rail", "api_x402", "--amount", "0.75",
                    "--budget", "team-research", "--request-id", "req-b",
                ])
            second = json.loads(out.getvalue())
            self.assertFalse(second["allowed"])

            with SpendStore(str(db_path)) as store:
                self.addCleanup(store.close)
                self.assertEqual(
                    store.db.execute("SELECT COUNT(*) FROM gateway_decisions").fetchone()[0], 2
                )
                self.assertEqual(
                    store.db.execute("SELECT COUNT(*) FROM spend_reservations WHERE status = 'active'").fetchone()[0], 1
                )

            with contextlib.redirect_stdout(io.StringIO()) as out:
                main(["release-reservation", "--db", str(db_path), "--request-id", "req-a"])
            self.assertEqual(json.loads(out.getvalue())["released"], 1)

    def test_duplicate_request_id_does_not_double_reserve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "spend.db"
            policy_path = Path(tmp) / "policy.json"
            with open(policy_path, "w", encoding="utf-8") as f:
                json.dump({"budgets": {"team-research": 10.0}}, f)
            argv = [
                "guard", "--db", str(db_path), "--policy", str(policy_path),
                "--agent", "research-bot", "--rail", "api_x402", "--amount", "1.00",
                "--budget", "team-research", "--request-id", "same-req",
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                main(argv)
            with contextlib.redirect_stdout(io.StringIO()):
                main(argv)
            with SpendStore(str(db_path)) as store:
                self.addCleanup(store.close)
                self.assertEqual(
                    store.db.execute("SELECT COUNT(*) FROM spend_reservations").fetchone()[0], 1
                )
                self.assertEqual(
                    store.db.execute("SELECT COUNT(*) FROM gateway_decisions").fetchone()[0], 1
                )

    def test_concurrent_gateway_requests_cannot_over_reserve_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "spend.db"
            policy_path = Path(tmp) / "policy.json"
            with open(policy_path, "w", encoding="utf-8") as f:
                json.dump({"budgets": {"team-research": 1.0}}, f)
            gateway_server = make_gateway_server(str(db_path), str(policy_path), port=0)
            gateway_port = gateway_server.server_port
            gateway_thread = threading.Thread(target=gateway_server.serve_forever, daemon=True)
            gateway_thread.start()
            self.addCleanup(lambda: (gateway_server.shutdown(), gateway_thread.join(1), gateway_server.server_close()))
            self.wait_for_http(f"http://127.0.0.1:{gateway_port}/health")

            results: list[bool] = []
            lock = threading.Lock()
            start = threading.Barrier(3)

            def call(idx: int) -> None:
                start.wait()
                req = urllib.request.Request(
                    f"http://127.0.0.1:{gateway_port}/guard",
                    data=json.dumps({
                        "agent": "research-bot",
                        "rail": "api_x402",
                        "amount": 0.75,
                        "budget": "team-research",
                        "request_id": f"parallel-{idx}",
                    }).encode(),
                    headers={"content-type": "application/json"},
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(req, timeout=2) as resp:
                        payload = json.load(resp)
                except urllib.error.HTTPError as exc:
                    payload = json.loads(exc.read())
                with lock:
                    results.append(payload["allowed"])

            threads = [threading.Thread(target=call, args=(i,)) for i in range(2)]
            for thread in threads:
                thread.start()
            start.wait()
            for thread in threads:
                thread.join(3)

            self.assertEqual(sorted(results), [False, True])

    def test_provider_gateway_streams_sse_without_logging_body(self) -> None:
        class StreamProvider(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("content-length", 0) or 0)
                if length:  # drain the request body, else an unread body can RST the socket
                    self.rfile.read(length)
                out = b"data: one\n\ndata: [DONE]\n\n"
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                self.wfile.flush()

            def log_message(self, fmt, *args):
                return

        provider = ThreadingHTTPServer(("127.0.0.1", 0), StreamProvider)
        provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
        provider_thread.start()
        self.addCleanup(lambda: (provider.shutdown(), provider_thread.join(1), provider.server_close()))
        old_key = os.environ.get("FAKE_STREAM_KEY")
        os.environ["FAKE_STREAM_KEY"] = "sk-stream"
        if old_key is None:
            self.addCleanup(lambda: os.environ.pop("FAKE_STREAM_KEY", None))
        else:
            self.addCleanup(lambda: os.environ.__setitem__("FAKE_STREAM_KEY", old_key))

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "spend.db"
            policy_path = Path(tmp) / "policy.json"
            with open(policy_path, "w", encoding="utf-8") as f:
                json.dump({
                    "gateway_tokens": ["agent-token"],
                    "providers": {"openai": {
                        "base_url": f"http://127.0.0.1:{provider.server_port}",
                        "api_key_env": "FAKE_STREAM_KEY",
                        "amount": 0.1,
                    }},
                }, f)
            gateway_server = make_gateway_server(str(db_path), str(policy_path), port=0)
            gateway_port = gateway_server.server_port
            gateway_thread = threading.Thread(target=gateway_server.serve_forever, daemon=True)
            gateway_thread.start()
            self.addCleanup(lambda: (gateway_server.shutdown(), gateway_thread.join(1), gateway_server.server_close()))
            self.wait_for_http(f"http://127.0.0.1:{gateway_port}/health")
            req = urllib.request.Request(
                f"http://127.0.0.1:{gateway_port}/openai/v1/chat/completions",
                data=json.dumps({"model": "gpt-test", "stream": True}).encode(),
                headers={
                    "content-type": "application/json",
                    "authorization": "Bearer agent-token",
                    "x-agent-id": "research-bot",
                    "x-budget-id": "team-research",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                self.assertIn("text/event-stream", resp.headers["content-type"])
                self.assertEqual(resp.read(), b"data: one\n\ndata: [DONE]\n\n")
            with SpendStore(str(db_path)) as store:
                self.addCleanup(store.close)
                row = store.db.execute("SELECT reasons_json FROM gateway_decisions").fetchone()
                self.assertNotIn("data: one", row["reasons_json"])

    def test_report_contains_gateway_audit_sections(self) -> None:
        store = SpendStore()
        self.addCleanup(store.close)
        req = GuardRequest(
            x_agent_id="research-bot",
            rail="api_x402",
            amount=0.25,
            x_budget_id="team-research",
            provider_name="x402",
            service_name="/scrape",
            x_merchant_id="0xtool",
        )
        store.reserve_and_record_gateway_decision(
            request_id="report-req",
            req=req,
            decision="deny",
            reasons=["blocked for test"],
            route_type="target",
            route_id="scraper-demo",
        )
        html = render(store, {}, [])
        self.assertIn("Gateway blocked", html)
        self.assertIn("Recent Gateway Decisions", html)
        self.assertIn("research-bot", html)

    def test_store_migrates_existing_ledgers_to_current_schema(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = f.name
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))

        old_columns = [c for c in COLUMNS if c != "x_source_event"]
        ddl = (
            "CREATE TABLE spend_events ("
            + ", ".join(f"{c} REAL" if c in {"billed_cost", "consumed_quantity"} else f"{c} TEXT" for c in old_columns)
            + ", PRIMARY KEY (event_id))"
        )
        db = sqlite3.connect(path)
        db.execute(ddl)
        db.commit()
        db.close()

        store = SpendStore(path)
        self.addCleanup(store.close)
        columns = {row["name"] for row in store.db.execute("PRAGMA table_info(spend_events)")}

        self.assertIn("x_source_event", columns)
        self.assertEqual(store.ingest([SpendEvent(
            "evt-migrated", "2026-06-29T01:00:00Z", "card", "stripe", "checkout",
            2.0, "USD", 1, "payment", "agent-a", "budget-a",
            x_source_event="stripe:sha256:" + "a" * 64,
        )]), 1)

    def test_machine_readable_run_artifacts_have_stable_shape(self) -> None:
        store, budgets = self.build_demo_store()
        alerts = run_all(store, budgets)
        summary = _run_summary(store, alerts, budgets)
        alert = _alert_row(alerts[0])

        self.assertEqual(summary["events"], 17)
        self.assertEqual(summary["agents"], 4)
        self.assertEqual(set(summary["rails"]), {"api_x402", "card", "llm_token", "stablecoin"})
        self.assertEqual(summary["alerts"]["total"], len(alerts))
        self.assertTrue({"kind", "subject", "detail", "severity", "value"} <= set(alert))

    def test_budget_file_env_loads_caps(self) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as f:
            json.dump({"team-a": 12, "team-b": 3.5}, f)
            path = f.name
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        old = os.environ.get("SPEND_BUDGETS_FILE")
        os.environ["SPEND_BUDGETS_FILE"] = path
        if old is None:
            self.addCleanup(lambda: os.environ.pop("SPEND_BUDGETS_FILE", None))
        else:
            self.addCleanup(lambda: os.environ.__setitem__("SPEND_BUDGETS_FILE", old))

        self.assertEqual(_load_budgets(), {"team-a": 12.0, "team-b": 3.5})

    def test_http_retry_helper_retries_transient_errors(self) -> None:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return None

            def read(self):
                return b'{"ok": true}'

        calls = {"count": 0}
        original = urllib.request.urlopen

        def fake_urlopen(req, timeout):
            calls["count"] += 1
            if calls["count"] == 1:
                raise urllib.error.URLError("temporary")
            return FakeResponse()

        urllib.request.urlopen = fake_urlopen
        self.addCleanup(lambda: setattr(urllib.request, "urlopen", original))

        req = urllib.request.Request("https://example.invalid")
        self.assertEqual(_request_json(req, timeout=1, retries=2), {"ok": True})
        self.assertEqual(calls["count"], 2)

    def test_env_int_falls_back_on_invalid_values(self) -> None:
        old = os.environ.get("SPEND_HTTP_TIMEOUT")
        os.environ["SPEND_HTTP_TIMEOUT"] = "not-an-int"
        if old is None:
            self.addCleanup(lambda: os.environ.pop("SPEND_HTTP_TIMEOUT", None))
        else:
            self.addCleanup(lambda: os.environ.__setitem__("SPEND_HTTP_TIMEOUT", old))

        self.assertEqual(_env_int("SPEND_HTTP_TIMEOUT", 30), 30)

    def test_from_llm_cost_rows_tags_provider(self) -> None:
        rows = [{"amount_usd": 0.42, "api_key_id": "key-1", "model": "gpt-5",
                 "event_time": "2026-06-30T00:00:00+00:00", "provider": "openai"}]
        events = from_llm_cost_rows(rows)
        self.assertEqual(events[0].provider_name, "openai")
        self.assertEqual(events[0].billed_cost, 0.42)
        self.assertEqual(events[0].rail, "llm_token")

    def test_fetch_openai_costs_parses_amount_value(self) -> None:
        import spend_collector.sources as sources
        canned = {"data": [{"start_time": 1781740800, "results": [
            {"amount": {"value": 0.42, "currency": "usd"},
             "api_key_id": "key-1", "line_item": "gpt-5, input"}]}]}
        original = sources._request_json
        sources._request_json = lambda req: canned
        try:
            rows = fetch_openai_costs("sk-test")
        finally:
            sources._request_json = original
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount_usd"], 0.42)  # OpenAI value is dollars, not cents
        self.assertEqual(rows[0]["provider"], "openai")
        self.assertEqual(rows[0]["api_key_id"], "key-1")
        self.assertEqual(rows[0]["model"], "gpt-5, input")

    def test_fetch_openrouter_generations_parses_metadata_envelope(self) -> None:
        import spend_collector.sources as sources
        canned = {"data": {
            "id": "gen-1",
            "created_at": "2026-06-30T00:00:00+00:00",
            "model": "openai/gpt-4o-mini",
            "provider_name": "OpenAI",
            "tokens_prompt": 10,
            "tokens_completion": 5,
            "total_cost": 0.0012,
            "external_user": "agent-key",
        }}
        original = sources._request_json
        sources._request_json = lambda req: canned
        try:
            rows = fetch_openrouter_generations("sk-or-test", ["gen-1"])
        finally:
            sources._request_json = original

        self.assertEqual(rows, [canned["data"]])
        events = from_openrouter_generation_rows(
            rows,
            key_to_agent={"agent-key": "router-bot"},
            key_to_budget={"agent-key": "team-router"},
        )
        self.assertEqual(events[0].provider_name, "openrouter")
        self.assertEqual(events[0].service_name, "openai/gpt-4o-mini")
        self.assertEqual(events[0].billed_cost, 0.0012)
        self.assertEqual(events[0].consumed_quantity, 15)
        self.assertEqual(events[0].x_agent_id, "router-bot")
        self.assertEqual(events[0].x_budget_id, "team-router")

    def test_aws_sigv4_headers_include_authorization(self) -> None:
        from datetime import datetime, timezone
        headers = _aws_sigv4_headers(
            url="https://ce.us-east-1.amazonaws.com",
            body=b"{}",
            access_key="AKIDEXAMPLE",
            secret_key="secret",
            region="us-east-1",
            service="ce",
            target="AWSInsightsIndexService.GetCostAndUsage",
            session_token="token",
            now=datetime(2026, 6, 30, tzinfo=timezone.utc),
        )

        self.assertIn("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/", headers["authorization"])
        self.assertEqual(headers["x-amz-security-token"], "token")
        self.assertEqual(headers["x-amz-target"], "AWSInsightsIndexService.GetCostAndUsage")

    def test_fetch_aws_cost_and_usage_maps_tag_groups_to_cloud_rows(self) -> None:
        import spend_collector.sources as sources
        canned = {"ResultsByTime": [{
            "TimePeriod": {"Start": "2026-06-29", "End": "2026-06-30"},
            "Groups": [{
                "Keys": ["agent_id$research-bot", "budget_id$team-research"],
                "Metrics": {"UnblendedCost": {"Amount": "1.23", "Unit": "USD"}},
            }],
        }]}
        original = sources._request_json
        sources._request_json = lambda req: canned
        try:
            rows = fetch_aws_cost_and_usage(
                "AKID",
                "SECRET",
                days=1,
                tag_agent="agent_id",
                tag_budget="budget_id",
            )
        finally:
            sources._request_json = original

        self.assertEqual(rows[0]["agent_id"], "research-bot")
        self.assertEqual(rows[0]["budget_id"], "team-research")
        self.assertEqual(rows[0]["amount"], 1.23)
        events = from_aws_cost_rows(rows)
        self.assertEqual(events[0].rail, "cloud")
        self.assertEqual(events[0].provider_name, "aws")
        self.assertEqual(events[0].billed_cost, 1.23)
        self.assertEqual(events[0].x_agent_id, "research-bot")
        self.assertEqual(events[0].x_budget_id, "team-research")

    def test_gcp_billing_export_maps_labels_to_cloud_rows(self) -> None:
        rows = load_gcp_billing_export(FIXTURES / "gcp_billing_export.json")
        events = from_gcp_billing_rows(rows)

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].rail, "cloud")
        self.assertEqual(events[0].provider_name, "gcp")
        self.assertEqual(events[0].service_name, "Cloud Run")
        self.assertEqual(events[0].billed_cost, 3.21)
        self.assertEqual(events[0].consumed_quantity, 12.5)
        self.assertEqual(events[0].pricing_unit, "seconds")
        self.assertEqual(events[0].x_agent_id, "research-bot")
        self.assertEqual(events[0].x_budget_id, "team-research")
        self.assertEqual(events[0].x_merchant_id, "agent-prod")

    def test_cli_pull_gcp_billing_file_ingests_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "spend.db"
            with contextlib.redirect_stdout(io.StringIO()):
                main([
                    "pull-gcp-billing-file",
                    "--db", str(db_path),
                    "--out-dir", tmp,
                    "--billing-export-file", str(FIXTURES / "gcp_billing_export.json"),
                ])
            with SpendStore(str(db_path)) as store:
                self.addCleanup(store.close)
                row = store.db.execute(
                    "SELECT rail, provider_name, SUM(billed_cost) AS spend FROM spend_events GROUP BY rail, provider_name"
                ).fetchone()

        self.assertEqual(row["rail"], "cloud")
        self.assertEqual(row["provider_name"], "gcp")
        self.assertAlmostEqual(row["spend"], 4.65)

    def test_fetch_azure_access_token_uses_service_principal_form(self) -> None:
        import spend_collector.sources as sources
        seen = {}
        original = sources._request_json

        def fake_request(req):
            seen["url"] = req.full_url
            seen["body"] = req.data.decode()
            seen["content_type"] = req.headers["Content-type"]
            return {"access_token": "azure-token"}

        sources._request_json = fake_request
        try:
            token = fetch_azure_access_token("tenant-1", "client-1", "secret-1")
        finally:
            sources._request_json = original

        self.assertEqual(token, "azure-token")
        self.assertIn("/tenant-1/oauth2/v2.0/token", seen["url"])
        self.assertEqual(seen["content_type"], "application/x-www-form-urlencoded")
        self.assertIn("scope=https%3A%2F%2Fmanagement.azure.com%2F.default", seen["body"])

    def test_fetch_azure_cost_usage_maps_tag_columns_to_cloud_rows(self) -> None:
        import spend_collector.sources as sources
        canned = load_fixture("azure_cost_query.json")
        seen = {}
        original = sources._request_json

        def fake_request(req):
            seen["url"] = req.full_url
            seen["auth"] = req.headers["Authorization"]
            seen["body"] = json.loads(req.data.decode())
            return canned

        sources._request_json = fake_request
        try:
            rows = fetch_azure_cost_usage(
                "azure-token",
                "/subscriptions/sub-1",
                days=1,
                tag_agent="agent_id",
                tag_budget="budget_id",
            )
        finally:
            sources._request_json = original

        self.assertIn("/subscriptions/sub-1/providers/Microsoft.CostManagement/query", seen["url"])
        self.assertEqual(seen["auth"], "Bearer azure-token")
        self.assertEqual(seen["body"]["dataset"]["grouping"][0]["type"], "TagKey")
        self.assertEqual(rows[0]["agent_id"], "research-bot")
        self.assertEqual(rows[0]["budget_id"], "team-research")
        self.assertEqual(rows[0]["start"], "2026-06-29")
        self.assertEqual(rows[0]["amount"], 2.34)

    def test_from_azure_cost_rows_maps_to_cloud_events(self) -> None:
        rows = [{
            "start": "2026-06-29",
            "end": "2026-06-29",
            "amount": 2.34,
            "currency": "USD",
            "service": "azure",
            "agent_id": "research-bot",
            "budget_id": "team-research",
            "scope": "/subscriptions/sub-1",
        }]
        events = from_azure_cost_rows(rows)

        self.assertEqual(events[0].rail, "cloud")
        self.assertEqual(events[0].provider_name, "azure")
        self.assertEqual(events[0].service_name, "azure")
        self.assertEqual(events[0].billed_cost, 2.34)
        self.assertEqual(events[0].x_agent_id, "research-bot")
        self.assertEqual(events[0].x_budget_id, "team-research")
        self.assertEqual(events[0].x_merchant_id, "/subscriptions/sub-1")

    def test_price_longest_prefix_match(self) -> None:
        self.assertAlmostEqual(_price("gpt-4o", 1_000_000, 0), 2.5)              # exact
        self.assertAlmostEqual(_price("gpt-4o-2024-08-06", 1_000_000, 0), 2.5)   # dated -> prefix
        self.assertAlmostEqual(_price("gpt-4o-mini-2024-07-18", 1_000_000, 0), 0.15)  # longest wins
        self.assertEqual(_price("totally-unknown", 1_000_000, 1_000_000), 0.0)   # unknown -> 0

    def test_gateway_records_forwarded_spend(self) -> None:
        raw = json.dumps({
            "id": "chatcmpl-1", "model": "gpt-4o-2024-08-06",
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500},
        }).encode()
        provider = {"provider": "openai"}
        guard_payload = {"agent": "advisor-bot", "budget": "team-edu", "session": "s1"}
        store = SpendStore()
        self.addCleanup(store.close)
        event = record_forwarded_spend(store, raw, provider, guard_payload)
        self.assertIsNotNone(event)
        self.assertEqual(event.x_agent_id, "advisor-bot")
        self.assertEqual(event.rail, "llm_token")
        self.assertEqual(event.consumed_quantity, 1500)
        self.assertGreater(event.billed_cost, 0)  # dated id -> longest-prefix price match
        self.assertEqual(store.total(), event.billed_cost)
        # streamed body with no usage records nothing
        self.assertIsNone(record_forwarded_spend(store, b"data: [DONE]\n", provider, guard_payload))

    def test_gateway_records_openrouter_usage_cost(self) -> None:
        raw = json.dumps({
            "id": "gen-1",
            "model": "anthropic/claude-sonnet-5",
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 500,
                "total_tokens": 1500,
                "cost": 0.0123,
            },
        }).encode()
        store = SpendStore()
        self.addCleanup(store.close)
        event = record_forwarded_spend(
            store,
            raw,
            {"provider": "openrouter"},
            {"agent": "router-bot", "budget": "team-router"},
        )

        self.assertIsNotNone(event)
        self.assertEqual(event.provider_name, "openrouter")
        self.assertEqual(event.billed_cost, 0.0123)
        self.assertEqual(event.consumed_quantity, 1500)
        self.assertEqual(store.total(), 0.0123)

    def test_gateway_records_anthropic_usage_shape(self) -> None:
        # Anthropic returns input_tokens/output_tokens, not prompt_/completion_tokens
        raw = json.dumps({
            "id": "msg_1", "model": "claude-opus-4-8",
            "usage": {"input_tokens": 1000, "output_tokens": 500},
        }).encode()
        store = SpendStore()
        self.addCleanup(store.close)
        event = record_forwarded_spend(store, raw, {"provider": "anthropic"},
                                       {"agent": "advisor-bot", "budget": "team-edu"})
        self.assertIsNotNone(event)
        self.assertEqual(event.consumed_quantity, 1500)
        self.assertGreater(event.billed_cost, 0)  # claude-opus-4-8 is priced

    def test_gateway_records_gemini_usage_shape(self) -> None:
        # Gemini native: usageMetadata.promptTokenCount / candidatesTokenCount, modelVersion
        raw = json.dumps({
            "modelVersion": "gemini-2.5-flash",
            "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 500},
        }).encode()
        store = SpendStore()
        self.addCleanup(store.close)
        event = record_forwarded_spend(store, raw, {"provider": "gemini"},
                                       {"agent": "advisor-bot", "budget": "team-edu"})
        self.assertIsNotNone(event)
        self.assertEqual(event.consumed_quantity, 1500)
        self.assertEqual(event.service_name, "gemini-2.5-flash")
        self.assertGreater(event.billed_cost, 0)

    def test_gateway_records_target_tool_spend(self) -> None:
        # non-LLM tools have no token usage -> record the flat per-call amount
        store = SpendStore()
        self.addCleanup(store.close)
        guard_payload = {"agent": "research-bot", "budget": "team", "rail": "api",
                         "provider": "deepgram", "service": "deepgram", "amount": 0.0043}
        event = record_target_spend(store, guard_payload, "req-tool-1")
        self.assertIsNotNone(event)
        self.assertEqual(event.rail, "api")
        self.assertEqual(event.provider_name, "deepgram")
        self.assertEqual(event.pricing_unit, "call")
        self.assertAlmostEqual(event.billed_cost, 0.0043)
        self.assertAlmostEqual(store.total(), 0.0043)
        # no amount or no request id -> nothing recorded
        self.assertIsNone(record_target_spend(store, {"amount": 0}, "req-x"))
        self.assertIsNone(record_target_spend(store, {"amount": 1.0}, ""))

    def test_off_hours_activity_detector(self) -> None:
        store = SpendStore()
        self.addCleanup(store.close)
        evs = [SpendEvent(f"day-{i}", f"2026-06-29T09:0{i}:00+00:00", "llm_token", "openai",
                          "gpt-4o", 0.5, "USD", 100, "token", "night-bot", "team") for i in range(6)]
        evs.append(SpendEvent("night", "2026-06-30T03:30:00+00:00", "llm_token", "openai",
                              "gpt-4o", 5.0, "USD", 1000, "token", "night-bot", "team"))
        store.ingest(evs)
        alerts = off_hours_activity(store)
        self.assertTrue(any(a.kind == "off_hours_activity" and a.subject == "night-bot" for a in alerts))

    def test_gateway_hourly_rate_cap_denies_burst(self) -> None:
        store = SpendStore()
        self.addCleanup(store.close)
        store.ingest([SpendEvent("r1", datetime.now(timezone.utc).isoformat(), "llm_token",
                     "openai", "gpt-4o", 4.0, "USD", 100, "token", "bot", "team-x")])
        policy = {"max_amount_per_hour": {"team-x": 5.0}}
        allow = decide(store, policy, GuardRequest(x_agent_id="bot", rail="llm_token", amount=0.5, x_budget_id="team-x"))
        self.assertEqual(allow.decision, "allow")   # 4 + 0.5 <= 5
        deny = decide(store, policy, GuardRequest(x_agent_id="bot", rail="llm_token", amount=2.0, x_budget_id="team-x"))
        self.assertEqual(deny.decision, "deny")     # 4 + 2 > 5
        self.assertTrue(any("hourly rate" in r for r in deny.reasons))

    def test_alert_multi_platform_formatting(self) -> None:
        os.environ.pop("SPEND_ALERT_FORMAT", None)
        self.assertEqual(_alert_platform("https://hooks.slack.com/services/x"), "slack")
        self.assertEqual(_alert_platform("https://discord.com/api/webhooks/x"), "discord")
        self.assertEqual(_alert_platform("https://open.feishu.cn/open-apis/bot/v2/hook/x"), "feishu")
        self.assertEqual(_alert_platform("https://acme.webhook.office.com/x"), "teams")
        self.assertEqual(_alert_platform("https://my-own.example/hook"), "generic")
        s = {"text": "hi", "alerts": [], "summary": {}}
        self.assertEqual(_format_alert("slack", "hi", s), {"text": "hi"})
        self.assertEqual(_format_alert("discord", "hi", s), {"content": "hi"})
        self.assertEqual(_format_alert("feishu", "hi", s)["msg_type"], "text")
        self.assertEqual(_format_alert("teams", "hi", s)["@type"], "MessageCard")
        self.assertEqual(_format_alert("generic", "hi", s), s)

    def test_triage_is_opt_in(self) -> None:
        old_env = {
            key: os.environ.get(key)
            for key in (
                "SPEND_TRIAGE_MODEL",
                "SPEND_TRIAGE_BASE_URL",
                "SPEND_TRIAGE_API_KEY",
                "SPEND_GATEWAY_TOKEN",
                "OPENAI_API_KEY",
            )
        }
        for key in old_env:
            os.environ.pop(key, None)

        def restore_env():
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore_env)
        highs = [Alert("spend_spike", "bot", "big", "high", 9.9)]
        self.assertIsNone(_triage_alerts(highs, {}))  # no SPEND_TRIAGE_MODEL -> off
        os.environ["SPEND_TRIAGE_MODEL"] = "gpt-4o-mini"
        warns = [Alert("new_merchant_provider", "bot", "new", "warn", 1.0)]
        self.assertIsNone(_triage_alerts(warns, {}))  # enabled but no high alert -> no call
        os.environ["OPENAI_API_KEY"] = "sk-provider-key"
        os.environ["SPEND_TRIAGE_BASE_URL"] = "http://127.0.0.1:8788/openai/v1"
        original_urlopen = urllib.request.urlopen

        def fail_if_called(*args, **kwargs):
            raise AssertionError("local gateway triage must not use OPENAI_API_KEY as its token")

        urllib.request.urlopen = fail_if_called
        self.addCleanup(lambda: setattr(urllib.request, "urlopen", original_urlopen))
        self.assertIsNone(_triage_alerts(highs, {}))  # no gateway token -> no outbound call

    def test_alert_payload_only_for_high_severity(self) -> None:
        warns = [Alert("new_merchant_provider", "bot", "new", "warn", 1.0)]
        highs = [Alert("spend_spike", "bot", "big charge", "high", 9.9)]
        self.assertIsNone(_alert_payload(warns, {"total_spend": 1}))
        payload = _alert_payload(highs, {"total_spend": 9.9})
        self.assertIsNotNone(payload)
        self.assertIn("spend_spike", payload["text"])
        self.assertEqual(len(payload["alerts"]), 1)

    def test_provider_catalog_and_usage_shapes(self) -> None:
        # usage_tokens spans the four LLM response families
        self.assertEqual(usage_tokens({"usage": {"prompt_tokens": 10, "completion_tokens": 5}}), (10, 5))
        self.assertEqual(usage_tokens({"usage": {"input_tokens": 10, "output_tokens": 5}}), (10, 5))
        self.assertEqual(usage_tokens({"usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}}), (10, 5))
        self.assertEqual(usage_tokens({"meta": {"billed_units": {"input_tokens": 10, "output_tokens": 5}}}), (10, 5))
        self.assertEqual(usage_tokens({}), (0, 0))
        # catalog lets a policy name a provider instead of writing base_url/api_key_env
        self.assertIn("groq", KNOWN_PROVIDERS)
        self.assertTrue(llm_provider("groq")["base_url"])
        self.assertIsNone(llm_provider("not-a-provider"))
        errs = validate_policy({"gateway_tokens": ["t"], "providers": {
            "mistral": {"service_from_body": "model", "amount": 0.25, "budget": "b"}}})
        self.assertEqual(errs, [])  # known provider name -> base_url/api_key_env optional

    def test_gateway_records_cohere_usage_shape(self) -> None:
        # Cohere: meta.billed_units.{input_tokens,output_tokens}; model from the request
        raw = json.dumps({
            "id": "c1", "meta": {"billed_units": {"input_tokens": 1000, "output_tokens": 500}},
        }).encode()
        store = SpendStore()
        self.addCleanup(store.close)
        event = record_forwarded_spend(store, raw, {"provider": "cohere"},
                                       {"agent": "advisor-bot", "budget": "team-edu", "service": "command-r-plus"})
        self.assertIsNotNone(event)
        self.assertEqual(event.consumed_quantity, 1500)
        self.assertEqual(event.service_name, "command-r-plus")
        self.assertGreater(event.billed_cost, 0)

    def test_tokencost_optional_pricing_fallback(self) -> None:
        try:
            import tokencost  # noqa: F401
            has_tc = True
        except ImportError:
            has_tc = False
        if not has_tc:  # no dependency -> None, and _price still works via static book
            self.assertIsNone(_tokencost_price("gpt-4o-mini", 1000, 500))
        # tokencost and the static book agree here, so this is deterministic either way
        self.assertAlmostEqual(_price("gpt-4o-mini", 1000, 500), 0.00045)
        self.assertGreater(_price("gemini-2.5-flash", 1000, 500), 0)  # added to fallback
        self.assertEqual(_price("totally-unknown-xyz", 1000, 500), 0.0)  # never crashes

    def test_gateway_records_streamed_spend(self) -> None:
        # stream:true -> gateway asks the provider for a final usage chunk
        injected = json.loads(_with_stream_usage(json.dumps({"model": "gpt-4o-mini", "stream": True}).encode()))
        self.assertTrue(injected["stream_options"]["include_usage"])
        plain = json.dumps({"model": "gpt-4o-mini"}).encode()  # non-stream / non-json pass through
        self.assertEqual(_with_stream_usage(plain), plain)
        self.assertNotIn(b"stream_options", _with_stream_usage(b"not json"))

        # a streamed SSE tail -> synthetic body -> priced + recorded
        sse = (
            b'data: {"id":"chatcmpl-9","model":"gpt-4o-mini","choices":[{"delta":{"content":"hi"}}],"usage":null}\n\n'
            b'data: {"id":"chatcmpl-9","model":"gpt-4o-mini","choices":[],'
            b'"usage":{"prompt_tokens":1000,"completion_tokens":500,"total_tokens":1500}}\n\n'
            b'data: [DONE]\n\n'
        )
        body = _usage_body_from_sse(sse)
        self.assertIsNotNone(body)
        store = SpendStore()
        self.addCleanup(store.close)
        event = record_forwarded_spend(store, body, {"provider": "openai"},
                                       {"agent": "advisor-bot", "budget": "team-edu"})
        self.assertIsNotNone(event)
        self.assertEqual(event.consumed_quantity, 1500)
        self.assertAlmostEqual(event.billed_cost, (1000 * 0.15 + 500 * 0.6) / 1_000_000)
        # a stream that carried no usage -> nothing to record
        self.assertIsNone(_usage_body_from_sse(b'data: {"choices":[{"delta":{}}]}\n\ndata: [DONE]\n\n'))

    def test_money_formatting_shows_subcent_costs(self) -> None:
        self.assertEqual(_money(0), "$0.00")
        self.assertEqual(_money(12.5), "$12.50")
        self.assertEqual(_money(1234.5), "$1,234.50")
        self.assertEqual(_money(0.0000018), "$0.000002")  # sub-cent LLM call, not "$0.00"
        self.assertEqual(_money(0.0034), "$0.0034")

    def test_dashboard_refresh_only_when_requested(self) -> None:
        store, budgets = self.build_demo_store()
        alerts = run_all(store, budgets)
        self.assertNotIn('http-equiv="refresh"', render(store, budgets, alerts))
        self.assertIn('http-equiv="refresh" content="30"', render(store, budgets, alerts, refresh_seconds=30))

    def test_chunked_json_is_not_treated_as_event_stream(self) -> None:
        # regression: OpenAI sends non-stream JSON with Transfer-Encoding: chunked;
        # detecting that as a stream SSE-parsed plain JSON, dropped usage, and the
        # forwarded spend went unrecorded. Only Content-Type text/event-stream counts.
        self.assertFalse(_is_event_stream("application/json"))
        self.assertFalse(_is_event_stream("application/json; charset=utf-8"))
        self.assertTrue(_is_event_stream("text/event-stream"))
        self.assertTrue(_is_event_stream("text/event-stream; charset=utf-8"))

    def test_gateway_dashboard_route_renders_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "spend.db"
            policy_path = Path(tmp) / "policy.json"
            with open(policy_path, "w", encoding="utf-8") as f:
                json.dump({"gateway_tokens": ["dash-token"], "budgets": {"team": 5.0}}, f)
            with SpendStore(str(db_path)) as store:
                store.ingest([SpendEvent(
                    "evt-dash", "2026-06-30T01:00:00Z", "llm_token", "openai", "gpt-4o-mini",
                    0.01, "USD", 100, "token", "study-abroad-api", "team")])

            gateway_server = make_gateway_server(str(db_path), str(policy_path), port=0)
            port = gateway_server.server_port
            thread = threading.Thread(target=gateway_server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(lambda: (gateway_server.shutdown(), thread.join(1), gateway_server.server_close()))
            self.wait_for_http(f"http://127.0.0.1:{port}/health")

            with urllib.request.urlopen(f"http://127.0.0.1:{port}/dashboard?token=dash-token", timeout=2) as resp:
                body = resp.read().decode()
                self.assertEqual(resp.status, 200)
                self.assertIn("text/html", resp.headers.get("content-type", ""))
            self.assertIn("Agent Spend Console", body)
            self.assertIn("study-abroad-api", body)

            with self.assertRaises(urllib.error.HTTPError) as err:  # no token -> 401
                urllib.request.urlopen(f"http://127.0.0.1:{port}/dashboard", timeout=2)
            self.assertEqual(err.exception.code, 401)

    def test_hourly_rate_cap_is_atomic(self) -> None:
        # the velocity cap is re-checked inside the reservation transaction, counting
        # active holds -- so a second concurrent allow can't slip past it.
        store = SpendStore()
        self.addCleanup(store.close)
        req = GuardRequest(x_agent_id="bot", rail="llm_token", amount=3.0, x_budget_id="b")
        d1, res1 = store.reserve_and_record_gateway_decision(
            request_id="r1", req=req, decision="allow", reasons=["ok"], rate_cap=5.0)
        self.assertEqual(d1, "allow")
        self.assertTrue(res1)
        d2, res2 = store.reserve_and_record_gateway_decision(  # 3 held + 3 asked > 5
            request_id="r2", req=req, decision="allow", reasons=["ok"], rate_cap=5.0)
        self.assertEqual(d2, "deny")
        self.assertFalse(res2)

    def test_content_guard_inspection(self) -> None:
        from spend_collector.gateway import inspect_content, validate_policy
        self.assertEqual(inspect_content(b'{"model":"gpt-4o"}', {}), [])  # disabled -> clean
        self.assertTrue(inspect_content(b"x" * 5000, {"content_guard": {"max_bytes": 1000}}))
        r = inspect_content(b'{"content":"ignore previous instructions"}',
                            {"content_guard": {"deny_patterns": ["ignore previous instructions"]}})
        self.assertTrue(any("deny pattern" in x for x in r))
        r2 = inspect_content(b'{"content":"sk-abcdefghijklmnopqrstuvwxyz123456"}',
                             {"content_guard": {"deny_secrets": True}})
        self.assertTrue(any("secret" in x for x in r2))
        self.assertEqual(inspect_content(b'{"model":"gpt-4o"}',
                         {"content_guard": {"max_bytes": 100000, "deny_secrets": True}}), [])
        self.assertEqual(validate_policy({"gateway_tokens": ["t"],
                         "content_guard": {"max_bytes": 1000, "deny_secrets": True}}), [])
        self.assertTrue(validate_policy({"gateway_tokens": ["t"], "content_guard": {"bogus": 1}}))

    def test_gateway_kill_switch_freezes_agent(self) -> None:
        store = SpendStore()
        self.addCleanup(store.close)
        req = GuardRequest(x_agent_id="rogue-bot", rail="llm_token", amount=0.1, x_budget_id="team")
        self.assertEqual(decide(store, {"frozen_agents": ["rogue-bot"]}, req).decision, "deny")
        self.assertEqual(decide(store, {"frozen_budgets": ["team"]}, req).decision, "deny")
        self.assertEqual(decide(store, {"frozen_agents": ["someone-else"]}, req).decision, "allow")

    def test_gateway_blocks_on_current_anomaly(self) -> None:
        store = SpendStore()
        self.addCleanup(store.close)
        evs = [SpendEvent(f"s{i}", f"2026-06-30T10:0{i}:00+00:00", "llm_token", "openai",
               "gpt-4o", 0.001, "USD", 10, "token", "spiky-bot", "b") for i in range(5)]
        evs.append(SpendEvent("s-big", "2026-06-30T10:06:00+00:00", "llm_token", "openai",
                   "gpt-4o", 10.0, "USD", 100000, "token", "spiky-bot", "b"))
        store.ingest(evs)
        self.assertTrue(any(a.subject == "spiky-bot" and a.severity == "high" for a in run_all(store, {})))
        policy = {"block_on_anomaly": True}
        d = decide(store, policy, GuardRequest(x_agent_id="spiky-bot", rail="llm_token", amount=0.01, x_budget_id="b"))
        self.assertEqual(d.decision, "deny")
        self.assertTrue(any("blocked on anomaly" in r for r in d.reasons))
        calm = decide(store, policy, GuardRequest(x_agent_id="calm-bot", rail="llm_token", amount=0.01, x_budget_id="b"))
        self.assertEqual(calm.decision, "allow")

    def test_freeze_cli_edits_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "policy.json"
            p.write_text(json.dumps({"gateway_tokens": ["t"]}))
            with contextlib.redirect_stdout(io.StringIO()):
                main(["freeze", "--policy", str(p), "--agent", "rogue-bot"])
            self.assertIn("rogue-bot", json.loads(p.read_text())["frozen_agents"])
            with contextlib.redirect_stdout(io.StringIO()):
                main(["unfreeze", "--policy", str(p), "--agent", "rogue-bot"])
            self.assertNotIn("rogue-bot", json.loads(p.read_text())["frozen_agents"])


if __name__ == "__main__":
    unittest.main()
