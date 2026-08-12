# 物流 Agent 系统

> 企业级物流业务自动化系统，包含价格采集、财务对账、OCR识别、车辆调度、AI客服五大模块。
> 远期目标：LangChain/LangGraph Agent 系统整合。

---

## ECS 发布入口

- 当用户提到“同步 ECS”“发版”“发布到 ECS”“部署到 ECS”时，默认先使用固定脚本，不要先到处搜索其它发布命令：
  - `powershell -ExecutionPolicy Bypass -File "\\wsl.localhost\Ubuntu\home\deng\projects\boyi-logistics\agent\deploy\publish_to_ecs.ps1"`
- 这个脚本是标准发布入口，默认 `auto` 模式会自动判断发布 `agent`、`console` 或两者一起发布，并执行远端健康检查。
- 只有在用户明确要求 `-Target all`、`-SkipRestart`、`-SkipHealthCheck` 等特殊参数时，才偏离这条默认命令。
- 生产控制台固定入口为 `https://boyi.homes`；Nginx 配置、ACME 启动配置和续期 reload 钩子统一维护在 `deploy/nginx/`，公网不得直接暴露 Console `8765` 端口。
- 数据库结构由 `migrations/` 的顺序 SQL 和 `scripts/run_migrations.py` 管理；运行期模块不得新增 `CREATE TABLE`、`ALTER TABLE` 或吞掉迁移异常，详见 `docs/database_migrations.md`。
- 发布白名单必须包含受管的 `migrations/` 和 `scripts/`，但不得递归发布业务数据、凭据或运行态目录。
- Agent 依赖以 Python 3.10 的 `requirements.txt` 和精确 `requirements.lock` 为准；Agent 与 Console 共用一个按两份锁文件联合 SHA-256 标识、并分别通过精确依赖校验的 `runtime-deps-<hash>` 虚拟环境。只有任一锁文件内容变化或环境校验失败时才构建新环境并原子切换。失败时使用当次暂存目录中的临时材料恢复旧环境和源码。健康检查成功后必须立即删除临时回滚材料、持久发布备份和所有非当前虚拟环境，ECS 最终只保留一个运行环境。提交前执行 Ruff、工具清单、仓库卫生、内部 API 契约和运行时导入边界检查，GitHub Actions 会独立验证 Agent 与 Console 的锁文件。

## 本地 WSL 与 ECS 运行隔离

- ECS 是飞书机器人、定时任务和生产自动化的唯一长期运行源；本地 WSL 只用于开发调试和临时验证。
- 本地 WSL 启动 `agent` 或飞书 WebSocket 只允许在测试窗口内运行，测试完成后必须停止，避免与 ECS 同时消费飞书消息。
- 执行“同步 ECS”“发版”“发布到 ECS”“部署到 ECS”前，先确认本地 WSL `agent` 已停止；常用检查是 `curl http://127.0.0.1:9000/health` 不再返回本地实例，必要时执行 `tmux kill-session -t codex-agent`。
- `/health` 只用于存活与发布 SHA 校验；排查组件、实例和工具状态时使用携带内部 Token 的 `/internal/v1/health`，不要把本地 MySQL/TMS 状态误判为 ECS 状态。

## HTTP 安全边界

- Agent 固定默认监听 `127.0.0.1:9000`。
- Console、TMS 工具和飞书内部调用必须发送 `X-Agent-Internal-Token`，其值来自 `AGENT_INTERNAL_API_TOKEN`；缺失配置必须显式失败。
- 只有 `/health`、飞书事件入口和带独立 Webhook Token 的 `/webhook/*` 属于公开路径。统一策略在 `agent/http_security.py`，不得在各路由重复实现。
- 所有日志和持久化审计通过 `shared/redaction.py` 脱敏；原始请求体、密码、Token、Cookie 和 Authorization 不得落盘。
- `agent/agent/` 不得导入 `tools` 或 `feishu`；直接工具执行器和飞书告警回调统一在 `main.py` 注入，TMS 会话事件通过 `shared/runtime_events.py` 发布。
- `session_broker.py` 只保留稳定门面；provider 执行、adapter、状态持久化和响应验证分别维护在同目录的 `session_provider_base.py`、`session_adapters.py`、`session_persistence.py` 与 `session_validation_service.py`。
- 新内部路由只能加入 `/internal/v1/*` 并返回 `ok/data/error`；旧路由只作为已鉴权的 deprecated 兼容层，不得新增调用方。

## 快速定位入口

收到“小改动 / 小修复 / 新增一个局部功能”时，**先读 `docs/code_navigation_index.md`，再进入目标目录的 `AGENTS.md` 或 `CLAUDE.md`，最后才打开命中的代码文件**。不要默认全仓扫描。

## 模块清单

