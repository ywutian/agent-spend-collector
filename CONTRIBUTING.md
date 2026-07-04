# Contributing

Thanks for helping build agent-spend-collector.

## Principles

- **Stdlib-only core.** No required runtime dependencies. Optional extras (e.g.
  `tokencost` for pricing) must degrade gracefully when absent.
- **Read-only by default.** The collector observes spend; it never moves money.
  The gateway is the only inline control, and it stores metadata only — never
  prompts, responses, request bodies, provider keys, or gateway tokens.
- **Lazy is good.** Prefer the smallest change that works; reach for the stdlib
  and native features before new code. Non-trivial logic ships with one runnable
  check.

## Develop

```bash
python3 -m unittest discover -s tests        # tests
python3 -m compileall spend_collector tests  # compile
python3 -m spend_collector demo              # product self-check
```

CI runs the same across Python 3.10–3.12.

## Adding a provider or rail

- **OpenAI-compatible LLM providers** usually need only a catalog entry in
  `providers.py` (and a price if it's a new model). Non-OpenAI response shapes go
  in `usage_tokens()`.
- **New payment / cost rails**: add a `from_*` adapter in `adapters.py` and a
  `pull-*` source in `sources.py`, mapping to the FOCUS `SpendEvent` shape.

## Pull requests

- Keep the diff focused — one concern per PR.
- Add or update a test for any behavior change (`tests/test_collector.py`).
- Update `README.md` / `docs/OPERATIONS.md` when you change the interface.
- No secrets in code, fixtures, or policy files — use `*_env` references.

By contributing you agree your work is licensed under the repository's MIT license.
