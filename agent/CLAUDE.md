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

## Claude Code 文档检索协议

### 核心原则

**不全量加载文档，按需检索。** 本 CLAUDE.md 是唯一始终加载到上下文的文件。

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

## 分批差错及问题件

- 飞书仅以精确文本“分批”触发：预览完整编号列表后，直接回复“确认”会执行全部候选；输入序号、多选或区间时只选择对应运单，回显后再回复“确认”执行；旧文本仅提示发送“分批”。
- 正式工具必须携带 `selected_bill_codes` 和 dry-run 返回的 `preview_fingerprint`，执行前重读并校验；仅对所选运单产生融辉业务操作。
- 少货/分批先上报差错，成功或重复后登记问题件；有发未到只登记问题件。MySQL 分别保留差错和问题件步骤状态，支持失败续跑并隐藏完整成功单。
- 到货统计成功后直接用本次 A:S 统计结果刷新“分批及有发未到表”和 MySQL 未齐快照；全部到齐时清空旧行，仅人工确认“分批”才允许产生融辉业务操作。
- 投诉页面能力位于不可独立调度的 `agent/tms_runtime/scripts/ronghui_split_complaint.py`；旧独立工具与运行时 target 已删除。

---

## 敏感信息

各模块的敏感变量统一存放在对应目录的 `.env` 文件中，详见各模块 CLAUDE.md。

---

## 项目本地控制台

- 本地入口：`http://127.0.0.1:8765/`
- OCR 工作区：`http://127.0.0.1:8765/ocr`
- 韵达录入页签：`http://127.0.0.1:8765/ocr?mode=yunda`，Console 同源 `/ocr/yunda/live/...` 转发到 Agent `/tms/yunda_waybill_proxy`，Agent 使用 `yunda` 登录态代理韵达原始 `kyinms.yunda56.com/ky_inms/public/...` 页面与接口，成功保存后由 Console 写入本地 `waybills`，并通过保存响应里的 `shipnow_autoprint_url` 打开 Console 本地热敏打印页。
- 车辆调度中心：`http://127.0.0.1:8765/dispatch`
- 自动化账号管理：`http://127.0.0.1:8765/automation-accounts`，Console 只代理 Agent `agent/tms_runtime/account_manager.py` 的账号元数据、凭据写入和登录态操作；列表中系统名下方的灰色账号备注来自 `name`，可在“编辑”中单独修改，且不会改动凭据、登录态或自动登录设置。业务账号密码不得写入 Console/MySQL 或 GET 响应。自动登录默认关闭，只能在页面保存完整账号密码后手动开启；账号管理不得把部署环境变量凭据计入或展示为已保存凭据。
- 启动脚本：`console/start_backend.sh`
- 停止脚本：`console/stop_backend.sh`
