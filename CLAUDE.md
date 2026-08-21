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

## ECS 固定发布流程

- 用户提到“同步 ECS”“发版”“发布到 ECS”或“部署到 ECS”时，直接读取并执行 `agent/deploy/publish_to_ecs.md`，唯一发布入口是 `agent/deploy/publish_to_ecs.ps1`；不得先搜索历史命令、旧脚本或临时目录来猜测流程。
- Agent、Console、Shared 或自动化插件平台任一发生变更时，常规控制平面发布固定使用 `-Target all`，并显式传入 `-AutomationPluginArtifactRoot` 与 `-AutomationPluginTrustRoot`。`-Target auto` 只用于已确认不跨控制平面边界的范围发布；`-SkipRestart`、`-SkipHealthCheck` 和紧急计划窗口覆盖必须由用户逐项明确授权。
- 固定顺序为：干净且已推送的最终提交通过 CI → 以最终 40 位 Git SHA 生成一次性 Ed25519 发布密钥、公钥信任根和完整签名插件工件 → 调用固定 PowerShell 发布器 → 核验远端用户、systemd `WorkingDirectory`、服务状态与 `/health.release_sha` → 清理本地一次性私钥和工件。私钥只在项目 `.task_tmp/` 的 `0700/0600` 临时目录中生成和使用，不读取既有私钥、不进入 Git、不上传 ECS、不打印内容。
- 发布成功后，远端当次 stage、精确回滚包、上一版共享虚拟环境和数据库快照必须保留到业务验收结束；本地一次性签名材料与构建工件在发布验收后精确清理。完整失败关闭、回滚和验收规则以 `agent/deploy/publish_to_ecs.md` 为准。

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
- 生产与 CI 固定使用 Python 3.10；服务依赖必须在各自 `requirements.txt` 和 `requirements.lock` 精确固定。Agent 与 Console 共用一个按两份锁文件联合 SHA-256 标识、并分别通过精确依赖校验的 `runtime-deps-<hash>` 虚拟环境；只有任一锁文件内容变化或环境校验失败时才构建新环境并在健康检查前原子切换。失败时从当次暂存目录恢复旧环境和源码；成功后也必须保留当次远端精确回滚包、上一版虚拟环境和数据库快照，直到业务验收完成后再以独立有界操作清理。
- 提交前运行 Ruff、工具清单、仓库卫生、内部 API 契约与导入边界检查，GitHub Actions 也必须覆盖这些检查。跟踪文本统一 UTF-8 无 BOM，单个 Python 文件不得超过 3,000 行。
- `.env` 只允许由服务或脚本入口通过显式 bootstrap 加载一次；库模块、测试导入和共享模块不得读取 `.env`、创建运行目录或连接数据库。

## Agent 统一控制平面

