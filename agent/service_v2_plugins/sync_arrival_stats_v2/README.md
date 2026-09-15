# 到货统计插件

当前业务算法只在本目录 `payload/action.py` 维护，服务接口由 `manifest.json` 和 `payload/plugin.py` 声明。共享适配器通过已绑定的 Host API 读取来源、保存结果并核验；不加载旧 V1 插件。

测试和升级入口见 [上级维护说明](../README.md)。`tests/test_sync_arrival_stats_service_v2_package.py` 核验 ZIP 与本目录动作源码一致；历史 V1/V2 对比仅用于离线迁移回归。
