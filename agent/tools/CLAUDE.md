## 每日应签源码归属

每日应签业务规则、采集和渲染位于仓库根 `agent/service_v2_plugins/sync_daily_should_sign_v2/payload/business/`（顺延与到齐规则在其中唯一的 `daily_sign_rules.py`）；`agent/tools/daily_sign_store.py` 仅持久化和回读核验，`agent/tools/daily_sign_values.py` 仅数据规范化。旧 V1 离线材料在 `agent/legacy/`，不进入发布清单。

# tools

## 目录职责

`tools/` 保留三类代码：

- 仍在使用的直接 reader 与业务入口：运单/单号查询、报价、OCR、财务与经营只读查询、回单、客服问题件等，由组合根注入或经 `registry.yaml` 调用。
- Host 原语复用的共享仓储与 helper：`phase7_mysql_store.py`、`daily_sign_store.py`、`split_pending_snapshot.py`、`feishu_cli_tool.py`，以及旧同步工具中被 `../plugin_core_adapters/` 复用的表头、区间和记录转换函数。
- 旧 V1 整段同步工具：只用于离线回归和上述 helper 复用，不再作为执行入口。

已插件化的业务算法改在对应 `../service_v2_plugins/<id>_v2/payload/`；只有共享接口或核心 primitive 变化才修改这里，不把整段旧工具重新接入运行时。

## 修改入口

- 运单查询：
  - `query_tool.py`
- 单号追踪：
  - `track_waybill_tool.py`（调用 Agent `/tms/tracking_query`，统一分发融辉 TMS、韵达和专线单号）
- OCR 工具封装：
  - `ocr_tool.py`
- 价格查询与 TMS 对接：
  - `price_tool.py`
  - `tms_tool.py`
  - `internal_http.py`（工具访问底层 TMS target 的能力请求头；缺少当前工具短期执行能力时显式失败，不读取或回退到 `AGENT_INTERNAL_API_TOKEN`）
  - 地址报价会同时调用融辉 `/tms/get_price` 和韵达 `/tms/yunda_price`；韵达结果包含录单页总价、网点明细，以及 `checkServiceScope.html` 返回的特殊区域加收/提醒；旧发站/到站兼容模式只走融辉
- 财务账本同步：
  - `finance_sync_service.py`（融辉/韵达多账号、多日期编排与提交前校验）
  - `sync_finance_bills_tool.py`（工具入口与进程/数据库双重单实例锁）
  - 真实页面采集位于 `../agent/tms_runtime/scripts/*finance*`，共享领域与仓储位于 `../../shared/finance/`
- 飞书 CLI：
  - `feishu_knowledge.py`、`feishu_knowledge_pdf.py`：限定授权 Wiki 空间的只读 CLI；支持在线正文与 PDF 文字层，PDF 子进程有界解析、按命中页及邻页摘录并标注页码，不把图表或整书误报为已读。不切换共享身份，权限不足和解析不完整明确失败。维护边界见 `../../docs/shipment_knowledge_queries.md`。
  - `feishu_cli_tool.py`
  - `receipt_feishu_detail_query_tool.py`（回单页缺失韵达明细时使用的精确只读能力；只接受 `waybill_no`，服务端固定飞书资源和字段，必须证明分页完整且只命中一条，歧义、缺字段或分页未知均显式失败；不得改用宽泛 `feishu_operation`）
- 共享仓储与 helper：
  - `phase7_mysql_store.py`（Phase 7 共享 MySQL 存储：`waybill_data` 到货基础表、扫描快照 `scan_codes`、分批未齐表，以及控制台 `waybills` 表的同步 upsert 入口；`waybills.status` 使用 `pending/in_transit/signed/cancelled`，`waybills.scan_status` 保存明确来源返回的当前扫描状态，同步时必须保留手动作废的 `cancelled`）。生产库在跨地域 RDS 上，单次往返约 35ms：批量写入的 `executemany` SQL 必须在 VALUES 中全部使用占位符（字面量会让 PyMySQL 逐行执行），`ensure_phase7_tables()` 按进程和连接目标只做一次成功校验。
  - `daily_sign_store.py` / `daily_sign_values.py`（每日应签版本化 MySQL 仓储与数据规范化；应签计算只在插件包的 `daily_sign_rules.py`，禁止在宿主复制口径）
  - `split_pending_snapshot.py`（统计与分批共享的 A:S 表头校验、未齐分类、MySQL 快照和“分批及有发未到表”覆盖刷新；零候选会清空旧行）
  - `phase7_sync_common.py`
