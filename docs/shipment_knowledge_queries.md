---
module: shipment-knowledge-queries
type: maintenance
status: active
authority: implementation
owner: repository
updated: 2026-09-24
---

# 发货吨位与飞书知识读取

本轮复用现有 Harness、统一会话、身份权限和只读边界，没有另建 Agent、账本或任务队列。合成验收不代表真实业务已经接通；尚未部署生产。

## 已确认与待核实

用户确认：邵阳大祥站与邵阳大祥S站是不同账户、不同网点；日期按开单，重量按计费，作废单排除。没有确认额外扣减退回货物，不能自行增加这一规则；回单与退回货物不是同一概念。

2026-09-24 原页只读核查确认：融辉「寄件运单查询」的寄件日期对应 `REGISTER_DATE`；「运单录入-成本2.0」的计费重量标签对应 `FEE_WEIGHT`。`BILL_WEIGHT` 为实际重量，`SETTLEMENT_WEIGHT` 为结算重量，不能互换。现有 `Send_order` 旧归一字段把 `FEE_WEIGHT` 命名为实际重量，不能借用该显示映射计算本指标；本次未修改旧映射及下游业务。

尚未核实重量单位、作废状态合同和两个账户到稳定站点身份的唯一映射。生产组合根明确使用 `unavailable_shipment_source`，不会从 `weight_volume` 显示文本猜算、选择默认账户或把失败报成零。正式接入必须在现有寄件数据拥有模块补充已核实的类型字段，复用既有分页、覆盖与发布，不另建平行账本。当前未实现类型字段的数据库缓存或真实来源按需补查。

## 维护入口

- `shared/shipment_metrics.py`：开单日期、公斤计费重量合同，半开时间区间，Decimal 汇总、分组和昨日对比；行数、分组总量、极值及反算校验。缺重、重复、分页不全、未知单位或范围漂移明确失败。合成合同分别记录作废、回单、子单排除原因，不推断退回货物额外扣减。
- `agent/agent/shipment_queries.py`、`shipment_conversation.py`：完整单日来源接口，以及既有内存会话内的结构化查询条件。按真实身份和会话隔离，有时限；过期及重启后不从旧文本猜测。最终吨位用程序模板输出，同一消息重投复用结果。
- `agent/tools/feishu_knowledge.py`：固定只读 CLI 参数、强制 bot，空间仅「融辉红头文件」「韵达红头文件」，同名不唯一即拒绝；核对节点、正文和读取后的更新时间。不持久缓存正文，每次重新核验权限；并发、时长、目录规模和正文大小有界，超时终止真实进程组。
- `agent/agent/knowledge_answers.py`：引用只来自本次读取对象；失败和无命中不靠模型记忆回答。缺少链接时保留标题和来源编号，不造 URL。知识正文不能改变工具、账户或写权限，附件与嵌入内容明确未读。

已核对 ECS 的 `lark-cli 1.0.3` 与 Feishu bot 身份。使用 `wiki spaces list/get_node`、`wiki nodes list`、`docs +fetch`；该版本全文 `docs +search` 只支持 user，故本次使用明确标记的范围内正文主题检索，没有切换共享 CLI 身份。协议依据为 [官方 v1.0.3 源码](https://github.com/larksuite/cli/tree/v1.0.3)。

正文仍有后续页时，本轮未核实续页位置，返回 `KNOWLEDGE_BODY_INCOMPLETE`；超过预算、格式不支持、权限不足、无命中分别报告。不伪造 revision，不把编辑时间当生效日期。真实长文分页、节点链接和冲突制度适用关系仍待获权后核实。

## 验收与发布边界

确定性隔离测试为 `tests/test_shipment_metrics.py`、`tests/test_shipment_conversation.py`、`tests/test_feishu_knowledge.py`，并回归既有 Harness、身份和插件意图测试。源数据与 Wiki 内容均为合成协议，CLI 超时使用真实子进程验证。

真实模型入口为 `tests/shipment_model_acceptance.py`、`tests/knowledge_model_acceptance.py`。模型采用后台观察到的 `deepseek-v4-flash`，通过既有环境变量调用相同供应商和模型，不读取 `.env` 或修改生产配置。报告保留语料、工具输入、答案、逐项结果和被验源码摘要；失败轮保留，替身与实模单列。

真实飞书仍返回缺少 `wiki:space:retrieve` 的错误；还需确认 `wiki:node:read`、`docx:document:readonly` 以及机器人在两个空间的成员权限。网页登录不等于机器人获权，本轮没有修改空间成员或文档。

发货来源合同、飞书授权、真实正文完整读取和规则到指标的映射未闭合前，第二、三轮不能标为完整真实业务验收通过。部署、真实 TMS 写入及生产模型/CLI 身份切换不在本次范围。回退沿用既有源码发布机制，不代表撤销外部业务或文档变更。
