---
module: 项目总览
type: 架构文档
tags: [项目总览, 模块关系, 本地控制台, OCR, 价格获取, 财务工作台, 财务对账, 车辆调度, AI客服]
related: [ocr/module_overview.md, price_scripts/project_structure.md, finance_module.md, finance_reconciliation/module_overview.md, dispatch/module_overview.md, ai_service/module_overview.md]
status: 架构基线已完成
updated: 2026-08-11
---

# 物流 Agent 项目总览

> 本文件是项目总览的唯一规范副本；仓库根或 `agent/` 根目录不得保留同名重复文档。

## 2026-08-11 架构基线

- 生产与 CI 统一使用 Python 3.10，Agent、Console 依赖分别由精确锁文件约束；ECS 发布按两份锁文件的联合哈希复用唯一共享环境，仅在依赖变化或校验失败时重建，成功后删除所有非当前环境。
- Console 保留 `ThreadingHTTPServer`，`app.py` 是组合入口，业务服务位于 `console/services/`，路由识别位于 `console/routes/`。
- TMS SessionBroker 是稳定门面，provider 执行、适配器、持久化和验证器已分层；`agent/agent/` 不再依赖 `tools` 或 `feishu`。
- Console 到 Agent 的调用全部进入 `/internal/v1/*`，使用统一 `ok/data/error` 契约；旧接口仅作鉴权后的 deprecated 兼容层。
- 数据库 DDL 只由版本化 SQL 迁移执行；仓库卫生、导入边界、接口契约、工具 Schema、Ruff、编译和测试均由 CI 门禁。
- 文本文件统一 UTF-8 无 BOM；聚合测试已按领域拆分，单个 Python 文件上限为 3,000 行。

## 项目定位

`物流 Agent` 是一个统一承接物流业务数据、流程和服务的本地控制台项目，不是单一 OCR 工具。当前按 5 个功能模块组织：

- `OCR识别`
  负责纸质托运单电子化、字段抽取、人工复核和数据库入库。
- `价格获取`
  负责高德地址库、融辉 TMS 批量报价、客户报价表和价格分析。
- `财务对账`
  新工作台负责融辉/韵达逐笔账本、费用项目绑定、BI 和同步审计；旧离线 ETL 继续负责支付流水、发票和工作簿差异报表，两条链路隔离。
- `车辆调度`
  负责车辆资源管理与智能调度，实时追踪运力状态、线路规划和运单分配。
- `AI客服`
  负责消费其他模块的结果，承接查单、报价问答和异常解释。

## 模块关系

1. `纸质单据 -> OCR识别`
   纸质托运单进入 OCR 工作区，转成结构化运单字段。
2. `高德 + TMS -> 价格获取`
   地址库和平台报价生成标准价格资产，供客服报价和内部测算使用。
3. `OCR结果 + 支付/发票/平台流水 -> 财务对账`
   运单和流水汇总后生成月度损益、差异和校验结果。
4. `OCR + 价格 + 车辆信息 -> 车辆调度`
   根据运单数据和价格基线进行智能派车、线路优化和运力监控。
5. `OCR + 价格 + 财务 + 调度 -> AI客服`
   AI 客服统一读取结构化运单、价格基线、财务解释和调度结果，对外提供问答和工单能力。

## 本地控制台路由

- `/`
  项目总览页，展示 5 个模块和它们的上下游关系。
- `/ocr`
  OCR 工作区，负责上传、预处理、裁图、复核和入库。
- `/modules/pricing`
  价格获取模块说明页。
- `/modules/finance`
  融辉/韵达财务工作台，提供 BI 总览、交易明细、费用项目绑定和同步记录。
- `/dispatch`
  车辆调度工作区，车辆管理、派单和线路规划。
- `/modules/dispatch`
  车辆调度模块说明页。
- `/modules/ai-service`
  AI 客服模块说明页。

## 启停脚本

- `console/start_backend.sh`
  WSL / Linux 下启动项目本地控制台。
- `console/stop_backend.sh`
  WSL / Linux 下停止项目本地控制台。

## 当前实现状态

