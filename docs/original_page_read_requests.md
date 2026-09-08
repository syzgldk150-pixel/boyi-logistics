# 原页初始化读取与已有任务追踪

## 原页 POST 读取

韵达、融辉原页在初始化时使用 POST 读取数据。只允许 GET 与运单保存 POST 的旧代理规则，会在页面尚未填单时返回 `MANUAL_PROXY_WRITE_DISABLED`，使库存、模板和网点规则无法加载。

`shared/manual_entry_contracts.py` 是 Console 与 Agent 共用的请求判定入口。只读 POST 使用真实登录页面核实的精确路径；融辉共用查询入口还要求唯一、明确的查询编号。不能将所有 POST、整个 `/dataQuery/` 或所有 `FIND_*` 编号视为只读。

本轮核实范围：

- 韵达：电子面单库存 `elecStock.html`、录单模板列表 `business/waybill/entry/getTemplateList.html`、费用提示 `getCostInfoPrompt.html`。
- 融辉 `/dataQuery/findAllByCallId`：`FIND_SYS_DATE`、`FIND_SITE_INFO_BY_SITE_CODE`、`FIND_SITE_AND_CENTER`、`FIND_TAB_SITE_BY_AGENT`、`FIND_TAB_QUOTE_SWITCH_SITE`、`FIND_TMS_SYS_SHARE_SET`、`FIND_TAB_SITE_BUSINESS_TYPE`、`FIND_BILL_CHECK`、`FIND_TAB_COLLAR_CURRENT_SITE`。
- 融辉 `/minic/combobox`：`optionCode=WEIGHT_RATIO`。
- 融辉领号后的存在性检查：`/dataQuery/findAllByCallId`，`id=GET_BILL_BY_BILLCODE`，正文仅非空 `BILL_CODE`。

判定必须使用实际转发的 query、正文和 Content-Type；重复查询编号、query/body 冲突、无法解析的正文不能被放行。认证仍由独立原页 capability、真实管理员会话与 Agent 验签承担。原有精确运单保存入口保留原权限约束；未知写入和已退役的同源原页入口继续拒绝。

后续增加读取接口时，先在真实登录原页记录方法、路径、query/body 字段、响应结构及用途，再更新这份共享契约及两端测试。不得保存凭据或业务原始值。页面初始化验证不等于已验证真实保存、领号、上传、删除或打印。

## 融辉电子主单领号

真实原页的 `crud.init()` 会恢复 `FIND_BILL_CHECK` 返回的“电子主单”保存偏好，并调用 `crud.refreshBillCode()`；电子主单开关、重置及无单号订单带入也会调用该函数。函数以 POST 请求 `/dataQuery/findAllByCallId?id=FIND_TMS_BILL_CODE_BY`，正文 `vCount=1`，将返回的单号填入 `BILL_CODE` 并设为只读，再调用上述存在性检查、本地生成子单号。现有证据只观察到一次领号请求被原代理规则拒绝，不能据此声称重复领号已发生。

这条接口是申请单号的**受审手工写入**，不属于只读初始化白名单，不进入查询缓存，也不自动重试。共享契约只接受这个精确 selector、唯一的 `vCount=1`；重复参数、额外操作、批量数量和 GET 领号均拒绝。授权沿用现有独立原页 capability、真实 `super_admin` 会话、Origin 与 Agent 签名校验。保留原页开关、保存偏好及初始化行为，不新增领号按钮或改写原生流程。因此用户打开带有该偏好的原页可能触发真实领号；维护人员不能把该页面刷新当成无写入的验收。

隔离回归使用合成账号元数据和末端 TMS 响应，运行真实 Console 入口、AgentApi 签名、Agent 验签/路由、dispatch、账号选择及 `run_once`，核验领号、后续查重、无缓存以及响应丢失不自动重试。此测试不证明上游库存具体持久化方式，也不代表已执行生产领号验收。

## 原页账号会话

原页代理必须使用调度层解析业务账号后注入的 session_profile；不得固定使用历史会话。未取得非空字符串会话标识时返回 ACCOUNT_SESSION_PROFILE_REQUIRED，不尝试其他账号。融辉只读查询缓存按会话标识隔离，相同 URL 的不同账号不能共享结果。

本机原生浏览器登录不会自动更新 ECS 的账号会话。服务器会话须通过现有“业务账号”登录流程维护，不导出或复制浏览器凭据。

韵达录入页初始化还会产生后续接口需要的服务端会话状态。2026-09-08 使用 ECS 现有 Broker 进行独立只读对照：每个接口从新建 Session 开始时，模板返回 HTTP 200 空正文、费用提示返回 HTTP 404 的 System Error 页面；在同一 Session 内加载实际录入页后，两者均返回 HTTP 200 与原站对应的 JSON 结构；再次新建 Session 又分别复现。该对照只处理响应格式和已核实的查询字段，不读取或输出凭据、Cookie 值及业务记录。

原页代理必须通过 Broker 延续每个账号的请求状态，不能在每次请求时重置为最初登录快照，也不能靠失败后额外加载页面或重试写入掩盖状态丢失。上游会话更新只保存在 Agent，独立原页响应仍剥除 `Set-Cookie`；会话提交须与并发原页请求串行，并以当前登录代际校验，禁止旧响应覆盖新登录或退出状态。此修复属于账号运行器的核心更新，不是业务插件 payload 更新。

## 原页错误原因

Agent 的旧 TMS 返回可能使用 HTTP 200、"ok: false" 与字符串错误。内部 API 必须将其转换为标准失败信封，保留机器码、脱敏原因及原有数据；不能包装成成功或丢失原因。Console 原页代理继续传递该机器码与原因。

"AUTH_REQUIRED" 既可能来自登录响应，也可能来自上游空响应。应依据实际原因核验，不应仅凭该机器码认定用户没有登录。

## 已有任务追踪

重复提交遇到 `AUTOMATION_ALREADY_RUNNING` 时，后台已有的 Run 是状态权威。Console 只传递安全的已有任务标识，页面绑定这个精确标识读取 `/automations/tasks/output`；不把冲突描述为新命令已受理，也不再次提交、取消或解除后台锁。

原实现丢失 `active_run_id` 后恢复了“执行”按钮，留下“正在执行”的错误提示。没有 Run ID 的旧工具输出并不能证明项目当前没有任务。后续状态必须按具体 Run 显示；读取失败保留追踪身份并明确提示暂时无法同步。

## 验证与发布

共享契约、Agent 路由和 Console 路由测试须覆盖正常初始化、未知或冲突请求拒绝、管理员/独立来源校验和原保存入口。自动化页面须通过实际 JavaScript 状态测试验证冲突后的已有 Run 追踪及结束恢复。

本修复涉及共享接口与两个服务，按[标准 ECS 发布流程](../agent/deploy/publish_to_ecs.md)发布核心。没有数据库迁移或插件业务 payload 变更；签名插件满足逐文件一致时可按发布手册复用。发布后可检查韵达读取请求；融辉保存偏好可能在打开原页时申请单号，因此不能由只读维护验收刷新或打开融辉原页。真实业务执行须有对应用户授权，并沿用正常入口、目标确认与写后核验。回滚使用本次发布器生成的精确恢复材料，保留新产生的业务数据。
