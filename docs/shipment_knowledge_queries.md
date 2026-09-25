---
module: shipment-knowledge-queries
type: maintenance
status: active
authority: implementation
owner: repository
updated: 2026-09-25
---

# 发货吨位与飞书知识读取

本轮复用现有 Harness、统一会话、身份权限和只读边界，没有另建 Agent、账本或任务队列。真实来源在隔离进程验证，尚未部署生产；合成测试与真实业务验收分别记录。

## 已确认与待核实

用户确认：邵阳大祥站与邵阳大祥S站是不同账户、不同网点；日期按开单，重量按计费，作废单排除。没有确认额外扣减退回货物，不能自行增加这一规则；回单与退回货物不是同一概念。

2026-09-24 原页只读核查确认：融辉「寄件运单查询」的寄件日期对应 `REGISTER_DATE`；「运单录入-成本2.0」的计费重量标签对应 `FEE_WEIGHT`。`BILL_WEIGHT` 为实际重量，`SETTLEMENT_WEIGHT` 为结算重量，不能互换。现有 `Send_order` 旧归一字段把 `FEE_WEIGHT` 命名为实际重量，不能借用该显示映射计算本指标；本次未修改旧映射及下游业务。

2026-09-25 用户补充确认计费重量单位为公斤，作废单会从寄件查询结果直接消失。两个实际账号登录页面均已核实：邵阳大祥站编号 7390004，邵阳大祥S站编号 7390017；分别对应宿主现有 `price` 与 `daxiang_s` 用途。运行时不硬编码账号 ID 或站点码，而从当前用途唯一绑定取得会话，再核对真实登录站点名称与代码；缺失、多候选、登录失败或绑定漂移明确失败。

`plugin_core_adapters/shipment_source.py` 在现有寄件数据所属模块按需读取完整账号/日期快照，复用 `waybill_query.collect_ronghui_day` 的原生分页；强制 `SEND_SITE_CODE` 与已核实站点一致。直接从 `REGISTER_DATE`、`FEE_WEIGHT`、`SEND_SITE_CODE`、`SEND_SITE`、`DESTINATION` 建立类型字段，沿用既有回单/子单识别。每次查询重新拉取全部当前结果，消失的作废单不会从旧快照残留；权威总数为零才返回零。没有新增数据库缓存、平行账本或生产数据写入，也没有借用 `weight_volume` 显示文本或旧归一字段计算。

`harness_composition.py` 在实际账号使用与发布静默边界内注入来源，同一查询固定账号并在每次读取前后复核；整个查询有时间预算，超过上限要求缩小范围。版本字段的 `observed:` 明确是本次采集时间标记，不是假造上游修订号；分页总数、身份和范围检查不代表上游提供了跨请求事务快照。

## 维护入口

- `shared/shipment_metrics.py`：开单日期、公斤计费重量合同，半开时间区间，Decimal 汇总、分组和昨日对比；行数、分组总量、极值及反算校验。缺重、重复、分页不全、未知单位或范围漂移明确失败。合成合同分别记录作废、回单、子单排除原因，不推断退回货物额外扣减。
- `agent/agent/shipment_queries.py`、`shipment_conversation.py`：完整单日来源接口，以及既有内存会话内的结构化查询条件。按真实身份和会话隔离，有时限；过期及重启后不从旧文本猜测。最终吨位用程序模板输出，同一消息重投复用结果。
- `agent/plugin_core_adapters/shipment_source.py`：宿主用途绑定、真实登录网点复核和计费重量类型转换；复用寄件原生分页，完整刷新及零行、分页变化、错站、缺重、绑定变动和发布静默验证见 `tests/test_shipment_source.py`。
- `agent/tools/feishu_knowledge.py`、`feishu_knowledge_pdf.py`：固定只读 CLI 参数、强制 bot，空间仅「融辉红头文件」「韵达红头文件」，同名不唯一即拒绝；核对节点、正文和读取后的更新时间。不持久缓存正文，每次重新核验权限；并发、时长、目录规模和正文大小有界，超时终止真实进程组。
- `agent/agent/knowledge_answers.py`：引用只来自本次读取对象；失败和无命中不靠模型记忆回答。缺少链接时保留标题和来源编号，不造 URL。知识正文不能改变工具、账户或写权限，附件与嵌入内容明确未读。

