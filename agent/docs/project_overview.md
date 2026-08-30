---
module: 项目总览
type: 架构文档
tags: [项目总览, Agent控制平面, 事项中心, OCR, 价格获取, 财务工作台, 财务对账, 车辆调度, AI客服]
related: [control_plane_v1.md, code_navigation_index.md, database_migrations.md, ocr/module_overview.md, finance_module.md, dispatch/module_overview.md, ai_service/module_overview.md]
status: active
updated: 2026-08-30
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
- 两种运行模型继续并存，解析失败不得跨模型回退；v1 项目不能原地升级成 v2，迁移必须建立独立
  v2 项目并行验证。

## 2026-08-13 Agent 统一控制平面

- 保留 Agent + Console 双服务。Agent 内新增持久化 Command Gateway、Work Item、Run、
  Step、Approval、Evidence、Domain Event 与 MySQL Outbox，不新增 LLM 服务或 Kafka。
- Console、飞书、APScheduler、Webhook、客服/回单业务入口和兼容工具 API 统一先提交
  Command；只有 WorkflowRunner 可以调用工具执行端口。登录/验证码、Console 本地 OCR 与
  博益手工运单 CRUD 继续使用原边界。
- LLM 目录只开放明确标记的只读/计算能力；风险、权限、审批、工具版本、Evidence 和写后
  条件全部来自受管 `registry.yaml`。第三方写入要求独立 `super_admin` 审批与写后验证，除非 Scheduler 命中当前有效的精确任务豁免，
  删除、付款及通用不可逆覆盖禁用。
- 新增 Console“事项中心”，只代理 Agent `/internal/v1/*`，展示事项、运行步骤、计划、
  审批、Evidence 与时间线；所有写动作使用真实管理员会话和同源校验。
- 共享内部 Token 只证明服务调用方；Console 使用独立 HMAC 把真实 MySQL 管理员身份与
  精确请求绑定。工具子进程不继承管理 Token，只能用 WorkflowRunner 签发的短期能力访问
  精确 TMS target。韵达/融辉活动原页只通过主站 launch ticket 进入 `www` 独立 origin；
  旧 Console 同源代理和旧韵达 JSON 录单入口固定返回 410 且不调用 Agent；本地 OCR、博益手工 CRUD 与控制平面命令保持可用。
- “每日应签”和融辉/韵达双向客服问题件作为首期只读事项投影。每日应签以 MySQL 账本和
  真实主单签收事件为准；问题件必须全账号、双方向、全分页，列表消失后按外部 ID 精确
  详情复核，不能把未知状态当成关闭。
- 新投影只影子运行并保存集合哈希、差异和完整性。连续三个完整业务日满足切换标准且差异
  经管理员确认后，才允许替换首页口径。
- 完整设计、状态机、权限、API、迁移和发布门禁见 `control_plane_v1.md`。

## 2026-08-14 每任务定时审批策略（实施/发布契约）

- 定时写操作不再依赖“某类任务一律免审”的规则。每个持久化 `scheduled_tasks` 行默认
  `REQUIRE_EACH_RUN`；只有符合工具 `approval.mode: schedule_allowlist` 资格的任务，才可由
  签名真实 MySQL Console 会话中的 `super_admin` 单独设置为 `EXACT_SCHEDULE_EXEMPT`。
- 免审仅对 Scheduler Command 生效。Console 立即运行、飞书、Webhook 和其他手工入口即使使用
  同一个工具/任务，仍需常规审批；Basic Auth、普通管理员及浏览器传入的身份/哈希均不能配置策略。
- Agent 服务端生成策略行为哈希，覆盖任务 ID、工具/版本、完整参数和账号、cron、启用状态、
  治理字段、postconditions、动态规则和配置版本。任务显示名称不是行为，故不进入哈希；任何其他
  受绑定配置或工具治理变更使原豁免 stale 并恢复逐次审批。
