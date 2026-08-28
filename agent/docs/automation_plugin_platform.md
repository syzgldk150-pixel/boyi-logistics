---
module: 自动化插件平台
type: 架构与运行手册
tags: [自动化插件, Ed25519, generation, Windows Worker, Cordis]
related:
  - ../first_party_automation_plugins/README.md
  - ../first_party_automation_plugins/MIGRATION_MATRIX.md
  - code_navigation_index.md
status: active
updated: 2026-08-28
---

# 自动化插件平台

## 边界与对象模型

一个签名 ZIP 只定义一个可复用动作 `plugin_id + version`，不携带
`automation_id`、业务账号 ID、凭据或具体定时时间。管理员每安装一次就由服务端创建一个新的
`automation_id` 项目实例；同一包可重复安装，实例各自保存名称、项目配置、账号/资源/设备绑定、
定时与审批策略。相同 `plugin_id + version` 的 ZIP、只读安装目录和 venv 共享，实例运行目录隔离；
最后一个引用完成两阶段卸载后才允许删除共享字节。

插件清单声明动作输入/输出 Schema、允许入口、配置 Schema、账号与资源角色、Broker 原语、
Evidence、写后条件和 Worker 要求。清单不能声明 cron，也不能注入 Console HTML/JS；Console
只按平台的安全投影和受限 Schema 组件渲染。运行解析始终使用 `automation_id`，不得用
`plugin_id` 或工具名猜实例；多实例匹配不唯一时显式返回歧义错误。

插件只安装动作；清单最多声明平台允许的调度类型，不携带实际时刻。`none/daily_times/startup`
等定时在安装后由系统项目配置保存，并与实例的账号、资源、入口和授权共同受版本 CAS 约束。
因此重复安装同一包时，每个 `automation_id` 都能独立绑定账号/资源并设置定时与权限，互不继承。
迁移时唯一受审的历史间隔计划 `customer_problems_shadow`（每 15 分钟）会被精确展开为等价的
96 个 `daily_times`；任意其他通配 Cron 仍然 fail closed，插件与浏览器均不会接收原始 Cron。

首方动作源码位于 `first_party_automation_plugins/`。`digests.json` 锁定确定性清单和包摘要，
`MIGRATION_MATRIX.md` 是动作提取、底层原语和独立写后验证的当前权威状态。每日应签只做动作包裹，
不在插件平台重写其账号、核验或账本语义。

当前发行范围是显式的 Linux/ECS 服务端子集：只有迁移矩阵标为 `RUNNABLE`、并同时进入代码
allowlist 的动作才会进入签名工件、bootstrap、Catalog、Broker 暴露和发布健康计数。矩阵中仍为
`BLOCKED` 的动作只保留为待提取源码清单，不会因“目录中存在 payload”而被打包或误启用。本轮还
明确推迟整个 Windows Worker/Tray 运行面，以及 `r7_arrival_checkin`、`r7_departure_checkin`；它们
不参与其余服务端插件的健康判断。恢复这些范围必须在同一受审提交中同时更新矩阵、代码 allowlist、
闭合 adapter、发行门禁和测试，不能通过环境变量或残留数据库记录旁路打开。

源码发行边界也读取同一份代码 allowlist。`scripts/first_party_release_scope.py` 只用 AST 解析该常量，
不会导入首方 payload；本地发布 payload 只复制共享 `_runtime` 和 allowlist 包，远端在编译前再次
核验 staged 包集合精确相等。CI 的阻断式编译、Ruff 和 pytest 同样只纳入该集合的首方源码/源码测试；
仍为 `BLOCKED` 的包进入独立 `continue-on-error` 审计，保留可见性但不能否决一个根本不含这些字节的
服务端发行。release-scope、签名构建、Catalog/Broker 禁入和发布器自身测试始终属于阻断门禁，不能
借此隔离绕过；共享 Agent 核心也仍完整受检，不存在 whole-tool 或旧源码回退。

## 签名、安装与供应链

