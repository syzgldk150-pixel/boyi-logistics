# 架构 V1 与原 V3.2 验收映射

原 A/B/C 50 组与 M01–M06 的要求和阈值仍以 `docs/low_maintenance_v32_acceptance.json` 为准；本映射不修改矩阵、不减 minimum_cases、不将 SKIP 记为通过。以下映射是代码现状核对，不表示这些组已经重新通过。

## 唯一完整驱动

先准备 Linux Python 3.10 虚拟环境（同时满足 Agent/Console 两份锁文件）、Bubblewrap、prlimit、Chromium，以及仅监听本机的独立 MySQL 8 测试实例（UTC 时区）。下例端口和空密码仅对应一次性测试实例，不得指向已有业务数据库。`TASK_PYTHON` 应为已激活测试虚拟环境里的 Python；沙箱需要实际可执行文件，不能使用指向环境外解释器的符号链接。

在仓库根目录定义隔离命令入口，后续局部测试也复用它：

```bash
TASK_PYTHON="$(command -v python)"
# 填写测试环境实际安装的 Chromium 可执行文件。
TASK_CHROMIUM=/absolute/path/to/chromium
mkdir -p .t/tmp .t/home .task_tmp/v1-report
isolated() {
  env -i HOME="$PWD/.t/home" TMPDIR="$PWD/.t/tmp" LANG=C.UTF-8 TZ=UTC \
    PATH="$(dirname "$TASK_PYTHON"):/usr/bin:/usr/local/bin:/bin" \
    PYTHONPATH="$PWD/agent:$PWD" PYTHON_DOTENV_DISABLED=1 \
    RUN_MYSQL_INTEGRATION=1 AGENT_DB_HOST=127.0.0.1 AGENT_DB_PORT=33330 \
    AGENT_DB_USER=root AGENT_DB_PASS='' AGENT_DB_NAME=architecture_test \
    MIGRATION_ENV_FILE=/dev/null V32_CHROMIUM_EXECUTABLE="$TASK_CHROMIUM" \
    PYTEST_ADDOPTS=--basetemp=.t/f "$@"
}
FREEZE="$PWD/.task_tmp/v1-report/host-freeze.json"
OUTPUT="$PWD/.task_tmp/v1-report/acceptance"
```

`.t/f` 使用短路径是为了满足 Linux 本地通信套接字的路径长度限制。每个完整验收应独占这些测试目录和测试库；不能并行启动另一个完整验收。

```bash
isolated "$TASK_PYTHON" -m tests.v32_acceptance.host_freeze freeze "$FREEZE"
isolated "$TASK_PYTHON" agent/scripts/accept_low_maintenance_v32.py --phase all --host-freeze "$FREEZE" --output-dir "$OUTPUT"
```

必须使用明确的隔离 MySQL 8 测试环境、Python 3.10 和锁文件。完整驱动自行设置每个 probe 的专用测试库；运行前停止占用同名测试库的其它验收。`--phase ci` 只是诊断切片，不能作为完整验收 PASS。冻结入口要求宿主源码已提交且无未提交宿主变更，不能在本轮仍修改宿主时伪造冻结清单。

## 新架构验收入口

`tests.v32_acceptance.daily_stats` / `daily_scan` 和财务、客服及维护演练现使用 `DirectFixture`：真实 MySQL、签名插件、隔离子进程和宿主 Broker，结果以本次 `invocation_id` 核验。A01/A02/A08/A09/A10/A11/A12/A13/A19 的业务与阈值继续适用；原 A03/A07 的持久领取、租约等待模型只保留历史回归，新短插件另外验证立即受理或明确繁忙拒绝、无历史积压、精确取消和迟到写保护，见 `tests/test_direct_plugin_invocation_mysql.py`、`tests/test_direct_daily_plugins_mysql.py`。历史 Runner 用例通过不能单独证明新架构通过。

财务/客服结果导航按当前 Invocation 的实际来源证明生成。M02/M03 必须在最后核心更新提交并冻结之后运行真实插件字段/判断变化、升级、回退与无关任务持续推进；旧工件和 PREPARATION 不计入本轮通过。

## 每组原始驱动映射

`pytest` 列列出矩阵中的原始测试文件；精确函数引用与完整需求直接见机器矩阵，避免维护第二套条件。`probe` 对应下一节实际命令。全部适用组由上面的完整驱动一次执行。

