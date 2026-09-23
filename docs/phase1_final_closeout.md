---
module: 阶段一收尾
type: 验收记录
status: active
authority: report
owner: repository
updated: 2026-09-23
---

# 第一轮最终收尾

任务合同：`BOYI-PHASE1-FINAL-CLOSEOUT-R1`。

第一轮代码与隔离验收通过，可基于被验提交进入第二轮开发；本次未部署或验证生产。

| 结论 | 结果 |
|---|---|
| IMPLEMENTATION_STATUS | COMPLETE |
| ISOLATED_ACCEPTANCE_STATUS | PASS |
| CI_STATUS | PASS，对应被验提交的正式 PR CI 已结束；发行范围外审计为 success |
| PRODUCTION_STATUS | NOT_DEPLOYED_NOT_VERIFIED |
| PHASE2_READY_FOR_DEVELOPMENT | YES，仅允许基于本页被验基线开始后续开发 |

`BASE_SHA=7ccc71445925f1fe3c7f35a3c7dd1a01198ff2ec`。`TESTED_CODE_SHA=a5a828a50caff4bce70a7ac30fdbf8e834814fc8`。
验收时间：2026-09-23T10:10:13.897361+00:00 至 2026-09-23T11:01:12.500491+00:00。
冻结文件 SHA-256：`6a7a29a1716bafb6cd7cbf8ebdcbc132e4e22f7190cebdb0e46560df19ca762e`；完整结果 SHA-256：`cb14351cf7e4fe074eabfe57d46b03cabe8b4cf83f0bcc3d800a9b66abe91c05`。
矩阵 SHA-256：`a98a4d9d9e2e34d8aa1626b75792adb65ffcaf46fcc50ec474d236aab2c664c0`。

