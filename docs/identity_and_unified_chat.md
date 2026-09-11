---
module: identity
type: implementation
status: active
authority: canonical
owner: repository
updated: 2026-09-11
---

# 身份权限与统一对话

本文说明当前工作区实现；线上是否启用须以发布记录和实际核验为准。

插件迁移从系统状态页的“插件迁移维护”进入 `/automations/maintenance`。页面只接受真实超级管理员，安装仍在各插件所属模块进行；迁移页调用已有配置复制、证据核验、切换、回退和完成 API。新鲜目录与 CAS 决定可执行动作，不自动运行脚本，不把安装成功当作业务成功。生产包安装检查使用真实 ZIP → Agent 投影 → Console 白名单的跨层测试 `tests/test_production_v2_console_inspection.py`，覆盖可集合账号、调用限额及不要求标题的消息入口。

插件安装脚本的 URL 缓存键随源码摘要更新，避免已登录浏览器继续运行旧安装器。页面入口变更后需要重新验证真实上传、检查和安装。

迁移准备中断会显示脱敏后的实际失败原因，并提供“继续迁移准备”；重试复用持久化迁移和原请求编号，仍检查当前超级管理员及记录版本，不创建重复迁移。未配置、未参与迁移的停用新实例可卸载，最终由 Agent 校验实际调用和代次状态，不要求其先具备业务运行条件。

## 管理方式

系统管理中新增命名身份，按功能勾选权限。普通后台账号分配一个身份；飞书可以绑定一个身份，或绑定一个后台账号并继承该账号的实时权限。超级管理员由系统原生赋予最高权限，不通过勾选生成。

权限包括业务查询、运单录入、客户服务、调度、专线维护、执行插件、财务查询、财务采集管理、业务账号管理和 AI 对话。财务采集管理必须同时具有财务查询权限。安装、升级、卸载、授权配置、身份与账号管理仍属于超级管理员操作。

所有飞书绑定在后台集中展示，包括原账号停用后失效的绑定和已解绑记录。可以改备注、重新分配身份或继承账号、解绑。新绑定通过一次性绑定码验证飞书身份，不能在页面伪造飞书用户标识。保留现有“绑定审批”指令，短码不能当运单查询。

修改身份、停用账号或解绑后，每次查询和插件执行重新读取有效权限。页面菜单和 Agent 接口使用同一权限目录。后台普通账号不能通过直接访问 API 绕过权限；插件运行记录按实际插件所属模块检查访问权。

## 对话调用

后台 AI 助手与飞书自然对话共用 `HarnessConversationService`、当前启用的模型、工具目录和 `ChatTextQueries`。`channel_chat.py` 只负责接入身份、会话和消息编号转换，不维护另一套提示词或模型循环。群聊按发送者隔离上下文。

明确单号和已支持的财务文本查询使用同一确定性解析器。固定飞书脚本命令继续直接调用独立插件。AI 可按身份权限选择插件，参数、平台账号和表格绑定来自后台已有配置。需要确认的扫描、自提和分批操作仍保留预览与确认。

不同会话可以并行；同一会话内部保持消息顺序，同一事件的重复投递取回同一次结果。会话当前存于内存，插件实际 Invocation 存入数据库。

Console 会话响应显式接收 Agent 的 `messages` 历史投影，仍逐层检查敏感及内部身份字段。`tests/test_harness_console_boundary.py` 将真实会话服务的创建、回复和刷新恢复结果送入 Console 响应校验，防止两端字段合同不一致导致页面连接失败。

## 维护归属

| 内容 | 维护入口 |
| --- | --- |
| 身份目录与共同规则 | `shared/identity_permissions.py` |
| 真实账号、身份与飞书绑定存储 | `shared/identity_repository.py` |
| HTTP 权限映射 | `shared/identity_routes.py`、`agent/agent/identity_access.py` |
| 后台身份管理 | `console/services/identities.py`、`console/templates/identity_management.html` |
| 飞书绑定验证 | `shared/feishu_approval_repository.py`、`agent/agent/orchestration/feishu_approval_service.py` |
| 统一 AI 会话 | `agent/agent/harness_application.py`、`agent/agent/channel_chat.py` |
| 确定性业务文本查询 | `agent/agent/chat_text_queries.py` |

## 数据升级与发布边界

迁移 `047_identity_permissions.sql` 增加身份表、账号身份引用和飞书绑定目标。旧超级管理员保持原生权限；旧普通管理员分配明确的“原管理员”身份保留现有业务权限；旧飞书绑定迁为继承原后台账号，保留绑定编号与归属。不会自动解绑或替用户重新分配生产身份。

