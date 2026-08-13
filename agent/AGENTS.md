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
- `AGENT_INTERNAL_API_TOKEN` 只鉴别服务连接，不授予管理员角色。Console 管理员命令、审批和账号管理必须额外使用独立 `CONSOLE_AGENT_SIGNING_SECRET`，把 method、精确 path/query、原始 body 哈希、时间戳、一次性 nonce 与真实 MySQL 管理员会话快照绑定；Agent 忽略请求体伪造的 actor、roles、source 和 authenticated_by。
- WorkflowRunner 为每个工具执行签发按工具名、target 和必要 action 绑定的短期能力。工具子进程必须剥离内部 API Token、Console 签名密钥、会话/Webhook/验证 Token，只能用该能力访问精确 `/tms/*`。
- 只有 `/health`、飞书事件入口和带独立 Webhook Token 的 `/webhook/*` 属于公开路径。统一策略在 `agent/http_security.py`，不得在各路由重复实现。
- 所有日志和持久化审计通过 `shared/redaction.py` 脱敏；原始请求体、密码、Token、Cookie 和 Authorization 不得落盘。
- `agent/agent/` 不得导入 `tools` 或 `feishu`；直接工具执行器和飞书告警回调统一在 `main.py` 注入，TMS 会话事件通过 `shared/runtime_events.py` 发布。
- 飞书、Webhook、Phase 7、客服与回单入口只能向 Command Gateway 提交命令；旧 `/tms/*` 写入口必须提供稳定幂等键并映射到精确工具。底层 TMS target 只接受 WorkflowRunner 为当前工具签发的短期执行能力，宽泛 `tms_query` 不得承载写端点。
- 韵达/融辉活动原页同源代理暂时禁用；Console 的四类原页前缀和旧 `/ocr/yunda/*` 入口固定返回 410 且不调用 Agent。Agent 的 `yunda_waybill_entry`、`yunda_waybill_proxy`、`ronghui_waybill_proxy` 在执行能力判断前固定返回 410，待独立来源隔离完成后才可重新评估。
- 登录/验证码仍走账号管理接口；账号状态转为 `authenticated` 时发布 `account.session_restored` 恢复原 `BLOCKED_LOGIN` Run，入口不得重新提交或盲目重试原工具。
- `session_broker.py` 只保留稳定门面；provider 执行、adapter、状态持久化和响应验证分别维护在同目录的 `session_provider_base.py`、`session_adapters.py`、`session_persistence.py` 与 `session_validation_service.py`。
- 新内部路由只能加入 `/internal/v1/*` 并返回 `ok/data/error`；旧路由只作为已鉴权的 deprecated 兼容层，不得新增调用方。

## 统一控制平面

- `main.py` 是唯一组合根，负责注入 `CommandGateway`、Context/Planner/Validator/Policy、Approval、WorkflowRunner、ResultVerifier、Outbox Dispatcher、真实仓储和执行 adapter，并按 Runner -> Outbox 的顺序停机。
- `agent/orchestration/` 只依赖端口和 `shared/orchestration_repository.py`；工具目录实现、TMS target、飞书 handler 和 Console 代码不得反向导入编排内部实现。
- Run/Work Item 状态转换必须走模型允许表和版本 CAS。登录恢复、补充信息恢复原 Run；`PARTIAL` 或终态失败创建关联新 Run。第三方/财务写的未知结果必须 `BLOCKED_DATA/WRITE_OUTCOME_UNKNOWN`，除非存在精确读后 reconciliation。
- Run 澄清只接受闭合 v1 字段 `note/account_id/argument_updates`；纯文本仅作审计 note。业务覆盖必须绑定原 `command_id`，重新通过工具 input_schema、权威账号、策略与 plan hash 校验，禁止猜测自然语言或跨 Command 复用。
- 计划固定 Schema v1，计划哈希必须覆盖上下文、目录哈希、工具版本、完整参数/账号、实际影响、Evidence 与写后条件。审批 15 分钟过期，执行前重算；变化时使旧审批失效并生成新轮次。
- `tools/registry.yaml` 的每项治理字段都必填；宽泛 `tms_query` 和 `feishu_operation` 不向 LLM 开放，破坏性通用飞书操作禁用。定时免审只允许精确任务/版本/参数/cron 命中的内部投影。
- 每日应签与客服问题件只读试点通过 `pilot_projection.py` 投影；每次采集（包括来源不完整或详情复核失败）都必须保存 COMPLETE/INCOMPLETE 影子 Evidence。客服旧口径集合必须从现有账号选择与站点过滤规则独立计算，不能从新集合反推。首页保持旧口径，直至连续三个完整业务日影子集合、来源完整性和差异证据满足切换标准。

## 快速定位入口

收到“小改动 / 小修复 / 新增一个局部功能”时，**先读 `docs/code_navigation_index.md`，再进入目标目录的 `AGENTS.md` 或 `CLAUDE.md`，最后才打开命中的代码文件**。不要默认全仓扫描。

## 模块清单

