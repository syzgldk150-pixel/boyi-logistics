---
module: 财务模块
type: 接口契约
tags: [连接器, 标准流水契约, finance.source_batch, 账单文件, 插件, 总账]
related: [../agent/docs/finance_module.md, plugin-platform-v2.md, ../agent/docs/plugin_account_http.md]
status: active
updated: 2026-11-07
---

# 财务连接器开发说明：标准流水契约 v1

本文说明如何为博益财务模块写一个连接器（Service V2 插件），把某个平台账户、银行卡、微信或支付宝商户的流水交给总账。Host 侧已在总账重建 P4 实现（迁移 `071_finance_intake.sql`、`agent/agent/automation_plugins/finance_source_*.py`、`shared/finance/intake/`）。

## 1. 原则

- **分工：** 连接器只负责取数和解析，不懂会计。科目、凭证、往来核销全部由 Host 的记账规则决定。
- **访问限制：** 连接器不能写库，也不能访问外网。在线抓取只能经插件平台账号的 `http.request` 能力；账单文件只能通过下面的 `read_file` 读取。
- **Host 的把关：** Host 只信任契约。结构、金额、去重、证据，以及批次声明的完整性校验全部通过，批次才会被接受。被接受的批次和流水不可修改。
- **金额格式：** 一律用十进制字符串，最多 4 位小数，禁止浮点数。缺失就是缺失，不得补 0。

## 2. 准备工作

1. 超级管理员在财务页“来源与流水”中新建来源，填写：
   - 来源代码 `source_key`，例如 `rh_main`；
   - 接入方式：在线抓取或文件导入；
   - 提供方代码 `provider`，例如 `ronghui`、`wechat`、`bank_icbc`；流水记账规则按提供方匹配；
   - 负责的连接器实例编号；
   - 资金科目，例如 `101203 融辉主发货`。不配置资金科目的来源只接收流水、不入账。
2. 在“财务数据源”页安装连接器 ZIP，并把实例编号填回来源。一个实例可以负责多个来源。
3. 文件来源：在“来源与流水”中上传账单（不超过 20MB），再在“财务数据源”页运行该连接器。

## 3. Manifest 声明

```json
"capabilities": [
  {"name": "service.invoke", "operations": ["list_sources", "read_file", "submit_batch"], "account_role": null, "resource_role": null},
  {"name": "http.request", "operations": ["read_json"], "account_role": "finance_source_account", "resource_role": null}
],
"requires": [
  {"service": "connector.boyi.finance_sources@1", "binding_kind": "host_internal"}
]
```

只做文件导入的连接器不需要 `http.request`。在线抓取所用接口的声明方式见 [插件账号请求](../agent/docs/plugin_account_http.md)。

## 4. Host Connector：`connector.boyi.finance_sources@1`

| 操作 | 读写 | 入参 | 结果 |
|---|---|---|---|
| `list_sources` | 只读 | `{}` | `sources`：本实例负责的已启用来源。每项含 `source_key`、`display_name`、`kind`、`provider`、`covered_until`（已接受批次覆盖到的日期），以及待解析文件 `pending_files`（编号、文件名、类型、大小、sha256） |
| `read_file` | 只读 | `{file_id, offset, length}`，`length` 不超过 4 MiB | `{file_id, offset, size_bytes, sha256, data_base64, eof}` |
| `submit_batch` | 内部写 | `{batch}`，格式见第 5 节 | 接受时返回 `{status:"accepted", batch_id, replayed, row_count, superseded_batch_id, posted_days, kept_days, closed_period_days}`；拒绝时返回 `{status:"rejected", batch_id, error_code, message}` |

`read_file` 的限制：
- 只能读本实例负责的来源下状态为“待解析”的文件；
- 读其他来源的文件返回 `SOURCE_FILE_FORBIDDEN`；
- 批次接受后文件变为“已使用”，再读返回 `SOURCE_FILE_NOT_PENDING`。

```python
def call(broker, operation, arguments):
    return broker("service.invoke", action=operation, role="__system__",
                  arguments={"service": "connector.boyi.finance_sources@1", "operation": operation, "arguments": arguments})

sources = call(broker, "list_sources", {})["sources"]
for source in sources:
    for item in source["pending_files"]:
        chunks, offset = [], 0
        while True:
            part = call(broker, "read_file", {"file_id": item["id"], "offset": offset, "length": 4 * 1024 * 1024})
            chunks.append(base64.b64decode(part["data_base64"]))
            offset += len(chunks[-1])
            if part["eof"]:
                break
        rows = parse_bill(b"".join(chunks))          # 包内解析，字段原文保留
        result = call(broker, "submit_batch", {"batch": {
            "contract": "finance.source_batch/v1", "source_key": source["source_key"],
            "period": {"start": "2026-10-01", "end": "2026-10-31"},
            "declares": {"balance_chain": False, "summary": False, "opening_closing": False, "complete_period": True},
            "rows": rows, "evidence": {"kind": "file", "refs": [item["id"]]},
        }})
        if result["status"] != "accepted":
            raise RuntimeError(f"{result['error_code']}: {result['message']}")
```

## 5. 批次格式