生产首方包和管理员上传包都必须通过受信 Ed25519 公钥校验；私钥不进仓库、不进 Agent，离线签名
CLI 只从调用方指定的受限文件读取私钥。安装顺序固定为：校验传输摘要、整包限制、canonical
manifest、文件表和签名，再防 Zip Slip/符号链接/重解析点/硬链接地物化，最后建立独立 venv。
依赖必须是精确版本和 SHA-256 锁，使用 `--require-hashes --no-deps` 安装；没有真实 OS sandbox
的平台不允许运行上传的 Python payload。

校验通过后，平台把“原始已签名 ZIP”按固定相对名复制进独立 installed 版本目录，以 `0600` 常规
文件保存，并在安装元数据中只记录相对名与 package SHA-256。首方发布包也走同一复制流程，后续
Worker 下载不依赖已经轮换的 release 目录。读取时同时校验版本目录边界、符号/硬链接、打开前后
inode、大小和摘要；管理投影及下载响应均不暴露服务器文件路径。

启动 bootstrap 会对数据库中已存在的首方签名版本重新核验完整 manifest、签名身份、确定性安装
路径、安装元数据、原始 ZIP 摘要、签名文件、payload 文件闭集和 Python 入口。若回滚曾保留精确的
数据库版本但删除了它唯一对应的 installed 目录，只允许从本次已验证的同一签名包重建该目录，并在
重建后逐字节匹配原数据库元数据；注册仍使用原数据库记录，不改写不可变字段。目标为符号链接、路径
不一致或已存在但损坏/多文件时一律 fail closed，既不覆盖也不删除。

远端发布器在任何插件或数据库 mutation 前通过受控只读 runner 捕获数据库已有首方版本的
`plugin_id/version/manifest SHA-256`，并在停服务后复核同一身份集合未漂移。发布前磁盘缺失、但由新
bootstrap 依照该身份恢复的根属于旧数据库状态，后续门禁失败时必须保留；本次新注册且不在 prestate
ownership 中的根仍须隔离移出。任一应恢复根缺失、身份变化、清单异常或路径不安全都使 rollback
incomplete，旧服务保持停止，stage 与 release hold 留待精确恢复。

生产组合根严格读取以下变量，不提供开发兜底：

- `BOYI_AUTOMATION_PLUGIN_ARTIFACT_ROOT`
- `BOYI_AUTOMATION_PLUGIN_TRUST_ROOT`
- `BOYI_AUTOMATION_PLUGIN_VERIFIED_RELEASE_SHA`
- `BOYI_AUTOMATION_PLUGIN_CURSOR_SECRET`

ECS 发布工件约定为
`/home/boyce/.boyi-automation-plugins/releases/${RELEASE_SHA}`，公钥目录为
`/home/boyce/.boyi-automation-plugins/trust`。前三项由发布流程写入运行时 EnvironmentFile；
`VERIFIED_RELEASE_SHA` 必须与当前运行 release SHA 精确相等。发布前只读校验入口为：

```text
python scripts/verify_first_party_plugins.py \
  --artifact-root <release-bound-directory> \
  --trust-root <public-key-directory> \
  --release-sha <running-release-sha>
```

`builtin_release` 仅可用于开发测试，不可进入生产 green health，也不可获得项目完全自动权限。

## 管理 API 与显式绑定

生产管理面由 `agent/automation_plugins/management_api.py` 的 router factory 注入，不导入组合根。
Console 请求体统一 `extra=forbid`，身份只从已签名的 MySQL Console principal 获取；浏览器不能提交
actor、`automation_id`、manifest 或签名/完整性内部字段。读取目录和 Worker 列表允许管理员，安装、
升级、启停、配置、卸载和设备配对只允许 `super_admin`。所有管理写入口还统一读取发布 hold；hold
存在、状态读取失败或返回值不明确时均在物化/事务前拒绝，目录和 Worker 安全投影仍可读取。稳定入口为：

- `GET /internal/v1/automation/plugins/catalog`
- `GET /internal/v1/automation/workers`
- `POST /internal/v1/automation/workers/pair`
- `POST /internal/v1/automation/plugins/install`
- `POST /internal/v1/automation/instances/{automation_id}/upgrade`
- `POST /internal/v1/automation/instances/{automation_id}/state`
- `POST /internal/v1/automation/instances/{automation_id}/uninstall`
- `PUT /internal/v1/automation/instances/{automation_id}/configuration`
- `POST /internal/v1/automation/workers/pair`

