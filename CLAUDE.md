# 最高优先级：Git 版本控制

本节优先级高于本项目内其他规则。除首次 GitHub 基线初始化外，每项改动必须先在仓库根目录执行 `git status -sb`，确认工作区归属后从最新 `main` 创建 `agent/<任务名>` 分支。验证通过后只能显式暂存本任务文件，禁止在混合工作区执行 `git add -A`；随后必须提交、推送并创建 Draft PR。不得提交 `.env`、凭据、Token、Cookie、业务原始资料、财务 `metadata`、OCR 原图、运行态或输出报表。推送或 Draft PR 未成功时，项目改动不视为完成。首次基线直接提交并推送 `main` 是唯一初始化例外；之后不得直接推送 `main`，除非用户明确授权。

## 固定执行流程

1. 开始前执行 `git status -sb`，检查并保留用户已有改动。
2. 更新本地 `main` 后创建语义明确的 `agent/<任务名>` 分支。
3. 修改前读取本文件、目标模块的 `CLAUDE.md` 及相关索引文档。
4. 执行与风险相称的测试、静态检查和敏感信息扫描。
5. 使用 `git add -- <明确文件列表>` 只暂存本任务文件，并用 `git diff --cached` 复核。
6. 提交、推送当前分支，创建以 `main` 为基线的 Draft PR。
7. 在交付说明中给出分支、提交 SHA、Draft PR 和验证结果。

详细操作和国内网络处理见 `docs/git_workflow.md`。

# 项目结构与边界

`boyi-logistics` 是私有单仓，目录职责如下：

- `agent/`：Agent 服务、飞书接入、TMS 自动化工具、发布脚本及其模块文档。
- `console/`：Console 服务、模板和静态资源。
- `shared/`：共享领域模型、金额规则、接口契约与仓储抽象；不得读取环境变量或产生导入副作用。
- `tests/`：跨模块共享测试；模块测试仍保留在各自目录。

原始业务表格/PDF、财务元数据、OCR 原图、生成报表和运行态不属于源码仓库。所有配置凭据只通过环境变量或部署环境注入，禁止写入代码或文档。

## 文档与模块规则

- 修改代码前先读取 `agent/docs/code_navigation_index.md`，再读取目标目录的 `AGENTS.md` 或 `CLAUDE.md`。
- 结构、入口、业务链路或发布方式变化时，同步更新对应层级的 `AGENTS.md` 与 `CLAUDE.md`，两套规则保持一致。
- 同一业务逻辑只能有一个实现；修改上游字段、公式或常量时必须搜索并检查所有下游引用。
- 线上运行时使用完整包路径，禁止依赖当前工作目录的裸导入或长期修改全局 `sys.path`。
- 旧脚本必须置于明确的 `legacy` 或离线命名空间，并与线上运行路径隔离。
- 数据库结构只由 `agent/migrations/` 的顺序 SQL 和部署期迁移器维护；服务、仓储、同步工具和 Console 请求路径只能校验结构及读写数据，不能运行 DDL。
- Console 保持现有 HTTP 框架；`console/app.py` 只负责组合、生命周期和请求分发，业务实现必须进入 `console/services/`，路由识别进入 `console/routes/`。
- TMS SessionBroker 只保留稳定门面；provider 执行、adapter、状态持久化和响应验证分别位于 `session_provider_base.py`、`session_adapters.py`、`session_persistence.py` 和 `session_validation_service.py`，调度器只依赖公开接口。
- `agent/agent/` 不得依赖 `tools` 或 `feishu`；跨包回调和事件必须由 `agent/main.py` 组合注入，或通过 `shared/runtime_events.py` 的中立契约发布。
- 生产与 CI 固定使用 Python 3.10；服务依赖必须在各自 `requirements.txt` 和 `requirements.lock` 精确固定。发布为每个 Git SHA 构建独立虚拟环境并在健康检查前原子切换，失败时恢复旧环境和源码。
- 提交前运行 Ruff、工具清单、仓库卫生、内部 API 契约与导入边界检查，GitHub Actions 也必须覆盖这些检查。跟踪文本统一 UTF-8 无 BOM，单个 Python 文件不得超过 3,000 行。
- `.env` 只允许由服务或脚本入口通过显式 bootstrap 加载一次；库模块、测试导入和共享模块不得读取 `.env`、创建运行目录或连接数据库。

## 安全与数据规则

- 永远不要读取、打印或提交 `.env`、凭据文件、私钥或其他敏感内容。
- 密码、Token、Cookie、Authorization 和原始请求体不得写入日志、审计记录或异常输出。
- 影响财务结算的金额必须使用 `Decimal(str(value))`，明确空值语义和最终舍入规则，并执行行数、总量、极值及关键反算校验。
- 页面和第三方接口逻辑必须来自真实页面、真实请求或官方契约；缺字段、多候选或解析失败必须显式失败，不得猜测或静默回退。
- ECS 固定使用 `boyce@123.57.106.70` 和既有系统 SSH 配置；禁止 `root`、密码回退和跳过主机密钥校验。

## Console 移动端框架

- Console 的唯一导航目录在 `console/navigation.py`；桌面侧栏、移动底栏、更多面板和后端白名单都从这里读取，禁止在模板或路由中复制导航清单。
- 管理员移动底栏偏好只保存到 `admin_users.ui_preferences_json`，其 schema 迁移必须新增到 `agent/migrations/`。应急 Basic Auth 没有管理员 ID，必须明确拒绝同步，不得以浏览器本地存储回退。
- 通用壳层在 `console/templates/base.html`、`console/static/style.css` 和 `console/static/console_ui.js`；Logo 资源为 `console/static/assets/boyi-logistics-logo.png`。响应式页面必须保留 WCAG 2.2 AA 的键盘、焦点、触控和减弱动效支持。
- 视觉与产品约束见根目录 `PRODUCT.md`、`DESIGN.md` 及 `.impeccable/`；结构改动时同步维护它们。

## 本地与生产隔离

- ECS 是飞书机器人、定时任务和生产自动化的唯一长期运行源；本地 WSL 仅用于开发调试和临时验证。
- 部署前必须确认本地 Agent 已停止，并确认远端用户、工作目录、Git SHA、备份、迁移预检、健康检查及失败回滚链路。
- 生产 Console 只监听 `127.0.0.1:8765`；Agent 默认只监听 `127.0.0.1:9000`，公网入口必须经受控代理和鉴权。

## Agent 内部接口安全基线

- `AGENT_INTERNAL_API_TOKEN` 是 Console、Agent 内部工具和飞书内部管理调用共享的服务间凭据；只允许由运行环境注入，不得写入源码、文档、日志或审计记录。
- Agent 仅公开精简 `/health`、`/feishu/webhook/event` 和带独立 Webhook Token 的 `/webhook/*`；其他 `/admin`、`/tms`、工具、知识库、调度和账号接口统一要求 `X-Agent-Internal-Token`。
- `/health` 只返回存活状态和 `release_sha`；组件、实例和工具状态只在鉴权后的 `/internal/v1/health` 返回。
- `/internal/v1/*` 使用唯一的 `ok/data/error` 响应契约；Console 调用 Agent 必须使用该接口族。旧内部接口只为兼容保留、继续鉴权并标记 deprecated，不得新增调用方。
- 日志、工具执行输出、MySQL 工具日志、回单审计和异常文本统一使用 `shared/redaction.py`，新增记录入口不得自建较弱的局部脱敏规则。