- 项目级控制台目录现已独立为与 agent 并列的 `console/` 工作区。
- 项目级本地控制台已接入 `OCR / 价格获取 / 财务对账 / 车辆调度 / AI客服` 5 个模块入口。
- 目前可交互运行的是 `OCR 工作区`、`融辉/韵达财务工作台` 和 `车辆调度工作区`。
- 财务工作台通过共享 MySQL 账本与 Agent `sync_finance_bills` 接通；旧 `finance_reconciliation/` Excel ETL 保持独立。
- `车辆调度` 已完成工作区页面（车辆列表、调度看板、快速调度面板），当前使用演示数据。
- `AI客服` 当前没有单独目录，运行时能力集中在 `agent/`、`feishu/` 和飞书工具链。
- ECS 上的 Agent 服务已提供 `/health`、`/chat`、`/run-tool`、`/tools`、`/admin/reload`、`/knowledge`、`/knowledge/search`、`/tool-logs` 等运行时接口，用于飞书机器人、调试和知识库维护。
- ECS 上的 Agent 服务已提供 `/scheduled-tasks`、`/admin/seed-phase7-tasks`、`/tms/*`、`/admin/tms/session/*`，用于统一承载调度模板、TMS 兼容业务接口和共享登录态管理。
- Phase 7 迁移所需的飞书表格、Webhook 等资源配置现统一保存在 Agent MySQL 的 `workflow_resources` 表中；运行时与控制台均直接读取这套独立配置，不再依赖 N8N sqlite。
- `sync_daily_should_sign` 使用 R13 独立 SSO，账号资源通过 `workflow_resources.phase7.r13_credentials` 维护，不复用顶部共享 TMS 登录态。
- `console` 现已与 Agent 统一使用同一套 MySQL，不再在运行时回退 SQLite。
- Agent、控制台、自动化调度、Phase 7 同步链路当前统一使用独立的 Agent MySQL；N8N 已从运行时链路移除，不再参与数据库读写、Webhook 映射或任务调度。
- `sync_daily_send_orders`、`sync_delivery_status`、`sync_daily_should_sign`、`sync_site_send_list`、`sync_arrive_list`、`sync_scan_codes`、`sync_arrival_stats` 已全部并入当前发布仓，由 `agent/tools/` 和 `agent/tms_runtime/` 统一承载；`sync_daily_send_orders` 写入飞书后会同步维护控制台 `waybills` SQL 表，并将明确返回的当前扫描状态写入 `scan_status`，后台 `/waybills` 可按融辉运单号检索。
- `sync_yunda_dispatch_forecast` 使用韵达独立登录态 `yunda`，默认每天 17:00 拉取次日“网点派件量预测主单表”并按应派时间覆盖写入飞书多维表格；融辉既有自动化继续使用 `ronghui/default` 登录态。
- `sync_yunda_send_waybills` 使用同一套韵达登录态 `yunda`，拉取当天“寄件运单管理”列表，补查快件跟踪详情与小眼睛解密接口后写入 `phase7.yunda_send_waybills_bitable`；历史按天累积，同一运单号重复同步时更新原记录，并同步维护控制台 `waybills` SQL 表，将明确返回的当前扫描状态写入 `scan_status`，后台 `/waybills` 可按韵达运单号检索。
- `init_waybills_sql_from_feishu` 可从飞书中的融辉寄件数据表和韵达寄件运单表全量回填控制台 `waybills` SQL 表，用作后台运单查询模块的初始化数据来源；该工具只写 SQL，不修改飞书。
- `r7_arrival_checkin` 和 `r7_departure_checkin` 已接入后台 `/automations` 和飞书直达指令；R7 登录独立于顶部 TMS 登录态，后台中走 R7 页面的任务会显示 R7 标识。
- `sync_arrive_list` 当前拉取 TMS「派件预报」作为到货基础清单；`sync_arrival_stats` 会用当天扫描数据反推缺失主单，补抓详情后追加到统计输出。
- 2026-05-18：`sync_arrival_stats` 会把 `20055750680002` 这类融辉纯数字子单归并到主单 `2005575068`，并在统计导出时过滤历史缓存中的子单行，避免旧误入库子单继续写入飞书。
- `sync_arrival_stats` 默认以扫描索引中的子单扫描数作为到货件数；主单总件数不参与统计计数，避免把仍在上游分拨的子单误算为到货。`count_result.quantity_gaps` 仅作为“扫描子单数低于主单总件数”的审计提示。
- `scan_codes` 表自 2026-04-26 起改为 UPSERT 累积写入：每次 `sync_arrival_stats` / `sync_scan_codes` 不再 TRUNCATE，而是按 `raw_code` 主键合并历史扫描记录。统计列含义随之从「当日件数」更名为「累计到货件数」。`scan_window_days`（默认 1）控制每次拉取 `/get_scan` 的时间窗，`scan_codes_retention_days`（默认 30）控制保留期；首次部署可临时传 `scan_window_days=30` 一次性回填历史。
- `sync_arrival_stats` 现额外输出「未齐货物」清单到 `phase7.pending_arrivals_sheet`，由 MySQL 视图 `v_arrival_progress` 实时计算（已到件数 < 应到件数 的主单），齐货后自动剔除；如未在 `workflow_resources` 中配置该资源，写入步骤会被自动跳过。
- `sync_arrival_stats` 成功完成后还会复用本次 19 列统计结果，通过 `tools/split_pending_snapshot.py` 自动覆盖 `phase7.split_pending_target_sheet` 和 `split_pending_problem_items`；全部到齐时清空“分批及有发未到表”旧行，仅保留表头，自动刷新不产生融辉差错或问题件上报。
- 2026-05-22: `sync_arrival_stats` archive snapshots in `phase7.stats_archive_sheet` are idempotent by date tab. The tool reuses an existing `YYYY-MM-DD` sheet, clears that tab's configured `default_write_range` expanded to cover previous rows, and rewrites the latest stats instead of creating duplicate tabs or failing on `sheet already exists`.
- `query_waybill_detail` 查询主单详情时默认带 `isView=true` 获取解密视图；若接口结果仍缺失或加密，再回退到快件跟踪页 MiniUI 解密按钮补齐。控制台 `/tracking/query` 的融辉运单详情在 `decrypt_masked=true` 且收寄件人姓名/电话缺失或带星号时，也会复用该详情补齐链路覆盖展示字段。`sync_arrival_stats` 会把历史缓存中收件人/电话仍带星号的主单重新纳入补抓。
- TMS 底层 HTTP / 浏览器脚本已并入 `agent/tms_runtime/`，不再依赖 ECS `root` 账户下的 `/root/http_service`。
- Phase 7 运行期 MySQL 当前承载共享配置表 `workflow_resources`、`scheduled_tasks`、到货统计所需的快照表 / 视图，以及给控制台 `/waybills` 运单查询使用的 `waybills` 同步记录。
- ECS 上的控制台已独立部署为 `console.service`，仅监听 `127.0.0.1:8765`；公网入口固定为 `https://boyi.homes`，由 Nginx 终止 TLS 并反向代理，HTTP 和 `www.boyi.homes` 统一跳转到根域名 HTTPS。
- 2026-05-18：`/automation-accounts` 账号编辑弹层支持点击页面其他区域自动收起；已保存密码仅在页面显示为掩码，保存时若未输入新密码会保留 Agent 侧原密码，`凭据已配置` 状态使用成功色展示。
- 2026-05-20：融辉 TMS 登录态默认切换为图片验证码；顶部 `/automations` 和业务账号管理页会展示 Agent 返回的验证码图片，融辉/大祥报价登录配置不再要求手机号，旧短信验证码页仍兼容。
- 2026-05-31：自动化业务账号按真实外部系统展示为 TMS融辉、韵达、R7、R13；大祥报价、自提问题件和大祥S站作为 TMS融辉账号用途维护，不再作为独立系统展示。
- 2026-08-11：账号页统一所有系统的管理契约：“立即登录”执行真实登录，自动登录只控制定时校验与掉线恢复，退出登录同时关闭自动登录，连续失败三次熔断。大祥报价改为显式绑定 `price_default` 账号及其 `price_default` profile，飞书报价与后台登录复用同一登录态；R7/R13 接入可持久、可校验、可清理的 SSO Token/Cookie 状态，不再显示“不支持”或把登录降级成凭据检查。每个账号仍按 `account_id` 隔离运行态，避免不同真实账号互相覆盖。

## 2026-04-03 Update

- 新增控制台入口 `/automations`，用于统一维护 Agent 自动化参数，并在页面顶部按分类管理 TMS融辉图片验证码登录态（旧短信验证码页兼容）与韵达账号密码/图片验证码登录态。
- `/automations` 顶部现支持保存默认账号、密码；融辉当前不再要求手机号，旧短信页出现时仍可使用保存的手机号。本地验证通过前不进入 ECS 切换。
- 控制台页现在直接操作共享 MySQL 中的 `workflow_resources` 与 `scheduled_tasks`。
- 任务在控制台保存后会触发 Agent `/admin/reload`，把最新的调度定义即时重载到 APScheduler。