配置必须显式提交签名角色对应的 Business Account ID、managed resource ID 和（需要时）命名 Windows
设备；解析器精确匹配账号池/资源池/配对设备，不读取 `is_default`，不选首项，也不按名称猜测。
配置、入口和系统定时在同一个 CAS 事务内保存并立即使旧授权 stale。保存或升级后同步尝试 reconcile；
依赖未就绪时响应只投影 `PREPARING/BLOCKED_DEPENDENCY`，不得宣称完成或提前启用。启用必须再次
确认当前 desired material 已有完全匹配的 `STABLE committed_generation`。
配置幂等重放还须精确匹配操作者身份和事件目标配置版本，后续配置漂移后不得读回最新值冒充旧请求结果。
稳定 generation 提交后，管理 API 在 Agent 事件循环内原子刷新 APScheduler；完整注册计划先校验，任何
加载失败都恢复旧 Job、返回 `REFRESH_FAILED`，调用方以同一请求 UUID 重试。`startup` 项目使用一次性
DateTrigger 和“上海业务日 + 配置版本”的稳定 Command 身份；release hold 启动不注册任何 startup Job。
Console 启用/停用使用浏览器动作 UUID 与实例 `record_version` 做精确 CAS，并在同一事务写入
`PLUGIN_STATE_CHANGED` 事件，绑定目标状态、CAS 前后版本和签名管理员身份；响应丢失后重放同一 UUID
只读回该次结果，不得再次推进。启用必须等待稳定 committed generation；停用作为止损动作可在依赖
协调中撤销新运行权限，但升级或卸载中的实例仍拒绝并发停用。

业务账号池和资源池只能通过闭合安全 descriptor 进入管理投影。资源 descriptor 精确为
`resource_id/name/kind/status`；Token、表格 ID、读写范围、文件路径、配置哈希/版本和原始配置留在
Agent 运行时，不得进入 Console 或浏览器。Console 再按签名 resource role 的 `kind` 精确过滤候选，
不会默认选中第一项；已保存 ID 也必须重新核验状态与类型。provider 不可用、descriptor 字段多/缺、
必填绑定缺失、资源停用或类型漂移时，目录投影标记资源池不可用，项目配置与执行 fail closed。
目录响应中的 `hidden_automation_ids` 只列出当前发行明确排除且确实存在于持久化实例表的身份；
Console 自动化列表成员只来自该目录的实例与持久化定时行，不维护本地隐藏名单，也不在目录故障时从静态
工作流元数据补卡。目录原始实例与安全规范化结果不一一对应时整份实例投影失败关闭；未关联实例的定时行
仍作为阻断项显示，不能猜测归属或把未持久化的 deferred 身份补回目录。

飞书固定文本命令由 Agent 代码中的只读注册表拥有，不从插件 manifest 或安装事件动态生成。注册表按
命令族声明唯一 `command_id`、唯一 `route_key` 和唯一触发工具名；预览与正式工具可在同一命令族内共享
route。文本匹配后仍必须由 committed、STABLE 且签名资源修订一致的项目 generation 唯一认领 route；
缺失、禁用、重复或失效均拒绝执行，也不会回退 LLM 或旧工具。扫描确认态故意不落盘，服务重启后必须
重新生成预览；其他既有 pending 按原 TTL 恢复，并在正式调用时重新经过 route 与项目治理校验。

迁移 `018` 的资源闭包由当前 16 个可发布首方实例模板反向校验：模板绑定的并集必须精确等于 26 个
审阅身份。其中 18 个代码内置身份由 `phase7_resource_import.BUILTIN_RESOURCES` 提供精确配置，另 8 个
外部文档身份只能预先配置、不得猜测物化；延期 R7 的两个 Feishu 路由既不在该并集，也不进入健康
计数。已有 route/webhook 行保持原来源和时间戳，但规范化 `route_key/path` 必须与内置可信入口精确
一致，否则迁移阻断；delivery status 多维表则必须具备运行时实际读取的
`base_token/table_id/view_id/view_name` 四字段。这样 generation 可在结构资源就绪时稳定提交，同时
Broker 仍在每次调用时独立重验账号登录态和精确绑定，缺失时 fail closed。

