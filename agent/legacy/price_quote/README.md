# 旧报价实现（离线）

此目录保留原系统 `get_price` 工具、融辉/韵达脚本及融辉批量报价脚本，仅用于显式历史回归。当前三平台报价由 `service_v2_plugins/quote_prices_v2/` 独立 ZIP 提供，飞书和自然对话调用已安装实例。

本目录不进入 ECS 发布清单，不注册为工具或 TMS target，不允许作为插件失败后的回退。测试使用 `legacy.price_quote` 完整包路径；不将此目录加入线上 `sys.path`，不恢复已移除的 `/tms/get_price` 或 `/tms/yunda_price`。

旧工具的脚本模式仍引用同为离线材料的 `agent/price_scripts/`；该模式只解释历史行为，不代表当前插件实现或生产验收。公共账号用途 `price` 仍供融辉原页代理与回单使用，不能因报价工具退役而删除。
