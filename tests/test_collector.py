from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import tempfile
import urllib.error
import urllib.request
import unittest
from pathlib import Path

from spend_collector.__main__ import _alert_row, _load_budgets, _run_summary, main
from spend_collector.adapters import from_llm_usage, from_stripe_events, from_x402_settlements
from spend_collector.detectors import run_all
from spend_collector.report import render
from spend_collector.schema import COLUMNS, SpendEvent
from spend_collector.sources import _env_int, _request_json, decode_transfer_log
from spend_collector.store import SpendStore

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"


def load_fixture(name: str):
    with open(FIXTURES / name, encoding="utf-8") as f:
        return json.load(f)


class CollectorTest(unittest.TestCase):
    def build_demo_store(self) -> tuple[SpendStore, dict]:
        store = SpendStore()
        self.addCleanup(store.close)
        store.ingest(from_llm_usage(load_fixture("llm_usage.json")))
        store.ingest(from_x402_settlements(load_fixture("x402_settlements.json")))
        store.ingest(from_stripe_events(load_fixture("stripe_events.json")))
        return store, load_fixture("budgets.json")

    def test_demo_fixtures_prove_cross_rail_security_story(self) -> None:
        store, budgets = self.build_demo_store()
        alerts = run_all(store, budgets)
        kinds = {a.kind for a in alerts}

        self.assertAlmostEqual(store.total(), 27.112, places=3)
        self.assertEqual(
            {"api_x402", "card", "llm_token"},
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

        self.assertEqual(len(rows), 15)
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

        self.assertEqual(summary["events"], 15)
        self.assertEqual(summary["agents"], 3)
        self.assertEqual(set(summary["rails"]), {"api_x402", "card", "llm_token"})
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


if __name__ == "__main__":
    unittest.main()