| 组 | 场景 | pytest 文件 | probe |
|---|---|---|---|
| A01 | 四个核心业务分别从实际入口到真实业务函数执行 | `tests/test_first_party_action_payloads.py`<br>`tests/test_problem_plugin_production_adapter.py` | daily_scan_statistics, daily_problems |
| A02 | 四个独立业务任务共同运行，另一个注入失败 | `tests/test_workflow_runner_durable_admission.py` | daily_concurrency |
| A03 | 4 个工作槽，1 个任务占资源，至少 4 个不同任务等待该资源，再提交独立任务 | `tests/test_workflow_runner_durable_admission.py` |  |
| A04 | 同账号不同资源，跨插件／角色／凭据访问同一资源 | `tests/test_workflow_runner_durable_admission.py` |  |
| A05 | 整表覆盖与表内写，多个资源反向申请 | `tests/test_workflow_runner_durable_admission.py` |  |
| A06 | 并发使用登录态、临时目录、环境与输出 | `agent/tests/test_session_process_isolation.py`<br>`agent/tests/test_yunda_proxy_session_state.py`<br>`tests/test_plugin_runtime_isolation_mysql.py`<br>`tests/test_session_boundaries.py`<br>`tests/test_workflow_runner_durable_admission.py` |  |
| A07 | 等待跨至少两个租约周期，旧领取与恢复领取竞争 | `tests/test_workflow_runner_durable_admission.py` |  |
| A08 | 等待取消、运行取消、租约丢失 | `agent/tests/test_tool_executor_cancel.py`<br>`tests/test_workflow_runner_cancellation.py`<br>`tests/test_workflow_runner_durable_admission.py` | scan_cancel_recovery |
| A09 | 同一 request_id 并发重发至少 20 次 | `tests/test_workflow_runner_durable_admission.py` |  |
| A10 | 已写响应丢失、确实未写、结果未知 | `agent/tests/test_scan_unknown_write_recovery.py`<br>`tests/test_result_verifier.py`<br>`tests/test_scan_readback_protocol.py`<br>`tests/test_scan_snapshot_recovery_mysql.py`<br>`tests/test_workflow_runner_recovery.py` | scan_recovery, unknown_resource_scope, daily_concurrency |
| A11 | 超时／重试与取消本次运行 | `tests/test_mysql_orchestration_integration.py`<br>`tests/test_scheduled_task_contracts.py`<br>`tests/test_scheduler_runner_lifecycle_mysql.py`<br>`tests/test_workflow_runner_scheduler_supersession.py` |  |
| A12 | 预览、排除、候选选择、确认、过期或配置变化 | `agent/tests/test_feishu_split_selection.py`<br>`tests/test_scan_preview_binding.py`<br>`tests/test_selection_preview_binding.py`<br>`tests/test_workflow_runner_policy_recheck.py` | daily_scan_statistics, daily_problems |
| A13 | 后台固定按钮、飞书固定指令和已开放定时 | `agent/tests/test_feishu_automation_project_entrypoints.py`<br>`tests/test_automation_project_api.py`<br>`tests/test_automation_project_entrypoints.py`<br>`tests/test_control_plane_execution_boundaries.py`<br>`tests/test_mysql_orchestration_integration.py`<br>`tests/test_scheduled_task_contracts.py` |  |
| A14 | 飞书资源／Worker／全量账号探测延迟 30 秒或失败 | `agent/tests/test_feishu_resource_catalog.py` | browser_performance |
| A15 | 单个坏实例、重复 ID、局部依赖故障 | `tests/test_automation_plugin_catalog_batch.py`<br>`tests/test_automation_plugin_platform.py`<br>`tests/test_automation_project_policy_service.py` | customer_collection_flow |
| A16 | 无可信首次目录、并发刷新、卸载与刷新竞态 | `console/tests/test_plugin_catalog_singleflight.py`<br>`tests/test_automation_plugin_catalog_batch.py` | catalog_delivery |
| A17 | 20 次已打开标签切换，再关闭标签／设置页 | `console/tests/test_control_plane_ui.py` | navigation_probe |
| A18 | 页面旧快照后项目被停用、权限或绑定变化 | `console/tests/test_plugin_catalog_singleflight.py`<br>`tests/test_automation_project_authorization.py`<br>`tests/test_automation_project_policy_service.py` | customer_collection_flow |
| A19 | 对单个同步查询注入慢响应，普通执行池饱和 | `tests/test_automation_project_api.py`<br>`tests/test_workflow_runner_durable_admission.py` | catalog_delivery |
| A20 | 真实 MySQL 8 领取／事务／唯一约束／必要迁移 | `tests/test_mysql_orchestration_integration.py` |  |
| A21 | 插件安装、设置、启停、定时、升级和多实例 | `tests/test_automation_plugin_lifecycle.py`<br>`tests/test_automation_project_policy_service.py`<br>`tests/test_plugin_service_v2_foundation.py`<br>`tests/test_service_v2_policy_runtime.py` | navigation_probe, run_acceptance, settings_effective, customer_collection_flow, field_maintenance |
| A22 | 录单／OCR、运单、跟踪、回单、客服、财务、登录与导航 | `agent/tests/test_session_broker.py`<br>`console/tests/test_admin_auth.py`<br>`console/tests/test_customer_service_module.py`<br>`console/tests/test_document_mode_switch.py`<br>`console/tests/test_finance_module.py`<br>`console/tests/test_manual_waybill.py`<br>`console/tests/test_menu_registration.py`<br>`console/tests/test_mobile_navigation.py`<br>`console/tests/test_receipts_management.py`<br>`console/tests/test_tracking_query.py`<br>`console/tests/test_waybill_entry_extensions.py`<br>`console/tests/test_waybill_query.py` | navigation_probe, legacy_page_smoke |
| A23 | 通知失败、可重建展示更新失败 | `agent/tests/test_outbox_dispatcher.py`<br>`tests/test_feishu_readback_delayed.py` | daily_concurrency |
| A24 | 扫描→统计等真实数据依赖 | `tests/test_sync_arrival_stats_v1_v2_parity.py`<br>`tests/test_sync_scan_codes_v1_v2_parity.py` | daily_scan_statistics, daily_concurrency |
| B01 | 采集插件、AI 或外部资源目录不可用，业务 DB 正常 | `console/tests/test_finance_module.py`<br>`tests/test_module_data_sources_mysql.py` | detail_browser, customer_collection_flow |
| B02 | 财务采集适配器的外部字段／响应变化 | `tests/test_finance_raw_plugin_protocol.py` |  |
| B03 | 隔离环境停用、卸载、重装某采集实例 | `tests/test_module_data_sources_mysql.py` | customer_collection_flow |
| B04 | 合法插件没有 AI 声明，模型未配置或 API 超时 | `tests/test_automation_plugin_manifest_contract.py`<br>`tests/test_service_v2_harness_contribution_contract.py` | run_acceptance, daily_scan_statistics, daily_problems, daily_concurrency |
| B05 | 同一业务数据分别由允许的不同入口重复提交 | `tests/test_finance_validation.py`<br>`tests/test_module_data_sources_mysql.py` | customer_collection_flow |
| B06 | 财务采集写入半批后中断，并重新提交 | `tests/test_finance_raw_plugin_protocol.py`<br>`tests/test_module_data_sources_mysql.py` |  |
| B07 | 本次触及模块的依赖与导入边界 | `tests/test_automation_plugin_catalog_batch.py`<br>`tests/test_control_plane_execution_boundaries.py` |  |
| B08 | 停用某插件后保留固定业务、其他独立任务及既有管理功能 | `tests/test_automation_plugin_lifecycle.py` | customer_collection_flow |
| C01 | 自动化／财务／客服归属 | `tests/test_automation_plugin_platform.py`<br>`tests/test_plugin_module_management.py` | module_scope |
| C02 | 所属模块内安装／实例操作 | `tests/test_action_v1_module_installation.py`<br>`tests/test_automation_plugin_lifecycle.py`<br>`tests/test_plugin_module_management.py` | customer_collection_flow, field_maintenance |
| C03 | 默认设置页 | `tests/test_automation_plugin_code_owned_fields.py`<br>`tests/test_automation_plugin_manifest_contract.py`<br>`tests/test_plugin_module_management.py` | navigation_probe, settings_effective, customer_collection_flow |
| C04 | 专属设置与保存 | `console/tests/test_plugin_settings_initialization.py`<br>`tests/test_automation_plugin_management_api.py`<br>`tests/test_automation_project_policy_service.py`<br>`tests/test_plugin_module_management.py` | custom_settings |
| C05 | Agent 账号复用 | `agent/tests/test_session_broker.py`<br>`agent/tests/test_session_process_isolation.py`<br>`agent/tests/test_yunda_proxy_session_state.py`<br>`tests/test_automation_project_entrypoints.py`<br>`tests/test_module_data_sources_mysql.py` | customer_collection_flow, settings_effective |
| C06 | 财务完整用户路径 | `agent/tests/test_finance_sync_service.py`<br>`tests/test_module_data_sources_mysql.py` | field_maintenance, finance_source_state |
| C07 | 客服完整用户路径 | `agent/tests/test_customer_service_problem_sync_tool.py`<br>`tests/test_module_data_sources_mysql.py` | customer_collection_flow |
| C08 | 动态来源注册和历史筛选 | `tests/test_module_data_sources_mysql.py` | navigation_probe, detail_browser, customer_collection_flow |
| C09 | BI 绑定与数据状态 | `console/tests/test_finance_service.py`<br>`tests/test_module_data_sources_mysql.py` | detail_browser, finance_source_state |
| C10 | 最后实例卸载和多实例 | `tests/test_lifecycle_disposal_order.py`<br>`tests/test_module_data_sources_mysql.py` | customer_collection_flow |
| C11 | 替换采集器接续同来源 | `tests/test_module_data_sources_mysql.py` |  |
| C12 | 多来源、账号与重复业务 | `tests/test_module_data_sources_mysql.py`<br>`tests/test_mysql_orchestration_integration.py` |  |
| C13 | 旧实例与引用迁移 | `tests/test_module_data_sources_mysql.py`<br>`tests/test_mysql_orchestration_integration.py` | customer_collection_flow, navigation_probe |
| C14 | 采集与消费解耦 | `tests/test_module_data_sources_mysql.py` | detail_browser, customer_collection_flow |
| C15 | 分模块轻量列表 | `console/tests/test_plugin_catalog_singleflight.py`<br>`tests/test_automation_plugin_catalog_batch.py`<br>`tests/test_automation_plugin_platform.py`<br>`tests/test_plugin_module_management.py` | browser_performance, catalog_delivery |
| C16 | 计划、结果与导航 | `agent/tests/test_feishu_automation_project_entrypoints.py`<br>`agent/tests/test_finance_alert_policy.py`<br>`console/tests/test_automation_control_plane_cutover.py`<br>`tests/test_collector_navigation.py`<br>`tests/test_scheduled_task_contracts.py` | navigation_probe, run_acceptance, finance_source_state, customer_collection_flow, field_maintenance |
| C17 | 固定模块与操作范围 | `console/tests/test_customer_service_module.py`<br>`console/tests/test_document_mode_switch.py`<br>`console/tests/test_finance_module.py`<br>`console/tests/test_manual_waybill.py`<br>`console/tests/test_menu_registration.py`<br>`console/tests/test_permission_registry.py`<br>`console/tests/test_receipts_management.py`<br>`console/tests/test_tracking_query.py`<br>`console/tests/test_waybill_query.py`<br>`tests/test_control_plane_execution_boundaries.py`<br>`tests/test_plugin_module_management.py` | customer_collection_flow |
| C18 | 部分发布、增量游标与迟到更新 | `tests/test_finance_raw_plugin_protocol.py`<br>`tests/test_first_party_action_payloads.py`<br>`tests/test_module_data_sources_mysql.py` |  |
| M01 | 常用配置的局部修改与恢复 | `tests/test_automation_project_policy_service.py` | settings_effective |
| M02 | 一个已接入采集器的字段／解析变化 |  | field_maintenance |
| M03 | 一个真实业务的局部决策变化 |  | decision_maintenance |
| M04 | 升级失败、在途版本和回退 | `tests/test_automation_plugin_lifecycle.py` | field_maintenance, decision_maintenance |
| M05 | 独立更新的真实边界与依赖 | `tests/test_action_v1_module_installation.py`<br>`tests/test_plugin_maintenance.py`<br>`tests/test_plugin_service_v2_foundation.py`<br>`tests/test_service_v2_developer_cli.py`<br>`tests/test_service_v2_developer_simulator.py` | plugin_cli_batch, field_maintenance |
| M06 | Codex 局部维护交付与可复现测试 | `tests/test_low_maintenance_v32_acceptance.py`<br>`tests/test_scan_preview_observation.py`<br>`tests/test_service_v2_developer_cli.py`<br>`tests/test_v32_e2e_fixture_reset.py` | plugin_cli_batch, field_maintenance, decision_maintenance |