[Draft PR #171](https://github.com/syzgldk150-pixel/boyi-logistics/pull/171)；
[该被验提交的正式 CI](https://github.com/syzgldk150-pixel/boyi-logistics/actions/runs/35847178273)。
测试结束后的报告提交仅更新文档和索引，不改变被验代码；实际报告提交另列于交付摘要与 PR。
相对基线变更 124 个文件，增加 5214 行、删除 716 行（不包含后续报告提交）。

执行期间远端 main 前进到 `da20899dedef077cf68b5990190b59bdc074217d`，运单录入与打印版本及分批/自提来源表修复均已合入本轮冻结提交。主分支运单回执迁移 052 原字节保留，本任务尚未发布的写入事实迁移改为 053；两个来源表的列语义回归纳入 A01 必需证据。
远程 CI 沿用仓库现行 PR 合并预览检出规则：实际检出 SHA、PR 头提交和合并基线逐作业保存在机器摘要的 `ci.checkout_records`。
该 CI 由上述 `TESTED_CODE_SHA` 触发；本地完整业务、性能及维护验收固定测试该提交本身，二者身份分别记录。

机器摘要见 [phase1_final_results.json](phase1_final_results.json)，现行要求见 [完整验收矩阵](low_maintenance_v32_acceptance.json)。
完整工件：`C:/Users/DENG/Desktop/BOYI_PHASE1_CLOSEOUT_20260916/final-a5a828a-PASS.zip`，SHA-256：`762a23eb40cf35751b8cee0c1fd56aeff97a9987d83be551af55314080f4dc51`。
归档保留原路径映射、原始样本、JUnit、截图、测试 ZIP、冻结记录及 CI；可先解压再按 `manifest.json` 核对。
首轮失败另存 `C:/Users/DENG/Desktop/BOYI_PHASE1_CLOSEOUT_20260916/attempt1-efeab453-FAIL.zip`，不计入最终通过结果。
旧规则的未完成验收另存 `attempt2-88884fa-SUPERSEDED.zip`；资源等待需求修订后的正式结果独立计算。
异步接口测试替身未同步导致的失败及中止记录另存 `attempt3-6623e81-FAILED-INCOMPLETE.zip`，保留原失败断言与对应 CI 状态，不计入最终通过结果。
启动检查的进程退出竞态导致的完整失败验收及复现记录另存 `attempt4-040e574-FAIL.zip`；该提交的 CI 达到原运行时限，终止前也有失败标记，因此不能算作通过。
随后远程 CI 发现等待取消收尾竞态，原始 CI 和本地中止记录另存 `attempt5-65d440b-CI-FAIL-INCOMPLETE.zip`；未将该次本地部分成功当作整轮通过。
合入用户指定的最新运单版本及分批/自提连接器修复前，完整验收被中止并标记 SUPERSEDED；此前代码提交的 CI 已通过，但不能替代最终整轮。记录另存 `attempt6-2381174-SUPERSEDED.zip`。
预览校验遭遇 1.195050 秒 UTC 时钟回拨，完整验收在根测试出现失败后终止；原失败与复现探针另存 `attempt7-b0c67fe-PREVIEW-CLOCK-FAIL-INCOMPLETE.zip`，不计入最终通过结果。
下一轮根、Agent、Console 和正式 CI 均通过，但旧测试插件目录令页面种子冲突；该轮已中止并另存 `attempt8-f18096b-FIXTURE-FAIL-INCOMPLETE.zip`。完整驱动现在用已有独占锁保护的显式重置重建自有 E2E 库和目录。


基线 `BASE_SHA=7ccc71445925f1fe3c7f35a3c7dd1a01198ff2ec`，已核对远端 main；
原工作区干净。本次使用 `codex/phase1-final-closeout` 独立 worktree，原工作区保持不动。
本次用户授权覆盖仓库默认 main 直推规则：仅提交任务分支，可创建 Draft PR；不合并、不部署生产。

用户后续明确要求替代原 R1 的忙时立即失败规则：同实例、执行容量或实际资源暂忙时，本次已受理请求在当前进程内等待，每次资源等待默认最多 30 秒，释放后继续，不要求再次提交；等待可取消，总执行容量不增加，活跃请求总量另有上限。超时、过载、停服和明确业务错误仍显式终结；已结束调用、UNKNOWN 和服务重启不自动重放。
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
| 性能、完整验收与 CI | 最终冻结基线已完成整轮验收和 CI | 诊断后冻结、整轮重跑；不拼接旧结果 | `agent/scripts/accept_low_maintenance_v32.py` |

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
本轮完整隔离验收还捕获到预览确认的偶发 `PREVIEW_INVALID`：预览记录的 UTC 生成时间比宿主校验时钟快 1.195050 秒。共享预览合同现仅允许 3 秒短时回拨容差；更远的未来时间及到期预览仍拒绝。失败轮和时差探针保留在 `attempt7-b0c67fe-PREVIEW-CLOCK-FAIL-INCOMPLETE.zip`，不计入最终通过结果。
随后一轮的根、Agent、Console 及正式 CI 通过，但页面段发现重复验收时旧测试插件目录与新包冲突。现让完整驱动调用现有独占锁保护的 E2E 显式重置，仅清理本任务的测试库和三个已验证目录；旧轮另存 `attempt8-f18096b-FIXTURE-FAIL-INCOMPLETE.zip`，不计入最终通过结果。
用户要求保留运单录入与打印的唯一最新版本。本分支已合入主分支截至 `da20899` 的运单模板、回执选择及分批/自提来源表列语义修复；原主分支运单迁移 `052_boyi_waybill_receipt_required.sql` 原字节保留，本任务尚未发布的问题件写入事实迁移顺延为 `053_problem_write_intents.sql`。分批和自提的合法电话、地址只在精确的来源表列中使用原业务文本校验，其他列及凭据泄漏仍拒绝；对应回归属于当前发行测试。

## 当前写动作与重触发边界

| 业务 | 实际目标与业务身份 | 写入与核验 | 当前证据入口 |
|---|---|---|---|
| 扫描 | 绑定站点、运单集合及原始扫描 operation_id | 外部扫描回执与服务端账本回读；本地快照独立发布，迟到/未知不盲目追加 | `scan_recovery.py`、`test_scan_readback_protocol.py`、当前 V2 日常链 |
| 到货统计 | 保存的飞书文档和子表、当前扫描发布快照 | 计算后覆盖指定表；物理范围互斥、完整回读，不将 HTTP 次数当作追加效果次数 | `daily_stats.py`、`daily_concurrency.py` |
| 自提/分批 | 权威站点、运单、问题类型、责任类型、内容摘要 | 外部问题件追加前回读；迁移 053 的精确目标事实跨 UUID/实例/账号防重。显式拒绝才释放；暂时空回读不能释放迟到风险 | `test_direct_problem_unknown_mysql.py` |
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
本轮六包 CLI、M02 字段变化和 M03 判断变化的实际命令、输出 ZIP 与摘要已随最终证据归档。

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

未来发布按现有发布器暂停新调用和定时，等待真实在途执行/核验结束，备份数据库、源码、依赖和安装包，再应用新增迁移及核验。运单回执迁移 052 保持主分支原字节；本任务的 053 仅增加写入事实表，不改历史迁移字节。旧样例升级及重入测试核对旧表全部行，并覆盖人工单号、日期、金额、备注。

候选维护失败时，通过现有生命周期安装该实例实际提交过的原版本；不把旧代码改高版本冒充回退。包/配置恢复不撤销外部已写业务。核心回退不得删除或清空问题件写入事实来解除 UNKNOWN；旧核心不具备新防重保证时保持相关写入口暂停，先核验未决目标。不得直接用旧备份覆盖发布后产生的新业务数据。

`PRODUCTION_STATUS=NOT_DEPLOYED_NOT_VERIFIED`。本轮最终实现、隔离验收、CI 和第二轮准入分别报告；冻结前准备结果不用于提前宣布第一轮通过。

## 最终测试与维护证据

| 集合 | PASS | FAIL | SKIP |
|---|---:|---:|---:|
| test-agent | 1302 | 0 | 0 |
| test-console | 748 | 0 | 0 |
| test-root | 4447 | 0 | 5 |

以上按 JUnit testcase 统计；子测试另保留在原日志。依赖锁、编译、ruff、注册表、导入边界、仓库卫生、文档和内部契约均通过。
非必需跳过及原因逐条保存在机器摘要；本轮必需引用无跳过。CI Windows 历史作业沿既有发行范围跳过，当前 Direct/V2/来源链未延期或标 xfail。

| 维护 | 基线版本 | 候选版本 | 宿主与进程 | 回退 |
|---|---|---|---|---|
| M02 财务字段 | 2.0.1 | 98.2.0 | 完全一致 | 恢复原 ZIP 并验证真实业务 |
| M03 自提判断 | 2.0.1 | 98.3.0 | 完全一致 | 恢复原 ZIP 并验证真实业务 |

M02 财务字段 baseline ZIP：`/home/deng/projects/boyi-phase1-final-closeout/.task_tmp/v32/m02/e1d1a737ad/artifacts/sync_finance_bills_v2-baseline-2.0.1.zip`；SHA-256：`b1ea9a3c0508c2de8c34adbba60eda5b185691bae09d9d2b1d46886546bd40df`。

M02 财务字段 candidate ZIP：`/home/deng/projects/boyi-phase1-final-closeout/.task_tmp/v32/m02/e1d1a737ad/artifacts/sync_finance_bills_v2-candidate-98.2.0.zip`；SHA-256：`df7a394770e1e4b7c369910a934bb86f32ac3ed474aa4093dc287f35a5f36d56`。

M03 自提判断 baseline ZIP：`/home/deng/projects/boyi-phase1-final-closeout/.task_tmp/v32/m03/run-b46710bb371744f4b655b1872c3cbc53/artifacts/self_pickup_problem_upload_v2-baseline-2.0.1.zip`；SHA-256：`ed7b47dbbe3bad13bfe392bb3bc803afeff192d069c17352f0298ee17a6f8c5c`。

M03 自提判断 candidate ZIP：`/home/deng/projects/boyi-phase1-final-closeout/.task_tmp/v32/m03/run-b46710bb371744f4b655b1872c3cbc53/artifacts/self_pickup_problem_upload_v2-candidate-98.3.0.zip`；SHA-256：`d15b9b88557c94612a797ccf8e17ea504bc2ec6c40d60ead1b03b5abf36430ce`。

维护演练期间无关调用完成，源格式变化先使旧包明确失败，再由候选包处理；回退不撤销已发生的外部业务。测试规则只存在于隔离候选包。

实际复现入口（先按 `tests/v32_acceptance/environment.md` 准备独占隔离环境）：

```bash
bash tests/v32_acceptance/isolated_environment.sh run --database v32_cli_test python -m scripts.plugin_maintenance test sync_finance_bills_v2
```

完整驱动、各探针和三个测试集合的实际命令均在归档 `result.json` 的 `checks[].command`；不是仅执行上述局部命令。

## 最终性能

| 指标 | 样本 | P50 ms | P95 ms | 最大 ms | 错误 | 目标 ms |
|---|---:|---:|---:|---:|---:|---:|
| automation/shell | 30 | 516.600 | 664.500 | 676.100 | 0 | 1000 |
| automation/list_and_controls | 30 | 830.300 | 966.700 | 983.500 | 0 | 1500 |
| finance/shell | 30 | 517.700 | 631.300 | 673.800 | 0 | 1000 |
| finance/list_and_controls | 30 | 850.900 | 968.100 | 1003.300 | 0 | 1500 |
| customer_service/shell | 30 | 517.500 | 615.700 | 623.600 | 0 | 1000 |
| customer_service/list_and_controls | 30 | 841.500 | 903.200 | 939.600 | 0 | 1500 |
| finance/source_detail | 100 | 261.000 | 305.800 | 346.700 | 0 | 1500 |
| customer_service/source_detail | 100 | 136.200 | 166.700 | 205.200 | 0 | 1500 |
| direct/admission | 100 | 174.828 | 237.134 | 297.414 | 0 | 500 |
| navigation/tab_switch | 20 | 74.300 | 111.700 | 113.700 | 0 | 200 |
| direct/independent_start | 20 | 47.621 | 74.336 | 90.121 | 0 | 2000 |
| direct/resume_after_release | 40 | 13.846 | 20.330 | 28.293 | 0 | 2000 |
| direct/waiting_cancel | 60 | 23.268 | 70.629 | 79.773 | 0 | 2000 |

受理样本最终状态：`{"COMPLETED": 100}`；繁忙拒绝 0。繁忙故障用例独立记录，不混入成功受理分位数。
分位数采用 nearest rank `ceil(p*n)`，不丢弃慢样本。没有可比基线实测，故只报告本轮绝对值，不宣称优化倍数。
环境：12th Gen Intel(R) Core(TM) i7-12700H，20 个逻辑处理器，内存 16595980288 bytes，Python 3.10.20；MySQL、浏览器、依赖锁和固定容量详见机器摘要。
冷样本使用新页面/DOM、独立管理员会话，每批清除服务端列表展示缓存，保留已安装包和真实数据库；loopback 路由关闭浏览器 HTTP 缓存。明细测量复用页面并发出真实本地 SQL 查询，未替换响应。

各入口安装实例数、财务/客服合成明细规模、并发客户端、依赖延迟注入与调用跟踪见原始页面工件。独立任务开始、资源释放后续行和等待取消的原始耗时来自 JUnit 属性；等待取消逐轮核对后续零新写。已发生外部操作的取消则保留真实核验及终结时间线。
生产默认资源等待预算为 30 秒，当前 V2 测试明确校验该值；重复超时故障演练注入较短预算，实际预算见用例源码及相关 JUnit 属性，以真实计时触发同一超时分支。资源释放、取消及无重复执行使用真实进程、MySQL 与调用记录核验。

## 各组最终结果

| 组 | 结果 | 命中用例统计 |
|---|---|---|
| A01 | PASS | `{"PASS": 6}` |
| A02 | PASS | `{"PASS": 81}` |
| A03 | PASS | `{"PASS": 140}` |
| A04 | PASS | `{"PASS": 80}` |
| A05 | PASS | `{"PASS": 80}` |
| A06 | PASS | `{"PASS": 129}` |
| A07 | PASS | `{"PASS": 61}` |
| A08 | PASS | `{"PASS": 141}` |
| A09 | PASS | `{"PASS": 60}` |
| A10 | PASS | `{"PASS": 184}` |
| A11 | PASS | `{"PASS": 108}` |
| A12 | PASS | `{"PASS": 57}` |
| A13 | PASS | `{"PASS": 204}` |
| A14 | PASS | `{"PASS": 15}` |
| A15 | PASS | `{"PASS": 83}` |
| A16 | PASS | `{"PASS": 7}` |
| A17 | PASS | `{"PASS": 12}` |
| A18 | PASS | `{"PASS": 102}` |
| A19 | PASS | `{"PASS": 82}` |
| A20 | PASS | `{"PASS": 107}` |
| A21 | PASS | `{"PASS": 150}` |
| A22 | PASS | `{"PASS": 294}` |
| A23 | PASS | `{"PASS": 10}` |
| A24 | PASS | `{"PASS": 23}` |
| B01 | PASS | `{"PASS": 59}` |
| B02 | PASS | `{"PASS": 5}` |
| B03 | PASS | `{"PASS": 43}` |
| B04 | PASS | `{"PASS": 44}` |
| B05 | PASS | `{"PASS": 54}` |
| B06 | PASS | `{"PASS": 47}` |
| B07 | PASS | `{"PASS": 30}` |
| B08 | PASS | `{"PASS": 34}` |
| C01 | PASS | `{"PASS": 50}` |
| C02 | PASS | `{"PASS": 55}` |
| C03 | PASS | `{"PASS": 64}` |
| C04 | PASS | `{"PASS": 132}` |
| C05 | PASS | `{"PASS": 112}` |
| C06 | PASS | `{"PASS": 59}` |
| C07 | PASS | `{"PASS": 49}` |
| C08 | PASS | `{"PASS": 45}` |
| C09 | PASS | `{"PASS": 70}` |
| C10 | PASS | `{"PASS": 45}` |
| C11 | PASS | `{"PASS": 41}` |
| C12 | PASS | `{"PASS": 44}` |
| C13 | PASS | `{"PASS": 47}` |
| C14 | PASS | `{"PASS": 44}` |
| C15 | PASS | `{"PASS": 57}` |
| C16 | PASS | `{"PASS": 189}` |
| C17 | PASS | `{"PASS": 235}` |
| C18 | PASS | `{"PASS": 49}` |
| M01 | PASS | `{"PASS": 78}` |
| M02 | PASS | `{"PASS": 1}` |
| M03 | PASS | `{"PASS": 1}` |
| M04 | PASS | `{"PASS": 35}` |
| M05 | PASS | `{"PASS": 64}` |
| M06 | PASS | `{"PASS": 28}` |

各必需子项的精确引用、有效要求、重复轮次和工件在同一份机器结果中。复用用例会被多组引用，不能把各组计数相加当作独立测试数。

## 第二轮接口补充

财务查询由 `console/finance_service.py` 校验 `start_date/end_date`（ISO 日期）、`platform/account_id/source_ids`（后者为逗号分隔的稳定来源 ID）、费用方向/级别/名称、运单号；明细使用 `page/page_size`。未填日期会采用服务端当前月到当前日，第二轮有“今天”语义时应明确传入业务日期，不能依赖这个默认。
财务金额来自已发布标准账本；返回 `period`、`failed_sources`、明细及来源状态。参数、仓储、契约和部分采集错误分别使用 `FINANCE_VALIDATION_ERROR`、`FINANCE_UNAVAILABLE`、`FINANCE_CONTRACT_ERROR`、`FINANCE_SYNC_PARTIAL_FAILED` 等既有码；查询失败不得改为金额零。
客服本地查询使用稳定 `source_ids` 或已授权 `account_ids`，`filters.direction/q/date_from/date_to/rows/page`；日期筛选实际作用于 `source_updated_at`，不能当作发货日期。请求仍通过登录身份和服务端上下文鉴别。错误筛选返回错误，数据库不可用为 `CUSTOMER_LOCAL_DATA_UNAVAILABLE`。`last_attempt_at/last_published_at`、上次成功结果和当前失败须分别展示，人工字段属于本地模块。
“邵阳大祥站”到稳定组织/来源 ID 的真实映射、发货重量字段与单位、业务日切换、退货作废口径和全量覆盖尚未在本轮生产数据上核实。不能凭账号显示名、到货数量或历史值构造吨位答案。

## 剩余范围

本次代码与隔离验收合同无未完成必需项。生产未部署、真实数据未验证；截图所示那次分批调用的具体失败阶段尚无线上记录证据，已修复的相关缺口仍需未来授权发布后生效。第二轮尚未实施。
