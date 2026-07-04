# Security

`agent-spend-collector` is designed to be self-hosted by default. Provider keys
stay in the user's machine, server, VPC, or secret manager; the project does not
depend on a hosted control plane.

## Trust Model

- Run the gateway in your own environment.
- Store real provider keys in environment variables or a secret manager.
- Put only environment variable names in policy files, for example
  `api_key_env: "OPENAI_API_KEY"`.
- Give agents gateway tokens, not provider keys.
- The gateway swaps a gateway token for the provider key only after an allow
  decision.
- The gateway does not send data to project-owned servers.
- Audit logs store metadata only: agent, rail, provider, amount, budget,
  decision, and reasons.
- Audit logs do not store prompts, request bodies, completions, responses,
  provider keys, gateway tokens, or x402 payment signatures.
- Configured `/x402/<resource-id>` routes settle already-signed payment payloads
  through your facilitator. Ledger rows store settlement metadata such as payer,
  transaction, amount, and resource, not the signed payment payload.

## Recommended Deployment

Use least-privilege credentials where providers support them:

- Restricted Stripe keys.
- Limited-scope LLM admin/read keys for cost pulls.
- Gateway-held provider keys for inline model/API calls.
- Limited wallet permissions or spend-limited wallets for payment rails.

For production, set `SPEND_GATEWAY_TOKEN` or `gateway_tokens` in policy whenever
`providers` or `targets` are configured. The gateway refuses to start forwarding
routes without gateway authentication.

## Reporting Vulnerabilities

Open a private security advisory or contact the maintainer directly before
publishing vulnerabilities. Do not include real provider keys, wallet material,
prompts, customer data, or raw provider responses in reports.