018 首次项目化仍保留历史 pre-image、grant 和 typed schedule 证据，供发布边界复核；生产既有 019
保持 generation lease / Run 绑定的原始字节，迁移 020 随后把
所有 `REQUIRE_EACH_RUN` / `LEGACY_SCHEDULE_ONLY` 统一转换为 `PROJECT_FULL_AUTO` 并写不可变审计。
迁移 021 再精确恢复当前策略仍为 `PROJECT_FULL_AUTO` 的 typed `WAITING_APPROVAL` Run：在同一条
MySQL 更新中废止 `PENDING` / `APPROVED` 审批、清除遗留 Worker 租约并把 Run 调度到当前时间；管理员
后来显式改回 `REQUIRE_EACH_RUN` 的项目不在恢复范围内。
全新数据库因 018 bootstrap 在 SQL 迁移之后发生，启动时还会执行同等的幂等全自动初始化。管理员后续
显式选择逐次审批优先，配置或插件代际变化不得再静默改写权限模式。

当前 018 仓储的低层 `pair_device` 没有 request UUID 审计合同，因此管理员配对入口固定返回
`PLUGIN_WORKER_PAIRING_AUDIT_UNAVAILABLE`，不会旁路调用无审计写。只有前向迁移提供原子且幂等的
`pair_device_with_audit` 聚合后才能开放；请求只接收 Ed25519 公钥和 TLS 客户端证书 SHA-256，绝不
接收设备私钥或证书私钥。

MySQL lifecycle 以一个事务注册不可变目标版本并 CAS 推进 desired generation，但保留管理员选择的项目
权限模式。prepare 期间禁止用旧配置启动新 Run；目标包、配置、账号、资源、Worker 和可逆 effects
全部闭合后才原子切换。相同 request UUID 的响应丢失重试只读取原目标，不会再推进一代；不兼容配置
在任何 desired 写入前拒绝。切换后的旧 generation 等已有 lease 排空后再 dispose，全程不靠重启清场。

首方 release bootstrap 会把已存在但版本较旧的保留实例一并推进到本次签名版本，而不是只登记新包。
升级前先用精确首方 ownership 规则重编译代码拥有的配置字段，再走通用 staged upgrade 与 generation
reconcile；配置、账号/资源绑定、入口选择（含全关）、定时和权限模式均保留，较新版本禁止降级。旧版本
不可变目录仅在旧 generation/lease、审计或发布恢复仍引用时保留，不得继续成为活动项目版本。

首方生产集合必须从同一已提交 SHA 一次性构建，禁止手工拼接或覆盖既有目录：

```text
python scripts/build_first_party_plugin_release.py \
  --private-key <受限的Ed25519私钥文件> \
  --key-id <受信key_id> \
  --release-sha <完整Git提交SHA> \
  --output-root <新的空发布目录>
```

构建器只为当前代码 allowlist 中的 `RUNNABLE` 服务端动作生成清单和 payload，写入与该集合严格
同数的签名 ZIP 及一个 canonical `release-index.json`；随后用同一套生产预检验证精确包集合和对应
迁移实例，既不夹带 `BLOCKED` 动作，也不接受缺包或额外包。任一入围包失败时不发布部分目录。
它只读取调用方明确传入且权限受限的私钥，不搜索、不复制、不输出私钥内容。

新增能力从 `agent/examples/automation_plugin/` 的无副作用示例开始。该目录不属于首方源码清单，也不由
bootstrap、Catalog、Broker、ECS 发布或签名发布发现。`validate_automation_plugin_source.py` 只执行签名前
源码预检：严格解析 manifest 与工具治理合同、确认 entrypoint 与字面量 `ACTION_ID`、编译 payload，并
拒绝导入 Agent、shared、tools、feishu 等运行时模块；它不生成签名、不安装插件、不创建实例。真实能力
若需要账号、资源、页面或外部写，必须在独立 TASK 中先冻结精确 Broker 合同、来源证据、写后条件和未知
结果处理，再分别更新核心 handler、测试、迁移矩阵、release allowlist、digest lock 与签名发行。