| 模块 | 代码路径 | 文档路径 | 状态 |
|------|----------|----------|------|
| 控制台工作区 | `console/` | `docs/project_overview.md` | 与 agent 并列部署 |
| 价格获取 | `price_scripts/` | `docs/price_scripts/` | 已完成（持续维护） |
| 财务模块 | `../shared/finance/`、`agent/tms_runtime/scripts/*finance*`、`tools/finance_sync_service.py`、`tools/sync_finance_bills_tool.py`、`agent/finance_brain.py`、`agent/llm_settings.py`、`../console/finance_service.py` | `docs/finance_module.md` | 唯一财务架构：融辉/韵达真实页面逐笔采集、共享账本、版本化标准科目、异常审批、运单净额、知识镜像及 DeepSeek/GLM 全局配置；00:10 同步并禁止自动模型回退 |
| OCR识别 | `console/` | `docs/ocr/` | 运行入口在控制台工作区 |
| 车辆调度 | `console/` | `docs/dispatch/` | 运行入口在控制台工作区 |
| Agent 自动化能力 | `agent/ + feishu/ + tools/` | `docs/agent_automation/` | 飞书机器人承载的全部能力都在此（直达指令 / pending 状态机 / 登录恢复） |
| AI客服 | `agent/ + feishu/`（规划中） | `docs/ai_service/` | 暂未开发；待启动后从 Agent 自动化能力剥离客户对话能力 |
| 通用规范 | — | `docs/common/` | 活跃 |

---

## 文档结构

```
docs/
├── code_navigation_index.md             # 修改入口索引：按需求类型定位到具体文件
├── project_overview.md               # 项目级架构：模块关系/本地控制台/路由
├── finance_module.md                 # 融辉/韵达财务同步、账本、绑定、BI 与校验口径
├── ai_service/
│   └── module_overview.md           # 模块文档：AI客服定位与上下游
├── agent_automation/
│   └── module_overview.md           # 模块文档：飞书直达指令 / pending 状态机 / 登录恢复
├── price_scripts/
│   ├── 01-amap-address-fetch.md      # 模块文档：高德POI地址库
│   ├── 02-tms-price-fetch.md       # 模块文档：TMS批量报价采集
│   ├── 03-quote-sheet-generation.md        # 模块文档：单价计算与客户报价
│   ├── project_structure.md         # 项目结构：目录树/scripts/数据流/业务流程
│   ├── tms-batch-quote-resume.md  # 操作手册：断点续传运行方法
│   ├── tms_price_structure_analysis.md      # 分析报告：逐公斤扫描验证
│   └── data_accuracy_audit_report.md   # 审计报告：全链路数据准确性
├── ocr/
│   └── module_overview.md             # 模块文档：OCR工作区与本地控制台接入
├── dispatch/
│   └── module_overview.md             # 模块文档：车辆调度工作区与运力管理
└── common/
    └── finance_data_baseline.md     # 编码规范：Decimal精度/自验证
```

所有 .md 文档使用 YAML frontmatter，包含 `module`、`type`、`tags`、`related`、`status` 字段。

---

## Codex 文档检索协议

### 核心原则

**不全量加载文档，按需检索。** 本 AGENTS.md 是唯一始终加载到上下文的文件。

### 检索流程

1. **接到任务时**：根据任务关键词判断涉及哪个模块
2. **先读定位索引**：优先查看 `docs/code_navigation_index.md`
3. **定位文档**：用 `Grep` 搜索 `docs/` 下的 frontmatter `tags` 或 `module` 字段
   ```
   # 按模块搜索
   Grep pattern="^module: 价格获取" path="docs/"
   # 按标签搜索
   Grep pattern="tags:.*同名消歧" path="docs/"
   # 按类型搜索
   Grep pattern="^type: 审计报告" path="docs/"
   ```
4. **进入目标目录**：读取对应目录下的 `AGENTS.md` 或 `CLAUDE.md`
5. **读取命中文档**：只读取与当前任务相关的文档，不全量加载
6. **跨模块任务**：搜索 `docs/` 全目录
7. **修改代码后**：检查并同步更新 `docs/` 下对应文档

### 搜索优先级

| 场景 | 先搜什么 |
|------|---------|
| 修改某个脚本 | `docs/{模块}/` 下对应模块文档 |
| 理解数据流 | `docs/price_scripts/project_structure.md` 或 `data_accuracy_audit_report.md` |
| 运行/续跑脚本 | `Grep pattern="tags:.*运行命令" path="docs/"` |
| 精度/财务规范 | `docs/common/finance_data_baseline.md` |
| 新建脚本 | 先搜已有同功能文档，检查是否可复用 |

### 文档更新规则

