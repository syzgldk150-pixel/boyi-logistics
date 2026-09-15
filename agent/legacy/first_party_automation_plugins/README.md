---
module: automation-plugin-platform
type: historical-source-guide
status: historical
updated: 2026-09-15
---

# V1 离线迁移回归材料

此目录不进入 ECS 发布清单。仅保留 V1 传输、已退役打卡/每日应签适配器、旧包摘要和迁移矩阵，供显式离线回归使用。矩阵描述历史迁移基线，不是当前运行状态。

仍复用的动作源码仅在 `agent/service_v2_plugins/<plugin_id>_v2/payload/` 维护，离线构建器通过 `agent/agent/automation_plugins/first_party_sources.py` 精确定位；不复制算法，不把此目录加入运行时导入路径。

当前开发和打包入口见仓库根 `agent/service_v2_plugins/README.md`。线上仅接受经过退役索引和完整迁移记录验证的 Service-V2-only 发布。
