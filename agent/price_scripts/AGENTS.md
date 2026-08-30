# price_scripts

## 当前定位

`price_scripts/` 是已隔离的旧离线报价代码，不是当前 Agent 的生产运行入口。仓库目前只保留 `scripts/02_tms_price_fetch/` 下的 `get_price.py`、`login_manager.py`、`browser_address_resolver.py`、`config.json`，以及 `scripts/shared/` 的少量共享实现；旧批量采集、流水线、报表生成脚本和业务数据均不在当前仓库。

## 修改入口

- 当前 Agent 报价工具：`../tools/price_tool.py`
- Agent 本地 TMS 调用门面：`../tools/tms_tool.py`
- 当前融辉/韵达报价 target：`../agent/tms_runtime/scripts/get_price.py` 与 `../agent/tms_runtime/scripts/yunda_price.py`
- 当前账号入口：`/admin/accounts/{account_id}/*`；融辉报价显式绑定 `price_default`

## 隔离规则

- 飞书或 Console 报价故障先查 `../tools/price_tool.py` 和 `../agent/tms_runtime/`，不要把本目录旧实现接回线上。
- 本目录与 `../agent/tms_runtime/scripts/` 存在 `get_price.py`、`login_manager.py`、`browser_address_resolver.py` 等同名模块；任何复用都必须使用完整包路径，禁止裸导入或长期修改 `sys.path`。
- `../docs/price_scripts/` 全部是历史快照，只用于追溯旧批量报价、数据审计和报表口径，不是运行手册。
- 旧批量流水线入口已不存在；不得根据历史文档重建或猜测入口。需要恢复离线批处理时，必须另立任务，从受审历史提交和真实数据源重新验证。

## 相关文档

- `../docs/code_navigation_index.md`
- `../AGENTS.md`