- 生产已执行的 `014_control_plane_task_cutover.sql` 按生产迁移历史校验和保持字节不可变；后续安全
  修正由 `015` 至 `018` 前向迁移完成。`015` 保存任务配置版本、当前策略及不可变策略审计；
  `016` 是历史迁移，只把每日应签的三个融辉角色收敛为一个 `account_id` 角色并保留独立 R13 来源账号；当前插件项目可把这两个角色分别绑定为任意同系统有效账号，下一次运行随绑定变化；`017` 精确升级两条
  打卡和财务任务契约；`018` 建立项目、配置、代际、26 项资源闭包和授权证据。各迁移先完整备份
  业务行，并提供可重入的恢复与重应用入口。
- 首次 post-018 bootstrap 精确核验 71 条历史身份（57 typed +14 deferred R7）、68 条启用和 16 个
  项目策略；项目分布固定为 10 个 LEGACY、6 个 REQUIRE，并由 55 条已启用旧任务的 grant/退休事件
  证明。marker 已存在后管理员可合法启停、改 schedule 或策略；后续发布不固定 typed 行数，但当前
  committed 项目与首次 marker/source snapshot 必须分别闭合，stale 授权只按逐次审批解释。
- 两项打卡使用 `clock_in_dual` v1.1，绑定精确账号/会话。外部写的安全契约是：不盲目重试、ACK
  只是执行证据而非独立读后验证、未知结果转为阻塞。财务启动补拉使用独立持久化任务
  `finance_startup_catchup` 的有效策略，不存在静态免审旁路。
- 发布在迁移和重启前根据有效外部写策略快照计算动态静默窗口；若将与外部写任务相撞，发布停止。
  停止服务前还会阻断正在 `RUNNING`/`VERIFYING` 的外部写、财务写或 destructive step。这些都是
  上线门禁，不是对某两个打卡任务的永久硬编码。
- 新 Agent 在发布 health/identity/post-018 project manifest/依赖记录全部通过前，以 release hold 同时保持 Scheduler
  paused 和 WorkflowRunner held（零领取、零 active Run）。该 held 进程不注册
  `finance_startup_catchup` DateTrigger，reload 与发布激活也不补建、改期或强制执行；只有未来未处于 hold 的
  正常服务启动才按持久化任务的启用状态注册启动补拉。签名管理接口先恢复并确认两者均可运行，再删除匹配
  本次 SHA 的 marker；删除前崩溃会让下一次启动继续 hold，响应丢失可幂等重试。该激活请求是发布提交点，
  发送后不再自动回滚可能已开始的业务动作。
- 保存或清除自动化账号凭据会先用账号级 MySQL 执行锁阻断显式或财务同步隐式引用账号的全部非终态受保护
  Run，再原子撤销精确定时免审并保留策略/Outbox 审计；锁、活动 Run 检查或撤权失败时凭据保持不变。
  每个受保护步骤在同一账号锁内重查当前策略并提交 `RUNNING`，旧免审失效时回到审批，已开始写只 reconcile。人工 terminal retry
  只支持原计划全部为 read/compute，任何写计划都必须
  新建 Command 并重新进入策略与审批，不能沿用 Scheduler 身份或历史豁免重放。

## 2026-08-11 架构基线

- 生产与 CI 统一使用 Python 3.10，Agent、Console 依赖分别由精确锁文件约束；ECS 发布按两份锁文件的联合哈希复用唯一共享环境，仅在依赖变化或校验失败时重建。发布成功后仍保留当次精确回滚包、上一版虚拟环境和数据库快照，直到业务验收完成后再以独立、有界操作清理。
- Console 保留 `ThreadingHTTPServer`，`app.py` 是组合入口，业务服务位于 `console/services/`，路由识别位于 `console/routes/`。
- TMS SessionBroker 是稳定门面，provider 执行、适配器、持久化和验证器已分层；`agent/agent/` 不再依赖 `tools` 或 `feishu`。
- Console 到 Agent 的调用全部进入 `/internal/v1/*`，使用统一 `ok/data/error` 契约；旧接口仅作鉴权后的 deprecated 兼容层。
- 数据库 DDL 只由版本化 SQL 迁移执行；仓库卫生、导入边界、接口契约、工具 Schema、Ruff、编译和测试均由 CI 门禁。
- 文本文件统一 UTF-8 无 BOM；聚合测试已按领域拆分，单个 Python 文件上限为 3,000 行。

