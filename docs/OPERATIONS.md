# Operations

`agent-spend-collector` is a read-only observer. It should run with restricted
provider credentials, persist `spend.db`, and generate `report.html` after every
pull.

## Local Production Run

1. Copy `.env.example` into your secret manager or shell profile.
2. Create a budget cap file:

   ```json
   {
     "team-research": 10.0,
     "team-support": 8.0,
     "default": 100.0
   }
   ```

3. Run each configured rail on a schedule:

   ```bash
   export SPEND_BUDGETS_FILE=budgets.json
   python3 -m spend_collector pull --db spend.db --days 7 --out-dir artifacts
   python3 -m spend_collector pull-stripe --db spend.db --days 7 --out-dir artifacts
   python3 -m spend_collector pull-x402 --pay-to 0xYourReceivingAddress --db spend.db --out-dir artifacts
   ```

4. Publish or archive these local artifacts:

   - `spend.db`
   - `artifacts/report.html`
   - `artifacts/alerts.json`
   - `artifacts/run-summary.json`
   - command logs
   - raw provider receipts in your own secure archive

## Scheduling

Use cron, systemd timers, GitHub Actions with secrets, or any job runner that can
persist `spend.db` between runs. Stripe Events have limited retention, so poll
frequently and keep the SQLite ledger as the durable history.

## Safety Boundaries

- Use restricted/read-only keys when providers support them.
- Do not store private keys, card data, PAN/CVV, or wallet seed material.
- The collector does not move funds and does not enforce policy.
- Treat alerts as evidence for investigation or for future inline enforcement.
- `report.html` shows a short evidence suffix, not the raw source payload.

## Inline Gateway

Use the gateway when an agent can ask before spending. Start with
`gateway.example.json`, then tighten the policy per agent:

```bash
export SPEND_POLICY_FILE=gateway.example.json
python3 -m spend_collector validate-policy --policy "$SPEND_POLICY_FILE"
python3 -m spend_collector audit-config --policy "$SPEND_POLICY_FILE"
python3 -m spend_collector gateway --db spend.db --policy "$SPEND_POLICY_FILE"
```

Agents or middleware call `POST /guard` before an LLM call, x402 payment, or
card-backed checkout. A `deny` response should block the spend; an `allow`
response should continue and later land in the ledger through the normal pull.
For allowlisted destinations, agents can call `POST /forward`; the gateway first
runs the same policy check, then forwards the original body only when allowed.
If denied, it returns JSON and does not call the destination.

For shell-based integrations, use:

```bash
python3 -m spend_collector guard \
  --policy gateway.example.json \
  --agent research-bot \
  --rail api_x402 \
  --provider x402 \
  --merchant 0xtool \
  --service /scrape \
  --amount 3.50 \
  --budget team-research \
  --enforce-exit-code
```

The gateway checks policy and ledger history only. It is the enforcement point
when your agent, LLM proxy, or x402 middleware honors the decision.
It does not return prompts, rewrite prompts, or inject model instructions.

For provider-compatible SDKs, keep the real provider key in the gateway process
and give agents only a gateway token:

```bash
export OPENAI_API_KEY=sk-real-provider-key
export SPEND_GATEWAY_TOKEN=dev-gateway-token
python3 -m spend_collector gateway --db spend.db --policy gateway.example.json
```

Then configure the agent SDK with:

```text
base_url = http://127.0.0.1:8787/openai/v1
api_key = dev-gateway-token
X-Agent-ID = research-bot
X-Budget-ID = team-research
```

The gateway swaps the gateway token for the provider key only after an allow
decision. If the policy denies the call, the provider is never contacted.
Every allow/deny is written to `gateway_decisions`. Allow decisions create
short-lived budget holds in `spend_reservations`; release a hold if an allowed
downstream call fails before money moves:

```bash
python3 -m spend_collector release-reservation --db spend.db --request-id req_123
```

## Live Dashboard and Service Restart

A running gateway also serves the dashboard live at `GET /dashboard`, token-gated
via `?token=<token>` or the `Authorization` header. It re-renders from `spend.db`
on every request and auto-refreshes in the browser, so a persistent gateway doubles
as an always-on monitor. If you run it under macOS `launchd`, restart it in place:

```bash
launchctl kickstart -k gui/$(id -u)/com.agentspend.gateway
```

Prefer `kickstart -k` over back-to-back `bootout`+`bootstrap`, which can race and
fail with `Input/output error` while the previous instance is still exiting.

## Failure Handling

Live HTTP/RPC pulls use bounded timeouts and retries. If a pull fails, rerun it:
the ledger is idempotent on `event_id`, so repeated events are ignored and will
not double-count.

## Evidence Model

Every ledger row has two audit pointers:

- `x_receipt_ref`: provider-native reference, such as request id, transaction
  hash, PaymentIntent id, or API key id.
- `x_source_event`: `provider:sha256:<hash>` of the raw source payload before it
  was normalized into the ledger.

The dashboard only displays the final hash suffix so it is safe to share as an
evidence page. Keep raw provider receipts in your own secure archive if you need
full forensic replay.

## Incident Checklist

When `report.html` shows high severity alerts:

1. Identify the agent, rail, merchant/provider, and receipt reference.
2. Check whether the spend was approved by the expected budget owner.
3. Rotate or revoke the implicated provider key/wallet permission if compromise
   is plausible.
4. Preserve `spend.db`, `report.html`, `alerts.json`, `run-summary.json`, and
   raw provider receipts for audit.

## Machine-Readable Outputs

`alerts.json` is a list of alert objects:

```json
[
  {
    "kind": "new_key_spike",
    "subject": "new-key-bot",
    "severity": "high",
    "detail": "first llm_token charge ...",
    "value": 15.0
  }
]
```

`run-summary.json` is intended for job runners and alert routing. It contains
total spend, event count, distinct agents, rails, configured budgets, and counts
of high/warn alerts. Set `SPEND_ALERT_WEBHOOK` to auto-POST high-severity alerts
(Slack-compatible JSON, metadata only — no prompts or keys) on every run, or a cron
wrapper can page when `alerts.high > 0`.

To slow a runaway loop before it drains the whole budget, add a velocity cap to the
policy: `max_amount_per_hour` — a per-budget map (`{"team-research": 5.0}`) or a bare
number for all budgets — denies once the last hour's spend for that budget would
exceed it.
