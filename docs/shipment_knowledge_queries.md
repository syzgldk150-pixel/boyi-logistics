---
module: shipment-knowledge-queries
type: maintenance
status: active
authority: implementation
owner: repository
updated: 2026-09-25
---

# 发货吨位与飞书知识读取

本轮复用现有 Harness、统一会话、身份权限和只读边界，没有另建 Agent、账本或任务队列。合成验收不代表真实业务已经接通；尚未部署生产。

## 已确认与待核实

用户确认：邵阳大祥站与邵阳大祥S站是不同账户、不同网点；日期按开单，重量按计费，作废单排除。没有确认额外扣减退回货物，不能自行增加这一规则；回单与退回货物不是同一概念。

2026-09-24 原页只读核查确认：融辉「寄件运单查询」的寄件日期对应 `REGISTER_DATE`；「运单录入-成本2.0」的计费重量标签对应 `FEE_WEIGHT`。`BILL_WEIGHT` 为实际重量，`SETTLEMENT_WEIGHT` 为结算重量，不能互换。现有 `Send_order` 旧归一字段把 `FEE_WEIGHT` 命名为实际重量，不能借用该显示映射计算本指标；本次未修改旧映射及下游业务。

2026-09-25 用户补充确认计费重量单位为公斤，作废单会从寄件查询结果直接消失。原页已核实邵阳大祥站编号为 7390004；大祥S站与账号的唯一映射仍待核实。来源同步必须以对应范围的完整当前查询更新，不能只追加而保留消失单的旧重量。生产组合根明确使用 `unavailable_shipment_source`，不会从 `weight_volume` 显示文本猜算、选择默认账户或把失败报成零。正式接入必须在现有寄件数据拥有模块补充已核实的类型字段，复用既有分页、覆盖与发布，不另建平行账本。当前未实现类型字段的数据库缓存或真实来源按需补查。

## 维护入口

- `shared/shipment_metrics.py`：开单日期、公斤计费重量合同，半开时间区间，Decimal 汇总、分组和昨日对比；行数、分组总量、极值及反算校验。缺重、重复、分页不全、未知单位或范围漂移明确失败。合成合同分别记录作废、回单、子单排除原因，不推断退回货物额外扣减。
- `agent/agent/shipment_queries.py`、`shipment_conversation.py`：完整单日来源接口，以及既有内存会话内的结构化查询条件。按真实身份和会话隔离，有时限；过期及重启后不从旧文本猜测。最终吨位用程序模板输出，同一消息重投复用结果。
- `agent/tools/feishu_knowledge.py`、`feishu_knowledge_pdf.py`：固定只读 CLI 参数、强制 bot，空间仅「融辉红头文件」「韵达红头文件」，同名不唯一即拒绝；核对节点、正文和读取后的更新时间。不持久缓存正文，每次重新核验权限；并发、时长、目录规模和正文大小有界，超时终止真实进程组。
- `agent/agent/knowledge_answers.py`：引用只来自本次读取对象；失败和无命中不靠模型记忆回答。缺少链接时保留标题和来源编号，不造 URL。知识正文不能改变工具、账户或写权限，附件与嵌入内容明确未读。

已核对 ECS 的 `lark-cli 1.0.3` 与 Feishu bot 身份。使用 `wiki spaces list/get_node`、`wiki nodes list`、`docs +fetch`；该版本全文 `docs +search` 只支持 user，故本次使用明确标记的范围内正文主题检索，没有切换共享 CLI 身份。协议依据为 [官方 v1.0.3 源码](https://github.com/larksuite/cli/tree/v1.0.3)。

在线文档正文仍有后续页时，本轮未核实续页位置，返回 `KNOWLEDGE_BODY_INCOMPLETE`；超过预算、格式不支持、权限不足、无命中分别报告。不伪造 revision，不把编辑时间当生效日期。在线文档续页、节点链接和冲突制度适用关系仍待核实。

## 验收与发布边界

确定性隔离测试为 `tests/test_shipment_metrics.py`、`tests/test_shipment_conversation.py`、`tests/test_feishu_knowledge.py`，并回归既有 Harness、身份和插件意图测试。源数据与 Wiki 内容均为合成协议，CLI 超时使用真实子进程验证。

真实模型入口为 `tests/shipment_model_acceptance.py`、`tests/knowledge_model_acceptance.py`。模型采用后台观察到的 `deepseek-v4-flash`，通过既有环境变量调用相同供应商和模型，不读取 `.env` 或修改生产配置。报告保留语料、工具输入、答案、逐项结果和被验源码摘要；失败轮保留，替身与实模单列。

2026-09-25 用户明确授权后，机器人已能列出两个指定空间、读取其节点并下载文件；两个空间各有一份 PDF：《融辉物流网络管理手册》-6.1.pdf、网点操作手册 (2).pdf。使用 `wiki:space:retrieve`、`wiki:node:read` 和 `drive:file:download`；机器人以可阅读成员加入两个空间，没有勾选其他批量权限，没有发送成员通知或修改原文。此前权限阻塞已解除，未进行生产源码部署。

PDF 使用已实测的 `drive +download --file-token … --output ./source.pdf --as bot`；v1.0.3 不接受该命令的 `--format` 或绝对输出路径。每次在项目临时目录下载并重新校验节点身份、类型和更新时间，完成或失败均删除临时文件。解析使用受父进程期限与内存限额约束的独立 pypdf 子进程，限制文件大小、页数和文字总量；空页文字层、加密或损坏文件显式失败，不用旧缓存。仅检索原主题词，不拆词回退。

完整扫描全部页文字层后，返回命中页与相邻页的完整文字，并标明 `excerpt_only`、页码及 `body_complete=false`；超过上下文上限要求更具体主题，不静默截断。回答只根据实际返回页，页码进入程序生成的来源说明。图示、扫描文字及表格版面没有被当作已识别内容；PDF 正文中的指令仍为不可信资料。真实两份 PDF 的页数和非空文字层已核对，文本提取不等于全书图表已识别，也不等于端到端问答验收完成。

发货来源合同、飞书授权、真实正文完整读取和规则到指标的映射未闭合前，第二、三轮不能标为完整真实业务验收通过。部署、真实 TMS 写入及生产模型/CLI 身份切换不在本次范围。回退沿用既有源码发布机制，不代表撤销外部业务或文档变更。
