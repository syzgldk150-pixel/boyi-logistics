# 旧执行记录退役：迁移 046

本迁移在独立插件架构切换时结束旧的待领取、待审批和等待恢复记录。迁移不会启动脚本，也不会把旧输入转换成新 Invocation。以后执行必须由用户、已授权关键词或正常定时入口发起一次新调用。

## 发布顺序

1. 按项目现有发布流程备份数据库与当前发布目录，启用发布暂停入口，并等待正在进行的查询、插件调用和后台写入实际结束。
2. 确认旧 Agent/Console 进程已停止领取和执行工作。暂停入口本身不能证明旧版进程已经停止；不得只把过期租约当作“已停止”。
3. 使用发布包自带的 `agent/scripts/run_migrations.py` 执行正常有序迁移；046 无需独立人工 SQL。迁移前后均保留正式迁移日志和数据库备份标识。然后使用迁移器 `--check` 确认迁移记录与校验摘要一致。
4. 启动新版本，核对旧记录的退役原因以及新接口健康状态，最后解除发布暂停。只读验证不会触发业务脚本。

迁移首先检查正在运行或校验的旧 Run/Step、仍被工作进程占用的未结束 Run、有效 Run 租约、处于 RUNNING/VERIFYING 的插件 generation lease，以及新的活动 Invocation。旧 Runner 异常退出步骤时可能已经终结父 Run，却留下原来的 RUNNING/VERIFYING Step；只有父 Run 为 COMPLETED、PARTIAL、FAILED_TERMINAL 或 CANCELLED，同时 finished_at 非空、worker_id 与 lease_expires_at 均为 NULL，才把该 Step 识别为历史记录并原样保留，不纳入活动 Step 计数。缺少终结时间、仍有 worker 或任何非空租约（即使已过期）均继续阻断；没有父 Run 证明时也不放行。此识别依据父 Run 的完整终结事实，不依据记录年龄、错误关键词或部分写回执推断未写，更不能代替发布前实际停进程的检查。全局活动 generation lease 和 Invocation 仍独立阻断。

其余活动事实任一存在均明确失败，不修改任何候选记录。非终态 Run 只要仍有 worker 或非空 lease_expires_at（即使过期），也不能被视为已停止。被识别为历史的 Step 仍保留原状态、错误、结果、核验字段、Evidence 和回执；STARTED/WRITE_OUTCOME_UNKNOWN 不改成已验证，也不会恢复领取或重放。现有发布前 protected-write 检查仍保持独立限制。

实际执行已停止后，普通等待记录终结为 `CANCELLED`；包含原 Run 未知写错误、`STARTED` / `WRITE_OUTCOME_UNKNOWN` 回执，或已尝试但缺少成功/未应用证明的写步骤的记录，终结为 `FAILED_TERMINAL`。这仅结束旧执行，不宣布业务写入成功、撤销或未发生，也不要求先把全部历史未知写查清才切换架构。原 `error_code`、`error_summary`、计划、业务结果、审批、回执、generation lease 和 Evidence 不变；需要人工核验的历史仍可按原事实核验。Work Item 没有 FAILED_TERMINAL 状态，因此以 CANCELLED 关闭执行，并保留原 reason/resolution，不表示业务已撤销。

旧 Runner 已完成写步骤的核验状态为 `VERIFIED` 或 `VERIFIED_AFTER_RECOVERY`。只有步骤同时为 `COMPLETED` 才认可这两种状态；非插件写没有插件回执时也按这项已保存的核验事实处理。后续读取阻塞不会使先前已核验写重新成为未知写。空值、`passed` 等非实际核验状态不作为成功证明；Run 未知写错误或任何未确认回执仍独立使本次退役归为“写入结果未确认”，不能被另一条成功证明覆盖。已完成步骤的结果、核验内容和 Evidence 保留原样；候选中的未终结步骤随父 Run 结束，原核验和错误字段保留。

已经终结的历史未知写保留原样，不阻挡无关等待记录退役。新的退役原因和原状态保存在 `domain_events` 的 `agent.legacy_execution.retired` 事件中，来源为 `migration-046`，并记录 `execution_retired=true`、`write_outcome_unconfirmed` 和具体 `unconfirmed_reasons`：原 Run 未确认错误、未确认写回执、已尝试写缺少核验证明。迁移不为这些审计事件建立 outbox，因此不会形成新队列或发送飞书消息。真实定时与业务表不变。旧 Runner 被禁用且终态 Run 不能领取；回滚门禁禁止恢复旧领取架构，因此不会把未知写历史变成待恢复任务。

正常迁移器只执行一次；在没有新增旧工作时，重新执行 SQL 不会重复退役或重复添加事件。迁移执行到中途失败时，当前事务回滚；修复实际阻断条件后由正常迁移器重试，不手工添加成功标记。

## 代码回滚

退役是执行事实，代码回滚不把已退役记录恢复为等待状态。保留 046 的迁移记录、退役审计、原错误和原回执。发布脚本恢复源码后、启动服务前，使用发布包内原有只读版本兼容检查同时读取 046 标记和恢复源码的实际执行合同：必须在 lifespan 构造并启动 Direct，保留的 Runner 必须明确关闭执行且实现关闭分支。仅放置一个 Direct 文件不能通过。

046 已应用而恢复源码仍属于旧领取架构时，回滚明确返回 `LEGACY_EXECUTION_RETIRED`，服务保持停止，发布暂停文件和恢复材料保留，不能自动清除暂停或重启旧 Runner。此时应继续修复并发布兼容 Direct 的版本；不同 Direct 版本之间的正常代码回滚仍执行原有插件版本和健康检查。不会因回滚恢复旧队列状态。

完整数据库恢复只能按已备份的发布回滚流程操作，并持续保持旧执行器、真实定时与外部业务写入停止。数据库恢复会同时恢复备份时的旧队列，因此不能据此自动重跑历史工作。需要重新执行的业务由用户明确发起新调用。

## 隔离复现

先按 [验收环境与命令入口](architecture_refactor_acceptance_mapping.md) 定义 `isolated`，再运行：

```bash
isolated "$TASK_PYTHON" -m pytest tests/test_legacy_execution_retirement_mysql.py \
    tests/test_retired_execution_rollback.py tests/test_release_boundaries.py -q
```

该测试使用随机命名的独立 MySQL 测试库与真实迁移器，验证普通等待记录取消、未确认写执行失败终结、原错误/回执/定时保留、重复执行幂等和旧队列不能再次领取。真实运行/核验、未释放所有权或租约、活动插件均导致整体原子拒绝，包括无关未知写记录也不能提前退役。多步骤历史场景覆盖普通非插件写的正常核验与恢复核验、后续读取阻塞，以及迁移后原结果和 Evidence 完整保留；已核验步骤旁仍有未知写回执或错误时，保留该未知结论并失败终结。四类已终态父 Run 下的遗留 RUNNING/VERIFYING Step 均验证原样保留，缺少终结或所有权释放证据则验证原子拒绝；终态父 Run 的活动 generation lease 仍不能因历史 Step 排除而获准。此命令不连接 ECS，也不执行真实 TMS 或飞书业务。