## 运行隔离与核心 Broker

动作由包内 `python_subprocess` 执行，payload 禁止导入 `agent`、`shared`、`tools` 或 whole-tool
入口。Linux 生产运行使用 Bubblewrap、独立进程组、私有临时目录、默认禁网和最小环境；插件不能
读取 Agent 源码、会话文件或内部 Token。运行前重新核验不可变安装树和签名文件摘要。

账号、Session、浏览器、文件、Office、投影与飞书等能力由核心平台保留。插件只持有一次调用期、
实例/代际/角色/操作绑定的短期 Broker grant，并调用清单精确列出的 `(operation, action)`；核心在
每次调用前重新检查账号 active 与 Session authenticated。账号 ID、Cookie、Token、密码和 Session
不得进入插件 stdin、env 或结果，结果只带服务端生成的 binding-set proof。缺绑定或登录失效返回
`BLOCKED_CONFIG` / `BLOCKED_LOGIN`，不选择默认账号、不切换账号、不回落旧工具。

Broker v2 请求使用有长度上限的压缩帧，并在解压后再次执行独立上限校验；因此完整快照不再受
`StreamReader.readline` 默认行长限制，同时仍拒绝超限或无效压缩数据。Broker 在旧 generation
lease 排空前继续接受原有换行 JSON 请求。同步核心 Handler 必须在线程中执行，不能占用 Agent
事件循环；Run 状态查询、Scheduler 和其他 API 在长时间浏览器/数据库调用期间仍须保持响应。
动作运行时只透传符合固定大写错误码格式的 Broker 错误，其他异常消息继续按安全诊断收口，禁止
把业务数据或原始异常文本带出 subprocess。

插件统一输出经过签名 output Schema 验证；写动作成功只进入 `VERIFYING`，必须由核心
ResultVerifier 验证具名 postcondition 与 Evidence 后才可成为 `WRITE_VERIFIED`。越过 started-mutating-call
boundary 后的超时、取消、无效 JSON 或异常一律视为 `WRITE_OUTCOME_UNKNOWN`；登录、资源、handler 或
精确 binding 校验失败仍是 pre-write，未知写会阻断旧代清理和卸载。

每个已签名 `broker_operations` 项还必须闭合声明 `effect: read|write`。Broker 将一次性标记回调
传入受管 Handler；Handler 只在全部权威参数和业务条件校验完成、并紧邻第一个变异端口调用前
记录“started mutating call”；
只读调用、鉴权失败、契约失败和资源/角色未绑定都不会被当作已开始写入。执行路由据此把零个
started writes 的失败保存为 `FAILED_BEFORE_WRITE`，任何已开始但未获权威终态证明的写保持
`WRITE_OUTCOME_UNKNOWN`，不会自动重放。

## Desired / committed generation

平台借鉴 Cordis 的时空可组合思想，把每个项目实例视为可热替换的 component/fiber：

- 项目配置、账号/资源/设备 revision 是 reactive coeffects；
- 包引用、venv、入口路由、Broker grant、定时和 Worker deployment 是有日志的 effects；
- `target_generation` 表示期望态，`committed_generation` 是所有实时入口唯一可见的运行态；
- 每代保存不可变、无凭据的 `execution_metadata`，旧 lease 因而继续使用旧版本、旧绑定和旧计划；
- effect 先以确定性 key 持久化为 `PLANNED`，再幂等 apply 并标记 `APPLIED`；
- prepare 完成后再次观测 coeffects，只有 revision 与 readiness 未漂移才 CAS 切换全部入口；
- 新调用在切换后只获取新代 lease，旧调用排空后按 effect 逆序 dispose；
- `PREPARED`、`COMMITTED`、`DRAINING`、`DISPOSING` 任一点崩溃都由启动 reconcile 确定性续跑。

外部网页、Office、飞书或财务写入不可逆，不能伪造 inverse。活跃、`VERIFYING`、过期但未定论的
lease 或未知写结果都会阻断 dispose，直到权威读后核验或人工处置。

