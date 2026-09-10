---
module: 项目总览
type: 架构文档
tags: [项目总览, Agent控制平面, 事项中心, OCR, 价格获取, 财务工作台, 财务对账, 车辆调度, AI客服]
related: [control_plane_v1.md, code_navigation_index.md, database_migrations.md, ocr/module_overview.md, finance_module.md, dispatch/module_overview.md, ai_service/module_overview.md]
status: active
updated: 2026-09-10
---

# 物流 Agent 项目总览

> 本文件是项目总览的唯一规范副本；仓库根或 `agent/` 根目录不得保留同名重复文档。

## 2026-08-15 自动化插件、账号/资源池与系统定时

- 签名插件只安装可复用动作，不携带业务账号、资源详情或实际定时。每次安装由服务端创建独立
  `automation_id`，重复实例各自选择业务账号、资源、系统定时和项目权限。
- 业务账号的凭据与登录态只在“业务账号”模块维护；自动化页不再显示顶部登录绿点、登录态
  popover、凭据表单或账号管理快捷入口。项目卡只消费 Agent 返回的安全账号投影。
- `workflow_resources` 的 Token、表格 ID、读写范围、路径、配置哈希/版本和原始配置只留在 Agent。
  Catalog 只暴露 `resource_id/name/kind/status`，Console 按签名 manifest 的 role+kind 精确筛选，
  不默认选择第一项；资源池不可用、字段漂移、缺失/停用或类型不符时阻断配置和运行。
- `none/daily_times/startup` 等定时在插件安装后由系统项目配置保存，不属于 ZIP/manifest；配置、
  账号/资源绑定、入口、定时和权限使用同一版本化合同，任何漂移都会 fail closed 并使授权 stale。

## 2026-08-30 自动化插件双轨定位

- `ACTION_V1` 是现有 Ed25519 签名动作包，当前运行与迁移合同见
  `automation_plugin_platform.md` 和首方迁移矩阵。
- `SERVICE_V2` 是无签名、仅由已验证 Console `super_admin` 安装的 ZIP 服务包，严格按
  `schema_version=2 + runtime_model=service_v2` 分流；开发、能力、托管存储和双轨迁移的权威说明见
  仓库根 `docs/plugin-platform-v2.md`。
- Service v2 Host API 由 `agent/automation_plugins/host_capability_registry.py` 按精确 API/capability/action
  管理 Schema、handler 和五态 effect；Provider 操作以 `{name,effect}` 声明，Host capability 的 action
  仍是字符串且 effect 只能由 Registry 给出。逐 contribution governance 进入 generation、Direct 调用校验、锁、
  Evidence 和 ResultVerifier，禁止按名称或 lifecycle effect 猜测。
- 两种运行模型继续并存，解析失败不得跨模型回退；v1 项目不能原地升级成 v2，迁移必须建立独立
  v2 项目并行验证。

## 架构 V1：业务接口与独立插件

- 保留 Console、后台服务与共享 MySQL。普通录入/查询直接走业务接口，博益录单存本地；
  外部平台原页存对应平台。OCR 属于运单录入，不另建任务系统。
- 手动、定时、固定飞书关键词直接调用当前安装插件，记录 Invocation 并返回实际结果；
  不创建 Command/Work Item/Run/Step，也没有等待领取或失败积压。不同插件可并行；
  只有仍在实际执行的同实例/资源受当前并发约束。
- 失败、取消和未知写均结束本次；取消等实际线程/子进程和必要核验停止后才完成。
  旧记录只用于追溯，不阻新触发；登录成功和服务重启均不重新执行业务。
- 账号模块只提供按账号隔离的凭据与登录态。权限、签名、精确账号/资源绑定和写后核验保留；
  人工操作不再进入事项审批。账号变更同时核验当前调用使用状态与既有数据库锁。
- 寄件默认查数据库，明确单号或日期覆盖不足时按真实平台来源范围补查；局部补查不删除
  其他数据、不冒充整日完整。夜间插件仅在来源范围和分页闭合时发布完整覆盖。
- 财务/客服采集由所属模块插件负责。财务采集完成不自动分析；AI 助手和飞书自然对话共用
  当前 Agent，能力仅包括已注册接口、已开放插件和显式数据分析，不新增通用流程编辑器。