## 项目定位

`物流 Agent` 是统一承接物流业务数据、流程和服务的 Agent + Console 双服务项目，不是单一 OCR
工具。`shared/business_modules.py` 是当前 14 个 Console 菜单身份的唯一不可变代码目录：

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
| `automation_accounts` | 业务账号 | `/automation-accounts` | 固定模块 |
| `llm_settings` | 智能模型 | `/settings/llm` | 固定模块 |
| `work_items` | 事项中心 | `/work-items` | 固定模块 |
| `system_settings` | 系统管理 | `/settings/accounts` | 固定模块 |

固定模块只由代码路由、既有登录/用户权限和业务前置条件控制；旧数据库生命周期状态和版本不能隐藏或阻断它们。
`/settings/system-status` 是仅真实 `super_admin` 可见的控制平面入口，不属于上述 14 个固定模块目录，只展示鉴权健康接口的白名单系统字段。`/settings/modules` 是退役重定向入口，旧 data/audit 子路径和 Agent 管理 API 只保留历史记录读取；数据库不能动态创造模块、菜单或实现。

## 主要业务数据关系

1. `纸质单据 -> OCR识别`
   纸质托运单进入 OCR 工作区，转成结构化运单字段。
2. `高德 + TMS -> 价格获取`
   地址库和平台报价生成标准价格资产，供客服报价和内部测算使用。
3. `OCR结果 + 支付/发票/平台流水 -> 财务对账`
   运单和流水汇总后生成月度损益、差异和校验结果。
4. `OCR + 价格 + 车辆信息 -> 车辆调度`
   根据运单数据和价格基线进行智能派车、线路优化和运力监控。
5. `运单 + 跟踪 + 价格 + 财务 + 调度 -> 客服/Agent 查询`
   固定命令和受管只读查询只消费已验证的结构化事实；没有来源或覆盖不完整时明确返回不可得，
   不由 LLM 补造业务结论。

## 启停脚本

- `console/start_backend.sh`
  WSL / Linux 下启动项目本地控制台。
- `console/stop_backend.sh`
  WSL / Linux 下停止项目本地控制台。

## 当前实现状态

