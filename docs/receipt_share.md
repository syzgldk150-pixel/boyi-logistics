---
module: 客户共享
type: 模块说明
tags: [回单, 签收图, 寄件单, 共享链接, NAS]
status: active
updated: 2026-11-14
---

# 客户共享链接

按客户单位生成只读链接：客户打开 `https://r.boyi.homes/r/{链接码}` 只能看到自己单位的图片，分“回单”（签收图、回单图）和“寄件主单”两个页签，不能进入后台。规格见设计包 ⑩a/⑩b/⑩c；其中客户页按用户 2026-11-13 的验收要求分页签，不是规格里的同网格混排。

## 组成

| 部分 | 位置 | 说明 |
|---|---|---|
| 规则 | `shared/receipt_share.py` | 链接码、访问码、三类图片、发货人匹配规则、唯一的文件命名函数、归档路径 |
| SQL | `shared/receipt_share_repository.py` | 调用方的连接与事务；拉取、后台、对外三组查询 |
| NAS | `shared/receipt_nas_store.py` | WebDAV 读写，路径逐段校验，错误不带地址与账号 |
| 拉取 | `agent/agent/receipt_share_refresh.py`、`agent/receipt_share_composition.py` | 每天 12:00、18:00、23:00（中国时间），有积压每 10 分钟续跑；Console 可触发单条链接 |
| 后台 | `console/services/receipt_shares.py`、`/receipts/shares` | 需要 `receipt.share` 权限（身份管理中分配） |
| 对外 | `console/receipt_share_server.py`、`templates/share_public.html` | 独立进程 `receipt-share.service`，只监听 `127.0.0.1:8766` |
| 数据 | 迁移 `078_receipt_shares.sql` | `waybill_db` 的 `receipt_share_*` 七张表 |

## 数据怎么来

客户 = 一组发货人匹配规则（名称／电话／地址，等于或包含）。刷新时按规则找出该客户未作废的运单，只向平台读取已签收且仍缺签收图或回单图的运单（缺图的每 6 小时再读，开单 90 天内有效；一次运行最多读 120 张运单）。博益开单从它绑定的融辉／韵达运单读取照片，客户看到的是博益单号。每张图片按“平台／月份／运单号”在 NAS 只存一次，服务器磁盘不落原图；随后在一个事务里整体替换该链接可见的图片。平台或 NAS 失败时链接保持原内容，后续重试。

“仅审核通过”按整张运单判断：回单审核通过的运单才出现在链接里。文件名字段 `{开单日期}` 取运单开单日期（系统没有可靠的签收日期）。寄件单图片尚未接入。

## 对外服务的边界

- 只有 `/r/{22 位链接码}` 及其 `data`、`img/{id}`、`file/{id}`、`zip`、`code` 子路径，其他路径一律 404；nginx 的 `r.boyi.homes` 只把 `/r/` 转给 8766，不转发到 Console 或 Agent。
- 进程不加载 Console／Agent 的环境文件，只读自己的 `public.env`：只读 NAS 账号、链接密钥、专用数据库账号。没有后台会话、Agent 地址和平台登录。
- 每个请求都按链接码摘要重新读取链接：停用、重置、过期立即生效；不存在、停用、过期、已重置的链接返回同一张“链接已失效”页面，不带任何数据。
- 图片只能通过列出它的那条链接访问（`share_id + image_id` 联合查询），NAS 路径不出服务器。
- 访问码校验通过后发 `HttpOnly; Secure; SameSite=Strict; Path=/r/{链接码}` 的签名 Cookie；访问码错误按客户端地址限 10 次／10 分钟。页面、图片、打包分别限流。
- 响应带 `noindex`、`no-referrer`、禁止内嵌与逐响应 nonce 的内容安全策略；nginx 不记访问日志（链接码在路径里）。
- 缩略图按内容摘要缓存在 `console/runtime/share_thumbs/`，上限 2G，超出删最旧的；原图每次从 NAS 读取。NAS 离线时页面照常打开，图片提示暂时无法读取。
- 打包下载按当前筛选，一次最多 500 张，包内文件名与页面一致。

## 部署期配置（不进仓库）

两个文件都在 ECS 的 `/home/boyce/.boyi-receipt-share/`，权限 `0600`：

| 文件 | 读取方 | 键 |
|---|---|---|
| `nas.env` | Agent、Console | `RECEIPT_NAS_URL`、`RECEIPT_NAS_RW_USER`、`RECEIPT_NAS_RW_PASS`、`RECEIPT_NAS_RO_USER`、`RECEIPT_NAS_RO_PASS`、`RECEIPT_SHARE_TOKEN_SECRET`、可选 `RECEIPT_SHARE_BASE_URL` |
| `public.env` | 对外服务 | `RECEIPT_NAS_URL`、`RECEIPT_NAS_RO_USER`、`RECEIPT_NAS_RO_PASS`、`RECEIPT_SHARE_TOKEN_SECRET`、`SHARE_DB_HOST`、`SHARE_DB_PORT`、`SHARE_DB_USER`、`SHARE_DB_PASS`、可选 `SHARE_DB_SSL_CA`、`SHARE_PORT` |

`nas.env` 不存在时 Agent 的拉取关闭、后台不能生成链接，其他功能不受影响。更换 `RECEIPT_SHARE_TOKEN_SECRET` 会使全部已发出的链接失效。

对外服务的数据库账号只需要（经 `agent_db` 视图访问，视图为 INVOKER，两边都要授权）：

- `SELECT`：`receipt_shares`、`receipt_share_customers`、`receipt_share_items`、`receipt_share_images`
- `UPDATE (view_count, last_viewed_at)`：`receipt_shares`
- `INSERT`：`receipt_share_access_logs`

## 上线顺序

1. 发布代码（含迁移 078）；Agent 账号与 Console 账号对新表已有权限（`waybill_db` 库级授权）。
2. 在 `nas.env` 追加 `RECEIPT_SHARE_TOKEN_SECRET`，重启 Agent 与 Console。
3. 建专用数据库账号并授权，写 `public.env`。
4. 安装 `console/receipt-share.service` 并启动；本机 `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8766/` 应为 404。
5. 为 `r.boyi.homes` 签证书，安装 `agent/deploy/nginx/r.boyi.homes.conf`，`nginx -t` 后 reload。
6. 后台给需要的账号分配“客户共享”权限，建客户、生成链接验收。

发布脚本目前不会自动重启 `receipt-share.service`：对外服务代码或模板变更后，需要手动 `sudo systemctl restart receipt-share`。

## 回归

`tests/test_receipt_share_rules.py`、`tests/test_receipt_nas_store.py`、`tests/test_receipt_share_composition.py`，以及需要真实 MySQL 的 `tests/test_receipt_share_refresh_mysql.py`、`tests/test_receipt_share_console_mysql.py`、`tests/test_receipt_share_public_mysql.py`（含真实浏览器），`console/tests/test_receipt_shares_browser.py`。