本次是共享权限及数据库变更，必须核心更新。迁移后不能只恢复旧源码：旧代码没有新绑定目标字段，也不能执行新权限规则。发布前必须按部署流程备份并停止两个服务后迁移。若在重新开放服务前失败，可恢复同一次备份的源码、依赖和数据库；开放服务后已有新业务写入时，不得用整库恢复抹掉新数据，须保留新权限结构向前修复或另行验证定向降级迁移。

## 隔离复验

根目录运行，使用 Python 3.10；测试禁止加载 `.env`：

```bash
PYTHONPATH=agent:. PYTHON_DOTENV_DISABLED=1 python -m pytest -q tests/test_identity_interfaces.py tests/test_unified_chat.py tests/test_plugin_conversation_intent.py
```

真实数据库用独立 MySQL 实例，设置 `RUN_MYSQL_INTEGRATION=1` 和该实例的 `AGENT_DB_HOST/PORT/USER/PASS`，运行 `tests/test_identity_permissions_mysql.py`、`tests/test_feishu_bindings_mysql.py`、`tests/test_identity_upgrade_mysql.py`。升级用例创建专用测试库，先应用旧迁移并写入合成账号与绑定，再实际迁移和核验，不触碰生产库。

## 插件 V2 迁移状态

发布程序的健康探针和发布激活属于服务运维身份，精确合同由 `shared/release_identity.py` 定义。它们先通过内部 Token、Console 请求签名和防重放校验，只能访问各自固定方法与路径，不解析为 `admin_users` 数字账号。业务接口仍实时读取账号权限；非数字业务账号明确拒绝，不返回 500。相关回归为 `tests/test_release_identity_permissions.py`。

统计的生产 Connector 实现在 `agent/agent/automation_plugins/arrival_connectors_v2.py`，复用已验证的分页、平台读写和写后回读原语。Host 私有调用上下文仅由能力代理传给连接器，不能从插件输入构造。连接器不加载 V1 包，也不执行整个旧业务脚本；统计算法仍由独立包承载。

`tests/test_arrival_connectors_v2.py` 使用真实统计算法、V2 Connector Registry、宿主能力代理和真实原语执行隔离数据测试，外部平台仅在基础接口处替换。覆盖实际件数、精确表格绑定、字段变化报错、登录过期和未核验写入。该测试不代表生产业务写入验收通过。

工作区已补齐清单、韵达、财务、寄件、签收、客服和每日应签的 V2 生产适配，局部测试/打包入口覆盖现有 V2 插件。真实 ZIP 通过隔离进程、宿主 Broker 和 ResultVerifier 核验；迁移切换与回退、字段及业务判断两次维护演练使用隔离数据库和外部服务。详细归属及复验命令见 [V2 维护入口](../agent/service_v2_plugins/README.md)。

上线迁移尚须按真实实例执行，不能把隔离测试报告称为生产执行结果。在全部现有实例完成切换前，V1 的生产启动与发布依赖仍须保留；完成切换后才清理其运行入口。

插件安装必须原样保存已经验证的清单，不能把日志脱敏用于字段定义。`shared/automation_plugin_repository.py` 将清单存储与审计脱敏分开，并按完整清单比较重复安装；`048_restore_arrival_plugin_manifest.sql` 只修复已知统计 V2 包中被误脱敏的字段定义，按插件、版本、包摘要和清单摘要精确限定，不改实例设置或业务数据。真实 ZIP、隔离 MySQL、安装响应丢失重试与有界修复回归见 `tests/test_plugin_manifest_persistence_mysql.py`。

已验证的插件字段定义、配置、编译参数、运行代次快照及执行租约统一使用 `shared/plugin_json.py` 原样序列化和计算摘要；写入前继续校验 Schema 并拒绝凭据字段，日志和审计继续使用脱敏。不得将用于日志的脱敏函数用于持久运行数据或其完整性比较。`tests/test_arrival_v2_configuration_mysql.py` 从真实统计 ZIP 开始，在隔离 MySQL 中完成配置、代次投影、启用、实际隔离进程执行与结果验证，覆盖空对象业务字段不被改成字符串。

Console 的插件目录识别包含 `harness` 在内的现行入口类型，避免同包声明 AI 入口时连带拒绝手动入口。运行按钮仍只选择当前已提交、已激活的 `console` 入口，不能把 AI 入口当作手动调用。真实 ZIP → MySQL → Agent 目录 → Console 按钮投影的回归与实际执行共用上述统计测试。
