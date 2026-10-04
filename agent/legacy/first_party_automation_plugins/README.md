---
module: automation-plugin-platform
type: historical-source-guide
status: historical
updated: 2026-09-15
---

# V1 离线迁移回归材料

此目录不进入 ECS 发布清单。仅保留 V1 传输、已退役打卡/每日应签适配器、旧包摘要和迁移矩阵，供显式离线回归使用。矩阵描述历史迁移基线，不是当前运行状态。

仍复用的动作源码仅在 `agent/service_v2_plugins/<plugin_id>_v2/payload/` 维护，离线构建器通过 `agent/agent/automation_plugins/first_party_sources.py` 精确定位，不把此目录加入运行时导入路径。

分批1.0.26为复现不可修改的旧包摘要，使用`split_pending_problem_upload/frozen/`内的`daily_sign_rules.py`和`daily_sign_values.py`原始字节，取自提交`08a585169e18551647e0e07971b59c90db31fd7f`。该快照只供显式V1离线构建，不进入V2包或ECS发布，也不作为当前算法的回退。现行每日应签到货计算继续仅在V2源码维护；旧摘要`digests.json`保持不变。

当前开发和打包入口见仓库根 `agent/service_v2_plugins/README.md`。线上仅接受经过退役索引和完整迁移记录验证的 Service-V2-only 发布。
