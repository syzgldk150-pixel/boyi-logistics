# 历史未知写入与新任务

本次修复属于核心更新，涉及 Runner、共享仓储、Console 和迁移 `042`。兼容插件的业务代码与现有日常脚本入口保持原有维护归属。

| 维护内容 | 唯一实现位置 | 更新范围 |
|---|---|---|
| 新任务互斥与中断读取恢复 | `automation_run_supersession.py`、`automation_run_lookup.py`、`workflow_runner.py` | 核心 |
| 原始写入范围与历史隔离 | `execution_resource_journal.py`、迁移 `042` | 核心和共享数据库 |
| 精确证据核验与代际闭合 | `automation_unknown_write_recovery.py`、`automation_plugin_generation_unknown_write_repository.py` | 核心 |
| 核验后是否唤醒执行 | `production_write_recovery.py` | 核心 |
| 事项历史记录与操作界面 | `automation_write_recovery_views.py`、Console 控制平面服务和 `unknown_write_recovery.js` | 核心和 Console |

这些规则不移入业务插件配置；业务字段与局部判断仍按原插件维护入口单独测试和打包。

## 执行规则

- 新 Command 的项目互斥只依据待领取、有效执行租约、真实运行步骤或待自动重试。已停止的历史未知写保留原 Run、Step、receipt、lease、Evidence 和事项，不作为整个项目的永久互斥锁。原 Run 不会被重新领取或伪装为成功。
- 进入写步骤时，仍按回执保存的原始资源范围检查冲突。有可靠原范围的未知写继续阻止实际冲突；不同范围可以执行。
- `042` 只标记迁移 `041` 引入原始范围日志前已停止、原始范围为 SQL NULL 或空数组、且没有有效执行租约的未知回执。固定边界取已有 `schema_migrations[041].applied_at`，与 Run/回执创建时间同属数据库本地时标，不按“过了多久”或当前会话时区动态豁免；执行租约仍按 UTC 判断。`legacy_scope_quarantined_at` 表示历史范围缺失待核验，不表示写入成功、未发生或可以重放。运行时写入入口不能设置该标记。
- 新的缺范围回执、损坏范围和仍在执行的记录继续明确报错。已标记回执若后来补回有效原范围，会重新进入冲突检查。回执按稳定主键分页读取，历史数量增加不会触发全局数量上限错误。
- Runner 重新领取中断任务后，在重新建立上下文或等待审批前闭合中断的 read/compute 步骤；写步骤继续走原有未知写核验。有效的其他 Worker 领取不会被覆盖。

## 人工查看与核验

事项详情按具体 Run 与 lease 展示历史记录。支持核验的插件可由超级管理员检查已保存的服务端证据；不支持的记录显示原因。浏览器不能提供核验结论、原始资源、外部账号或证据。

事项中的人工入口使用 `resume_run=False`，Agent 在事务内重新验证事项、Run、项目、代际和 lease 的精确关联。只有原有证明规则确认 APPLIED / NOT_APPLIED 时才闭合该 lease 并写审计；原 Run 和 Step 保持停止，不投递执行消息、不唤醒 Runner。证据不足继续 UNKNOWN。本入口不代表每个插件都已实现第三方系统的现场回读。

现有 `GET /control-plane/work-items/{work_item_id}` 的 `unknown_write_recoveries[].write_attempts` 提供该 lease 的只读回执诊断：精确回执 ID、已记录的操作与 action、结果状态、记录数量和创建／更新时间，以及原始范围的类别与缺失、损坏、历史隔离标记。创建／更新时间不是写后验证时间；没有保存的字段不推断补齐。查询必须同时匹配该事项已关联的 lease、项目、代际与 Run，每个事项最多返回 1000 条回执，超过上限明确失败。

该诊断不返回账号、资源键原值、定位器、请求参数或业务行，也不改动回执、锁或任务状态。`RESOURCE_WAIT` 的 `error_summary` 能区分通道占用与未知写冲突；回执诊断用于查明具体操作，不能只凭插件名或某条 lease 的历史隔离标记断言其是否阻塞当前任务。页面中的“检查已保存证据”仍沿用上述证明规则。

已归档代际的最后一条未知写闭合后，仅其 `BLOCKED/WRITE_OUTCOME_UNKNOWN` 状态进入既有 DRAINING 清理流程，不恢复旧项目路由。其他独立错误保持原样。未知证据仍须保留，卸载和数据保留规则不因此绕过。

## 隔离复现

使用本项目既有 MySQL 8 测试环境，确认 `PYTHON_DOTENV_DISABLED=1`、`RUN_MYSQL_INTEGRATION=1` 和 `AGENT_DB_*` 指向合成测试库。不得连接生产库执行这些测试。

```sh
PYTHONPATH=agent:. pytest -q \
  tests/test_legacy_unknown_scope_migration_mysql.py \
  tests/test_workflow_runner_read_recovery_mysql.py \
  tests/test_manual_unknown_write_recovery.py \
  tests/test_automation_write_recovery_views.py \
  tests/test_automation_unknown_write_recovery_transaction.py \
  tests/test_automation_project_policy_service.py
```

迁移和 Runner 集成用例使用独立 UUID 测试库；实际运行注册工具并验证 SQL 写入结果，核对旧记录未被重放或改写。该 SQL 测试用于本次执行控制修复，不代替四个业务脚本或整轮 V3.2 的原有验收。

## 发布与回退

按 [ECS 发布流程](../agent/deploy/publish_to_ecs.md) 使用通过 CI 的最终 Git SHA 和对应签名包索引，执行 shared/migration 更新。先保存生产一致性备份，在隔离副本上验证 `042` 幂等、原回执/Run/Step 不变和范围门禁；副本只用于备份与演练，不上传替换生产数据库。

`042` 是增加可空列和标记历史记录，回退代码时保留该列、迁移记录和所有原证据。旧应用可以忽略新增列，但会恢复旧的全局阻塞行为。不得删除未知回执、修改成功状态、删除迁移校验和或用旧数据库覆盖发布后业务。保留标准发布器生成的本次精确回滚材料，按其中恢复流程排空运行后回退代码与服务。
