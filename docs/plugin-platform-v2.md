---
module: 自动化插件平台 v2
type: 开发手册
tags: [ZIP 插件, service_v2, Host API, 独立插件]
related: [identity_and_unified_chat.md, architecture_direct_invocation.md, ../agent/service_v2_plugins/README.md, ../agent/docs/service_v2_developer_tooling.md]
status: active
updated: 2026-10-30
---

# 独立插件与宿主接口

## 当前实现与线上状态

启动恢复入口时先读取当前实例启停状态。停用的实例保留已验证的持久代次记录，但不恢复命令、路由或服务的活跃入口，避免与现用实例冲突。

Service V2 插件是包含实际业务代码的独立 ZIP。宿主负责账号、权限、接口、隔离进程、执行记录和安装升级；插件负责字段适配、筛选、计算和业务步骤。短插件触发后直接启动，结束后返回结果，失败和取消不会留下待领取任务。

现有正式业务均已提供 V2 包和生产接口适配，安装、打包、隔离进程执行及写后核验均有实际测试；隔离测试仍不代替线上业务验收，线上版本以发布报告和后台实例记录为准。

系统只运行 Service V2。2026-10 删除了 V1 运行时、V1→V2 迁移配对、签名发行、Windows Worker 与旧逐日财务采集：Agent 只投影和执行 `runtime_model=SERVICE_V2` 的实例；Connector 可调用的 Host 原语只来自 `agent/agent/automation_plugins/reviewed_primitives.py` 的静态清单；固定飞书命令与既有 Webhook 的插件归属写死在 `agent/agent/automation_plugins/fixed_entrypoints.py`。迁移 064 删除了数据库中的旧插件行（含旧控制平面历史与未知写记录）和迁移配对、018 引导、Windows Worker 等旧表，财务账本保留；被删行在迁移时先导出为 JSON 存入迁移备份目录。核心发布只复制 `agent/scripts/release_scope.py` 列出的当前包。

插件清单、业务文件归属、局部测试和打包命令见[当前插件维护入口](../agent/service_v2_plugins/README.md)。本手册替代旧文档中“生产 Connector 为空”“AI 只读”“V2 只有离线候选实现”的状态说明。

R7 车线每日打卡包 `r7_vehicle_checkin_v2` 1.1.0 使用通用账号 `http.request`，查询、提交和核验规则均在 ZIP 内，支持宿主账号选择及默认关闭的项目定时。通用能力首次需要核心更新；后续同系统业务只升级 ZIP。详见[通用账号请求](../agent/docs/plugin_account_http.md)。接口证据、构建方式与本次验收范围见 [R7车线每日打卡](../agent/docs/r7_vehicle_checkin.md)。

