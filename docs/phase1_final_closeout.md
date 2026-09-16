---
module: 阶段一收尾
type: 验收记录
status: active
authority: report
owner: repository
updated: 2026-09-16
---

# 第一轮最终收尾

任务合同：`BOYI-PHASE1-FINAL-CLOSEOUT-R1`。当前执行中，本页不是通过声明。

基线 `BASE_SHA=7ccc71445925f1fe3c7f35a3c7dd1a01198ff2ec`，已核对远端 main；
原工作区干净。本次使用 `codex/phase1-final-closeout` 独立 worktree，原工作区保持不动。
本次用户授权覆盖仓库默认 main 直推规则：仅提交任务分支，可创建 Draft PR；不合并、不部署生产。

用户后续明确要求替代原 R1 的忙时立即失败规则：同实例、执行容量或实际资源暂忙时，本次已受理请求在当前进程内最多等待 30 秒，释放后继续，不要求再次提交；等待可取消，总执行容量不增加，活跃请求总量另有上限。超时、过载、停服和明确业务错误仍显式终结；已结束调用、UNKNOWN 和服务重启不自动重放。
新用户动作使用新请求身份，同请求重投返回同一事实。
不恢复持久业务队列、旧 Runner 领取、登录恢复续跑或失败自动重放。
UNKNOWN 不意味着未写；只有权威业务证据允许再次写，证据不足须限制受影响目标。

## 收尾差距与证据

| 工作项 | 基线实际情况 | 当前处理 | 实际证据入口 |
|---|---|---|---|
| 正式检查 | 基线 CI 的 root 集合因迁移清单缺失 050/051 失败；Agent 后续集合未执行 | 保留迁移原字节，补受审清单；分别运行三个集合 | `tests/test_runtime_repositories.py`、`tests/test_waybill_source_coverage_mysql.py` |
| Direct 终结与新请求 | 已有忙时失败、有界写等待、真实取消排空 | 按用户修订增加 30 秒进程内资源等待，核验取消、超时、原请求续行与 UNKNOWN 隔离 | `tests/test_direct_plugin_invocation_mysql.py` |
| M02 字段维护 | 使用旧 Action V1 签名包；已有原始字段 Host 接口 | 迁到当前财务 V2 ZIP，只改候选 payload 解析 | `tests/v32_acceptance/finance_maintenance.py` |
| M03 判断维护 | 使用旧 Action V1 自提包 | 迁到当前自提 V2 ZIP、实际预览确认和回退 | `tests/v32_acceptance/decision_maintenance.py` |
| 当前验收合同 | 多项仍引用旧 Runner 语义与历史通过说明 | 保留原要求，新增 R1 有效要求；结果另由完整驱动计算 | `docs/low_maintenance_v32_acceptance.json` |
| 性能、完整验收与 CI | 本次尚未得到最终冻结基线结果 | 诊断后冻结、整轮重跑；不拼接旧结果 | `agent/scripts/accept_low_maintenance_v32.py` |

环境只使用本任务新建的 loopback MySQL 数据目录、禁用 dotenv 的清洁环境、
系统 Python 3.10、两份锁文件、真实 Chromium 与 bwrap/prlimit。外部协议只绑定本地端口。
本次没有读取生产凭据、访问 ECS、启动生产定时或向真实 TMS／飞书写入。

首轮完整驱动及对应 CI 保留失败证据；未将局部修复结果拼接为整轮通过。第二次旧规则验收因用户新增资源等待需求中止并标记 SUPERSEDED，不能视为通过；最终必须在修订代码提交后重新冻结并完整执行。
诊断修复包括：真实问题件回读补传已核验网点；分批事件携带必填分类；
财务 V2 回执从已审 Connector 的真实参数记录批次，运行来源链接使用当前操作身份；
离线历史财务测试与当前 V2 维护包隔离；CI 发行范围包含当前 Direct 来源切换测试；
客服和字段维护工件收集实际 Invocation 结果。UNKNOWN 文案明确先核对本次结果，避免重复提交。
受理实测另复现启动竞态：读取 Linux 进程状态时，刚退出的进程返回 ESRCH，被误判为沙箱进程计数不可用。现按系统明确的进程退出结果处理，与已有 ENOENT 处理一致；权限或解析异常仍失败，不扩大进程预算。CI 在真实数据库组合仍推进时达到原作业时限，因此增加测试作业时间预算并在首个断言失败时立即输出原因；成功仍要求完整集合通过，业务执行容量及性能目标不变。
截图所示线上调用尚未读取实际 Invocation 或日志，不能将已发现的代码缺口当作该次执行的确诊结论。
远程 CI 另揭示等待取消的时序竞态：任务先观察到取消标记并开始落库收尾，随后到达的取消信号可能中断收尾，使状态停在 CANCELLING。现复用已有线程排空机制，完整保存核验及终态后才释放占用；以可控事件固定该时序，重复取消不再留下未终结记录。

## 当前写动作与重触发边界