- 旧同步工具（V1 整段入口仅离线回归；现行算法在对应 V2 包）：
  - `send_order_sync_tool.py` → `sync_daily_send_orders_v2`；`../plugin_core_adapters/waybill_query.py` 复用其控制台运单记录转换与日期解析。
  - `delivery_status_sync_tool.py` → `sync_delivery_status_v2`；Host 复用 `delivery_status_common.py` 的状态与运单规范化。
  - `site_send_list_sync_tool.py` → `sync_site_send_list_v2`。
  - `arrive_list_sync_tool.py` → `sync_arrive_list_v2`；Host 复用其表标题与表格资源写入 helper。
  - `arrival_stats_sync_tool.py` → `sync_arrival_stats_v2`；Host 复用其统计表区间、标题与归档 helper。
  - `scan_sync_tool.py` → `sync_scan_codes_v2`。
  - `yunda_dispatch_forecast_sync_tool.py` → `sync_yunda_dispatch_forecast_v2`。
  - `yunda_send_waybills_sync_tool.py` → `sync_yunda_send_waybills_v2`；Host 复用其控制台运单记录转换。
- 运维与回填工具：
  - `daily_sign_backfill_tool.py`（只读影子计算或显式 `apply=true` 的历史回填；来源缺失时只标记待核验，不猜测日期、不发布飞书）
  - `init_waybills_sql_from_feishu_tool.py`（SQL 初始化回填；从飞书融辉寄件数据和韵达寄件运单表全量读取历史记录，按运单号 upsert 到控制台 `waybills`，不删除历史）
- TMS 问题件上报：
  - `self_pickup_problem_upload_tool.py`（legacy 兼容只读封装，不参与飞书当前预览或正式写入；当前飞书链路调用 V2 committed project route 的 dry-run，并从已验证候选 Invocation 确认。兼容脚本仍只按 `邵阳自提部` 以及 `邵阳大祥S站 + 派送方式=自提` 规则读取来源，账号必须由自动化项目角色显式绑定，任何路径都不得注入固定账号或默认 session profile）
- R7 到达/发车打卡（当前发行已移除）：
  - `r7_arrival_checkin_tool.py`、`r7_departure_checkin_tool.py` 只为追溯保留，不进入当前自动化、Scheduler 和飞书入口。R7 相关表由 `../migrations/006_r7_runtime_tables.sql` 创建；工具仅校验表存在，禁止在运行时建表。
- 经营摘要执行统计：
  - `query_automation_operations` 的 `1.1.0` 只读合同返回当前 `invocations` 及显式记录来源；实际处理器由组合根绑定 `../agent/business_query.py`，脚本占位入口缺少组合根时明确失败。终态成功率包含失败、取消及未知写，不统计旧 Command/Run。
- 工具对外注册定义：
  - `registry.yaml`

## 修改原则

- 改单条业务链，优先只动对应工具文件和 `registry.yaml`
- 多个同步工具共享逻辑时，优先提取到 `phase7_sync_common.py`
- `phase7_sync_common.sync_sheet_snapshot()` 清空普通飞书电子表格时走 `feishu_cli_tool.clear_sheet`，不要改回写入大量空白单元格；否则大范围清空会产生过多 OpenAPI 写入分块。`clear_sheet` 通过飞书行维度删除旧快照行，清空范围必须从 A 列开始，配置里的 `Sheet1` 这类标题会在实际调用前解析成飞书 `sheet_id`，并按工作表当前最大行数裁剪清空结束行；后续 `write_sheet` 会在写入前自动补足目标范围需要的行数。
- 普通飞书电子表格只有一个页签时，`feishu_cli_tool` 会把旧配置里的 `Sheet1` 自动映射到唯一页签的真实 `sheet_id`，避免用户重命名页签后触发 `sheetId not found`。
- Snapshot sync tools generally treat an empty TMS fetch (`records=[]` or `data=[]`) as `no_fetched_rows`: skip Feishu Bitable writes/deletes, skip ordinary spreadsheet refresh, and skip SQL `replace_date` when applicable, so a source-side empty result cannot clear existing target data. This applies to `yunda_send_waybills_sync_tool.py`. Daily sign is ledger-based: a structurally complete zero-row R13 result is a valid empty publication set; each Feishu sink then clears its earlier projection and freshly reads back zero rows, while source failures never count as zero rows.
- `site_send_list_sync_tool.py` is the exception: an empty TMS fetch is an intentional empty snapshot and must still clear/overwrite the Feishu Bitable and ordinary spreadsheet targets.
- TMS 兼容接口返回 `AUTH_REQUIRED` / `AUTH_PENDING_CODE` 时，工具必须直接返回顶层 `error_code`，不得包装为“返回格式异常”；统一使用 `phase7_sync_common.tms_auth_error_result()` / `raise_tms_auth_error_if_present()`

## Phase 7 历史兼容说明

以下工具与 Runner 描述用于保留接口和离线回归；新增业务算法从 V2 插件维护入口定位，不恢复旧领取链。