已核对 ECS 的 `lark-cli 1.0.3` 与 Feishu bot 身份。使用 `wiki spaces list/get_node`、`wiki nodes list`、`docs +fetch`；该版本全文 `docs +search` 只支持 user，故本次使用明确标记的范围内正文主题检索，没有切换共享 CLI 身份。协议依据为 [官方 v1.0.3 源码](https://github.com/larksuite/cli/tree/v1.0.3)。

在线文档正文仍有后续页时，本轮未核实续页位置，返回 `KNOWLEDGE_BODY_INCOMPLETE`；超过预算、格式不支持、权限不足、无命中分别报告。不伪造 revision，不把编辑时间当生效日期。在线文档续页、节点链接和冲突制度适用关系仍待核实。

## 验收与发布边界

确定性隔离测试为 `tests/test_shipment_metrics.py`、`tests/test_shipment_conversation.py`、`tests/test_feishu_knowledge.py`，并回归既有 Harness、身份和插件意图测试。源数据与 Wiki 内容均为合成协议，CLI 超时使用真实子进程验证。

真实模型入口为 `tests/shipment_model_acceptance.py`、`tests/knowledge_model_acceptance.py`。模型采用后台观察到的 `deepseek-v4-flash`，通过既有环境变量调用相同供应商和模型，不读取 `.env` 或修改生产配置。报告保留语料、工具输入、答案、逐项结果和被验源码摘要；失败轮保留，替身与实模单列。

2026-09-25 用户明确授权后，机器人已能列出两个指定空间、读取其节点并下载文件；两个空间各有一份 PDF：《融辉物流网络管理手册》-6.1.pdf、网点操作手册 (2).pdf。使用 `wiki:space:retrieve`、`wiki:node:read` 和 `drive:file:download`；机器人以可阅读成员加入两个空间，没有勾选其他批量权限，没有发送成员通知或修改原文。此前权限阻塞已解除，未进行生产源码部署。

PDF 使用已实测的 `drive +download --file-token … --output ./source.pdf --as bot`；v1.0.3 不接受该命令的 `--format` 或绝对输出路径。每次在项目临时目录下载并重新校验节点身份、类型和更新时间，完成或失败均删除临时文件。解析使用受父进程期限与内存限额约束的独立 pypdf 子进程，限制文件大小、页数和文字总量；空页文字层、加密或损坏文件显式失败，不用旧缓存。仅检索原主题词，不拆词回退。

为使真实问答中的连续检索留在现有会话时限内，每次检索最多并行读取两个已核实节点，仍共用原整次期限与检索并发门限，不新增正文缓存。任一来源失败则整次不返回部分证据；权限/版本前后核验保持不变。并行读取和失败不泄漏部分依据的测试在 `tests/test_feishu_knowledge.py`。

完整扫描全部页文字层后，返回命中页与相邻页的完整文字，并标明 `excerpt_only`、页码及 `body_complete=false`；超过上下文上限要求更具体主题，不静默截断。回答只根据实际返回页，页码进入程序生成的来源说明。图示、扫描文字及表格版面没有被当作已识别内容；PDF 正文中的指令仍为不可信资料。真实两份 PDF 的页数和非空文字层已核对，文本提取不等于全书图表已识别，也不等于端到端问答验收完成。

真实模型验收须使用本次实际读取的 PDF 页和完整寄件快照，不能用旧合成分数替代。制度只能支持其实际条款，不把两份手册自动解释为用户已确认吨位口径的替代来源。部署、真实 TMS 写入及生产模型/CLI 身份切换不在本次范围。回退沿用既有源码发布机制，不代表撤销外部业务或文档变更。