未知写不会被清除、伪造为成功或自动重放。失败 Run 与同代未知 lease/receipt/Evidence 保持不可变，
但当前 committed generation 继续作为稳定路由：再次手工执行或定时触发必须创建新的 Command、Run
与 generation lease，不复用历史 Run 或 lease。未知 lease 仍阻断该 generation 的 dispose/uninstall；
generation 退出当前路由后，历史代落为 `BLOCKED` 审计归档，迟到 finalizer 只能更新旧代，不能把新
committed 路由改成不可运行。旧代完成 `BLOCKED` 归档时，如果当前目标已经稳定提交且没有其他待排空
代际，项目必须在同一事务中退出 `DRAINING` 并恢复 `STABLE`；历史未知写继续保留，但不得让新路由
长期显示“排空中”。

操作员恢复入口只负责闭合历史未知 receipt，不是重新执行的前置步骤。Console 只提交浏览器生成的
请求 UUID，Agent 只依据持久 receipt 与权威回读判定 `WRITE_VERIFIED`、`NOT_APPLIED` 或 `UNKNOWN`；
浏览器不得提交 generation、lease_id 或 evidence，恢复过程也不得重放原 Run。普通重新执行直接走
新的 Command，并重新校验当前签名、配置、账号、资源、入口和写后证据。

### Broker write receipts and recovery

Broker `read` calls never constitute a started write. For the five reviewed
server projects, each signed `effect=write` action emits a durable,
payload-free receipt with an action-specific opaque locator only at the
started-mutating-call boundary, immediately before the handler's first real
mutating port call.
A new receipt can
later prove `WRITE_VERIFIED` or a zero-receipt `FAILED_BEFORE_WRITE`; a real
adapter call that started but has no terminal verification remains
`WRITE_OUTCOME_UNKNOWN`. Historical leases without receipts also remain
`UNKNOWN` and are never replayed by inference.