| 模块 | 代码路径 | 文档路径 | 状态 |
|------|----------|----------|------|
| 控制台工作区 | `console/` | `docs/project_overview.md` | 与 agent 并列部署 |
| 价格获取 | `price_scripts/` | `docs/price_scripts/` | 已完成（持续维护） |
| 财务对账 | `finance_reconciliation/` | `docs/finance_reconciliation/` | ETL v5.1 完成 |
| 财务工作台 | `../shared/finance/`、`agent/tms_runtime/scripts/*finance*`、`tools/finance_sync_service.py`、`tools/sync_finance_bills_tool.py`、`../console/finance_service.py` | `docs/finance_module.md` | 融辉/韵达逐笔账本、费用绑定、BI、00:10 同步与失败审计；和旧 Excel ETL 隔离 |
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
├── finance_reconciliation/
│   ├── module_overview.md             # 模块文档：ETL管道结构与数据源
│   └── data_structure_analysis.md         # 分析报告：全量字段定义与关联关系
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
- 韵达/融辉录入兼容 URL 不再创建第三方活动 iframe；`/ocr?mode=yunda`、`/ocr?mode=ronghui` 回到博益本地录单壳并显示停用提示。`/ocr/yunda/*` 与 `/ocr/ronghui/live/*` 对所有方法返回 `410 ACTIVE_ORIGINAL_PAGE_DISABLED`，不调用 Agent；比价仍可读取真实价格，但第三方原页预填按钮禁用。
- 统一回单管理：Console `/receipts/sync` 与 `/receipts/{id}/audit` 只向 `/internal/v1/commands` 提交精确的 `receipts_sync` / `receipts_audit` 计划，浏览器 UUID 形成 Console 幂等键；提交阶段不得 upsert 同步结果或提前修改本地审核状态。`receipts_audit` 由高风险审批后的 WorkflowRunner 执行并读后核验；缺关键字段、多候选、登录失效、韵达未适配或写后状态不一致均显式失败。旧隐藏 iframe 自动写入兜底已删除，两个回单活动原页前缀对所有方法固定返回 410；页面只保留本地照片、证据和控制平面审核。
- 车辆调度中心：`http://127.0.0.1:8765/dispatch`
- 自动化账号管理：`http://127.0.0.1:8765/automation-accounts`，Console 只代理 Agent `agent/tms_runtime/account_manager.py` 的账号元数据、凭据写入和登录态操作；账号系统按真实外部系统展示，大祥报价、自提问题件、大祥S站等通过 TMS融辉账号用途区分。所有账号统一提供保存凭据、立即登录、登录状态、退出登录、自动登录开关、三次失败熔断和重新启用；协议差异只留在后端 provider。列表灰色备注来自 `name`，可独立修改且不得影响凭据和状态。业务账号密码不得写入 Console/MySQL 或 GET 响应。大祥报价显式使用 `price_default` 账号及其 `price_default` profile，飞书报价与后台登录复用同一状态，不再写死特殊 `price` 身份；R7/R13 使用可持久和在线校验的 SSO Token/Cookie，不得显示“不支持”或只做凭据检查。每个账号仍按 `account_id` 隔离运行态，所有 profile 只使用页面保存的独立凭据，不继承部署级账号密码。自动登录默认关闭，只能在页面保存完整凭据后开启；账号管理不得把环境变量凭据计入或展示为已保存凭据。
- 启动脚本：`console/start_backend.sh`
- 停止脚本：`console/stop_backend.sh`






## 分批差错及问题件

- 飞书文本仅精确指令“分批”触发低风险只读工具 `preview_split_pending_problems`；自提问题件预览使用 `preview_self_pickup_problems`。两条封装器只接受显式 `account_id` 并强制旧实现 `dry_run=true`，任何写入参数都会被拒绝；“分批问题件”“上报分批差错”“分批差错”和“上传分批/未到问题件”等旧文本只提示发送“分批”，不得执行旧工具或进入 LLM。
- 交互仍可生成 dry-run 编号列表和选择快照，但正式第三方写当前固定 `IMPACT_PREVIEW_REQUIRED/BLOCKED_DATA`：在按每个运单从目标系统读回问题件/差错记录并形成权威写后 Evidence 之前，不得因用户确认而执行。
- 来源资源固定为 `phase7.split_pending_source_sheet`（每日到货表 A:S），目标资源固定为 `phase7.split_pending_target_sheet`（分批及有发未到表 A:S）。
- `sync_arrival_stats` 每次成功统计后必须用本次内存中的 A:S 统计结果刷新目标 Sheet 与 MySQL 未齐快照，不依赖人工发送“分批”；全部到齐时清空目标旧行并保留表头。自动刷新不得触发融辉差错或问题件上报。
- MySQL 表 `split_pending_problem_items` 分别保存 `complaint_status` 与问题件 `upload_status`；同类型刷新保留历史步骤结果，完整成功单隐藏，失败或未完成步骤继续显示，类型变化才重置。
- 历史正式模式的 `selected_bill_codes` 与 `preview_fingerprint` 仅保留为离线/测试契约，不构成控制平面可执行条件；不得以工具返回的 `saved/success` 代替第三方读后验证。
- 历史业务顺序为 `0 < 已到 < 应到` 先差错、再问题件，`已到=0` 只登记“有发未到”问题件；该写链在新的权威读后验证器落地前保持停用。