- 系统保持 Agent + Console 双服务；业务编排、审批、执行恢复和事务 Outbox 全部位于 Agent，禁止新增独立 LLM 服务、消息中间件或 Console 侧编排器。完整规范见 `agent/docs/control_plane_v1.md`。
- 除登录/验证码、Console 本地 OCR 与手工运单 CRUD 外，Console、飞书、APScheduler、Webhook 和兼容工具 API 必须提交 Command；只有 `agent/agent/orchestration/workflow_runner.py` 可以调用 `ToolExecutionPort`。
- Command、Work Item、Run、Step、Approval、Evidence、Domain Event 和 Outbox 使用 `shared/orchestration_repository.py` 的显式 Unit of Work；通用仓储原语、结构要求和定时审批仓储分别位于 `shared/orchestration_repository_support.py`、`shared/orchestration_schema.py`、`shared/scheduled_task_approval_repository.py`。连接必须 `autocommit=False`，运行时不得执行 DDL。Worker 领取只支持 MySQL 8 `FOR UPDATE SKIP LOCKED`。
- Run 澄清只接受闭合 v1 字段 `note/account_id/argument_updates`；纯文本仅作审计 note。业务覆盖必须绑定原 `command_id`，重新通过工具 input_schema、权威账号、策略与 plan hash 校验，禁止猜测自然语言或跨 Command 复用。
- 风险、审批角色、调度免审、Evidence 与写后条件只读取受管工具契约。LLM 只能选择开放的只读/计算工具；第三方写要求 `super_admin` 独立审批和可核验写后证据，除非 Scheduler 命中当前有效的精确任务豁免；未知写结果不得盲目重试。
- 定时任务的审批不是按工具一刀切：每个持久化任务都有 `REQUIRE_EACH_RUN`（默认）或 `EXACT_SCHEDULE_EXEMPT` 两种策略。只有真实 MySQL 管理员会话签名的 Console `super_admin` 可以变更策略；该豁免只由 Scheduler 使用，手工、Console、飞书和 Webhook 发起的同一工具仍走逐次审批。`registry.yaml` 的 `approval.mode: schedule_allowlist` 只是可配置豁免的资格上限，不会自动授权。
- `EXACT_SCHEDULE_EXEMPT` 必须绑定任务 ID、工具/版本、完整参数及账号、cron、启用状态、治理字段、写后条件、动态规则与配置版本；显示名称不属于行为哈希。迁移 `018` 将当前发行的 57 条计划项目化并安全退休旧任务级 EXACT；release hold 下的一次性项目策略 bootstrap 只有在 018 pre-image、原 grant、退休事件、typed committed generation 和当前行全部闭合时，才建立 `LEGACY_SCHEDULE_ONLY`。首次 post-018 门禁固定核验 71 条历史身份（57 typed + 14 deferred R7）、68 条启用、16 个项目策略及 10 LEGACY/6 REQUIRE；任一绑定漂移都回到逐次审批。
- 生产已经执行的 `014_control_plane_task_cutover.sql` 必须保持与 `schema_migrations` 一致的原始字节，不得把后续修复回写到旧迁移；`015` 建立任务级策略表，`016`/`017` 完成账号与任务合同升级，`018` 建立项目代际和一次性授权证据，`019` 把每个新 generation lease 绑定到权威 Run，遗留未绑定 lease 禁止猜测恢复。首次 post-018 发布要求 71/68/16 与 10 LEGACY/6 REQUIRE 全部闭合；marker 已存在的后续发布允许管理员合法启停、改 schedule 或改回逐次审批，但当前项目、策略与原始 marker 证据必须各自可验证。
- 保存或清除自动化账号凭据前，Agent 必须在同一事务中把所有显式引用、以及 `sync_finance_bills` 等代码声明的隐式账号依赖对应的 `EXACT_SCHEDULE_EXEMPT` 降为 `REQUIRE_EACH_RUN` 并写审计/Outbox；账号级 MySQL 执行锁必须让凭据变更与所有非终态受保护写 Run 串行化，凭据变更租约存续期间禁止重新授予相关免审，撤权、活动 Run 检查或锁获取失败时凭据写入必须 fail closed。每个受保护写步骤在同一账号锁内重新评估当前策略并提交 `RUNNING`；免审已失效时原子回到 `WAITING_APPROVAL`，已有 `RUNNING/VERIFYING` 写步骤只允许 reconcile，未知结果不得重放。终态 Run 的人工 `retry` 只允许原计划全部为 read/compute；任何外部写、财务写、内部投影写或 destructive step 都必须提交新 Command 并重新经过策略/审批，禁止复制原 Scheduler 身份重放。
- 生产发布必须持有远端互斥锁，在任何 mutation 前捕获 `014`/`016`/`017`/`018` 与各 bootstrap marker 的原状态；停止服务前后都要确认没有 `RUNNING`/`VERIFYING` 的受保护写。失败回滚只撤销本次从 pending/marker-absent 产生的状态，并按项目策略 bootstrap、`018`、旧任务 bootstrap、`017`、`016`、`014` 的逆序恢复。新 Agent 重启时必须由部署标记同时保持 Scheduler 暂停和 WorkflowRunner 不领取 Run；签名 identity smoke、post-018 项目 manifest 和依赖记录全部通过后，签名管理接口才先恢复并确认两者均可运行，最后删除匹配本次 SHA 的 marker。marker 删除是发布提交点；删除前异常或进程退出必须保留 marker，使下次启动继续 hold，响应丢失后的重复激活必须幂等完成，提交请求发出后不得再自动回滚可能已经开始执行的任务。
- “每日应签”和客服问题件先作为只读影子投影。每日应签只由真实主单签收证据关闭；问题件列表消失必须按外部 ID 精确详情复核。未连续三个完整业务日满足完整性与集合一致标准前，不得切换首页口径。
- Console 事项中心只能代理 Agent `/internal/v1/*`，不得直读控制平面表。所有 POST 使用真实 MySQL 管理员会话、同源校验和服务端身份覆盖；Basic Auth 不具备控制平面写权限。

## 安全与数据规则

