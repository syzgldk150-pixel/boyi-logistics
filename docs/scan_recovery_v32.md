---
module: 扫描
type: 未知写与数据保留
status: active
updated: 2026-10-29
---

# 扫描未知写与数据保留

`sync_scan_codes_v2` 的扫描先预览、再由发起人确认。正式扫描已开始外部写但结果未证实时，本次 Invocation 终结为 `WRITE_OUTCOME_UNKNOWN`，保留真实写回执与证据，不重放、不自动核验；新的明确触发重新生成预览并确认，创建独立请求，历史 UNKNOWN 不阻止它。

历史 Run 的扫描未知写核验与恢复入口已于 2026-10 随 V1 插件一并删除；已有 UNKNOWN 记录只读保留。

## 仍在使用的 041 结构

迁移 `041_scan_write_recovery_snapshot.sql` 增加的结构继续服务当前扫描投影：

- 日期级 `automation_scan_snapshot_heads`（`snapshot_date` 主键）记录投影内容摘要、写入身份和单调 revision。每次替换先锁同一日期行再写入，由 `shared/scan_snapshot_recovery.py` 的 `lock_snapshot_head` / `record_snapshot_head` 维护；写入身份由宿主经 `scan_snapshot_write_identity` 传入，插件不能自行填写。
- 写回执保存原准入取得的规范资源锁键（`shared/execution_resource_journal.py`）。新的写动作只与仍具有效活动执行租约的原写范围比较；相冲突的自动化直接失败为 `EXECUTION_RESOURCE_BUSY`，不留积压。已取消或停止任务的 UNKNOWN 原样保留，不形成新任务锁。完整规则见 [历史记录与新任务](historical_write_recovery.md)。

升级保留 041 的列、表和索引原字节；回退旧核心保留兼容新增列和日期表，不删除业务历史。

## 隔离复现

`tests/v32_acceptance/isolated_environment.sh run --database v32_recovery_test python -m tests.v32_acceptance.scan_recovery` 验证实际 Direct 扫描的未知写事实、排空取消与不重放；附加 `--physical-scope` 验证原回执上的生产资源准入，`--database v32_scan_cancel_test --cancel-only` 验证取消路径。041 可重复应用由 `tests/test_scan_snapshot_recovery_mysql.py` 覆盖。所有数据库和外部 HTTP 端点均是测试拥有的隔离资源。
