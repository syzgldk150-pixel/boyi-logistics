# V3.2 第一轮维护边界

调用层已由 [架构改造 V1](architecture_direct_invocation.md) 更新；本文件其余模块归属、签名、数据正确性与性能标准继续适用。

本文件落实用户指定的第一轮低维护成本范围，替代本轮此前“全部插件集中自动化”“插件必须有 AI 声明”“只有账号也必须自带 HTML”三类冲突规则。最终验收状态以 `low_maintenance_v32_acceptance.json` 和本轮实际执行证据为准，本文件不代表全部验收通过。

## 基线与保留内容

GitHub 比较基线为 `2c8c6ac98b3a9033e688bcfae01018b258c289a3`，开始执行时本地 HEAD 与 origin/main 相同。本地尚未发布的修改另行保留在本任务分支，原工作目录不覆盖。没有执行 ECS 部署、重启、真实定时修改或外部业务写入。

四项日常业务保留原有独立入口、预览/确认、固定飞书指令与业务依赖；现有 AI 助手不移除。以下表列出应修改的位置，实例 ID 必须以持久化项目与既有迁移映射为准，不按显示名称猜测。

## 维护归属

| 范围 | 常改内容与位置 | 日常更新单位 | 需要核心更新的变化 |
|---|---|---|---|
| 扫描 | `agent/service_v2_plugins/sync_scan_codes_v2/payload/`；既有 ACTION_V1 的 `agent/first_party_automation_plugins/sync_scan_codes/payload/` | 对应已安装插件及相关测试；账号引用和计划从设置保存 | Host 写入/核验协议、共享资源身份、授权或运行器 |
| 到货统计 | `agent/service_v2_plugins/sync_arrival_stats_v2/payload/`；既有统计首方 action payload | 对应统计插件；继续核对扫描发布快照依赖 | 快照共享契约、金额/计数公共口径、整表写保护 |
| 分批问题件 | 唯一业务源码 `agent/first_party_automation_plugins/split_pending_problem_upload/payload/action.py`；v2 包由 `agent/service_v2_plugins/split_pending_problem_upload_v2/` 和共享打包器引用 | 当前实际安装的分批插件；局部候选筛选和判断连同测试，不另写 v2 算法副本 | 预览确认契约、共享问题件身份、Host 能力 |
| 自提问题件 | 唯一业务源码 `agent/first_party_automation_plugins/self_pickup_problem_upload/payload/action.py`；v2 包由 `agent/service_v2_plugins/self_pickup_problem_upload_v2/` 和共享打包器引用 | 当前实际安装的自提插件；局部业务判断连同测试，不另写 v2 算法副本 | 共享审批、结果核验或账号会话协议 |
| 财务采集 | `agent/first_party_automation_plugins/sync_finance_bills/payload/`；领域账本为 `shared/finance/` | 相容的字段适配/采集编排属于财务采集插件，公共账本保持核心 | 数据集、金额规则、成功发布条件、数据库迁移 |
| 客服采集 | `agent/first_party_automation_plugins/sync_customer_service_problems/payload/customer_problem_fields.py` 与 `action.py` | 客服采集插件，真实 raw 字段经插件规范化后发布 | 来源身份/方向业务键、人工字段权属、公共数据库 |
| 账号 | 既有 Agent 账号库、SessionBroker 与默认账号设置桥 | 维护已有账号引用；多个插件复用 | 凭据存储、会话互斥和 Host 授权协议 |
| 来源与历史 | `shared/data_sources.py`、`shared/customer_service_repository.py`、`shared/finance/publication.py` | 显式接续兼容生产者；暂停/卸载保留来源与业务历史 | 新数据集、来源等价关系、共享表结构 |
| 页面 | `console/services/automation_catalog_projection.py`、`module_data_sources.py` 与对应模板/脚本 | 常规参数修改不改页面；专属设置随插件包 | 通用设置桥、模块目录、主导航 |

实际局部测试/打包入口见 [`agent/docs/plugin_maintenance.md`](../agent/docs/plugin_maintenance.md)。工具根据明确插件 ID 选已有测试与打包器；相对基线发现共享代码、运行器或其他包变更时拒绝宣称插件兼容更新。测试签名只供隔离演练，不作为生产信任凭据。

## 产品入口与契约

功能插件仍在 `/automations`。财务采集在 `/modules/finance/data-sources`，客服采集在 `/modules/customer-service/data-sources`，两者复用原安装、配置、启停、计划、执行和升级服务。业务来源查询直接读本地数据库，采集器停用或卸载不删除固定财务/客服页面。

Manifest 可选 `management`，字段精确为 `purpose/module/dataset/version`。功能为 `action/automation/空/空`；财务为 `collector/finance/finance.transactions/1`；客服为 `collector/customer_service/customer_service.problems/1`。旧已签名清单不改字节；仅对已确认的两个旧采集 plugin_id 作固定归属映射。新包归属在上传检查及安装前验证；升级不能静默改变模块或数据契约。

