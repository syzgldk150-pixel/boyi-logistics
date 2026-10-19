# 监控查询（按单号看卸车录像与仓库位置）

Console 固定模块“监控查询”（`/warehouse-monitor`，菜单位于“自动化”之后）按运单号读取真实扫描时间，并调出仓库录像机对应时段的录像。权限为 `warehouse.monitor`，超级管理员默认拥有，其他身份需在身份管理中勾选。

## 数据来源

- 扫描时间：`scan_codes.scanned_at`（迁移 061），由扫描插件 2.0.3 起保存融辉扫描来源的 `SCAN_DATE`，同一条码取当天最早一次。之前的历史行为空，页面显示“未记录”，不推算时间。
- 查询：Console 只调用 Agent 直接业务读取 `warehouse-scan-times`（参数仅 `code`），由 `tools/phase7_mysql_store.list_scan_times_for_tracking` 按主单或子单号读取最近 60 天、最多 500 行；Console 不直读该表。
- 位置：当前只显示目的网点。网点与库区的对应关系及仓库布局尚未提供，页面明确提示“暂不推定库区”；按画面识别实际落点属于后续工作。

## 录像

- 录像机：海康 DS-7808N-K2（`192.168.31.228`），录像约保存 44 天；本机时钟比准确时间慢约 88 秒。
- 录像服务运行在仓库笔记本的独立项目 `warehouse-vision`（不在本仓库）：通过海康 SDK 只读按时间段下载，修正时钟偏差后转码为 H.264 MP4，不保存录像；Windows 任务计划 `WarehouseVision` 以不存储密码方式开机自启，经 `tailscale serve` 只在 Tailscale 内网开放 18765 端口，每个请求须携带令牌。
- Console 服务器端以环境变量 `WAREHOUSE_VIDEO_BASE_URL`、`WAREHOUSE_VIDEO_TOKEN` 访问录像服务，令牌不进入浏览器；未配置、笔记本离线或连不上录像机时分别明确提示，并给出通道与时间便于在 iVMS 中手动查看。
- 页面默认播放扫描时刻前 20 秒起的 60 秒车尾画面（通道 3），可切换通道、前后调整 30 秒或 2 分钟、选择 30 秒到 5 分钟时长。

| 通道 | 摄像头 | 画面 |
|---|---|---|
| 3 | 192.168.31.4 | 车尾 |
| 5 | 192.168.31.6 | 车厢侧面与车下地面 |
| 7 | 192.168.31.8 | 仓库内·对车厢 |
| 2 | 192.168.31.3 | 仓库内·纵深 |
| 4 | 192.168.31.5 | 仓库另一侧 |
| 1 | 192.168.31.11 | 院子一 |
| 6 | 192.168.31.7 | 院子二 |

## 代码入口

- Agent：`agent/business_composition.py`（注册读取）、`agent/tools/phase7_mysql_store.py`（查询）。
- Console：`console/services/warehouse_monitor.py`、`console/routes/warehouse_monitor.py`、`console/templates/warehouse_monitor.html`、`console/static/warehouse_monitor.js`、`console/static/warehouse_monitor.css`。
- 权限：`shared/identity_permissions.py`、`shared/identity_routes.py`；模块身份 `shared/business_modules.py`、`console/navigation.py`、`console/permission_registry.py`。
- 回归：`tests/test_warehouse_monitor.py`。
