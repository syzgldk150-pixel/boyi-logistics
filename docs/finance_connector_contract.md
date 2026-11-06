---
module: 财务模块
type: 接口契约
tags: [连接器, 标准流水契约, finance.source_batch, file.read, 插件, 总账]
related: [../agent/docs/finance_module.md, plugin-platform-v2.md, ../agent/docs/common/finance_data_baseline.md]
status: planned
updated: 2026-11-07
---

# 财务连接器开发说明：标准流水契约 v1（草案）

> 状态：**草案**。本契约随总账重建 P1 先行发布，供连接器提前开发；Host 侧接收、校验与入账在 P4 实现（迁移 070、通用适配器 `finance.source_batch.submit`、受控 `file.read` 能力）。P4 完成前提交会返回 `CAPABILITY_UNAVAILABLE`。字段名与校验语义按本文冻结；如需调整，会先改本文并升级 `contract` 版本号。

## 1. 原则

- 连接器只负责**取数和解析**，不懂会计。科目、凭证、往来核销全部由 Host 的记账规则决定。
- 连接器不能写库、不能访问外网（在线抓取只经插件平台账号 `http.request` 能力），只能提交批次。
- Host 只信任契约：结构、金额、去重、证据和你声明的完整性校验全部通过，批次才会被接受；被接受的批次与行不可变。
- 金额一律是**十进制字符串**，最多 4 位小数，禁止浮点数；缺失就是缺失，不得补 0。

## 2. 两种进入方式

| 方式 | 适用 | 证据 |
|---|---|---|
| 在线抓取 | 平台有可用接口或页面（融辉、韵达、银行网银等） | Host 核对 Broker 的 `http.request` 请求观测，`evidence.kind = "http_observation"` |
| 文件导入 | 微信、支付宝、银行导出的账单文件 | 用户在财务页上传文件并选择来源，文件不可变保存并计算哈希；Host 发起该来源连接器的调用并授予 `file.read`（只读、只能读这一个文件、有大小上限、调用结束失效）。`evidence.kind = "file"` |

一个连接器可以同时支持两种方式；同一来源两种方式提交的行用同一套 `source_key` 规则。

## 3. 批次格式

```jsonc
{
  "contract": "finance.source_batch/v1",
  "source_id": "src_…",                    // module_data_sources 中 module='finance' 的来源
  "period": {"start": "2026-10-01", "end": "2026-10-31"},
  "declares": {
    "balance_chain": true,                 // 每行带交易前/后余额，且可排成连续余额链
    "summary": false,                      // 附带平台自身的分项汇总
    "opening_closing": true,               // 附带期初、期末余额
    "complete_period": true                // 保证 period 内的流水是全量
  },
  "opening_balance": "1000.0000",          // declares.opening_closing=true 时必填
  "closing_balance": "987.5000",
  "summary": [                             // declares.summary=true 时必填
    {"item_l1": "运费", "item_l2": "", "direction": "out", "count": 3, "amount": "12.5000"}
  ],
  "rows": [{
    "source_key": "2026100314220500012",   // 来源内稳定唯一（平台流水号最佳）
    "business_date": "2026-10-03",         // 业务日期，必须在 period 内
    "posted_at": "2026-10-03T14:22:05+08:00",
    "amount": "-12.5000",                  // 带符号：+ 流入本资金账户，− 流出
    "item_l1": "运费",                     // 平台原始一级项目（原文，不翻译）
    "item_l2": "",                         // 平台原始二级项目
    "counterparty": "…",                   // 对方户名/网点（原文）
    "waybill_no": "…",                     // 能识别时填写，用于按运单核销
    "reference": "…",                      // 平台单据号/备注
    "balance_before": "1000.0000",         // declares.balance_chain=true 时必填
    "balance_after": "987.5000",
    "raw": {}                              // 平台原始字段，原样保存供追溯；不参与校验
  }],
  "evidence": {"kind": "http_observation", "refs": ["…"]}
}
```

字段约束：

- `source_key`：同一来源内永久唯一且稳定。重抓同一笔必须得到同一个键；没有平台流水号时，用能唯一确定该笔的原始字段拼接后哈希，不要用行号。
- 日期 `YYYY-MM-DD`，时间带时区。
- `rows` 可以为空（该期间确实无流水）；空批次同样要满足声明的校验。
- 单批最多 50,000 行；超过请按期间拆分。

## 4. Host 校验（按声明执行）

| 校验 | 何时执行 | 失败码 |
|---|---|---|
| 结构、类型、金额格式；日期在期间内 | 总是 | `INTAKE_SCHEMA` / `AMOUNT_INVALID` |
| `source_key` 唯一；同键不同内容视为冲突 | 总是 | `INTAKE_DUPLICATE` / `INTAKE_CONFLICT` |
| 来源已启用；插件实例是该来源当前生产者；账号角色一致 | 总是 | `INTAKE_SOURCE` |
| 证据与 Broker 观测一致，或与上传文件哈希一致 | 总是 | `INTAKE_EVIDENCE` |
| 前余额 + 金额 = 后余额；排序后余额链连续 | `balance_chain` | `INTAKE_BALANCE_CHAIN` |
| 明细按项目合计 = 汇总 | `summary` | `INTAKE_SUMMARY` |
| 期初 + Σ金额 = 期末 | `opening_closing` | `INTAKE_OPENING_CLOSING` |

声明为 `false` 的校验不执行，来源卡片会明确标注“未校验余额链”等。能声明的尽量声明：`complete_period` 是封账前检查的必要条件，`opening_closing` 用于资金账户对账。

## 5. 接受之后

- 批次状态 `accepted / rejected / superseded`，记录声明、校验结果、合计、哈希、插件版本与调用 ID 或文件 ID。
- **重提**：同一来源同一期间再次提交时，新旧批次逐 `source_key` 比较。开放期间：差异行自动冲销旧凭证并重记；已封账期间：不改账，记 `CLOSED_PERIOD_DRIFT` 异常并飞书提醒。
- **入账**：每个来源在财务设置里配置一个资金科目（如 `101203 融辉主发货`）。流水按“来源流水记账规则”（精确匹配 `item_l1/item_l2`、方向等，带版本）生成凭证；未命中规则的流水记入暂记科目 `224199` 并进入待审，人工确认后重记。没有配置资金科目的来源可以接收数据，但不入账。
- 每条总账分录都能追溯：总账 → 凭证 → 来源行 → 批次 → 插件调用或上传文件。

## 6. 新增一个数据源

上传连接器 ZIP → 在财务数据源页绑定账号 → 在财务设置选资金科目。不需要发布或重启。P4 会同时提供一个仅用于测试的假连接器样板。
