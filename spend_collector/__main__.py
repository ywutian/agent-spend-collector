"""CLI for the read-only cross-rail agent spend collector."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .adapters import from_llm_usage, from_stripe_events, from_x402_settlements
from .detectors import run_all
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
    _write_json_artifact(alerts_path, [_alert_row(a) for a in alerts])
    _write_json_artifact(summary_path, _run_summary(store, alerts, budgets))
    print(f"Wrote {alerts_path} and {summary_path}")


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


def pull(db_path: str | Path = "spend.db", out_dir: str | Path = ".", days: int = 7) -> None:
    """Pull real cost data from the Anthropic Cost API (read-only, admin key)."""
    from .sources import env_admin_key, fetch_anthropic_cost_report, from_llm_cost_rows
    key = env_admin_key()
    if not key:
        print("Set ANTHROPIC_ADMIN_KEY to pull real cost data:\n"
              "  export ANTHROPIC_ADMIN_KEY=sk-ant-admin01-...\n"
              "  python3 -m spend_collector pull")
        sys.exit(1)
    with SpendStore(str(db_path)) as store:
        n = store.ingest(from_llm_cost_rows(fetch_anthropic_cost_report(key, days=days)))
        print(f"ingested {n} cost rows -> {db_path}")
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
        print(f"loaded ledger {db_path}")
        _finish_run(store, _load_budgets(), out_dir)


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

    pull_p = sub.add_parser("pull", parents=[common], help="pull Anthropic cost report rows")
    pull_p.add_argument("--db", default="spend.db", help="SQLite ledger path")
    pull_p.add_argument("--days", type=int, default=7, help="days of provider history to request")

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

    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    cmd = args.cmd or "demo"
    if cmd == "demo":
        demo(args.out_dir)
    elif cmd == "pull":
        pull(args.db, args.out_dir, args.days)
    elif cmd == "pull-x402":
        pull_x402(args.db, args.out_dir, args.pay_to, args.lookback_blocks)
    elif cmd == "pull-stripe":
        pull_stripe(args.db, args.out_dir, args.days, args.limit)
    elif cmd == "report":
        report(args.db, args.out_dir)


if __name__ == "__main__":
    main()
