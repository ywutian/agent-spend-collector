# Changelog

Notable changes to this project. Loosely follows Keep a Changelog; versioning is
[SemVer](https://semver.org).

## [0.1.0] — 2026-07-04

First tagged release: a read-only, cross-rail agent-spend collector with a
self-hosted pre-spend gateway.

### Rails & ingestion
- LLM token cost, x402 settlements, direct Base USDC transfers, AWS / GCP / Azure
  cloud cost, and Stripe card payments — normalized into one FOCUS-shaped ledger.
- Usage recording across OpenAI, Anthropic, Gemini, and Cohere response shapes.
- Pricing via optional `tokencost` (400+ models) with a built-in fallback book.
- Provider catalog (`providers.py`): LLM, tool APIs, and payment rails; naming a
  known LLM provider lets the gateway fill its base URL and key.

### Gateway
- Pre-spend allow/deny from policy + ledger history; forwards, records actual
  spend, and releases the reservation — for LLM and non-LLM tool calls.
- Budget caps, per-rail / per-amount limits, new-merchant rules, and a race-safe
  hourly **velocity cap** (`max_amount_per_hour`).
- **Kill-switch** (`freeze` / `unfreeze`) and **behavioral blocking**
  (`block_on_anomaly`): deny a call while its agent is frozen or currently flagged
  by a detector.
- **Content guard** (`content_guard`): reject oversized payloads, deny patterns,
  and outbound secrets before spending — a deterministic Layer-1 signal.
- **x402 seller-side middleware**: serve `/x402/<resource-id>`, answer with HTTP
  402 + payment requirements, verify/settle via a facilitator, then forward.
- Live token-gated `/dashboard` that auto-refreshes.

### Detection & alerting
- Phase-0 detectors: spend spikes, multi-window burn-rate, spend-per-task, new
  key, new merchant/provider, and off-hours activity.
- Alert delivery to Slack / Discord / Feishu / Teams / generic webhooks, with
  opt-in AI triage (likely cause + recommended action) that keeps detection
  deterministic.

### Foundations
- Append-only idempotent SQLite ledger, per-row evidence hashes, a static HTML
  dashboard, and machine-readable `alerts.json` / `run-summary.json`.
- Stdlib-only; Python 3.10+; MIT.

[0.1.0]: https://github.com/ywutian/agent-spend-collector/releases/tag/v0.1.0