自提/分批的选择上限、问题件计划、统计分批判断以及财务分页由包内规则决定。Host 绑定已提交计划并核验真实读写证据；显式合同及三个 2.1.0 包的协同升级顺序见[插件维护入口](../agent/service_v2_plugins/README.md#业务规则与-host-的边界)。

2026-10-08 已完成核心 `17ad00c` 发布及自提、分批、到货统计三个 2.1.0 ZIP 升级，原绑定和定时配置保持不变。线上自提与分批只读预览已验证；本次未提交正式业务写入。范围、版本和证据见[修复与上线记录](plugin_business_boundary_20261008.md)。

## 只读预览与未启用定时

扫描、自提和分批的后台按钮及预览确认接口支持 Service V2 的实际实例 UUID。扫描首次调用按 V2 目录已验证的 `super_admin_upload/builtin_bundle` 来源识别并执行只读预览；正式调用必须绑定本实例、当前代次、当前账号发起且未过期的预览。Console 按真实 Invocation 的插件身份区分扫描和候选列表，不用浏览器参数推断执行阶段。V1 的签名来源判断不适用于 V2 安装。

扫描批次规则归插件：Host 对 `preview_evidence` 校验结构、候选集合及批次总量约束，将插件给出的 `batch_count/batch_plan_sha256` 原样绑定到本次正式确认，不按 Host 的默认批量重新生成计划。插件正式执行前重读来源并重新计算、比较同一计划摘要，变化时在首个写入前失败；实际提交继续由 Host 写回执和独立账本读回核验。这样插件调整默认批量不需要同步修改 Host。历史 Run 的冻结恢复规则仍只服务历史核验。

回归：`tests/test_direct_preview_state.py` 覆盖预览状态与正式绑定，`tests/test_scan_preview_binding.py` 覆盖正式确认入口条件，`tests/test_scan_codes_action_payload.py`、`tests/test_production_v2_packaged_protocol.py` 覆盖批次计划摘要和确认后漂移时零写入；Broker 使用可核对的测试账本，不代表生产扫描已经执行。

`agent/agent/automation_plugins/harness_permissions.py` 是只读能力投影的单点。注册 Harness、生成模型工具目录和发放实际 read/compute 调用权限时，按 Host 注册表保留明确的只读操作。打卡插件可查询预检及核验接口，但正式提交接口不会进入这次只读调用；service.invoke 仍在分发前核验实际目标及当前调用的 effect ceiling。原签名权限不修改，缺字段或治理伪造直接失败。

实例可以保留已关闭的物理定时配置。未启用的 Scheduler contribution 对应物理行必须关闭且参数为保存层明确写入的空对象；仍核对其声明、实例、代次和配置版本，但不把空对象当作可执行参数。它不产生可调用定时合同，也不阻止已有的手动和 AI 入口；重新启用时必须按当前设置编译并完整校验执行参数。意外开启的物理定时、未声明入口或配置漂移仍报错。

复现回归：`PYTHONPATH=agent:. PYTHON_DOTENV_DISABLED=1 python -m pytest -q tests/test_service_v2_read_capability_lifecycle.py tests/test_service_v2_disabled_schedule_contract.py`。这些测试核验真实生产 Manifest 的编译、注册、代次切换和撤销，以及实际 Broker 的读取放行与提交拒绝；不代表执行了真实打卡。

## 用户入口与权限

- `/automations` 管理功能插件的安装、设置、运行、定时、启停、升级和卸载。财务、客服采集放在各自模块的数据源入口，复用同一插件运行机制。
- 普通账号和飞书身份继承后台设置的身份权限，超级管理员拥有全部权限。权限变更实时影响页面、接口、固定查询及插件调用，见[身份与统一对话](identity_and_unified_chat.md)。
- Console AI 与飞书自然对话共用会话服务、当前激活模型和工具目录。固定关键词直接进入插件，不额外经过模型。账号与渠道身份仍分别核验。
- AI 可以调用当前身份获准使用的已安装插件。扫描、自提、分批先返回预览，正式执行使用宿主持有的一次性确认；模型不能自填账号、资源、执行身份或任意业务参数。
- 安装、升级和权限管理要求真实超级管理员。读取结果和执行插件使用当前角色权限及该插件的入口设置。

## 包合同与开发入口

ZIP 根目录包含 `manifest.json` 和 `payload/`；声明专属设置页时可包含 `settings/`。根目录不能再套一层文件夹。

Manifest 必须明确 `schema_version=2`、`runtime_model=service_v2`、版本、Host API 范围、运行方式、服务、操作、账号/资源角色、能力及贡献入口。解析失败直接拒绝，没有其他格式可回退。

V2 由超级管理员上传无签名 ZIP，按内容摘要确认传输及已安装字节。相同版本不能覆盖不同内容。升级前检查权限、接口和配置兼容性；当前代切换完成后新调用才使用新包，旧调用排空。回退使用该实例已安装过的精确版本，不从同名目录猜测旧包。

现有 Console 入口只新增真正可选的运行时输入时，升级器先以旧合同核验保存的调用模板，再按目标合同迁移新增的 Host resolver。原参数、既有 resolver、服务目标、治理、账号和资源绑定、定时及启用入口必须保持一致；新增必填输入或改写既有字段仍拒绝。模板、配置与策略见证和版本切换在同一事务提交，失败全部回滚，重复请求沿用原请求标识。

Manifest Schema 位于 `agent/extension_sdk/schemas/manifest-v2.schema.json`。通用本地开发命令见 [Service V2 开发工具](../agent/docs/service_v2_developer_tooling.md)；现有业务优先使用 `agent/scripts/plugin_maintenance.py` 选择对应插件进行测试和打包。

设置页运行于独立 sandbox iframe，没有主页面同源权限或任意网络权限；通过会话绑定桥读取和保存自身配置。账号和表格只使用宿主返回的不透明引用。设置保存与定时保存分别校验版本并相互保留。“每天指定时间”的时间点属于主系统定时器，只改时间点时以目录提供的定时摘要 `schedule_sha256`（保存参数 `expected_schedule_sha256`）和配置版本 CAS 改写当前已提交代次的定时行并只同步该插件的定时任务，不新建代次；定时方式与开关仍是代次内容。

## 运行与宿主接口

插件采用 `on_demand` 独立进程，在 Linux Python 3.10、Bubblewrap 和资源限制下执行。`resident` 尚未实现，应明确返回 `RESIDENT_RUNTIME_UNAVAILABLE`。隔离环境不可用即失败，不在宿主进程回退执行。

每个服务操作声明精确 effect：`read`、`compute`、`internal_write`、`external_write` 或 `destructive`。预览按预览操作确定权限和读写类型，不因为同一包还有正式写操作而被视为写调用。

`service.invoke` 只接受 Manifest 声明的服务和操作。插件不能任意访问宿主 Python、SQL 连接、文件、网络或第三方凭据。宿主 `production_connectors.py` 注册已实现的到货、扫描、问题件、寄件、签收、财务、客服和每日应签接口。融辉网点打卡复用已审核的 `browser.session` 动作；R7 等新网页业务通过包声明的 `http_requests` 使用通用账号请求，宿主不增加专用业务接口。测试用 tracking fixture 只供显式隔离测试使用，不是生产物流接口。

Connector 在宿主解析当前账号和资源，验证输入和调用范围，并记录真实操作观测。敏感内部身份不进入模型或插件返回值。缺字段、分页不完整、范围不明、重复游标、空响应或权威回读失败必须给出具体失败结果；退出码或 `success=true` 不能替代业务成功。

统计完成完整查询后得到的明确空记录集合，按“无数据”清理本次统计及分批汇总，并独立回读确认没有旧记录残留。它不同于外部表格空响应、缺表头或解析失败，后者仍明确失败。停用且已退出迁移的 V2 准备失败实例可从后台请求卸载；实际卸载仍由 Agent 核验活动调用、未知写、代次排空和清理结果。

终态执行详情附带本次 Invocation 的写入核验摘要，页面显示已确认、未确认数量及未确认记录编号。查询严格关联本次调用及对应代次租约，不读取其他任务的结果，不暴露业务请求内容；旧失败记录保持原状态。这是只读诊断，不会重试写入或清除未知写。

写动作由宿主记录写尝试，完成后按具体业务进行独立回读和结果验证。写入结果未知时保留事实，不自动重发原写。新调用不查询历史失败记录决定准入；只有仍活跃的同实例调用及真实写目标协调会影响本次执行。

Registry 按已提交代次发布服务和入口。准备新代时完整校验，原子切换后才开放；停用、卸载和空代撤销实际路由。旧代可排空已开始的调用，但不继续接收新调用。

## 定时、飞书、Webhook 与事件

定时复用现有 APScheduler 和已保存时间，不生成默认业务时间、不补跑停机期间的任务。启用的定时贡献必须有真实 schedule。关闭定时仍保留时间和业务设置，但不要求开启定时入口，也不注册可执行 Job；重新启用时按当前设置编译执行参数。默认 cron 只接受当前宿主可无损表达的格式。

固定飞书命令及动态插件命令都使用可信消息身份和精确匹配，沿用登录、确认、取消等交互优先级。命令冲突拒绝整个新代，不选择第一个插件。单号文本使用统一确定性查询；自然语言对话才进入共用模型。

现有扫描、统计、签收状态三个 Webhook 的映射由 `agent/agent/automation_plugins/fixed_entrypoints.py` 单点定义，只发往对应插件唯一启用、当前 contribution 精确声明该路由的实例，不开放任意新公网路由或任意请求参数。

通用 Webhook/Event Dispatcher 的 `READY` 只说明持久注册与当前进程入口绑定有效，不代表已经存在公网传输或外部事件总线。`durable=true` 事件仍不可用，不隐式降级。跨进程调度、持久事件重放及未来长任务不属于本轮短插件运行机制。

## 实例与依赖

插件自身入口按精确实例、包和已提交代次调用；安装其他版本或重复实例不能阻断该入口。目录中的跨插件依赖只计算已启用、已配置且提交稳定的实例，待配置和停用实例不占可调用路由。同一服务存在多个可调用实例时，裸服务名依赖仍明确拒绝，不按安装顺序或版本高低选择。真实 MySQL 安装新版 ZIP 后继续执行原统计实例的回归见 `tests/test_arrival_v2_configuration_mysql.py`。

统计与到货清单 Connector 共用 `list_connectors_v2.encode_arrive_page_result`，将现有读取适配器的固定列数组无损映射为字段对象；缺列、多列仍报错，不新增平台字段或业务默认值。
