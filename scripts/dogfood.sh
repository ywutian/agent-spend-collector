#!/usr/bin/env bash
# One-shot real-data dogfood: pull whatever rails have creds set, then open the report.
# Set any of these first (all read-only):
#   export ANTHROPIC_ADMIN_KEY=sk-ant-admin01-...
#   export X402_PAY_TO=0xYourBaseReceivingAddress
#   export STRIPE_SECRET_KEY=rk_live_...      # restricted read key
set -euo pipefail
cd "$(dirname "$0")/.."

db="${SPEND_DB:-spend.db}"
out_dir="${SPEND_OUT_DIR:-artifacts}"
ran=0
[ -n "${ANTHROPIC_ADMIN_KEY:-}" ] && { python3 -m spend_collector pull --db "$db" --out-dir "$out_dir"; ran=1; }
[ -n "${X402_PAY_TO:-}" ]        && { python3 -m spend_collector pull-x402 --pay-to "$X402_PAY_TO" --db "$db" --out-dir "$out_dir"; ran=1; }
{ [ -n "${STRIPE_SECRET_KEY:-}" ] || [ -n "${STRIPE_API_KEY:-}" ]; } && { python3 -m spend_collector pull-stripe --db "$db" --out-dir "$out_dir"; ran=1; }

if [ "$ran" = 0 ]; then
  echo "No creds set. Export one or more, then re-run:"
  echo "  export ANTHROPIC_ADMIN_KEY=sk-ant-admin01-..."
  echo "  export X402_PAY_TO=0x..."
  echo "  export STRIPE_SECRET_KEY=rk_live_..."
  exit 1
fi

python3 -m spend_collector report --db "$db" --out-dir "$out_dir"
# Open the dashboard (macOS: open, Linux: xdg-open).
{ command -v open >/dev/null && open "$out_dir/report.html"; } \
  || { command -v xdg-open >/dev/null && xdg-open "$out_dir/report.html"; } \
  || echo "Open $out_dir/report.html in a browser."
