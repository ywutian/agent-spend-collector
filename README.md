# agent-spend-collector

**See and govern every dollar your AI agents spend, across every rail.**

A free, read-only, cross-rail **agent spend collector**. It pulls what your agents
spend (LLM token cost + x402 payments + Stripe card payments today; USDC next),
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
ingest -> one ledger -> anomaly detectors -> report.html
```

Open `report.html` in a browser.

## Real data (read-only)

```bash
# LLM token cost (admin/usage key, read-only)
export ANTHROPIC_ADMIN_KEY=sk-ant-admin01-...
python3 -m spend_collector pull

# x402 payments: USDC settlements into your merchant address on Base
python3 -m spend_collector pull-x402 0xYourReceivingAddress

# card payments: Stripe succeeded PaymentIntents (restricted read key)
export STRIPE_SECRET_KEY=rk_live_...
python3 -m spend_collector pull-stripe
```

All commands write to `spend.db` and run the detectors. Attribution:

- LLM: one API key per agent.
- x402: payer wallet.
- Stripe: `PaymentIntent.metadata.agent_id` and `metadata.budget_id`.

OpenAI and OpenRouter can follow the Anthropic cost-report shape.

## What's inside

| File | Role |
|---|---|
| `schema.py` | FOCUS-shaped `SpendEvent` (one row shape for every rail) |
| `store.py` | Append-only, idempotent SQLite ledger + summaries |
| `adapters.py` | Normalizers: token usage / x402 settlements / Stripe events -> ledger rows |
| `sources.py` | Live read-only pulls: Anthropic cost API, Base USDC logs, Stripe Events API |
| `detectors.py` | Phase-0 anomaly signals: per-agent robust z-score, budget burn-rate |
| `report.py` | Zero-dependency static HTML dashboard |

## The detection ceiling

Read-only detects, alerts, and keeps evidence. It cannot block a payment. Stopping
spend needs inline enforcement (LLM gateway / x402 middleware), and the unbypassable
backstop is on-chain caps (ERC-7715 / Coinbase Spend Permissions). That's the
roadmap:

```text
detect -> inline -> on-chain
```

## Roadmap

1. Done: closed loop on mock data (ingest -> ledger -> detect -> report).
2. Done: real Anthropic cost pull (`pull`).
3. Done: real x402 pull, on-chain USDC on Base (`pull-x402`).
4. Done: Stripe Events rail, token + crypto + card in one ledger (`pull-stripe`).
5. Next: Grafana/Metabase on the DB; richer detectors (multi-window burn-rate, LLMjacking signals).
6. Later: inline enforcement (gateway/middleware), Phase 1.

Requires Python 3.10+. No dependencies. License: MIT.
