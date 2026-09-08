# 扫描未知写恢复与数据保留

`sync_scan_codes` 的原扫描算法和确认预览保持不变。历史核验仅由显式人工入口调用，启动、登录恢复与新预览不再调用旧 Run 恢复。核心恢复链只读取原 Command 的已验证预览、原 generation 账号、原写尝试 receipt，以及外部独立发件扫描账本。

迁移 `041_scan_write_recovery_snapshot.sql` 为扫描快照写 receipt 增加封闭的目标日期与原规范行 journal，随原 receipt 的审计生命周期保留。它不保存账号凭据、页面内容或整个外部响应。只有已授权 `sync_scan_codes` 的精确 `scan.snapshot.replace` 调用可以写入，参数摘要必须与原 receipt 相等。

日期级 `automation_scan_snapshot_heads` 使用 `snapshot_date` 主键，记录投影内容摘要、原 Run/lease 和单调 revision。正常替换与恢复都先锁同一日期行。恢复只在原所有者仍匹配时重建缺失投影；若新任务已经发布不同事实，返回 `SCAN_PROJECTION_SUPERSEDED`。新任务已经发布完全相同规范数据时只验证现状，不改写数据或所有者。

外部只读核验必须精确覆盖原预览全部目标和原写时间范围，重复、部分、范围不明或不可达均保持 UNKNOWN。APPLIED 在同一 MySQL 事务补齐原投影、原结果和 lease/receipt 状态，外部写不再执行。NOT_APPLIED 依原 Step 的 retry_safe 合同处理；扫描原合同不可自动重试时结束原任务，再由新预览与确认创建新请求。已取消任务保持取消，但可补齐实际已经发生的写及核验证据。

原 Run、Step、generation、预览摘要和每批参数摘要必须一致。缺少 journal 的历史未知写不能推测 APPLIED 的完整结果，核验继续返回 UNKNOWN；它不阻止新请求独立执行。其独立未写证明仍可按既有合同处理。

041 同时将 Runner 原准入取得的规范资源锁键随现有写 receipt 保存。该值由宿主 ContextVar 经实际执行线程进入 Broker grant，插件不能自行填写。新的写动作只与仍具有效活动执行租约的原写 scope 比较；相冲突的自动化直接失败为 `EXECUTION_RESOURCE_BUSY`，不留积压，不冲突的工作继续。已取消或停止任务的 UNKNOWN 原样保留而不形成新任务锁。有效执行上的范围缺失或损坏仍明确失败。完整执行规则见 [历史记录与新任务](historical_write_recovery.md)。

该变化涉及共享数据库与核心恢复器，属于核心更新。升级先执行 041 再启动新核心；回退旧核心保留兼容新增列和日期表，不删除业务历史。018 隔离恢复删除原 receipt 表时将 041 标记为重新应用，日期所有者表独立保留，不添加会阻止保留历史的外键。

UNKNOWN 检索使用 041 的 `(outcome, receipt_id)` 索引，只读取有界的待核验写记录；该索引与两个新增列可以重复应用。核心实现分别在 `shared/scan_snapshot_recovery.py`（原投影与结果）、`shared/execution_resource_journal.py`（原资源键）、`agent/agent/automation_plugins/scan_recovery_context.py`（原代际账号），继续复用既有 receipt 和资源冲突比较规则。

隔离复现：`tests/v32_acceptance/isolated_environment.sh run --database v32_recovery_test python -m tests.v32_acceptance.scan_recovery`；日期所有者竞争和幂等使用 `tests/test_scan_snapshot_recovery_mysql.py` 的真实 MySQL 用例。附加 `--physical-scope` 验证实际原 receipt 上的跨 Runner 生产资源准入，后半使用受控 metadata 和注册本地工作负载，不能称为第二套已装插件完整业务；四个已装插件共享资源完整链由 `daily_concurrency` 复用验证。取消恢复入口使用 `--database v32_scan_cancel_test` 和 `--cancel-only`。所有数据库和外部 HTTP 端点均是测试拥有的隔离资源。