## Probe 实际命令

以下命令使用完整驱动相同的模块与参数；单独执行时还需使用该驱动创建的隔离配置和数据库准备。推荐运行完整驱动，避免漏掉 fixture、fresh artifact 及性能检查。

| probe | 测试库 | 命令 |
|---|---|---|
| daily_scan_statistics | `v32_a01_test` | `python -m tests.v32_acceptance.daily_stats ` |
| daily_problems | `v32_a01_problem_test` | `python -m tests.v32_acceptance.daily_problems ` |
| daily_concurrency | `v32_a02_test` | `python -m tests.v32_acceptance.daily_concurrency ` |
| settings_effective | `v32_m01_test` | `python -m tests.v32_acceptance.settings_effective ` |
| custom_settings | `v32_c04_test` | `python -m tests.v32_acceptance.custom_settings ` |
| catalog_delivery | `v32_catalog_delivery_test` | `python -m tests.v32_acceptance.catalog_delivery ` |
| scan_recovery | `v32_recovery_test` | `python -m tests.v32_acceptance.scan_recovery ` |
| unknown_resource_scope | `v32_recovery_test` | `python -m tests.v32_acceptance.scan_recovery --physical-scope` |
| scan_cancel_recovery | `v32_scan_cancel_test` | `python -m tests.v32_acceptance.scan_recovery --cancel-only` |
| customer_collection_flow | `v32_customer_flow_test` | `python -m tests.v32_acceptance.customer_collection_flow --reset-owned-fixture` |
| finance_source_state | `v32_m02_test` | `python -m tests.v32_acceptance.finance_source_state --reset-owned-fixture` |
| field_maintenance | `v32_m02_test` | `python -m tests.v32_acceptance.finance_maintenance --reset-owned-fixture --host-freeze "$FREEZE"` |
| decision_maintenance | `v32_m03_test` | `python -m tests.v32_acceptance.decision_maintenance --prepare --host-freeze "$FREEZE"` |
| plugin_cli_batch | `v32_cli_test` | `python -m tests.v32_acceptance.plugin_cli_batch --report "$OUTPUT/plugin-cli-summary.json" --host-freeze "$FREEZE"` |