这里是对 [Cordis 论文仓库](https://github.com/cordiverse/paper)《A Programming Paradigm for
Spatiotemporal Composability》中 reversible effects、reactive coeffects 和时空组合模型的工程借鉴，
不是形式化等价或正确性证明。该论文在本项目采用时仍是 2026-08-13 标注、持续修订中的预印本。
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 的 “Everything is a Plugin”
组合方式也只作为实现参考。恶意代码隔离和外部写不可逆问题并不会由这个范式自动解决，仍分别依赖
OS sandbox、闭合 Broker、Evidence 和 fail-closed 门禁。

## 项目权限与就地审批

审批以 `automation_id` 为单位，项目只暴露“完全自动”和“每次运行审批”两个设置；项目内所有
Scheduler、Console、飞书和 Webhook 入口共享同一项目策略。`PROJECT_FULL_AUTO` 是持久化管理员意图，
不绑定某代 contract hash；它覆盖签名清单内的外部写、财务写、破坏性和极高风险动作。运行时仍逐次
校验当前 committed generation、签名、enabled action、闭合参数、账号/资源、入口和写后 Evidence；
任一项缺失都不可运行，不得回退成审批模式。权限保存只写策略与审计，不创建 runtime generation。

`enabled_entrypoints` 可为允许入口的任意子集（含空集）。关闭入口后由服务器阻断；Scheduler 关闭时
保留管理员设置的时间，但提交代际将对应 `scheduled_tasks` 物化为 disabled，不注册或触发 Job；管理
响应和 Console 必须显示 `ENTRYPOINT_DISABLED`，不能把已保留时间误写成运行中定时已开启。

卡片内批量审批只提交服务端返回的待审批集合摘要和请求 UUID。服务端锁内重验项目、角色、plan
hash 与集合；集合变化、任一计划失效或角色不足时零批准，不允许部分成功。每条 Run 仍保存独立
审批决定和 Evidence；事项中心只负责全局检索、历史和异常处置。

Console 超级管理员可生成 10 分钟、单次使用的高熵绑定码，数据库只保存摘要；飞书发送
`绑定审批 <绑定码>` 后建立管理员关联。每次消息都实时校验后台账号 active 与 `control_plane_role`。
`agent.approval.requested` 通过事务 Outbox 为每名启用通知的绑定管理员建立持久化串行队列，每次只激活
一条；精确回复 `1` 批准、`2` 驳回。批准事务先将 `WAITING_APPROVAL` Run 的调度时间置为当前时间，
提交后再唤醒 Runner，避免事项状态已批准而原 Run 永久停留。过期、重复或已被他人决定会跳到下一条。
审批请求必须在 Run/WorkItem 已提交为 `WAITING_APPROVAL` 后才创建并投递，防止飞书快速回复看到尚未
等待的 Run。仍为 `PENDING` 的 Run 休眠到审批 `expires_at`，不参与普通 5 秒轮询；批准、驳回、策略、
配置或插件版本变化会按 Run→Approval 的统一锁序废止旧上下文并把精确 Run 调度到当前时间。
每次审批请求提交后 Runner 都会再读取一次当前策略；若策略/配置/插件变化恰好发生在首次评估与审批
创建之间，刚创建的旧上下文会立即失效而不会睡到过期。飞书投递若发现当前 ACTIVE 已失效，会自动
完成该投递并主动推送下一条，不要求管理员先回复旧消息才能推进队列。
若 Run 等待期间项目被管理员改为完全自动，Runner 重新领取时会使旧审批失效并以当前策略恢复同一
已验证计划；计划或合同发生变化时仍重新验证，不能拿旧审批替新计划授权。

## 升级、停用与卸载

升级先准备新 generation，验证依赖/Broker/Worker/coeffects 后原子切入口，不覆盖实例配置或定时。
停用与卸载先在服务端撤销入口和新授权。硬卸载使用持久 purge journal 和设备 cleanup directive：
设备离线时返回 `UNINSTALL_PENDING`，重连后先清理；所有实例目录与最后共享版本清理都 ACK、且无
受保护/未知写后才 finalize。崩溃或局部文件删除进入可恢复状态，禁止盲目重试或把部分删除标成完成。
卸载只删除应用自身插件数据，不能撤销已写入外部系统的结果，也不保证删除代理日志或数据库备份。

## Windows Worker 与 Tray Runner

> 当前发行状态：整套 Windows Worker/Tray 延后，不属于本轮可执行范围。Agent 不装载 Worker 签名
> 密钥、不创建 transport、不挂载 Worker 路由，发布脚本也不要求 Worker mTLS、服务端身份或
> dispatcher readiness。以下内容是未来重新启用时必须同时满足的设计与安全合同，不是当前上线说明。

Windows 生产入口：

```text
python.exe <absolute-agent-root>\windows_worker_host.py --config <absolute-json-path> validate
python.exe <absolute-agent-root>\windows_worker_host.py --config <absolute-json-path> service
pythonw.exe <absolute-agent-root>\windows_worker_host.py --config <absolute-json-path> tray
python.exe <absolute-agent-root>\windows_worker_host.py --config <absolute-json-path> \
  --python-executable <absolute-python.exe> --tray-user <DOMAIN\user> install
python.exe <absolute-agent-root>\windows_worker_host.py --config <absolute-json-path> \
  --python-executable <absolute-python.exe> --tray-user <DOMAIN\user> uninstall
```

配置 JSON 不保存明文凭据，只保存 DPAPI 保护文件、TLS 证书/私钥文件和公钥目录的绝对路径；文件
ACL 由安装脚本收紧为 SYSTEM、Administrators 和精确 Tray 登录用户所需的最小权限。`install` 只注册
自动启动的 SCM Service 与指定用户登录触发的单实例 Scheduled Task，不启动进程、不读取密钥内容、
不连接网络或生成生产密钥；Service 在下次启动、Tray 在该用户下次登录时运行。

后台 Service 仅出站 HTTPS 拉取签名命令、安装包、上报心跳/结果和执行清理；
登录用户会话的 Tray Runner 通过受鉴权本地 IPC 提供 Office COM、浏览器和桌面 UI 能力。设备绑定
使用命名 `device_id`；交互会话区分 `AVAILABLE`、`LOCKED`、`LOGGED_OUT`，锁屏/登出时排队到动作
deadline，不自动换设备。

Service 与 Tray 只交换最多 1 MiB 的 canonical JSON，协议绑定版本和每请求 UUID，Named Pipe 的
32–128 字节 DPAPI 保护 auth key 完成双端认证；禁止 pickle/任意对象反序列化。Tray 使用登录会话
`Local\BoyiAutomationTray-<device_id>` mutex 保证单实例，并在每次 RUN/CLEANUP 前重新探测当前输入
桌面；锁屏或非活动登录会话固定 `INTERACTIVE_SESSION_UNAVAILABLE`。源码发行版目前没有可安全复用
的闭合 Windows 浏览器/Office action adapter，默认 runner 固定返回
`TRAY_ACTION_ADAPTER_UNAVAILABLE`，不得回落到任意 subprocess、旧 whole-tool 或动态导入。

`uninstall` 先请求 SCM 正常停止并等待 Tray 停止，再只读核验 SQLite：任何其他 `RUNNING` job 或
`OUTCOME_UNKNOWN` 写均固定 `WORKER_UNINSTALL_BLOCKED`，状态库缺失但本地目录非空也拒绝。通过后
只移除 Service/Task 注册；state、package、配置和 DPAPI 文件保留，必须在写结果完成核验后另行审阅
清理。单个插件实例的 generation/instance 清理同样只排除当前 cleanup job 自身，任何其他活动作业或
未知写仍阻断文件删除。

Worker 本地心跳始终报告 `release_hold=true`。只有服务端在发布解除后签发的单条 COMMAND 可携带
`dispatch.release_hold=false`、一次性 `authorization_id` 和当前 `release_sha`；该授权不能变成持久
全局放行。消息序列与 replay 状态持久化，响应丢失重发同一已签 envelope。cleanup ACK 和未知写核验
可在 release hold 期间继续，但 claim/install/invoke/新 cleanup dispatch 均停止。

Worker 传输、状态机、Service/Tray 外壳已提供 fail-closed 运行边界；具体 Windows 网页/Office 动作
只有在对应闭合 adapter、真实字段来源和写后核验齐备时才是 `RUNNABLE`，不得把未接底层原语的动作
描述为已上线。

重新启用后，公网 Worker 传输只经过版本化的 `deploy/nginx/boyi-worker-mtls.conf`。同一 HTTPS server 使用
`ssl_verify_client optional` 保持 Console 和普通 `/internal/v1/*` 客户端兼容，但精确
`/internal/v1/automation/worker/` location 只接受 `$ssl_client_verify=SUCCESS`。Nginx 覆盖验证状态和
escaped client certificate 头后代理到回环 Agent，并清空 internal token/Console principal 等其他
鉴权头；设备 ID 仍只是选择器。发布前必须确认安装 snippet 与当前 staged release 哈希一致、固定 CA
和配置路径为 root-owned 且不可被 group/other 写、站点只引用一次，并通过 `nginx -t`，否则在任何
应用 mutation 前失败关闭。发布器不读取或部署 CA/私钥。

## 发布健康门禁

新进程先在 release hold 下启动。当前只有 allowlist 中的签名包完整、其对应已安装实例具有稳定
committed generation、没有 `PREPARING/SWITCHING/DRAINING/BLOCKED/UNKNOWN`，且 Scheduler 与
WorkflowRunner 已确认运行后，服务端才最后消费匹配 release SHA 的 marker。首次 018 切换还必须由
独立只读 post-018 validator 精确核验 71 条历史身份、68 条启用、16 个策略、项目 bootstrap marker/items、
旧任务 grant/退休事件和 generation snapshot；后续切换改按当前 committed schedule 与原始 marker 证据
闭合，不把合法新增 schedule 误当初始 71 行。精确延后的 R7 项目即使
数据库仍有旧记录，也从 Catalog 和健康计数中排除；其他未知或未入围的 persisted 项目仍失败关闭。
Windows Worker 当前只投影固定的 `enabled=false/state=disabled/release_hold=false/active_jobs=0`，不读取
Worker 仓储，也不是本轮发布门禁。任一步失败都保留 marker 和 hold。健康接口不得把入围动作的缺失
公钥、缺失首方工件、缺 Broker 原语或不稳定 generation 降级为 warning。