- `tms_tool.py`
  - 默认走 `http://127.0.0.1:9000/tms/*` 兼容层
  - 所有工具对本机 `/tms/*` 的调用必须使用 `internal_http.internal_api_headers()` 发送按工具/target 绑定的短期执行能力；该兼容函数名不代表共享 Token，工具子进程不得继承或发送 `X-Agent-Internal-Token`
  - 当前线上权威执行源为 `agent/tms_runtime/`
  - 图片/短信验证码共享登录态由 Agent 的 `/admin/accounts/{account_id}/*` 管理；`/admin/tms/*-session` 仅是旧兼容入口

## 相关文档

- `../docs/code_navigation_index.md`
- `../docs/rules_and_definitions.md`
- `../../AGENTS.md`

- 分批及有发未到问题件：
  - `split_pending_problem_upload_tool.py` dry-run 返回未完成候选、步骤状态、隐藏成功数量和指纹；该只读预览指纹的字段与规范化序列化必须和签名 action 保持完全一致；正式参数缺少 `selected_bill_codes` / `preview_fingerprint` 必须拒绝。
  - 分批与自提兼容工具缺少项目账号绑定时必须在来源读取或登录前返回 `blocked_config`；问题件权威回读成功后，分批链路必须先写并核验每日应签问题事件，最后才把问题件结果标记为成功，避免事件失败后候选被永久隐藏。
  - 正式执行先校验最新来源与状态指纹，再刷新全部当前未齐 Sheet/MySQL 快照；融辉外部操作只处理所选运单，并通过 `phase7_mysql_store.py` 独立回写问题件结果。
  - `0 < 已到 < 应到` 不再进入投诉方登记，直接登记“少货/分批 / 交接异常”，内容严格为 `应到XX件 实际到XX件`；遗留 `complaint_status` 字段统一写 `not_applicable`，只以问题件权威回读判定成功。
  - 统计成功完成输出后调用共享快照模块；结构异常或重复运单显式失败并保留旧快照，正常统计但全部到齐时写空候选以清理目标旧行，且不得调用融辉上报。
  - 完整成功的问题件上报必须同时写入 `waybill_problem_events`，保留外部唯一 ID、精确类型和 TMS 登记时间；后续补齐或当前未齐快照删除不得抹除历史延期证据。

## 每日应签共享台账

- 只使用自动化项目业务账号池绑定的 `r13_account_id`；后台改绑后下一次运行必须使用新账号，不得回落默认或固定 ID。R13 站点在精确账号登录后按原页协议从 `/gateway/public/aurora/auth` 读取，请求只使用 R13 同源 `Origin` 与 `aurora-token`，不得继承 SSO `Origin` 或附加 Bearer；中心账号使用空站点列表，其他账号使用其真实 `siteCode`。调用参数或嵌套请求体覆盖均拒绝。历史 TMS 账号角色只作为可选配置保留，运行不调用。
- 发布集合就是本次读取的 R13 截至今天全部未签收单（无起始日期，含超过30天旧单），不追加历史台账候选，也不按 TMS 签收或本地计算时间二次删行；原页字段直接透传，十二列协议及原页证据见仓库根 `docs/identity_and_unified_chat.md`。融辉子单号必须使用 `phase7_mysql_store.py` 的共享识别规则排除，禁止作为应签主单发布。
- 每轮发布前核对 R13 当前查询与发布集合的运单号完全一致。结构完整且权威总数为零是合法结果，最终空发布清除旧投影并回读为零；R13 分页、结构或完整性失败时停止发布并保留上一成功表，真实来源异常不得当作零行。普通表按十二列 A:L 写入和清尾，可从精确匹配的旧八/九列表头迁移。
- 顺延规则由插件包内唯一 `daily_sign_rules.py` 计算：正常到齐为到货业务日次日 23:59:59，部分到货初始同样次日应签；未齐期间，17:00 前完整成功的少货/分批登记只能把应签日顺延至该登记次日 23:59:59，每天继续延期需要新的有效登记，重读旧事件不自动延期；补齐当天应签为当天 23:59:59。只有精确类型“客户要求延迟派送”“联系不上收件人”“客户拒收/拒付费用”“客户原因要求自提”“改派送地址”且完整成功、登记时间严格早于 17:00:00 的人工问题件可顺延到登记次日，多个事件只能把日期延后。Host 验证并保存插件明确提交的布尔判断，不维护或重算类型规则。回归见 `tests/test_daily_sign_ledger.py` 与仓库根 `tests/test_daily_sign_v2_packaged_protocol.py`。
- 到货件数按运单号取截至当前业务日该单最近一次成功有效的“统计”累计值，不跨日求和；当天统计未出现的单号仍使用该单最近快照，并记录来源业务日期、运行编号及是否使用前日快照。最新快照为空不得取更早数字，从未有统计记录时普通表显示“无数据”、多维表数值列留空；真实0保持0。
- 动态黄色条件格式由插件 `daily_sign_format.py` 维护，Host 仅提供精确绑定表的格式读写（`feishu_sheet_formats.py`）；写后回读与协议依据见仓库根 `docs/identity_and_unified_chat.md`。
