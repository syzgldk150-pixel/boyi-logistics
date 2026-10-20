# 监控查询（按单号看卸车录像与仓库位置）

Console 固定模块“监控查询”（`/warehouse-monitor`，菜单位于“自动化”之后）按运单号读取真实扫描时间，定位到仓库录像机对应时段；录像只在仓库局域网内播放，服务器不经手视频。权限为 `warehouse.monitor`，超级管理员默认拥有，其他身份需在身份管理中勾选。

## 数据来源

- 扫描时间：`scan_codes.scanned_at`（迁移 061），由扫描插件 2.0.3 起保存融辉扫描来源的 `SCAN_DATE`，同一条码取当天最早一次。之前的历史行为空，页面显示“未记录”，不推算时间。
- 查询：Console 只调用 Agent 直接业务读取 `warehouse-scan-times`（参数仅 `code`），由 `tools/phase7_mysql_store.list_scan_times_for_tracking` 按主单或子单号读取最近 60 天、最多 500 行；Console 不直读该表。
- 位置：当前只显示目的网点。网点与库区的对应关系及仓库布局尚未提供，页面明确提示“暂不推定库区”；按画面识别实际落点属于后续工作。

## 录像

- 录像机：海康 DS-7808N-K2（`192.168.31.228`），录像约保存 44 天；本机时钟比准确时间慢约 88 秒。
- 分工：后台（ECS）只负责定位——查扫描时间，并为每次查询签发 15 分钟有效的“看录像”链接；播放由仓库笔记本的独立项目 `warehouse-vision`（不在本仓库）在局域网内完成。
- 链接：Console 以 `WAREHOUSE_VIDEO_TOKEN` 对 `open:{exp}` 计算 HMAC-SHA256，生成 `WAREHOUSE_VIDEO_LAN_URL/open?exp=&sig=`，页面再追加 `code` 与 `scan`。令牌只在服务器端用于签名，不进入浏览器；未配置时页面明确提示“只能查看扫描时间”。
- 回放：浏览器在仓库网络中打开链接，笔记本校验签名后发放 12 小时会话 Cookie 并进入回放页；回放页经海康设备网络 SDK（与 iVMS 相同的私有协议，7 个通道均可取）修正时钟偏差后边下载边转成分片 MP4 播放，保持录像机登录时首个画面约 0.5 秒，不保存录像。默认从扫描前 20 秒起播放 60 秒车尾（通道 3），可切换通道、前后调整 30 秒或 2 分钟、选择 30 秒到 5 分钟。
- 网络：笔记本以 Windows 端口转发把局域网 18766 端口转到 WSL 内服务，防火墙只放行本地子网；`WAREHOUSE_VIDEO_LAN_URL` 须与笔记本局域网地址一致，建议在路由器为笔记本固定 IP。笔记本关机或不在仓库网络时回放页打不开，可在 iVMS 中按页面给出的时间手动查看。
- 海康 WebSDK（V3.3）网页插件不适用：其回放只走 RTSP，而该录像机的 RTSP 回放仅通道 5、6 出流（2026-10-20 复测），且插件需要局域网服务器代理 ISAPI。

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