- 修改代码时，同步更新 `docs/` 下对应文档
- 新增脚本时，在对应模块文档中补充文件说明
- 变更数据流时，更新 `project_structure.md` 和 `data_accuracy_audit_report.md`
- 所有文档更新时同步修改 frontmatter 的 `updated` 字段

---

## 敏感信息

各模块的敏感变量统一存放在对应目录的 `.env` 文件中，详见各模块 AGENTS.md。

---

## 项目本地控制台

- 本地入口：`http://127.0.0.1:8765/`
- 实时消息监控大盘：首页 `/`，Console 通过 `/monitoring/summary`、`/monitoring/stream`、`/monitoring/detail-link` 代理 Agent `/internal/v1/admin/monitoring/snapshot` 和 `/internal/v1/admin/monitoring/detail-link`；Agent 只返回分类、数量、状态和非敏感原系统跳转标识。
- OCR 工作区：`http://127.0.0.1:8765/ocr`
- 韵达录入页签：`http://127.0.0.1:8765/ocr?mode=yunda`，Console 同源 `/ocr/yunda/live/...` 转发 GET/POST/PUT/PATCH/DELETE 到 Agent `/tms/yunda_waybill_proxy`，Agent 使用 `yunda` 登录态代理韵达原始 `kyinms.yunda56.com/ky_inms/public/...` 页面与接口，成功保存后由 Console 写入本地 `waybills`，并通过保存响应里的 `shipnow_autoprint_url` 打开 Console 本地热敏打印页。
- 融辉录入页签：`http://127.0.0.1:8765/ocr?mode=ronghui`，Console 只加载当前录单模式 iframe，非当前模式原页延迟到切换后加载；同源 `/ocr/ronghui/live` 转发 GET/POST/PUT/PATCH/DELETE 到 Agent `/tms/ronghui_waybill_proxy`，Agent 使用账号管理中的大祥报价 `price_default` 登录态以浏览器 XHR 头解析菜单 id `1622` 的融辉原始 `/widget/home` 运单录入页，菜单或页面返回登录页时透传 `AUTH_REQUIRED`，融辉原页代理目标在调度层允许 12 并发以承接浏览器首屏接口突发，固定字典/站点/客户下拉 GET 初始化接口在 Agent 侧短缓存 5 分钟且忽略 `_` 缓存破坏参数，运行时代理脚本会同步移除这些安全初始化接口 URL 的 `_` 参数以启用 Chrome 缓存，不缓存生成单号、日期、保存提交或带关键字的地址查询，`/static/...` 大 JS/图片资源直连融辉原站以避免代理大文件，CSS 与字体资源保留同源代理以避免字体 CORS 导致 MiniUI 图标显示异常，静态 CSS/字体响应带 `Cache-Control: public, max-age=86400` 供 Console 保留，并把大祥报价登录态里的必要 `userInfo` 字段桥接到同源 Cookie，初始地图 iframe 延迟到目的地/派件网点地图相关操作时再加载，重写允许的业务页面/接口链接、JSON/XML/XHTML/text/SVG 响应 URL（含 `\/` 斜杠转义形式）、协议相对 URL、跳转响应头 `Location/Refresh`、移除响应头和 HTML meta CSP、静态和动态 meta refresh、静态和动态 `<base href>`、静态和动态 iframe `srcdoc`、静态和动态 `<object data>`、组件 `url/data-url/data-src/data-href/poster/background` 属性、动态样式 URL（`style/cssText/setProperty/insertRule`，含 `url(...)` 与 `@import`）、动态 XHR/fetch/jQuery Ajax/MiniUI `mini.open`/`mini.ajax`/Beacon/SSE/Worker/表单提交、DOM URL 属性（含图片、脚本、iframe、表单、媒体、source/track/embed/object、area/input image）、动态 HTML 注入入口（`innerHTML/outerHTML/insertAdjacentHTML/document.write/writeln`）、DOM 子树和 URL 属性变化扫描（MutationObserver）、`window.open` URL、`history.pushState/replaceState` URL 和静态 `location.assign/replace` 参数，成功保存后由 Console 记录请求/响应快照。
- 统一回单管理：Console `/receipts/sync` 调 Agent `/tms/receipts_sync`，脚本位于 `agent/tms_runtime/scripts/receipts_sync.py`；融辉使用 `price` 登录态按方向请求 `FIND_SEND_RETURN_PROCESS`（寄方跟踪）或 `FIND_DISP_RETURN_PROCESS`（派方处理），并按处理记录 `FIND_TAB_PROCESS_RECORD` 继续解析附件：人工记录查 `FIND_TAB_PROCESS_RECORD_PATH`，系统生成记录按原页 `renderReplyFiles` 逻辑查 `FIND_TAB_PIC_SCAN_ALL`；韵达使用 `yunda` 登录态从实际回单页的 `#dg` datagrid 配置发现数据 URL 后拉取。返回给 Console 的数据只包含标准化回单字段、附件来源 URL/hash 和统计，不返回 Cookie、Token、密码、SSO 参数；Console 的 `/receipts/yunda/live/...`、`/receipts/ronghui/live/...` 原页模式继续复用现有 waybill proxy 脚本；Console 回单详情补齐可调用 `/tms/query_waybill_detail`，韵达飞书兜底走 `tools/feishu_cli_tool.py` 的 `feishu_operation.search_records`，只用 `records/search` + `运单编号` 等值筛选查询单票业务字段，不分页扫全表；Console 审核按钮点击后先 POST `/receipts/{id}/audit` 调 Agent `/tms/receipts_audit`，融辉已按真实原页 `saveBtn -> saveData()` 抓取并直连 `/dataOperation/saveTables`，提交前会从“寄方回单跟踪/派方回单处理”菜单 URL 取得 `authenticationKey/pageId` 请求头，否则融辉会返回“非法的请求”；本地记录缺处理记录 `GUID` 时会先按同票查询 `FIND_TAB_PROCESS_RECORD` 取得唯一处理记录，再提交 `TAB_PROCESS_RECORD_UPT` 的 `AUDIT_STATUS=2/3`，使用 `price` 登录态 `userInfo` 补审核网点/人员字段；缺登录人字段、处理记录无法唯一确定或韵达未适配时显式失败或返回 `AUDIT_CAPTURE_REQUIRED`，才由前端隐藏同源原页 iframe 兜底执行并通过 `execution=original_page` 回写本地状态，不打开可见原页；审核不通过仍先展示原因/确认，再走同一后台执行链路；不得猜未抓实的第三方审核接口。
- 车辆调度中心：`http://127.0.0.1:8765/dispatch`
- 自动化账号管理：`http://127.0.0.1:8765/automation-accounts`，Console 只代理 Agent `agent/tms_runtime/account_manager.py` 的账号元数据、凭据写入和登录态操作；账号系统按真实外部系统展示，大祥报价、自提问题件、大祥S站等通过 TMS融辉账号用途区分。所有账号统一提供保存凭据、立即登录、登录状态、退出登录、自动登录开关、三次失败熔断和重新启用；协议差异只留在后端 provider。列表灰色备注来自 `name`，可独立修改且不得影响凭据和状态。业务账号密码不得写入 Console/MySQL 或 GET 响应。大祥报价显式使用 `price_default` 账号及其 `price_default` profile，飞书报价与后台登录复用同一状态，不再写死特殊 `price` 身份；R7/R13 使用可持久和在线校验的 SSO Token/Cookie，不得显示“不支持”或只做凭据检查。每个账号仍按 `account_id` 隔离运行态，所有 profile 只使用页面保存的独立凭据，不继承部署级账号密码。自动登录默认关闭，只能在页面保存完整凭据后开启；账号管理不得把环境变量凭据计入或展示为已保存凭据。
- 启动脚本：`console/start_backend.sh`
- 停止脚本：`console/stop_backend.sh`