- 项目级控制台目录现已独立为与 agent 并列的 `console/` 工作区。
- Console 导航固定登记上述 14 个模块身份；迁移 `027` 保存的历史生命周期状态和 Lite 审计仅供只读兼容，不参与固定模块菜单、页面、API 或 Command 可用性判断。依赖 Agent、账号、资源或其他业务数据的具体操作仍由各自合同独立失败关闭。
- OCR、运单、跟踪、回单、客服、融辉财务、调度、自动化、账号、智能模型、事项中心和系统管理均沿既有页面与服务边界运行；韵达财务适配器待真实来源验收后再启用。
- 财务工作台通过共享 MySQL 账本与 Agent `sync_finance_bills` 接通；当前生产只调度融辉三个财务角色，逐笔汇总、平台汇总与 signed-net 必须一致，旧 Excel ETL 已从线上运行时删除。
- `车辆调度` 已完成工作区页面（车辆列表、调度看板、快速调度面板），当前使用演示数据。
- 面向客户的独立 AI 客服模块仍未建立；现有固定命令、受限只读查询和客服工作台分别由 `agent/`、`feishu/` 与 Console 既有链路承载。
- Agent 公开面只保留精简 `/health`、飞书事件入口和带独立 Webhook Token 的 `/webhook/*`；主要管理与业务代理接口位于 `/internal/v1/*`。`/chat`、`/run-tool` 等旧入口只作为继续鉴权的 deprecated 兼容层，不得新增调用方。
- 调度模板、TMS 兼容接口和共享登录态仍由 Agent 承载；Console 通过受控内部接口访问，不把 `/tms/*` 当作新的控制平面写入口。
- Phase 7 迁移所需的飞书表格、Webhook 等资源配置统一保存在 Agent MySQL 的 `workflow_resources` 表中，不再依赖 N8N sqlite；Console 只读取闭合安全 descriptor，不直接读取 Token、表格 ID、范围、路径或原始配置。
- `sync_daily_should_sign` 必须显式绑定项目当前选择的独立 `r13_account_id` 与融辉 TMS `account_id`；后台可改绑为任意同系统有效账号，下一次运行只使用新绑定。R13 在精确账号登录后按原页协议从 `/gateway/public/aurora/auth` 读取实际站点范围，请求使用 R13 同源 `Origin` 与 `aurora-token`，不继承 SSO `Origin` 或附加 Bearer；中心账号使用空过滤，其他账号使用其 `siteCode`。缺账号上下文、刷新后范围漂移或请求体覆盖账号/站点都会阻塞。结构完整且权威总数为零的 R13 结果仍完成其他来源证据核验；若最终发布集合为空，则正常删除多维表旧记录、清空电子表格旧数据并回读为零行，真实来源异常则在投影变更前失败。同一个 TMS 登录态统一用于问题件、主单签收、轨迹核验和地址补全，不读取旧 `workflow_resources.phase7.r13_credentials`，也不接受请求体内联凭据或隐式默认账号。
- R13 只作为应签候选和冲突诊断；TMS 主单“签收”事件是唯一关闭证据。长历史签收按 31 天窗口完整分页并校验汇总/明细总量，离开当前 R13 的候选由迁移 `013` 按 1/3/7 天退避进行精确轨迹核验。
- `console` 现已与 Agent 统一使用同一套 MySQL，不再在运行时回退 SQLite。
- Agent、控制台、自动化调度、Phase 7 同步链路当前统一使用独立的 Agent MySQL；N8N 已从运行时链路移除，不再参与数据库读写、Webhook 映射或任务调度。
- `sync_daily_send_orders`、`sync_delivery_status`、`sync_daily_should_sign`、`sync_site_send_list`、`sync_arrive_list`、`sync_scan_codes`、`sync_arrival_stats` 已全部并入当前发布仓，由 `agent/tools/` 和 `agent/tms_runtime/` 统一承载；`sync_daily_send_orders` 写入飞书后会同步维护控制台 `waybills` SQL 表，并将明确返回的当前扫描状态写入 `scan_status`，后台 `/waybills` 可按融辉运单号检索。
- `sync_yunda_dispatch_forecast` 使用韵达独立登录态 `yunda`，默认每天 17:00 拉取次日“网点派件量预测主单表”并按应派时间覆盖写入飞书多维表格；融辉既有自动化继续使用 `ronghui/default` 登录态。
- `sync_yunda_send_waybills` 使用同一套韵达登录态 `yunda`，拉取当天“寄件运单管理”列表，补查快件跟踪详情与小眼睛解密接口后写入 `phase7.yunda_send_waybills_bitable`；历史按天累积，同一运单号重复同步时更新原记录，并同步维护控制台 `waybills` SQL 表，将明确返回的当前扫描状态写入 `scan_status`，后台 `/waybills` 可按韵达运单号检索。
- `init_waybills_sql_from_feishu` 可从飞书中的融辉寄件数据表和韵达寄件运单表全量回填控制台 `waybills` SQL 表，用作后台运单查询模块的初始化数据来源；该工具只写 SQL，不修改飞书。
- `r7_arrival_checkin` 和 `r7_departure_checkin` 已接入后台 `/automations` 和飞书直达指令；R7 登录独立于顶部 TMS 登录态，后台中走 R7 页面的任务会显示 R7 标识。该接入当前只完成 Command/Gateway 治理：在缺少真实任务 ID 集合与远端版本的权威只读预览前，计划固定返回 `IMPACT_PREVIEW_REQUIRED/BLOCKED_DATA`，不会执行第三方打卡写入。
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
