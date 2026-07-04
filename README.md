# agent-spend-collector

**See and govern every dollar your AI agents spend, across every rail.**

A free, read-only, cross-rail **agent spend collector**. It pulls what your agents
spend (LLM token cost + x402 payments + direct USDC transfers + AWS/GCP/Azure cloud cost + Stripe card payments),
normalizes it into **one [FOCUS](https://focus.finops.org/)-shaped ledger**, and
flags anomalies: runaway loops, cost spikes, budget burn. **It never touches your
money** (read-only), so it clears security review on day one.

> Why this exists: FinOps tools track token cost, payment startups track payments.
> Nobody gives you one neutral book of record across all of it, and nobody reads
> agent spend as a security signal. A cost spike is also how a hijacked key or a
> prompt-injected agent can look. That gap is the product.

## Quick start (no dependencies, no keys)

```bash
python3 -m spend_collector demo
```

Runs the full loop on mock data:

```text
fixtures -> one ledger -> anomaly detectors -> report.html
```

Open `report.html` in a browser.

## What the demo proves

The demo is a security + cross-rail product demo, not random sample data. It reads
public fixtures from `fixtures/`:

| Fixture | Purpose |
|---|---|
| `llm_usage.json` | LLM token baseline, runaway token spike, and a new high-spend key |
| `x402_settlements.json` | Paid API calls settled in USDC on the x402 rail |
| `usdc_transfers.json` | Direct Base USDC wallet/smart-account payments |
| `stripe_events.json` | Stripe card payments with agent/budget metadata |
| `budgets.json` | Team caps used by the budget detectors |

Those fixtures intentionally trigger the core Phase-0 signals:

| Signal | Why it fires |
|---|---|
| `spend_spike` | `research-bot` has four tiny LLM calls, then one runaway token-heavy call |
| `spend_per_task` | The same runaway request is expensive versus the agent's task baseline |
| `budget_burn` | `team-support` crosses its budget after x402 + Stripe + token spend |
| `budget_burn_rate` | Recent spend is burning monthly budgets too quickly |
| `new_key_spike` | `new-key-bot` appears for the first time with a large LLM charge |
| `new_merchant_provider` | `support-bot` spends at a first-seen paid API/card merchant |

The point is to prove the wedge: token cost, paid API spend, and card payments can
land in one neutral ledger, and abnormal spend can become a security signal.

## Dashboard

Every command that ingests data runs the detectors and writes the static dashboard:

```bash
python3 -m spend_collector demo
open report.html
```

To write artifacts into a directory for CI or scheduled jobs:

```bash
python3 -m spend_collector demo --out-dir artifacts
```

On Windows, open `report.html` from Explorer or run:

```powershell
start report.html
```

The dashboard shows total spend, alert counts, rail mix, budget burn, Phase-0
security signals, agent-by-rail totals, and recent ledger events. Recent events
include an Evidence column: the short suffix of a stable `provider:sha256:<hash>`
pointer to the raw source payload. It is a local HTML file with no server and no
external assets.

Each run also writes machine-readable artifacts:

| File | Purpose |
|---|---|
| `alerts.json` | Alert rows with kind, subject, severity, detail, and value |
| `run-summary.json` | Total spend, event/agent/rail counts, budgets, and alert counts |

To regenerate the dashboard from an existing ledger:

```bash
python3 -m spend_collector report
python3 -m spend_collector report --db path/to/spend.db --out-dir artifacts
```

## Pre-spend gateway

The collector can also sit in front of agent spend. An agent asks before it
spends; the gateway returns `allow` or `deny` from policy plus ledger history.

## Trust model

The gateway is self-hosted by default. Real provider keys should stay in your
environment variables or secret manager, not in policy files. Agents receive only
gateway tokens; the gateway swaps that token for a provider key only after an
allow decision. The project does not call project-owned servers, and gateway
audit logs store metadata only: no prompts, request bodies, completions,
responses, provider keys, or gateway tokens. See `SECURITY.md`.

Try a one-off decision:

```bash
python3 -m spend_collector guard \
  --policy gateway.example.json \
  --db spend.db \
  --agent research-bot \
  --rail api_x402 \
  --provider x402 \
  --merchant 0xtool \
  --service /scrape \
  --amount 3.50 \
  --budget team-research \
  --enforce-exit-code
```

Or run a local HTTP gateway:

```bash
python3 -m spend_collector gateway --policy gateway.example.json --db spend.db
```

Then call it before a payment/model call:

```bash
curl -X POST http://127.0.0.1:8787/guard \
  -H 'content-type: application/json' \
  -d '{"agent":"research-bot","rail":"api_x402","provider":"x402","merchant":"0xtool","service":"/scrape","amount":3.5,"budget":"team-research"}'
```

For calls the gateway should forward, define an allowlisted `targets` entry in
`gateway.example.json`, then call `/forward`:

```bash
curl -X POST http://127.0.0.1:8787/forward \
  -H 'content-type: application/json' \
  -d '{"agent":"research-bot","target":"scraper-demo","body":{"query":"pricing data"}}'
```

The gateway checks the target's policy first. If allowed, it forwards the request
body to the configured URL. If denied, it does not call the target and returns a
JSON decision such as `{"decision":"deny","allowed":false,"reasons":[...]}`.
It never returns prompts, rewrites prompts, or injects model instructions.

For true x402 seller-side middleware, define an `x402_resources` entry. The
gateway exposes it at `/x402/<resource-id>`:

```bash
# First request: no payment yet, returns HTTP 402 + PAYMENT-REQUIRED.
curl -i http://127.0.0.1:8787/x402/scraper-paid \
  -H 'X-Agent-ID: research-bot' \
  -H 'X-Budget-ID: team-research'

# Second request: x402-capable clients retry with PAYMENT-SIGNATURE.
curl -i http://127.0.0.1:8787/x402/scraper-paid \
  -H 'content-type: application/json' \
  -H 'X-Agent-ID: research-bot' \
  -H 'X-Budget-ID: team-research' \
  -H "PAYMENT-SIGNATURE: $PAYMENT_SIGNATURE" \
  -d '{"query":"pricing data"}'
```

On the paid retry, the gateway checks policy and budget, verifies and settles the
payment through the configured x402 facilitator, forwards the request to the
protected upstream only after settlement, returns `PAYMENT-RESPONSE`, and records
the settlement on the `api_x402` rail. It also accepts the legacy `X-PAYMENT`
header for clients that still use that spelling. The signed payment payload is
bound to the configured `amount`, `pay_to`, `network`, `asset`, and
`resource_url` (or `url` when `resource_url` is omitted), so a payment signed for
one resource cannot be reused against another configured resource.

For SDKs that support a custom base URL, put the real provider key only on the
gateway and give the agent a gateway token:

```bash
export OPENAI_API_KEY=sk-real-provider-key
export OPENROUTER_API_KEY=sk-or-real-provider-key
export SPEND_GATEWAY_TOKEN=dev-gateway-token
python3 -m spend_collector gateway --policy gateway.example.json --db spend.db
```

Then point the agent's OpenAI-compatible SDK at the gateway:

```text
base_url = http://127.0.0.1:8787/openai/v1
api_key = dev-gateway-token
headers = {"X-Agent-ID": "research-bot", "X-Budget-ID": "team-research"}
```

For OpenRouter, use the same SDK shape and change only the provider path:

```text
base_url = http://127.0.0.1:8787/openrouter/v1
api_key = dev-gateway-token
headers = {"X-Agent-ID": "research-bot", "X-Budget-ID": "team-research"}
```

The gateway checks policy, replaces the gateway token with the provider key, and
forwards the original request only when allowed. OpenRouter responses include
usage and cost metadata, so successful gateway calls are recorded with the
actual OpenRouter charge instead of a local price estimate.

Validate and audit the gateway config before starting it:

```bash
python3 -m spend_collector validate-policy --policy gateway.example.json
python3 -m spend_collector audit-config --policy gateway.example.json
```

If an allowed downstream call fails before money moves, release its hold:

```bash
python3 -m spend_collector release-reservation --db spend.db --request-id req_123
```

This is the first inline-control layer: provider proxy routes do not move funds,
while `/x402/<resource-id>` routes settle an already-signed x402 payment before
delivering the protected resource. Callers should block the spend when the
decision is `deny`.

## Verify locally

The project needs no required dependencies. Run the test suite and product demo with:

```bash
python3 -m unittest discover -s tests
python3 -m compileall spend_collector tests
python3 -m spend_collector demo
```

The tests cover ledger idempotency, rail adapters, x402 log decoding, Phase-0
detectors, fixtures, and dashboard rendering.

## Real data (read-only)

Fastest setup: copy one config, fill the addresses/env-var names you use, then
pull every configured rail into the same ledger.

```bash
cp spend.config.example.json spend.config.json
# edit spend.config.json: receiving addresses, enabled rails, wallet -> agent/budget map
python3 -m spend_collector pull-all --config spend.config.json
```

The `wallets` map is what turns chain addresses into useful agent spend:

```json
{
  "0xresearchwallet": {
    "agent_id": "research-bot",
    "budget_id": "team-research"
  }
}
```

For direct USDC only, use the small form:

```bash
python3 -m spend_collector pull-usdc \
  --pay-to 0xYourReceivingAddress \
  --wallet-map wallet-map.example.json \
  --db spend.db \
  --out-dir artifacts
```

Single-rail pulls still work:

```bash
# LLM token cost (admin/usage key, read-only)
export ANTHROPIC_ADMIN_KEY=sk-ant-admin01-...
python3 -m spend_collector pull --db spend.db --days 7 --out-dir artifacts

# OpenRouter generation metadata by generation id
export OPENROUTER_API_KEY=sk-or-...
python3 -m spend_collector pull-openrouter --generation-id gen_... --db spend.db --out-dir artifacts

# AWS cloud cost: Cost Explorer grouped by cost allocation tags
export AWS_ACCESS_KEY_ID=AKIA...
export AWS_SECRET_ACCESS_KEY=...
python3 -m spend_collector pull-aws --tag-agent agent_id --tag-budget budget_id --db spend.db --out-dir artifacts

# GCP cloud cost: BigQuery Cloud Billing export rows saved as JSON/NDJSON/CSV
python3 -m spend_collector pull-gcp-billing-file \
  --billing-export-file gcp-billing-export.ndjson \
  --label-agent agent_id \
  --label-budget budget_id \
  --db spend.db \
  --out-dir artifacts

# Azure cloud cost: Cost Management grouped by tags
export AZURE_COST_SCOPE=/subscriptions/00000000-0000-0000-0000-000000000000
export AZURE_ACCESS_TOKEN="$(az account get-access-token --resource https://management.azure.com/ --query accessToken -o tsv)"
python3 -m spend_collector pull-azure \
  --scope "$AZURE_COST_SCOPE" \
  --tag-agent agent_id \
  --tag-budget budget_id \
  --db spend.db \
  --out-dir artifacts

# x402 payments: USDC settlements into your merchant address on Base
python3 -m spend_collector pull-x402 --pay-to 0xYourReceivingAddress --wallet-map wallet-map.json

# direct USDC payments: Base USDC transfers into your receiving address
python3 -m spend_collector pull-usdc --pay-to 0xYourReceivingAddress --wallet-map wallet-map.json

# card payments: Stripe succeeded PaymentIntents (restricted read key)
export STRIPE_SECRET_KEY=rk_live_...
python3 -m spend_collector pull-stripe --db spend.db --days 7 --out-dir artifacts
```

All commands write to `spend.db`, run the detectors, and write `report.html`.
Budget caps can be supplied as a JSON object:

```bash
export SPEND_BUDGETS_FILE=budgets.json
python3 -m spend_collector pull-stripe
```

Example `budgets.json`:

```json
{
  "team-research": 10.0,
  "team-support": 8.0
}
```

For production scheduling, artifact retention, and incident handling, see
[`docs/OPERATIONS.md`](docs/OPERATIONS.md). A safe starting config is provided in
`spend.config.example.json`, `wallet-map.example.json`, `.env.example`, and
`budgets.example.json`.

Or do it all at once: **`scripts/dogfood.sh`** pulls whatever credentials are set
and opens the dashboard. After any pull, `python3 -m spend_collector report --db
spend.db --out-dir artifacts` re-renders the evidence page.

Attribution:

- LLM: one API key per agent.
- OpenRouter: gateway headers (`X-Agent-ID`, `X-Budget-ID`) for live calls; generation metadata `external_user` for post-hoc pulls.
- AWS: Cost Allocation Tags, default `agent_id` and `budget_id`.
- GCP: Cloud Billing export labels, default `agent_id` and `budget_id`.
- Azure: Cost Management tags, default `agent_id` and `budget_id`; use a Cost Management Reader-capable identity.
- x402: payer wallet.
- USDC: payer wallet by default; map wallet addresses to agents/budgets upstream.
- Stripe: `PaymentIntent.metadata.agent_id` and `metadata.budget_id`.

OpenAI can follow the Anthropic cost-report shape. OpenRouter is easiest through
the gateway because responses include usage and cost metadata.

## Providers and pricing

Agent spend is more than LLM tokens. `spend_collector/providers.py` is a curated
catalog across three categories (base URLs / unit costs are defaults — verify and
override per your account):

- **LLM** (forward + record token usage): `openai`, `anthropic`, `gemini`,
  `cohere`, `groq`, `together`, `fireworks`, `deepinfra`, `deepseek`, `xai`,
  `mistral`, `perplexity`, `openrouter`, `moonshot`, `dashscope`, `zhipu`,
  `ollama`, `vllm`.
- **Paid tools / data APIs** (forward via a `target`, per-call cost): `tavily`,
  `serper`, `exa`, `brave`, `firecrawl`, `scrapingbee`, `apify`, `elevenlabs`,
  `deepgram`, `replicate`, `fal`, `e2b`.
- **Payment rails** (captured by ingestion): `stripe`, `x402` (+ `skyfire`,
  `coinbase`).

Tool spend is recorded in real time as the target's flat per-call `amount` — enough
for budget caps and call-volume anomalies. Tools meter differently (audio seconds,
characters, pages), so treat that as an estimate; the authoritative cost is the
provider's billed charge, captured separately by `pull-stripe`. They are two views
of the same spend (real-time estimate on the `api` rail vs. billed truth on the
`card` rail) — reconcile by comparing them, not by summing; the delta is a signal.

**Naming a known LLM provider is enough** — the gateway fills its base URL and key
env from the catalog, so a policy entry can be just a budget and cap. Route at
`/<provider>/...`:

```json
"providers": { "mistral": {"service_from_body": "model", "amount": 0.25, "budget": "team"} }
```

Override `base_url`/`api_key_env` for anything custom (self-hosted, Azure, a
region). Recording auto-detects token usage across **OpenAI**
(`prompt_tokens`/`completion_tokens`), **Anthropic** (`input_tokens`/
`output_tokens`), **Gemini** (`usageMetadata`), and **Cohere** (`meta.billed_units`).

**Pricing:** install `tokencost` (`pip install spend-collector[pricing]`) for
accurate, maintained rates across 400+ models. Without it, a small built-in price
book covers common models and everything else prices at zero until added.

## What's inside

| File | Role |
|---|---|
| `schema.py` | FOCUS-shaped `SpendEvent` (one row shape for every rail) |
| `store.py` | Append-only, idempotent SQLite ledger + summaries |
| `adapters.py` | Normalizers: token usage / cloud cost / x402 settlements / USDC transfers / Stripe events -> ledger rows |
| `providers.py` | Curated provider catalog (LLM + tool APIs + payment rails) + usage-shape resolver |
| `sources.py` | Live read-only pulls: Anthropic/OpenAI cost APIs, OpenRouter generation metadata, AWS Cost Explorer, GCP Billing Export files, Azure Cost Management, Base USDC logs, Stripe Events API |
| `detectors.py` | Phase-0 anomaly signals: spend spikes, burn-rate, task cost, new keys, new merchants, off-hours activity |
| `gateway.py` | Pre-spend allow/deny decisions from policy + ledger history |
| `report.py` | Zero-dependency static HTML dashboard |

## The detection ceiling

Read-only detects, alerts, and keeps evidence. Each ledger row keeps provider
receipt metadata plus `x_source_event`, a stable hash of the raw source event used
for audit correlation without storing secrets in the dashboard. It cannot block a
payment. Stopping spend needs inline enforcement (LLM gateway / x402 middleware),
and the unbypassable backstop is on-chain caps (ERC-7715 / Coinbase Spend
Permissions). That's the roadmap:

```text
detect -> inline -> on-chain
```

## Roadmap

1. Done: closed loop on mock data (ingest -> ledger -> detect -> report).
2. Done: real Anthropic cost pull (`pull`).
3. Done: real x402 pull, on-chain USDC on Base (`pull-x402`).
4. Done: direct USDC stablecoin rail on Base (`pull-usdc`).
5. Done: AWS cloud cost rail via Cost Explorer (`pull-aws`).
6. Done: GCP cloud cost rail via Billing Export files (`pull-gcp-billing-file`).
7. Done: Azure cloud cost rail via Cost Management (`pull-azure`).
8. Done: Stripe Events rail, token + crypto + cloud + card in one ledger (`pull-stripe`).
9. Done: richer Phase-0 detectors (multi-window burn-rate, spend-per-task, new key, new merchant/provider).
10. Done: inline pre-spend gateway and LLM proxy (`guard`, `/guard`, provider routes).
11. Done: x402 seller-side middleware with `PAYMENT-REQUIRED`, facilitator verify/settle, and ledger recording.
12. Next: Grafana/Metabase on the DB; stronger request-bound replay protection for dynamic x402 pricing.

## Open source, and what's commercial

Everything in this repository is **MIT-licensed and free** — the read-only
cross-rail collector, pricing and detectors, the local dashboard, and the
self-hosted pre-spend gateway (policy, allow/deny, record, reserve/release,
velocity caps, multi-platform alerts, opt-in AI triage). Self-hosted, single-node,
single-tenant. Yours to run and extend.

What is **not** here — and is a separate commercial product — is the enterprise
control plane around it: SSO / RBAC / multi-tenancy, managed HA and a hosted data
layer, SOC 2 / immutable audit / approval workflows, SIEM & ITSM integrations, a
policy-management UI with simulation, and role-scoped chargeback reporting. The
open-source engine detects and enforces on one node; the control plane is what
makes it a governed platform for an org.

The line is deliberate: the collector should be trivially adoptable and never hold
your governance hostage, while the operational substrate an enterprise pays for
stays a product, not a feature.

## License & requirements

Requires Python 3.10+. No required dependencies; optional `tokencost`
(`pip install spend-collector[pricing]`) for accurate pricing across 400+ models,
otherwise a small built-in price book. License: MIT.