没有 Harness/AI contribution 的合法插件可安装。只有账号角色时使用宿主默认账号页，无账号无需填额外参数；平铺的字符串、数字、布尔值与枚举使用简单设置，签名采集器内部字段不交给用户编辑；需要复杂必填业务配置或资源选择时由插件提供专属设置页面。专属页面保持 sandbox、CSP、会话绑定及 Host 桥权限，不能获得原始凭据。保存配置保留当前计划和明确关闭的入口。历史恢复只接受同清单且曾成功应用的已保存代际，经现有配置 CAS 与验证重新提交；不能恢复任意历史 JSON。

已有实例的资源引用完整时，简单设置可修改账号和常用标量参数并保留这些引用；保存仍重新验证当前资源与账号。安装时缺少必填资源，或者存在必须填写的复杂结构，仍要求对应专属设置。预览指纹、候选集合等宿主管理字段不能通过简单设置编辑。

来源身份由外部系统的真实组织/网点证据与数据集建立，显示名、账号 ID、凭据和采集实例均不作为等价证明。更换生产者要求同数据契约和已验证账号来源别名，原生产者先停止并无活动/未知写租约，再通过来源 revision CAS 接续；迟到结果按旧代际和 revision 拒绝。未验证组织身份、歧义或缺字段都明确失败。升级插件不得绕过这项约束。

## 查询和执行约束

财务按来源和业务日期发布分区：批次已终结后，其中成功或明确无数据的分区可见，包括部分来源失败批次中的已完成分区；未终结的半批不能覆盖此前已发布分区。失败分区保留此前已发布历史，同时显示失败状态及数据更新时点差异，不能把它称为本次完整成功。客服仅公开已发布的本地明细。人工字段与业务历史归业务模块所有。迁移 040 将实际持久化代际的插件、版本、包与清单摘要写入来源发布溯源，卸载后仍保留；旧记录没有这项证据时明确为版本未知。旧记录迁移采用已经保存的真实来源证据，映射不明确的项目必须留诊断，不能填默认站点。

目录按模块先查询项目身份，再验证各实例；同一请求复用只读事务，权限范围和模块均纳入 Console 缓存键。并发刷新合并，配置/卸载使旧响应不可重新填充缓存。账号候选和外部资源探测在设置需要时读取，首屏不全量探测。

目录内的完整定时合同与活动迁移关系按本模块实例批量读取，复用原单项读取的 SQL 与校验；缓存只在本次目录读取事务中有效。执行、加锁、配置修改和迁移切换继续读取当前权威数据，不能使用页面读缓存。对应真实 MySQL 回归见 `tests/test_automation_plugin_catalog_batch.py`。

插件由 Invocation 直接执行，主系统不启用旧 Runner 领取。写目标有界协调只发生在本次存活调用中，不生成持久等待队列；取消不能在线程尚在执行时假报零副作用。通知或展示失败不重新执行业务写。

## 本轮复现与发布准备

整轮入口为 `python agent/scripts/accept_low_maintenance_v32.py`，必须配合入口要求的隔离测试环境；它实际运行检查并产生机器结果，必需项失败、受阻或未运行均非零。真实浏览器入口位于 `tests/v32_acceptance/`。测试环境和测试签名包不得用于生产。具体命令及首次核心发布、插件回退顺序见 [复现与发布说明](low_maintenance_v32_release.md)。

首次上线属于核心更新：备份应用与业务库，按当前迁移机制执行增量迁移，审阅旧实例/来源映射，排空在途任务并保留未知写核验记录，安装匹配版本核心与插件，最后按已有发布流程校验。生产账号、真实定时及外部写入需要另一次明确发布授权。本任务只准备说明与工件。

插件回退沿用现有升级入口上传原签名 ZIP，只允许本实例曾实际提交、已排空的历史版本。宿主核对历史快照与目标包的版本、包摘要、清单摘要、信任来源和运行模型，重新验证当前配置、账号、资源、计划与调用契约，再经原 CAS 和代际切换；未提交的准备版本、其他实例的历史、坏摘要或不相容配置均明确拒绝。审计记录标注 rollback，不以改旧代码再发布更高版本冒充回退。配置通过同版本历史恢复，计划保持独立。核心回滚使用发布备份与兼容应用版本，新增业务数据保留，不删除新表或回滚后手工覆盖账本。存在未知写、迁移不兼容或共享协议变化时停止插件级回滚，按核心发布流程处理。

本轮交付后停止。自然语言查数、飞书知识库、新 Skill/智能运行时、流程编辑器和自动修复平台不在本轮实施范围。
