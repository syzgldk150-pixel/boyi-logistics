# tools

## 目录职责

`tools/` 是业务能力实现层。绝大多数功能修复、新增同步链路、飞书写入、财务 ETL、TMS 查询都从这里开始。

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
  - `internal_http.py`（本机 Agent HTTP 请求头；缺少 `AGENT_INTERNAL_API_TOKEN` 时显式失败）
  - 地址报价会同时调用融辉 `/tms/get_price` 和韵达 `/tms/yunda_price`；韵达结果包含录单页总价、网点明细，以及 `checkServiceScope.html` 返回的特殊区域加收/提醒；旧发站/到站兼容模式只走融辉
- 财务 ETL：
  - `finance_tool.py`
- 飞书 CLI：
  - `feishu_cli_tool.py`
- Phase 7 同步链路：
  - `send_order_sync_tool.py`（拉融辉寄件数据；支持 `target_date` 单日或 `start_date/end_date` 范围；按 `发件日期 + 运单编号` 安全替换同日飞书快照，并同步 upsert 到控制台 `waybills` 表供 `/waybills` 运单查询；`sql_only=true` 时只刷新控制台 SQL，不写飞书；签收状态映射到 SQL `status=signed/in_transit`，明确返回的当前扫描状态写入 `scan_status`）
  - `delivery_status_sync_tool.py`（查询并更新签收状态；无入参时定时扫描融辉寄件数据表的 `未签收明细` 视图，已签收才写回，并同步更新控制台 `waybills.status=signed`；仍兼容旧 webhook 单号 + record_id 模式）
  - `daily_sign_sync_tool.py`（维护按主单号累积的共享应签台账：R13 提供当前归属、系统应签日及参考签收状态，实际到货快照负责启动本系统应签日，问题件事件负责有效延期，只有 TMS 主单“签收”扫描能关闭；普通表固定输出九列并与多维表差异同步）
  - `daily_sign_backfill_tool.py`（只读影子计算或显式 `apply=true` 的历史回填；合并到货归档、当前应签表、R13 历史、TMS 问题件及签收扫描，来源缺失时只标记待核验，不猜测日期、不发布飞书）
  - `daily_sign_rules.py` / `daily_sign_store.py`（共享应签计算规则和版本化 MySQL 仓储；多个采集/统计/问题件脚本必须调用这里，禁止复制应签口径）
  - `site_send_list_sync_tool.py`
  - `arrival_stats_sync_tool.py`（统计到货数据；用当天扫描数据补齐派件预报缺失主单，写主/副统计表 + 未齐货物表 + 归档快照，并以本次统计结果自动刷新分批及有发未到表；所有输出步骤完整成功后才激活当日共享到货快照，失败不得覆盖上一成功版）
  - `split_pending_snapshot.py`（统计与分批工具共享的 A:S 表头校验、未齐分类、MySQL 快照和“分批及有发未到表”覆盖刷新；零候选会清空旧行）
  - `arrive_list_sync_tool.py`（拉 TMS 派件预报基础清单，过滤 `H...` / `HR...` 回单号；写 waybill_data + 主/副到货清单表，并保存完整成功的预计到货快照；预计数据不启动应签计时）
  - `yunda_dispatch_forecast_sync_tool.py`（拉韵达网点派件量预测主单表；默认次日应派时间，按应派时间覆盖指定飞书多维表格）
  - `yunda_send_waybills_sync_tool.py`（拉韵达寄件运单管理；支持 `target_date` 单日或 `start_date/end_date` 范围；补充快件跟踪详情和小眼睛解密字段，按运单号 upsert 到飞书多维表格，同步 upsert 到控制台 `waybills` 表供 `/waybills` 运单查询，并在单日同步时刷新普通飞书电子表格副本；`sql_only=true` 时只刷新控制台 SQL，不读写飞书或电子表格；默认 SQL `status=in_transit`，明确返回的当前扫描状态写入 `scan_status`）
  - `init_waybills_sql_from_feishu_tool.py`（SQL 初始化回填；从飞书融辉寄件数据和韵达寄件运单表全量读取历史记录，按运单号 upsert 到控制台 `waybills`，不删除历史）
  - `phase7_mysql_store.py`（Phase 7 共享 MySQL 存储；包含 `waybill_data` 到货基础表，也维护控制台 `waybills` 表的同步 upsert 入口；`waybills.status` 使用 `pending/in_transit/signed/cancelled`，`waybills.scan_status` 保存明确来源返回的当前扫描状态，同步时必须保留手动作废的 `cancelled`）
  - `phase7_sync_common.py`
- TMS 投诉/问题件上报：
  - `self_pickup_problem_upload_tool.py`（"自提到货问题件"：读飞书到货表，筛 `邵阳自提部` 以及 `邵阳大祥S站 + 派送方式=自提` 的单号，先 dry-run 预览，确认后调 `/tms/self_pickup_problem_upload` 上传 `开单为自提件` 问题件和货拉拉截图；自提部账号 `ronghui_self_pickup_problem`，大祥S站账号 `ronghui_daxiang_s`，不要使用 `price_default`）
