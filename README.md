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

### Quick Start

Run the full demo with no dependencies and no real keys:

```bash
python3 -m spend_collector demo
```

Then open `report.html`.

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

Fast path: copy the example config, fill in only the rails you use, then pull everything configured into the same ledger.

```bash
cp spend.config.example.json spend.config.json
# edit spend.config.json
python3 -m spend_collector pull-all --config spend.config.json
```

Common single-rail pulls:

```bash
# LLM cost: Anthropic or OpenAI
export ANTHROPIC_ADMIN_KEY=sk-ant-admin01-...
python3 -m spend_collector pull --provider anthropic --db spend.db --days 7 --out-dir artifacts

# OpenRouter generation metadata
export OPENROUTER_API_KEY=sk-or-...
python3 -m spend_collector pull-openrouter --generation-id gen_... --db spend.db --out-dir artifacts

# Stripe card payments
export STRIPE_SECRET_KEY=rk_live_...
python3 -m spend_collector pull-stripe --db spend.db --days 7 --out-dir artifacts

# Base USDC / x402
python3 -m spend_collector pull-usdc --pay-to 0xYourReceivingAddress --wallet-map wallet-map.example.json
python3 -m spend_collector pull-x402 --pay-to 0xYourReceivingAddress --wallet-map wallet-map.example.json
```

Cloud examples:

```bash
python3 -m spend_collector pull-aws --tag-agent agent_id --tag-budget budget_id
python3 -m spend_collector pull-gcp-billing-file --billing-export-file gcp-billing-export.ndjson
python3 -m spend_collector pull-azure --scope "$AZURE_COST_SCOPE"
```

Budget caps can be supplied with `SPEND_BUDGETS_FILE`:

```bash
export SPEND_BUDGETS_FILE=budgets.json
```

Example:

```json
{
  "team-research": 10.0,
  "team-support": 8.0
}
```

For scheduling, retention, webhooks, and incident handling, see [`docs/OPERATIONS.md`](docs/OPERATIONS.md).

### Pre-Spend Gateway

The collector can also run as a local gateway. Agents ask before spending; the gateway returns `allow` or `deny` from policy plus ledger history.

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

Run the HTTP gateway:

```bash
export SPEND_GATEWAY_TOKEN=dev-gateway-token
python3 -m spend_collector validate-policy --policy gateway.example.json
python3 -m spend_collector gateway --policy gateway.example.json --db spend.db
```

Call it before a spend:

```bash
curl -X POST http://127.0.0.1:8787/guard \
  -H "content-type: application/json" \
  -H "authorization: Bearer dev-gateway-token" \
  -d '{"agent":"research-bot","rail":"api_x402","provider":"x402","merchant":"0xtool","service":"/scrape","amount":3.5,"budget":"team-research"}'
```

The gateway can also:

- proxy allowlisted API calls with `/forward`
- proxy OpenAI-compatible provider routes after policy checks
- serve x402 resources at `/x402/<resource-id>`
- create and release short-lived spend reservations
- freeze or unfreeze agents and budgets as an incident kill-switch
- block on deterministic request-content rules or recent anomalies

See [`docs/OPERATIONS.md`](docs/OPERATIONS.md) and [`SECURITY.md`](SECURITY.md) before using the gateway in production.

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

### 快速开始

无需依赖、无需真实 key，直接跑完整 demo：

```bash
python3 -m spend_collector demo
```

然后打开 `report.html`。

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

最快方式：复制示例配置，只填你实际用到的数据来源，然后统一拉入同一个账本。

```bash
cp spend.config.example.json spend.config.json
# edit spend.config.json
python3 -m spend_collector pull-all --config spend.config.json
```

常见单来源拉取：

```bash
# LLM cost: Anthropic or OpenAI
export ANTHROPIC_ADMIN_KEY=sk-ant-admin01-...
python3 -m spend_collector pull --provider anthropic --db spend.db --days 7 --out-dir artifacts

# OpenRouter generation metadata
export OPENROUTER_API_KEY=sk-or-...
python3 -m spend_collector pull-openrouter --generation-id gen_... --db spend.db --out-dir artifacts

# Stripe card payments
export STRIPE_SECRET_KEY=rk_live_...
python3 -m spend_collector pull-stripe --db spend.db --days 7 --out-dir artifacts

# Base USDC / x402
python3 -m spend_collector pull-usdc --pay-to 0xYourReceivingAddress --wallet-map wallet-map.example.json
python3 -m spend_collector pull-x402 --pay-to 0xYourReceivingAddress --wallet-map wallet-map.example.json
```

云账单示例：

```bash
python3 -m spend_collector pull-aws --tag-agent agent_id --tag-budget budget_id
python3 -m spend_collector pull-gcp-billing-file --billing-export-file gcp-billing-export.ndjson
python3 -m spend_collector pull-azure --scope "$AZURE_COST_SCOPE"
```

预算可以通过 `SPEND_BUDGETS_FILE` 提供：

```bash
export SPEND_BUDGETS_FILE=budgets.json
```

示例：

```json
{
  "team-research": 10.0,
  "team-support": 8.0
}
```

生产调度、产物保留、webhook 告警和事件处理请看 [`docs/OPERATIONS.md`](docs/OPERATIONS.md)。

### 花钱前拦截网关

这个项目也可以作为本地网关运行。Agent 在花钱前先问网关，网关根据策略和历史账本返回 `allow` 或 `deny`。

单次决策示例：

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

启动 HTTP 网关：

```bash
export SPEND_GATEWAY_TOKEN=dev-gateway-token
python3 -m spend_collector validate-policy --policy gateway.example.json
python3 -m spend_collector gateway --policy gateway.example.json --db spend.db
```

花钱前调用：

```bash
curl -X POST http://127.0.0.1:8787/guard \
  -H "content-type: application/json" \
  -H "authorization: Bearer dev-gateway-token" \
  -d '{"agent":"research-bot","rail":"api_x402","provider":"x402","merchant":"0xtool","service":"/scrape","amount":3.5,"budget":"team-research"}'
```

网关还可以：

- 通过 `/forward` 代理 allowlist 里的 API 请求
- 在策略检查后代理 OpenAI-compatible provider routes
- 通过 `/x402/<resource-id>` 提供 x402 资源
- 创建和释放短期花费预留
- 冻结或解冻 agent / budget，作为事故 kill-switch
- 根据确定性的请求内容规则或近期异常进行阻断

生产使用网关前，请先看 [`docs/OPERATIONS.md`](docs/OPERATIONS.md) 和 [`SECURITY.md`](SECURITY.md)。

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
