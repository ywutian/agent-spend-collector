# agent-spend-collector

[![CI](https://github.com/ywutian/agent-spend-collector/actions/workflows/ci.yml/badge.svg)](https://github.com/ywutian/agent-spend-collector/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)

[![English](https://img.shields.io/badge/README-English-blue)](#english)
[![中文](https://img.shields.io/badge/README-%E4%B8%AD%E6%96%87-red)](#中文)

## English

**A read-only spend ledger and safety layer for AI agents.**

`agent-spend-collector` collects LLM token cost, x402 payments, direct USDC transfers, AWS/GCP/Azure cloud cost, and Stripe card payments into one FOCUS-shaped ledger. It then flags runaway loops, spend spikes, budget burn, new keys, and new merchants.

Collection is read-only. It does not move your money, and provider keys, wallet keys, prompts, request bodies, and completions should stay outside the ledger and dashboard.

### Which path should I take?

| I want to... | Command |
|---|---|
| Just see what it does | `spend-collector demo` |
| Track my real spend | `spend-collector init` → `spend-collector pull-all` |
| Block spend before it happens | `spend-collector gateway` |

> On macOS/Linux use `python3 -m spend_collector ...`; on Windows use `python -m spend_collector ...`. After `pip install .` the `spend-collector` command shown above works everywhere.

### Quick Start

Run the full demo with no dependencies and no real keys:

```bash
python3 -m spend_collector demo
```

It builds the ledger, runs the detectors, and opens `report.html` in your browser automatically. Add `--no-open` to skip that (handy in CI).

The demo path is:

```text
fixtures -> one SQLite ledger -> anomaly detectors -> report.html
```

Expected outputs:

| File | Meaning |
|---|---|
| `report.html` | Static dashboard |
| `alerts.json` | Machine-readable alerts |
| `run-summary.json` | Spend and alert summary |

To write outputs to a folder:

```bash
python3 -m spend_collector demo --out-dir artifacts
```

On Windows:

```powershell
python -m spend_collector demo
start report.html
```

### Why It Exists

Agents now spend through many rails: model tokens, paid APIs, stablecoins, cloud resources, and cards. Most tools only see one slice. This project gives you one neutral book of record and treats unusual spend as a security signal.

It helps answer:

- Which agent spent the money?
- Which rail or provider did it use?
- Which budget did it burn?
- Is this normal for that agent?
- Should the next request be blocked before it spends?

### What It Collects

| Rail | Examples |
|---|---|
| LLM token cost | Anthropic, OpenAI, OpenRouter, gateway-recorded usage |
| Paid API | x402, allowlisted gateway targets |
| Stablecoin | Base USDC transfers |
| Cloud | AWS Cost Explorer, GCP Billing Export files, Azure Cost Management |
| Card | Stripe succeeded PaymentIntents |

Each source is normalized into a single `SpendEvent` shape and stored in SQLite. Rows are idempotent, append-only, and include evidence references for audit correlation.

### What It Detects

- `spend_spike`: sudden agent spend spike
- `spend_per_task`: one task is too expensive
- `budget_burn`: budget crossed
- `budget_burn_rate`: budget is burning too fast
- `new_key_spike`: new key appears with high spend
- `new_merchant_provider`: new merchant or provider
- off-hours activity

The demo fixtures intentionally trigger several alerts so you can see the product behavior immediately.

### Real Data

Use one config file for normal runs. Enable only the rails you use, keep secrets in environment variables, then run `pull-all`.

Run `init` to scaffold the config and check the environment variables it needs:

```bash
python3 -m spend_collector init
```

`init` writes `spend.config.json` from the template (it will not overwrite an existing one unless you pass `--force`) and prints an `[ok]` / `[missing]` checklist for every enabled rail, so you know exactly which keys to export before `pull-all`.

Example `spend.config.json`:

```json
{
  "db": "spend.db",
  "out_dir": "artifacts",
  "days": 7,
  "budgets": {
    "team-research": 50.0,
    "team-support": 25.0,
    "default": 100.0
  },
  "wallets": {
    "0xresearchwallet": {
      "agent_id": "research-bot",
      "budget_id": "team-research"
    }
  },
  "rails": {
    "llm": {
      "enabled": true,
      "provider": "anthropic",
      "api_key_env": "ANTHROPIC_ADMIN_KEY"
    },
    "stripe": {
      "enabled": true,
      "api_key_env": "STRIPE_SECRET_KEY"
    },
    "x402": {
      "enabled": true,
      "pay_to": "0xYourX402ReceivingAddress"
    },
    "usdc": {
      "enabled": true,
      "pay_to": "0xYourUSDCReceivingAddress"
    },
    "aws": {
      "enabled": false,
      "tag_agent": "agent_id",
      "tag_budget": "budget_id"
    }
  }
}
```

Set the environment variables named in the config:

```bash
export ANTHROPIC_ADMIN_KEY=sk-ant-admin01-...
export STRIPE_SECRET_KEY=rk_live_...
```

Edit checklist:

| Field | What to put there |
|---|---|
| `budgets` | Budget caps by team, agent group, or environment |
| `wallets` | Wallet address -> `agent_id` and `budget_id` mapping |
| `rails.*.enabled` | `true` for sources you want `pull-all` to ingest |
| `rails.*.api_key_env` | Environment variable name that holds the provider key |
| `rails.x402.pay_to` / `rails.usdc.pay_to` | Receiving address to scan on Base |

Pull every enabled rail and render the dashboard:

```bash
python3 -m spend_collector pull-all --config spend.config.json
```

That writes to the configured `db`, runs detectors, and creates `report.html`, `alerts.json`, and `run-summary.json` in the configured `out_dir`.

Single-rail commands still exist for debugging one source at a time:

```bash
python3 -m spend_collector pull --provider anthropic
python3 -m spend_collector pull-stripe
python3 -m spend_collector pull-usdc --pay-to 0xYourReceivingAddress
python3 -m spend_collector pull-x402 --pay-to 0xYourReceivingAddress
python3 -m spend_collector pull-aws
python3 -m spend_collector pull-gcp-billing-file --billing-export-file gcp-billing-export.ndjson
python3 -m spend_collector pull-azure --scope "$AZURE_COST_SCOPE"
```

For OpenRouter, add a rail with `provider: "openrouter"` and `generation_ids` or `generation_ids_file`. For cloud spend, enable the cloud rail and set the provider credentials in env vars. For scheduling, retention, webhooks, and incident handling, see [`docs/OPERATIONS.md`](docs/OPERATIONS.md).

### Pre-Spend Gateway

The gateway is the inline control layer. Use it when an agent can ask before it spends, or when you can route the agent's API calls through a local proxy.

Minimal policy, saved as `gateway.example.json`:

```json
{
  "gateway_tokens": ["dev-gateway-token"],
  "budgets": {
    "team-research": 10.0
  },
  "max_amount": 3.0,
  "agents": {
    "research-bot": {
      "budgets": ["team-research"],
      "rails": ["llm_token", "api_x402"],
      "max_amount": 2.0
    }
  },
  "targets": {
    "scraper-demo": {
      "url": "https://example.com/scrape",
      "method": "POST",
      "rail": "api_x402",
      "provider": "x402",
      "merchant": "0xtool",
      "service": "/scrape",
      "amount": 1.5,
      "budget": "team-research"
    }
  }
}
```

Start it:

```bash
export SPEND_GATEWAY_TOKEN=dev-gateway-token
python3 -m spend_collector validate-policy --policy gateway.example.json
python3 -m spend_collector gateway --policy gateway.example.json --db spend.db
```

Example 1: ask for a decision before spending.

```bash
curl -X POST http://127.0.0.1:8787/guard \
  -H "content-type: application/json" \
  -H "authorization: Bearer dev-gateway-token" \
  -d '{"agent":"research-bot","rail":"api_x402","provider":"x402","merchant":"0xtool","service":"/scrape","amount":1.5,"budget":"team-research"}'
```

Allowed response:

```json
{
  "decision": "allow",
  "allowed": true,
  "reasons": []
}
```

If the amount, rail, agent, or budget violates policy, the gateway returns `deny` and the caller should stop the spend.

Example 2: let the gateway proxy an allowlisted paid API.

```bash
curl -X POST http://127.0.0.1:8787/forward \
  -H "content-type: application/json" \
  -H "authorization: Bearer dev-gateway-token" \
  -d '{"agent":"research-bot","target":"scraper-demo","body":{"query":"pricing data"}}'
```

The gateway checks the same policy first. If denied, it does not call the upstream URL. If allowed, it forwards the body and records the estimated spend.

Example 3: put an OpenAI-compatible provider behind the gateway.

```json
{
  "providers": {
    "openai": {
      "api_key_env": "OPENAI_API_KEY",
      "budget": "team-research",
      "amount": 0.25
    }
  }
}
```

Then point the agent SDK at:

```text
base_url = http://127.0.0.1:8787/openai/v1
api_key = dev-gateway-token
headers = {"X-Agent-ID": "research-bot", "X-Budget-ID": "team-research"}
```

The real provider key stays on the gateway. The agent only gets the gateway token.

For x402 seller-side middleware, freeze/unfreeze, content guards, anomaly-based blocking, and production safety notes, see [`docs/OPERATIONS.md`](docs/OPERATIONS.md) and [`SECURITY.md`](SECURITY.md).

### Dashboard

Every ingest command writes a local static dashboard:

```bash
python3 -m spend_collector report --db spend.db --out-dir artifacts
```

The dashboard shows total spend, rail mix, alert counts, budget burn, agent-by-rail totals, recent ledger events, and short evidence hashes. It uses no server and no external assets.

### Project Map

| Path | Role |
|---|---|
| `spend_collector/schema.py` | `SpendEvent` data shape |
| `spend_collector/store.py` | SQLite ledger |
| `spend_collector/adapters.py` | Source normalizers |
| `spend_collector/sources.py` | Read-only provider pulls |
| `spend_collector/detectors.py` | Anomaly detectors |
| `spend_collector/gateway.py` | Policy decisions |
| `spend_collector/report.py` | Static dashboard |
| `spend_collector/providers.py` | Provider catalog and pricing helpers |
| `fixtures/` | Demo data |
| `docs/OPERATIONS.md` | Production runbook |

### Local Verification

```bash
python3 -m unittest discover -s tests
python3 -m compileall spend_collector tests
python3 -m spend_collector demo
```

The project has no required third-party dependencies. Optional pricing support:

```bash
pip install ".[pricing]"
```

### Open Source Boundary

This repository is MIT-licensed and includes the collector, SQLite ledger, detectors, dashboard, pricing helpers, and self-hosted gateway.

What is not included: a hosted enterprise control plane with SSO/RBAC, multi-tenancy, managed HA, SOC 2 controls, approval workflows, SIEM/ITSM integrations, policy UI, and role-scoped chargeback.

### Requirements and License

- Python 3.10+
- No required dependencies
- Optional: `tokencost` via `pip install ".[pricing]"`
- License: MIT

## 中文

**给 AI Agent 用的只读花费账本和安全控制层。**

`agent-spend-collector` 会把 LLM token 成本、x402 支付、USDC 转账、AWS/GCP/Azure 云账单和 Stripe 卡支付统一写进一张 FOCUS 形态的账本，然后检测循环失控、费用暴涨、预算烧穿、新 key 异常、新商户等风险。

采集阶段是只读的，不会动你的钱。provider key、钱包私钥、prompt、请求体和模型输出都不应该进入账本或 dashboard。

### 我该走哪条路？

| 我想… | 命令 |
|---|---|
| 先看看效果 | `spend-collector demo` |
| 接入真实花费 | `spend-collector init` → `spend-collector pull-all` |
| 花钱前拦截 | `spend-collector gateway` |

> macOS/Linux 用 `python3 -m spend_collector ...`，Windows 用 `python -m spend_collector ...`。执行 `pip install .` 后，上表里的 `spend-collector` 命令在所有平台都能直接用。

### 快速开始

无需依赖、无需真实 key，直接跑完整 demo：

```bash
python3 -m spend_collector demo
```

它会建好账本、跑完检测器，并自动在浏览器里打开 `report.html`。加 `--no-open` 可以跳过自动打开（CI 里更合适）。

demo 会完成这条链路：

```text
fixtures -> one SQLite ledger -> anomaly detectors -> report.html
```

输出文件：

| 文件 | 作用 |
|---|---|
| `report.html` | 静态看板 |
| `alerts.json` | 机器可读的告警 |
| `run-summary.json` | 花费和告警汇总 |

如果要把产物写到目录里：

```bash
python3 -m spend_collector demo --out-dir artifacts
```

Windows 下可以运行：

```powershell
python -m spend_collector demo
start report.html
```

### 为什么需要它

现在 Agent 的花费可能来自很多地方：模型 token、付费 API、稳定币、云资源、银行卡。大多数工具只能看到其中一部分。这个项目的目标是给你一张中立的总账，并把异常花费当成安全信号来看。

它可以回答这些问题：

- 哪个 Agent 花的钱？
- 走的是哪条支付或账单通道？
- 消耗的是哪个预算？
- 这对该 Agent 来说是否异常？
- 下一次请求是否应该在花钱前被拦住？

### 支持的数据来源

| 通道 | 示例 |
|---|---|
| LLM token 成本 | Anthropic, OpenAI, OpenRouter, gateway 记录的 usage |
| 付费 API | x402, 网关 allowlist 里的目标服务 |
| 稳定币 | Base USDC transfers |
| 云账单 | AWS Cost Explorer, GCP Billing Export files, Azure Cost Management |
| 卡支付 | Stripe succeeded PaymentIntents |

所有来源都会被归一化成同一种 `SpendEvent`，写入 SQLite。账本是幂等追加的，并保留审计用的证据引用。

### 会检测什么

- `spend_spike`：单个 Agent 花费突增
- `spend_per_task`：单次任务成本异常
- `budget_burn`：预算被烧穿
- `budget_burn_rate`：预算消耗速度过快
- `new_key_spike`：新 key 首次出现就高消费
- `new_merchant_provider`：新商户或新 provider
- 非工作时间活动

demo 数据会故意触发多个告警，方便你一跑就看到效果。

### 接入真实数据

正常使用时建议只走一个配置文件。你只需要启用自己用到的数据来源，把密钥放在环境变量里，然后运行 `pull-all`。

用 `init` 生成配置，并检查它需要的环境变量：

```bash
python3 -m spend_collector init
```

`init` 会从模板生成 `spend.config.json`（已存在时不会覆盖，除非加 `--force`），并为每个已启用的 rail 打印 `[ok]` / `[missing]` 清单，让你清楚 `pull-all` 之前还需要 export 哪些 key。

示例 `spend.config.json`：

```json
{
  "db": "spend.db",
  "out_dir": "artifacts",
  "days": 7,
  "budgets": {
    "team-research": 50.0,
    "team-support": 25.0,
    "default": 100.0
  },
  "wallets": {
    "0xresearchwallet": {
      "agent_id": "research-bot",
      "budget_id": "team-research"
    }
  },
  "rails": {
    "llm": {
      "enabled": true,
      "provider": "anthropic",
      "api_key_env": "ANTHROPIC_ADMIN_KEY"
    },
    "stripe": {
      "enabled": true,
      "api_key_env": "STRIPE_SECRET_KEY"
    },
    "x402": {
      "enabled": true,
      "pay_to": "0xYourX402ReceivingAddress"
    },
    "usdc": {
      "enabled": true,
      "pay_to": "0xYourUSDCReceivingAddress"
    },
    "aws": {
      "enabled": false,
      "tag_agent": "agent_id",
      "tag_budget": "budget_id"
    }
  }
}
```

设置配置里写到的环境变量：

```bash
export ANTHROPIC_ADMIN_KEY=sk-ant-admin01-...
export STRIPE_SECRET_KEY=rk_live_...
```

编辑时主要看这几个字段：

| 字段 | 填什么 |
|---|---|
| `budgets` | 按团队、Agent 组或环境设置预算上限 |
| `wallets` | 钱包地址到 `agent_id` 和 `budget_id` 的映射 |
| `rails.*.enabled` | 需要 `pull-all` 拉取的来源设为 `true` |
| `rails.*.api_key_env` | 存放 provider key 的环境变量名 |
| `rails.x402.pay_to` / `rails.usdc.pay_to` | 需要扫描的 Base 收款地址 |

一条命令拉取所有已启用的数据来源，并生成看板：

```bash
python3 -m spend_collector pull-all --config spend.config.json
```

它会写入配置里的 `db`，运行检测器，并在配置里的 `out_dir` 生成 `report.html`、`alerts.json` 和 `run-summary.json`。

单源命令仍然保留，但更适合用来调试某一个来源：

```bash
python3 -m spend_collector pull --provider anthropic
python3 -m spend_collector pull-stripe
python3 -m spend_collector pull-usdc --pay-to 0xYourReceivingAddress
python3 -m spend_collector pull-x402 --pay-to 0xYourReceivingAddress
python3 -m spend_collector pull-aws
python3 -m spend_collector pull-gcp-billing-file --billing-export-file gcp-billing-export.ndjson
python3 -m spend_collector pull-azure --scope "$AZURE_COST_SCOPE"
```

OpenRouter 可以在配置里把 `provider` 设成 `"openrouter"`，并配置 `generation_ids` 或 `generation_ids_file`。云账单同理，启用对应 rail，再通过环境变量提供云厂商凭据。生产调度、产物保留、webhook 告警和事件处理请看 [`docs/OPERATIONS.md`](docs/OPERATIONS.md)。

### 花钱前拦截网关

网关是“花钱前”的控制层。适合两种场景：Agent 能在花钱前先问一下，或者你能把 Agent 的 API 请求先路由到本地网关。

最小策略示例，保存为 `gateway.example.json`：

```json
{
  "gateway_tokens": ["dev-gateway-token"],
  "budgets": {
    "team-research": 10.0
  },
  "max_amount": 3.0,
  "agents": {
    "research-bot": {
      "budgets": ["team-research"],
      "rails": ["llm_token", "api_x402"],
      "max_amount": 2.0
    }
  },
  "targets": {
    "scraper-demo": {
      "url": "https://example.com/scrape",
      "method": "POST",
      "rail": "api_x402",
      "provider": "x402",
      "merchant": "0xtool",
      "service": "/scrape",
      "amount": 1.5,
      "budget": "team-research"
    }
  }
}
```

启动网关：

```bash
export SPEND_GATEWAY_TOKEN=dev-gateway-token
python3 -m spend_collector validate-policy --policy gateway.example.json
python3 -m spend_collector gateway --policy gateway.example.json --db spend.db
```

示例 1：花钱前先问能不能花。

```bash
curl -X POST http://127.0.0.1:8787/guard \
  -H "content-type: application/json" \
  -H "authorization: Bearer dev-gateway-token" \
  -d '{"agent":"research-bot","rail":"api_x402","provider":"x402","merchant":"0xtool","service":"/scrape","amount":1.5,"budget":"team-research"}'
```

允许时返回类似：

```json
{
  "decision": "allow",
  "allowed": true,
  "reasons": []
}
```

如果金额、通道、Agent 或预算不符合策略，网关会返回 `deny`，调用方应该停止这次花费。

示例 2：让网关代理一个 allowlist 里的付费 API。

```bash
curl -X POST http://127.0.0.1:8787/forward \
  -H "content-type: application/json" \
  -H "authorization: Bearer dev-gateway-token" \
  -d '{"agent":"research-bot","target":"scraper-demo","body":{"query":"pricing data"}}'
```

网关会先跑同一套策略。拒绝时不会请求上游 URL；允许时才转发 body，并记录这次预估花费。

示例 3：把 OpenAI-compatible provider 放到网关后面。

```json
{
  "providers": {
    "openai": {
      "api_key_env": "OPENAI_API_KEY",
      "budget": "team-research",
      "amount": 0.25
    }
  }
}
```

然后把 Agent SDK 指到：

```text
base_url = http://127.0.0.1:8787/openai/v1
api_key = dev-gateway-token
headers = {"X-Agent-ID": "research-bot", "X-Budget-ID": "team-research"}
```

真实 provider key 只放在网关进程里，Agent 只拿 gateway token。

x402 seller-side middleware、freeze/unfreeze、content guard、根据近期异常阻断和生产安全说明请看 [`docs/OPERATIONS.md`](docs/OPERATIONS.md) 和 [`SECURITY.md`](SECURITY.md)。

### 看板

每次采集都会生成本地静态看板：

```bash
python3 -m spend_collector report --db spend.db --out-dir artifacts
```

看板展示总花费、rail 分布、告警数量、预算消耗、Agent x rail 汇总、近期账本事件和短证据哈希。它不需要服务器，也不加载外部资源。

### 项目结构

| 路径 | 作用 |
|---|---|
| `spend_collector/schema.py` | `SpendEvent` 数据结构 |
| `spend_collector/store.py` | SQLite 账本 |
| `spend_collector/adapters.py` | 数据归一化 |
| `spend_collector/sources.py` | 只读 provider 拉取 |
| `spend_collector/detectors.py` | 异常检测 |
| `spend_collector/gateway.py` | 网关策略决策 |
| `spend_collector/report.py` | 静态看板 |
| `spend_collector/providers.py` | provider 目录与价格辅助 |
| `fixtures/` | demo 数据 |
| `docs/OPERATIONS.md` | 生产运行手册 |

### 本地验证

```bash
python3 -m unittest discover -s tests
python3 -m compileall spend_collector tests
python3 -m spend_collector demo
```

项目默认不需要第三方依赖。可选安装更完整的模型价格库：

```bash
pip install ".[pricing]"
```

### 开源边界

本仓库使用 MIT 协议，包含采集器、SQLite 账本、检测器、看板、价格辅助逻辑和自托管网关。

这里不包含托管企业控制台，例如 SSO/RBAC、多租户、高可用托管、SOC 2、审批流、SIEM/ITSM 集成、策略 UI、按角色分摊账单等。

### 要求与协议

- Python 3.10+
- 默认无必需依赖
- 可选：通过 `pip install ".[pricing]"` 安装 `tokencost`
- License: MIT