- R7 到达打卡：
  - `r7_arrival_checkin_tool.py`（直接调用 `agent/tms_runtime/scripts/auto_checkin_r7.py`；使用 R7 登录，不依赖 TMS 共享登录态；写 `r7_arrival_checkin_log` 并按 `daily_success_limit` 控制当天后续定时跳过）
  - R7 事件、状态和到达/发车打卡日志表由 `../migrations/006_r7_runtime_tables.sql` 创建；工具仅校验表存在，禁止在运行时建表。
- 工具对外注册定义：
  - `registry.yaml`

## 修改原则

- 改单条业务链，优先只动对应工具文件和 `registry.yaml`
- 多个同步工具共享逻辑时，优先提取到 `phase7_sync_common.py`
- `phase7_sync_common.sync_sheet_snapshot()` 清空普通飞书电子表格时走 `feishu_cli_tool.clear_sheet`，不要改回写入大量空白单元格；否则大范围清空会产生过多 OpenAPI 写入分块。`clear_sheet` 通过飞书行维度删除旧快照行，清空范围必须从 A 列开始，配置里的 `Sheet1` 这类标题会在实际调用前解析成飞书 `sheet_id`，并按工作表当前最大行数裁剪清空结束行；后续 `write_sheet` 会在写入前自动补足目标范围需要的行数。
- 普通飞书电子表格只有一个页签时，`feishu_cli_tool` 会把旧配置里的 `Sheet1` 自动映射到唯一页签的真实 `sheet_id`，避免用户重命名页签后触发 `sheetId not found`。
- Snapshot sync tools generally treat an empty TMS fetch (`records=[]` or `data=[]`) as `no_fetched_rows`: skip Feishu Bitable writes/deletes, skip ordinary spreadsheet refresh, and skip SQL `replace_date` when applicable, so a source-side empty result cannot clear existing target data. This applies to `yunda_send_waybills_sync_tool.py`. `daily_sign_sync_tool.py` is ledger-based: an empty but complete R13 result never clears history and continues to retain all TMS-unconfirmed records; an incomplete R13 result stops publication.
- `site_send_list_sync_tool.py` is the exception: an empty TMS fetch is an intentional empty snapshot and must still clear/overwrite the Feishu Bitable and ordinary spreadsheet targets.
- TMS 兼容接口返回 `AUTH_REQUIRED` / `AUTH_PENDING_CODE` 时，工具必须直接返回顶层 `error_code`，不得包装为“返回格式异常”；统一使用 `phase7_sync_common.tms_auth_error_result()` / `raise_tms_auth_error_if_present()`

## Phase 7 补充说明

- `tms_tool.py`
  - 默认走 `http://127.0.0.1:9000/tms/*` 兼容层
  - 所有本机 Agent HTTP 调用必须使用 `internal_http.internal_api_headers()` 发送 `X-Agent-Internal-Token`
  - 当前线上权威执行源已切换为 `agent/tms_runtime/`
  - 图片/短信验证码共享登录态由 `agent` 的 `/admin/tms/session/*` 管理

## 相关文档

- `../docs/code_navigation_index.md`
- `../docs/rules_and_definitions.md`
- `../../1/AGENTS.md`

- 分批差错及问题件：
  - `split_pending_problem_upload_tool.py` dry-run 返回未完成候选、步骤状态、隐藏成功数量和指纹；正式参数缺少 `selected_bill_codes` / `preview_fingerprint` 必须拒绝。
  - 正式执行先校验最新来源与状态指纹，再刷新全部当前未齐 Sheet/MySQL 快照；融辉外部操作只处理所选运单，并通过 `phase7_mysql_store.py` 独立回写差错和问题件结果。
  - `sync_arrival_stats` 成功完成统计输出后调用共享快照模块；结构异常或重复运单显式失败并保留旧快照，正常统计但全部到齐时写空候选以清理目标旧行，且不得调用融辉上报。
  - 完整成功的问题件上报必须同时写入 `waybill_problem_events`，保留外部唯一 ID、精确类型和 TMS 登记时间；后续补齐或当前未齐快照删除不得抹除历史延期证据。

## 每日应签共享台账

- 候选为当前/历史未关闭 R13、成功到货统计及 R13 后续改单转入单号的并集。R13 消失、显示已签或空结果均不能关闭记录。
- `daily_sign_ledger` 中 B 口径为 R13 原始应签时间，C 口径为本系统测算时间；无实际到货时 C 与到货件数为空。普通电子表格必须先精确校验九列表头，再写新数据，成功后才清理尾部旧行。
- 正常到齐为到货业务日次日 23:59:59；无有效少货/分批事件的部分到货同样次日应签；17:00 前完整成功的少货/分批事件使未齐期间 C 为空，补齐当天恢复为 23:59:59。
- 只有精确类型“客户要求延迟派送”“联系不上收件人”且 TMS 登记时间严格早于 17:00:00 的人工问题件可顺延到登记次日，多个事件只能把日期延后。
- 只有扫描单号等于主单号且扫描类型精确等于“签收”的 TMS 事件可关闭；R13 签收状态只触发少量冲突单精确轨迹核验，子单签收不得关闭主单。
- R13/问题件分页、结构或完整性失败时停止发布并保留上一成功表；签收扫描失败允许新增候选但不得删除旧行，运行结果必须标记降级。