```jsonc
{
  "contract": "finance.source_batch/v1",
  "source_key": "rh_main",                 // 财务页建立的来源代码
  "period": {"start": "2026-10-01", "end": "2026-10-31"},   // 最长 92 天
  "declares": {
    "balance_chain": true,                 // 每行带交易前/后余额，按提交顺序连续
    "summary": false,                      // 附带平台自身的分项汇总
    "opening_closing": true,               // 附带期初、期末余额
    "complete_period": true                // 保证期间内的流水是全量
  },
  "opening_balance": "1000.0000",          // 仅 opening_closing=true 时提交
  "closing_balance": "987.5000",
  "summary": [                             // 仅 summary=true 时提交
    {"item_l1": "运费", "item_l2": "", "direction": "out", "count": 1, "amount": "12.5000"}
  ],
  "rows": [{
    "source_key": "2026100314220500012",   // 来源内稳定唯一（平台流水号最佳），≤191 字
    "business_date": "2026-10-03",         // 业务日期，必须在 period 内
    "posted_at": "2026-10-03T14:22:05+08:00",
    "amount": "-12.5000",                  // 带符号：+ 流入本资金账户，− 流出；不能为 0
    "item_l1": "运费",                     // 平台原始一级项目（原文，不翻译）
    "item_l2": "",                         // 平台原始二级项目
    "counterparty": "…",
    "waybill_no": "…",                     // 能识别时填写，用于按运单号核销
    "reference": "…",
    "balance_before": "1000.0000",         // balance_chain=true 时必填
    "balance_after": "987.5000",
    "raw": {}                              // 平台原始字段，原样保存，≤16KB，不参与校验
  }],
  "evidence": {"kind": "http_observation", "refs": ["<本次 http.request 的 host_evidence_ref>", "…"]}
}
```

字段约束：

- **`source_key`：** 同一来源内永久唯一且稳定，重抓同一笔必须得到同一个键。没有平台流水号时，用能唯一确定该笔的原始字段拼接后取哈希，不要用行号。
- **单批上限：** 最多 50,000 行；超过请按期间拆分。
- **证据 `evidence`：**
  - 在线来源填本次调用中只读 `http.request` 返回的 `host_evidence_ref`，Host 逐个核对确实是本次调用观测到的只读请求；
  - 文件来源填唯一一个待解析文件编号。

## 6. Host 校验（按声明执行）

| 校验 | 何时执行 | 失败码 |
|---|---|---|
| 结构、类型、金额格式；日期在期间内 | 总是 | `INTAKE_SCHEMA` / `AMOUNT_INVALID` |
| 批次内 `source_key` 唯一；与其他期间已接受流水同键 | 总是 | `INTAKE_DUPLICATE` / `INTAKE_CONFLICT`（同键不同内容） |
| 来源存在、已启用、本实例是其生产者 | 总是 | `INTAKE_SOURCE` |
| 证据与本次 Host 观测或待解析文件一致 | 总是 | `INTAKE_EVIDENCE` |
| 与已接受批次的期间部分重叠 | 总是 | `INTAKE_PERIOD_OVERLAP` |
| 前余额 + 金额 = 后余额；行间连续 | `balance_chain` | `INTAKE_BALANCE_CHAIN` |
| 明细按项目、方向合计 = 汇总 | `summary` | `INTAKE_SUMMARY` |
| 期初 + Σ金额 = 期末；有余额链时首尾一致 | `opening_closing` | `INTAKE_OPENING_CLOSING` |

声明为 `false` 的校验不执行，页面会逐批显示“余额链（未校验）”等。`complete_period` 只记录为“已声明”，P5 封账前检查会要求每个启用来源整月都有声明完整的批次覆盖。

## 7. 接受之后

- **重复提交：** 同一来源同一期间再提交完全相同的内容，直接返回原批次（`replayed=true`）。
- **重新提交：** 内容有变化时，新批次替换旧批次，旧批次标记为 `superseded`，然后逐天比较两批流水：
  - 没有变化的天沿用原凭证；
  - 开放期间里有变化的天，冲销当天凭证后按新流水重记；
  - 已封账期间里有变化的天不改账，记一条 `CLOSED_PERIOD_DRIFT` 异常，由人工确认知悉。
- **入账：**
  - 每个来源每天一张凭证。每笔流水对应两行分录：一行记资金科目，一行记对方科目，并带 `source_line_id`。
  - 规则按“提供方 + 一级项目 + 二级项目 + 方向”精确匹配，不做模糊匹配。
  - 往来动作可选：无 / 固定往来单位 / 按运单号核销应收应付 / 内部转账（记资金在途）。
  - 未命中规则的流水记入暂记科目 `224199`，并在“规则与待审”中生成待审。处理时选择对方科目并保存规则，系统冲销暂记，按规则重记开放期间的对应各天。
- **追溯链：** 每条总账分录都能逐级追溯：总账 → 凭证 → 来源流水 → 批次 → 插件实例与证据（宿主观测或上传文件）。

## 8. 新增一个数据源

上传连接器 ZIP → 新建来源并填实例编号与资金科目 → 运行连接器。不需要发布或重启。