- 长任务 Runner 保留代码但 `execution_enabled=False`，运行状态为 `reserved`；迁移 046
  结清旧等待记录，不删除历史错误、未知写和业务结果。旧任务的取消、重试、澄清、指派和审批
  写入口为 410，事项页面只读深链，不是新执行入口。
- 发布 hold 期间暂停 Scheduler 与 Direct 新调用；健康、身份、迁移和依赖检查通过后才激活。
  财务没有启动自动补拉。回退不得重新启用旧 Runner 领取已退役记录。

职责图与维护归属见 [业务接口与独立插件调用](../../docs/architecture_direct_invocation.md)，
接口定位见 [代码索引](code_navigation_index.md)，寄件规则见 [寄件查询](../../docs/direct_waybill_query.md)，
验收入口见 [架构验收映射](../../docs/architecture_refactor_acceptance_mapping.md)。
旧 Command、Plan、定时逐次审批及 014–018 迁移合同保留在 [历史控制平面](control_plane_v1.md)；
退役和发布回退限制见 [旧执行链退役](../../docs/legacy_execution_retirement.md)，不得作为当前调用要求。

## 2026-08-11 架构基线

- 生产与 CI 统一使用 Python 3.10，Agent、Console 依赖分别由精确锁文件约束；ECS 发布按两份锁文件的联合哈希复用唯一共享环境，仅在依赖变化或校验失败时重建。发布成功后仍保留当次精确回滚包、上一版虚拟环境和数据库快照，直到业务验收完成后再以独立、有界操作清理。
- Console 保留 `ThreadingHTTPServer`，`app.py` 是组合入口，业务服务位于 `console/services/`，路由识别位于 `console/routes/`。
- TMS SessionBroker 是稳定门面，provider 执行、适配器、持久化和验证器已分层；`agent/agent/` 不再依赖 `tools` 或 `feishu`。
- Console 到 Agent 的调用全部进入 `/internal/v1/*`，使用统一 `ok/data/error` 契约；旧接口仅作鉴权后的 deprecated 兼容层。
- 数据库 DDL 只由版本化 SQL 迁移执行；仓库卫生、导入边界、接口契约、工具 Schema、Ruff、编译和测试均由 CI 门禁。
- 文本文件统一 UTF-8 无 BOM；聚合测试已按领域拆分，单个 Python 文件上限为 3,000 行。

## 项目定位

`物流 Agent` 是统一承接物流业务数据、流程和服务的 Agent + Console 双服务项目，不是单一 OCR
工具。`shared/business_modules.py` 是当前 15 个 Console 菜单身份的唯一不可变代码目录：

| 模块代码 | 菜单 | 主页面 | 运行身份 |
|---|---|---|---|
| `overview` | 概览 | `/` | 固定模块 |
| `waybill_entry` | 运单录入 | `/ocr` | 固定模块 |
| `waybill_query` | 寄件运单查询 | `/waybills` | 固定模块 |
| `tracking` | 物流跟踪 | `/tracking` | 固定模块 |
| `receipts` | 回单管理 | `/receipts` | 固定模块 |
| `customer_service` | 客户服务 | `/modules/customer-service` | 固定模块 |
| `finance` | 财务模块 | `/modules/finance` | 固定模块 |
| `dispatch` | 货拉拉调度 | `/dispatch` | 固定模块 |
| `line_haul` | 专线分流 | `/line-haul-contacts` | 固定模块 |
| `automations` | 自动化 | `/automations` | 固定模块 |
| `harness` | AI 助手 | `/harness` | 固定模块（已注册只读接口、插件能力与显式分析） |
| `automation_accounts` | 业务账号 | `/automation-accounts` | 固定模块 |
| `llm_settings` | 智能模型 | `/settings/llm` | 固定模块 |
| `work_items` | 历史事项 | `/work-items` | 只读深链，不列入日常导航 |
| `system_settings` | 系统管理 | `/settings/accounts` | 固定模块 |

固定模块只由代码路由、既有登录/用户权限和业务前置条件控制；旧数据库生命周期状态和版本不能隐藏或阻断它们。
`/settings/system-status` 是仅真实 `super_admin` 可见的控制平面入口，不属于上述 15 个固定模块目录，只展示鉴权健康接口的白名单系统字段。`/settings/modules` 是退役重定向入口，旧 data/audit 子路径和 Agent 管理 API 只保留历史记录读取；数据库不能动态创造模块、菜单或实现。