- 永远不要读取、打印或提交 `.env`、凭据文件、私钥或其他敏感内容。
- 密码、Token、Cookie、Authorization 和原始请求体不得写入日志、审计记录或异常输出。
- 影响财务结算的金额必须使用 `Decimal(str(value))`，明确空值语义和最终舍入规则，并执行行数、总量、极值及关键反算校验。
- 页面和第三方接口逻辑必须来自真实页面、真实请求或官方契约；缺字段、多候选或解析失败必须显式失败，不得猜测或静默回退。
- ECS 固定使用 `boyce@123.57.106.70` 和既有系统 SSH 配置；禁止 `root`、密码回退和跳过主机密钥校验。

## Console 移动端框架

- Console 的唯一导航目录在 `console/navigation.py`；桌面侧栏、移动底栏、更多面板和后端白名单都从这里读取，禁止在模板或路由中复制导航清单。
- 管理员移动底栏偏好只保存到 `admin_users.ui_preferences_json`，其 schema 迁移必须新增到 `agent/migrations/`。应急 Basic Auth 没有管理员 ID，必须明确拒绝同步，不得以浏览器本地存储回退。
- 通用壳层在 `console/templates/base.html`、`console/static/style.css` 和 `console/static/console_ui.js`；Logo 使用内容哈希命名的 `console/static/assets/boyi-logistics-logo-7e1f2994.webp`，Feather 图标使用锁定版本的本地资源 `console/static/vendor/feather-4.29.2.min.js`。字体按首屏、常用字和完整回退分层存放在 `console/static/assets/fonts/`：中文固定使用思源黑体，英文与数字固定使用 Inter，不得改回在线字体或图标服务。响应式页面必须保留 WCAG 2.2 AA 的键盘、焦点、触控和减弱动效支持。
- 视觉与产品约束见根目录 `PRODUCT.md`、`DESIGN.md` 及 `.impeccable/`；结构改动时同步维护它们。

## 本地与生产隔离

- ECS 是飞书机器人、定时任务和生产自动化的唯一长期运行源；本地 WSL 仅用于开发调试和临时验证。
- 部署前必须确认本地 Agent 已停止，并确认远端用户、工作目录、Git SHA、当次临时回滚材料、迁移预检、健康检查及失败回滚链路；远端当次 stage、回滚包、上一版共享虚拟环境和数据库快照保留到业务验收结束后再独立清理。
- 生产 Console 只监听 `127.0.0.1:8765`；Agent 默认只监听 `127.0.0.1:9000`，公网入口必须经受控代理和鉴权。

## Agent 内部接口安全基线

- `AGENT_INTERNAL_API_TOKEN` 只证明服务调用方，不代表管理员身份；只允许由运行环境注入，不得写入源码、文档、日志或审计记录。Console 管理员身份必须使用独立 `CONSOLE_AGENT_SIGNING_SECRET` 对精确请求和真实 MySQL 会话快照签名，Agent 不信任请求体中的 actor、roles、source 或 authenticated_by。
- Agent 仅公开精简 `/health`、`/feishu/webhook/event` 和带独立 Webhook Token 的 `/webhook/*`；其他 `/admin`、工具、知识库、调度和账号接口要求 `X-Agent-Internal-Token`。WorkflowRunner 工具子进程不得继承该 Token，只能使用按工具/target 绑定的短期执行能力访问精确 `/tms/*`。
- 韵达/融辉活动原页不得在 Console 管理员同源上下文运行。主站 `https://boyi.homes` 只签发 30 秒一次性 ticket，原页固定在 `https://www.boyi.homes/original/{yunda|ronghui}/` 的独立 origin 运行，交换为路径限定、HttpOnly、Secure、SameSite=Strict 能力 Cookie，并在每次请求重验真实 MySQL 管理员会话。旧 `/ocr/yunda/*`、`/ocr/ronghui/live/*`、`/receipts/yunda/live/*` 与 `/receipts/ronghui/live/*` 仍对所有方法固定返回 `410 ACTIVE_ORIGINAL_PAGE_DISABLED`。
- `/health` 只返回存活状态和 `release_sha`；组件、实例和工具状态只在鉴权后的 `/internal/v1/health` 返回。
- `/internal/v1/*` 使用唯一的 `ok/data/error` 响应契约；Console 调用 Agent 必须使用该接口族。旧内部接口只为兼容保留、继续鉴权并标记 deprecated，不得新增调用方。
- 日志、工具执行输出、MySQL 工具日志、回单审计和异常文本统一使用 `shared/redaction.py`，新增记录入口不得自建较弱的局部脱敏规则。