## 分批差错及问题件

- 飞书文本仅精确指令“分批”触发 `split_pending_problem_upload`；“分批问题件”“上报分批差错”“分批差错”和“上传分批/未到问题件”等旧文本只提示发送“分批”，不得执行旧工具或进入 LLM。
- 交互为 dry-run 编号列表 → 首次回复“确认”直接执行全部候选；数字/多选/区间只选择对应运单，回显选择后再回复“确认”正式执行。两个 pending 阶段均为 10 分钟，重新发送“分批”会丢弃旧选择并刷新列表。
- 来源资源固定为 `phase7.split_pending_source_sheet`（每日到货表 A:S），目标资源固定为 `phase7.split_pending_target_sheet`（分批及有发未到表 A:S）。
- `sync_arrival_stats` 每次成功统计后必须用本次内存中的 A:S 统计结果刷新目标 Sheet 与 MySQL 未齐快照，不依赖人工发送“分批”；全部到齐时清空目标旧行并保留表头。自动刷新不得触发融辉差错或问题件上报。
- MySQL 表 `split_pending_problem_items` 分别保存 `complaint_status` 与问题件 `upload_status`；同类型刷新保留历史步骤结果，完整成功单隐藏，失败或未完成步骤继续显示，类型变化才重置。
- 正式模式必须同时提供 `selected_bill_codes` 与 `preview_fingerprint`；执行前重读来源和状态，指纹变化整批零业务写入。正式刷新全部当前未齐 Sheet/MySQL 快照，但融辉只处理所选运单。
- `0 < 已到 < 应到` 先上报“分批”差错，成功或重复后登记“少货/分批”问题件；差错失败跳过该票问题件并继续后续运单。`已到=0` 只登记“有发未到”问题件。