## 主要业务数据关系

1. 运单录入内完成 OCR/人工校核：博益保存本地；韵达、融辉等原页保存各自平台。
2. 每日寄件采集插件按实际平台范围存数据库；后台寄件查询先读数据库并补查明确缺口。
3. 跟踪、回单、客服查询复用平台适配器与登录态；人工业务操作直接返回核验结果。
4. 财务插件采集逐笔账本，查询/汇总读取共享数据；分析只在明确请求时运行。
5. 货拉拉调度当前为高德地图规划和本地试算，真实货拉拉业务接口尚未接入。
6. Agent 只使用已注册接口和开放插件的真实结果；来源缺失、范围不足或核验失败必须明确返回。

## 启停脚本

- `console/start_backend.sh`
  WSL / Linux 下启动项目本地控制台。
- `console/stop_backend.sh`
  WSL / Linux 下停止项目本地控制台。

## 当前实现状态

- 项目级控制台目录现已独立为与 agent 并列的 `console/` 工作区。
- Console 导航固定登记上述 15 个模块身份；迁移 `027` 保存的 14 行历史生命周期状态和 Lite 审计仅供只读兼容，不参与固定模块菜单、页面、API 或新调用可用性判断。依赖 Agent、账号、资源或其他业务数据的具体操作仍由各自合同独立失败关闭。
- OCR、运单、跟踪、回单、客服、融辉财务、调度、自动化、账号、智能模型、事项中心和系统管理均沿既有页面与服务边界运行；韵达财务适配器待真实来源验收后再启用。
- 财务工作台通过共享 MySQL 账本与 `sync_finance_bills` 插件接通；代码来源注册表开放融辉三个财务角色，实际启停、时刻与账号以已安装实例的持久设置为准，本轮未核验或修改生产配置。逐笔汇总、平台汇总与 signed-net 必须一致，旧 Excel ETL 已退出当前运行时。
- `车辆调度` 当前为 `map_only`：高德地图、路线规划与本地试算；没有真实货拉拉派单、车辆或平台接口。
- 面向客户的独立 AI 客服模块仍未建立；现有固定命令、受限只读查询和客服工作台分别由 `agent/`、`feishu/` 与 Console 既有链路承载。
- Agent 公开面只保留精简 `/health`、飞书事件入口和带独立 Webhook Token 的 `/webhook/*`；主要管理与业务代理接口位于 `/internal/v1/*`。`/chat`、`/run-tool` 等旧入口只作为继续鉴权的 deprecated 兼容层，不得新增调用方。
- 调度模板、TMS 兼容接口和共享登录态仍由 Agent 承载；Console 通过受控内部接口访问，不把 `/tms/*` 当作新的控制平面写入口。
- Phase 7 迁移所需的飞书表格、Webhook 等资源配置统一保存在 Agent MySQL 的 `workflow_resources` 表中，不再依赖 N8N sqlite；Console 只读取闭合安全 descriptor，不直接读取 Token、表格 ID、范围、路径或原始配置。
- `sync_daily_should_sign` 必须显式绑定项目当前选择的独立 `r13_account_id` 与融辉 TMS `account_id`；后台可改绑为任意同系统有效账号，下一次运行只使用新绑定。R13 在精确账号登录后按原页协议从 `/gateway/public/aurora/auth` 读取实际站点范围，请求使用 R13 同源 `Origin` 与 `aurora-token`，不继承 SSO `Origin` 或附加 Bearer；中心账号使用空过滤，其他账号使用其 `siteCode`。缺账号上下文、刷新后范围漂移或请求体覆盖账号/站点都会阻塞。结构完整且权威总数为零的 R13 结果仍完成其他来源证据核验；若最终发布集合为空，则正常删除多维表旧记录、清空电子表格旧数据并回读为零行，真实来源异常则在投影变更前失败。同一个 TMS 登录态统一用于问题件、主单签收、轨迹核验和地址补全，不读取旧 `workflow_resources.phase7.r13_credentials`，也不接受请求体内联凭据或隐式默认账号。
- R13 只作为应签候选和冲突诊断；TMS 主单“签收”事件是唯一关闭证据。长历史签收按 31 天窗口完整分页并校验汇总/明细总量，离开当前 R13 的候选由迁移 `013` 按 1/3/7 天退避进行精确轨迹核验。
- `console` 现已与 Agent 统一使用同一套 MySQL，不再在运行时回退 SQLite。
- Agent、控制台、自动化调度、Phase 7 同步链路当前统一使用独立的 Agent MySQL；N8N 已从运行时链路移除，不再参与数据库读写、Webhook 映射或任务调度。
- `sync_daily_send_orders`、`sync_delivery_status`、`sync_daily_should_sign`、`sync_site_send_list`、`sync_arrive_list`、`sync_scan_codes`、`sync_arrival_stats` 已全部并入当前发布仓，由各自 `first_party_automation_plugins/<id>/payload/` 经 Broker 和 `plugin_core_adapters/` 执行；`agent/tms_runtime/` 负责平台协议，旧 whole-tool 不进入新链；`sync_daily_send_orders` 写入飞书后会同步维护控制台 `waybills` SQL 表，并将明确返回的当前扫描状态写入 `scan_status`，后台 `/waybills` 可按融辉运单号检索。
- `sync_yunda_dispatch_forecast` 按插件实例绑定的韵达账号读取次日“网点派件量预测主单表”并写入绑定的飞书资源。17:00 是历史默认定时说明，不代表当前生产设置；运行时使用已安装实例的实际定时及精确账号绑定，融辉插件同样不回落 `ronghui/default`。
- `sync_yunda_send_waybills` 复用该实例绑定韵达账号的登录态，拉取当天“寄件运单管理”列表，补查快件跟踪详情与小眼睛解密接口后写入绑定的寄件资源；历史按天累积，同一运单号重复同步时更新原记录，并同步维护控制台 `waybills` SQL 表，将明确返回的当前扫描状态写入 `scan_status`，后台 `/waybills` 可按韵达运单号检索。
- `init_waybills_sql_from_feishu` 可从飞书中的融辉寄件数据表和韵达寄件运单表全量回填控制台 `waybills` SQL 表，用作后台运单查询模块的初始化数据来源；该工具只写 SQL，不修改飞书。
- `r7_arrival_checkin` 和 `r7_departure_checkin` 已从当前发行的后台 `/automations`、调度注册和飞书直达入口移除；历史项目、运行及审计记录继续保留，不参与当前健康计数，也不会执行第三方打卡写入。
- `sync_arrive_list` 当前拉取 TMS「派件预报」作为到货基础清单；`sync_arrival_stats` 以“目标日 arrive-list ∪ 目标日实际扫描主单”为当天范围，过滤历史已到齐且当天未重扫的重复主单，历史未齐主单以到货 0 保留，当天重扫主单始终保留。
- 2026-05-18：`sync_arrival_stats` 会把 `20055750680002` 这类融辉纯数字子单归并到主单 `2005575068`，并在统计导出时过滤历史缓存中的子单行，避免旧误入库子单继续写入飞书。
- `sync_arrival_stats` 以累计子单扫描数作为到货件数并按主单开单件数封顶；`count_result.quantity_gaps` 记录扫描不足，`quantity_adjustments` 记录超量封顶。
- `scan_codes` 表按 `raw_code` 主键 UPSERT 累积；`sync_arrival_stats` 的 `scan_window_days` 只允许 1，保证当天范围不被历史扫描污染。首次部署或历史回填必须单独运行 `sync_scan_codes`。
- `sync_arrival_stats` 的「未齐货物」飞书清单是可选输出。迁移生成的签名插件实例默认使用 `pending_sheet_disabled=true` 且不绑定 `arrival_stats_pending_sheet`，因此不要求存在 `phase7.pending_arrivals_sheet`；只有先在 `workflow_resources` 配置并显式绑定该资源，再把开关改为 false 才会写入。清单仍由 MySQL 视图 `v_arrival_progress` 实时计算（已到件数 < 应到件数 的主单），齐货后自动剔除。
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

## 2026-04-03 历史更新

- 当时新增 `/automations` 统一维护 Agent 自动化参数；其中顶部登录态、默认账号/密码和直接资源配置入口已由 2026-08-15 插件项目页取代，凭据与登录态现只在“业务账号”模块维护。
- 当前 Console 不直接操作 `workflow_resources` 的完整配置；只消费 Agent 的安全资源 descriptor。`scheduled_tasks` 由安装后的系统项目定时配置生成和维护。
- 任务在控制台保存后会触发 Agent `/admin/reload`，把最新的调度定义即时重载到 APScheduler。