浏览器 probe 统一使用 `v32_e2e_test`。驱动依次执行 `prepare_database`、`management_fixture`、`data_fixture`，随后运行 `browser_performance --scope-only`、`navigation_probe`、`legacy_page_smoke`、`detail_browser`、`browser_performance` 与 `run_acceptance`。保留三类外部依赖延迟、冷首屏、重复标签切换、取消响应等原阈值。

## 本轮 P4 与到货报表适用切片

新增 `tests/test_waybill_source_coverage*.py` 验证来源身份、半批不发布、精确 upsert 和日尾段，对应 C12/C18 的数据正确性要求，并补充本 V1 寄件查询需求；`console/tests/test_waybill_query.py` 对应 C17。`tests/test_arrival_report_ownership*.py` 与实际签名包业务链验证 A01/A10 中统计报表所有权和未知写；不替代这些整组其它子项。

M02 字段变化入口为 `tests.v32_acceptance.finance_maintenance`；M03 判断变化入口为 `tests.v32_acceptance.decision_maintenance`。两者的正式结论以完整驱动本次生成的报告为准，准备性运行不能代替宿主冻结后的演练。

P4 真实运行命令与证据见 `docs/direct_waybill_query.md`。已完成两平台当前原页查询范围、寄件日期字段与唯一父单详情的只读核验；仅声明当前查询范围，未声明全组织完整或生产 ECS 运行通过。整体验收结果必须由根任务汇总当前源码、当前 JUnit 和本次 fresh probe 后生成。
