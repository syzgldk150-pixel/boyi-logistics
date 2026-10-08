---
module: 到货统计
type: 插件维护说明
status: active
updated: 2026-10-08
---

# 到货统计插件

当前统计编排在本目录 `payload/action.py` 维护，服务接口由 `manifest.json` 和 `payload/plugin.py` 声明。插件读取到货与扫描事实，计算累计到货数，生成已分类快照和完整表格行；Host 负责已绑定来源的读写、保存与独立新鲜回读，不重新生成分批分类或表格内容。

分批分类唯一源码位于 `../split_pending_problem_upload_v2/payload/split_rules.py`。共享构建器把相同字节装入到货统计与分批两个独立 ZIP；修改该规则必须同时测试、打包并升级这两个包。零条分批结果仍提交表头并清理旧投影，不能保留旧成功数据冒充本次结果。

测试和升级入口见[上级维护说明](../README.md)。`tests/test_sync_arrival_stats_service_v2_package.py` 核验 ZIP 与当前源码一致；`tests/test_plugin_business_boundary.py` 覆盖冻结 Host 后仅改变包内分类，以及空结果清理。历史 V1/V2 对比仅用于离线迁移回归，不加载或恢复旧 V1 业务。

2026-10-08 已安装 2.1.0，恢复实例启用，保留原有定时关闭状态；该次没有触发统计写表。安装与验证范围见[上线记录](../../../docs/plugin_business_boundary_20261008.md)。后续版本和状态以实际实例为准。