| 业务 | 实际目标与业务身份 | 写入与核验 | 当前证据入口 |
|---|---|---|---|
| 扫描 | 绑定站点、运单集合及原始扫描 operation_id | 外部扫描回执与服务端账本回读；本地快照独立发布，迟到/未知不盲目追加 | `scan_recovery.py`、`test_scan_readback_protocol.py`、当前 V2 日常链 |
| 到货统计 | 保存的飞书文档和子表、当前扫描发布快照 | 计算后覆盖指定表；物理范围互斥、完整回读，不将 HTTP 次数当作追加效果次数 | `daily_stats.py`、`daily_concurrency.py` |
| 自提/分批 | 权威站点、运单、问题类型、责任类型、内容摘要 | 外部问题件追加前回读；迁移 052 的精确目标事实跨 UUID/实例/账号防重。显式拒绝才释放；暂时空回读不能释放迟到风险 | `test_direct_problem_unknown_mysql.py` |
| 分批本地事件 | 外部问题件 GUID 和已验证登记信息 | 插件提交截止/顺延分类；共享规则单点计算，本地 upsert 后逐字段核验 | 分批 V2 `payload/action.py`、日常应签规则与适配器测试 |
| 财务 | 来源组织身份、业务日期、外部记录键、发布批次 | 外部只读；事务写标准账本，失败不覆盖此前有效分区，保留金额与来源溯源 | `finance_maintenance.py`、`finance_source_state.py` |
| 客服 | 来源身份、外部 GUID、来源方向 | 外部只读；本地事务发布，与人工字段分开；结束的 Invocation 或旧生产者不能迟到发布 | `customer_collection_flow.py`、`test_direct_source_switch_mysql.py` |

目标写入事实不是执行队列，无过期自动释放或补跑线程。已应用回读返回已有结果；未知目标拒绝新写，无关运单继续。该保证依赖已审融辉接口与业务键，不承诺任意第三方接口的通用 exactly-once。

## 维护和复现入口

使用 `tests/v32_acceptance/isolated_environment.sh` 的专属隔离环境。完整入口仍是
`agent/scripts/accept_low_maintenance_v32.py --phase all --host-freeze ...`；正式证据需要先提交宿主、测试和矩阵，再生成新冻结文件。`--phase ci` 与 `preparation.json` 均不是整轮通过。

局部维护复用 `python -m scripts.plugin_maintenance describe/test/package <准确 V2 plugin_id>`。
工具选择现有真实打包测试，并把测试源码、清单、实际 ZIP 成员及包摘要绑定；后续变化必须重新测试。
`--base-ref` 发现共享宿主变更返回 `CORE_UPDATE_REQUIRED`，跨插件共享改动返回 `MULTI_PLUGIN_REVIEW_REQUIRED`。
本轮六包 CLI、M02 字段变化和 M03 判断变化的实际命令、输出 ZIP 与摘要将随最终证据归档。

## 第二轮交接：只记录现有接口

以下是可复用现有能力，不是新增自然语言查询实现。第二轮必须从本轮实际被验提交或经过确认的集成版本继续。

| 入口 | 身份与输入 | 现有输出及边界 |
|---|---|---|
| Console `/finance/summary`、`/finance/trend`、`/finance/entries` | 当前登录身份和财务模块权限；来源及日期等现有筛选由 `console/services/monitoring_finance.py` 转交财务服务 | 已发布的本地金额/明细；来源、业务日期与发布时间分开，部分失败不等于完整新采集 |
| Console `/customer-service/problems/query` | 管理页面的真实写上下文鉴别；请求 `source_ids/account_ids` 与 `filters.direction/q/date_from/date_to/rows/page` | 只读已发布本地明细、统计、来源状态与错误；不可达返回 `CUSTOMER_LOCAL_DATA_UNAVAILABLE`，不是空成功 |
| 直接业务注册 `send-waybills-query`、`receipts-query`、`receipt-feishu-detail` | `agent/business_composition.py` 组合现有服务；账号与资源来自宿主，沿用现有查询参数校验 | 原有运单/回单读取；不能让模型自行提交凭据、执行身份或任意 SQL |
| 插件管理及执行 | `/automations`、`/modules/finance/data-sources`、`/modules/customer-service/data-sources`；准确实例、已保存角色绑定、配置版本与新 request_id | 统一 Direct Invocation，终态与写回执可追溯；预览确认使用原预览身份，不能绕过 |

“今天邵阳大祥站发了多少吨”尚需第二轮核实：真实站点与来源身份、发货日期时区/业务口径、重量字段及单位、退货/作废处理、完整分页和采集时点。本轮没有确认该自然语言问题的完整可用字段链，不能用到货统计、地址关键词或重量体积混合文本推测发货吨位；没有建立新 LLM 路由、Skill、知识库或指标引擎。

## 发布与回退边界（未执行生产）

本轮涉及 Host、共享模块和新增数据库表，整体属于核心更新。分批 V2 `2.0.3` 明确传入分类字段；每日应签 `2.0.9` 复用单一分类函数。旧分批包缺字段会明确失败，因此未来经授权发布须将 Host 与配套分批包作为一个兼容集合审核，不能只升级一侧。现有包应保留精确原 ZIP、版本、摘要、账号配置、来源身份与计划。

未来发布按现有发布器暂停新调用和定时，等待真实在途执行/核验结束，备份数据库、源码、依赖和安装包，再应用新增迁移及核验。052 仅增加写入事实表，不改历史迁移字节；旧样例升级及重入测试核对旧表全部行，并覆盖人工单号、日期、金额、备注。

候选维护失败时，通过现有生命周期安装该实例实际提交过的原版本；不把旧代码改高版本冒充回退。包/配置恢复不撤销外部已写业务。核心回退不得删除或清空问题件写入事实来解除 UNKNOWN；旧核心不具备新防重保证时保持相关写入口暂停，先核验未决目标。不得直接用旧备份覆盖发布后产生的新业务数据。

`PRODUCTION_STATUS=NOT_DEPLOYED_NOT_VERIFIED`。本轮最终实现、隔离验收、CI 和第二轮准入分别报告；冻结前准备结果不用于提前宣布第一轮通过。
