---
module: 项目总览
type: 架构文档
tags: [项目总览, 模块关系, 本地控制台, OCR, 价格获取, 财务对账, 车辆调度, AI客服]
related: [ocr/module_overview.md, price_scripts/project_structure.md, finance_reconciliation/module_overview.md, dispatch/module_overview.md, ai_service/module_overview.md]
status: 开发中
updated: 2026-05-22
---

# 物流 Agent 项目总览

## 项目定位

`物流 Agent` 是一个统一承接物流业务数据、流程和服务的本地控制台项目，不是单一 OCR 工具。当前按 5 个功能模块组织：

- `OCR识别`
  负责纸质托运单电子化、字段抽取、人工复核和数据库入库。
- `价格获取`
  负责高德地址库、融辉 TMS 批量报价、客户报价表和价格分析。
- `财务对账`
  负责支付流水、平台流水、运单和发票对账，输出损益与差异报表。
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
  财务对账模块说明页。
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

## 本地 WSL 与 ECS 运行边界

- ECS 是飞书机器人、定时任务和生产自动化的唯一长期运行源。
- 本地 WSL 只用于开发测试；测试完成或同步 ECS 前必须停止本地 `agent`，避免本地飞书 WebSocket 与 ECS 同时消费消息。
- 排查线上问题时优先核对 `/health.instance_id`、`components.mysql` 和 `components.feishu_ws`，确认报错来自 ECS 还是本地测试实例。

## 当前实现状态

- 项目级控制台目录现已独立为与 agent 并列的 `console/` 工作区。
- 项目级本地控制台已接入 `OCR / 价格获取 / 财务对账 / 车辆调度 / AI客服` 5 个模块入口。
- 目前可交互运行的是 `OCR 工作区` 和 `车辆调度工作区`。
- `价格获取` 和 `财务对账` 以模块说明和运行入口梳理为主。
- `车辆调度` 已完成工作区页面（车辆列表、调度看板、快速调度面板），当前使用演示数据。
- `AI客服` 当前没有单独目录，运行时能力集中在 `agent/`、`feishu/` 和飞书工具链。
- ECS 上的 Agent 服务已提供 `/health`、`/chat`、`/run-tool`、`/tools`、`/admin/reload`、`/knowledge`、`/knowledge/search`、`/tool-logs` 等运行时接口，用于飞书机器人、调试和知识库维护。
- ECS 上的 Agent 服务已提供 `/scheduled-tasks`、`/admin/seed-phase7-tasks`、`/tms/*`、`/admin/tms/session/*`，用于统一承载调度模板、TMS 兼容业务接口和共享登录态管理。
- Phase 7 迁移所需的飞书表格、Webhook 等资源配置现统一保存在 Agent MySQL 的 `workflow_resources` 表中；运行时与控制台均直接读取这套独立配置，不再依赖 N8N sqlite。
- `console` 现已与 Agent 统一使用同一套 MySQL，不再在运行时回退 SQLite。
- Agent、控制台、自动化调度、Phase 7 同步链路当前统一使用独立的 Agent MySQL；N8N 已从运行时链路移除，不再参与数据库读写、Webhook 映射或任务调度。
- `sync_daily_send_orders`、`sync_delivery_status`、`sync_daily_should_sign`、`sync_site_send_list`、`sync_arrive_list`、`sync_scan_codes`、`sync_arrival_stats` 已全部并入当前发布仓，由 `agent/tools/` 和 `agent/tms_runtime/` 统一承载。
- `sync_yunda_dispatch_forecast` 已接入韵达独立登录态 `yunda`，每天 17:00 可同步次日“网点派件量预测主单表”到飞书多维表格；融辉现有自动化继续使用 `ronghui/default` 登录态。
- `sync_yunda_send_waybills` 已接入韵达独立登录态 `yunda`，可同步当天“寄件运单管理”到 `phase7.yunda_send_waybills_bitable`；数据来自寄件运单列表、快件跟踪详情和小眼睛解密接口，按运单号更新避免重复追加。
- TMS 底层 HTTP / 浏览器脚本已并入 `agent/tms_runtime/`，不再依赖 ECS `root` 账户下的 `/root/http_service`。
- Phase 7 运行期 MySQL 当前承载共享配置表 `workflow_resources`、`scheduled_tasks` 以及到货统计所需的快照表 / 视图。
- ECS 上的控制台已独立部署为 `console.service`，监听 `:8765`，并支持通过 Basic Auth 做内部访问控制。

## 2026-04-03 Update

- 新增控制台入口 `/automations`，用于统一维护 Agent 自动化参数，并在页面顶部直接管理 TMS 短信验证码共享登录态。
- `/automations` 顶部现支持保存默认账号、密码、手机号；发送验证码固定使用这套已保存凭据，本地验证通过前不进入 ECS 切换。
- 控制台页现在直接操作共享 MySQL 中的 `workflow_resources` 与 `scheduled_tasks`。
- 任务在控制台保存后会触发 Agent `/admin/reload`，把最新的调度定义即时重载到 APScheduler。
