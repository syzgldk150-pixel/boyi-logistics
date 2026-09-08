# 原页初始化读取与已有任务追踪

## 原页 POST 读取

韵达、融辉原页在初始化时使用 POST 读取数据。只允许 GET 与运单保存 POST 的旧代理规则，会在页面尚未填单时返回 `MANUAL_PROXY_WRITE_DISABLED`，使库存、模板和网点规则无法加载。

`shared/manual_entry_contracts.py` 是 Console 与 Agent 共用的请求判定入口。只读 POST 使用真实登录页面核实的精确路径；融辉共用查询入口还要求唯一、明确的查询编号。不能将所有 POST、整个 `/dataQuery/` 或所有 `FIND_*` 编号视为只读。

本轮核实范围：

- 韵达：电子面单库存 `elecStock.html`、录单模板列表 `business/waybill/entry/getTemplateList.html`、费用提示 `getCostInfoPrompt.html`。
- 融辉 `/dataQuery/findAllByCallId`：`FIND_SYS_DATE`、`FIND_SITE_INFO_BY_SITE_CODE`、`FIND_SITE_AND_CENTER`、`FIND_TAB_SITE_BY_AGENT`、`FIND_TAB_QUOTE_SWITCH_SITE`、`FIND_TMS_SYS_SHARE_SET`、`FIND_TAB_SITE_BUSINESS_TYPE`、`FIND_BILL_CHECK`、`FIND_TAB_COLLAR_CURRENT_SITE`。
- 融辉 `/minic/combobox`：`optionCode=WEIGHT_RATIO`。

判定必须使用实际转发的 query、正文和 Content-Type；重复查询编号、query/body 冲突、无法解析的正文不能被放行。认证仍由独立原页 capability、真实管理员会话与 Agent 验签承担。原有精确运单保存入口保留原权限约束；未知写入和已退役的同源原页入口继续拒绝。

后续增加读取接口时，先在真实登录原页记录方法、路径、query/body 字段、响应结构及用途，再更新这份共享契约及两端测试。不得保存凭据或业务原始值。页面初始化验证不等于已验证真实保存、领号、上传、删除或打印。

## 原页错误原因

Agent 的旧 TMS 返回可能使用 HTTP 200、"ok: false" 与字符串错误。内部 API 必须将其转换为标准失败信封，保留机器码、脱敏原因及原有数据；不能包装成成功或丢失原因。Console 原页代理继续传递该机器码与原因。

"AUTH_REQUIRED" 既可能来自登录响应，也可能来自上游空响应。应依据实际原因核验，不应仅凭该机器码认定用户没有登录。

## 已有任务追踪

重复提交遇到 `AUTOMATION_ALREADY_RUNNING` 时，后台已有的 Run 是状态权威。Console 只传递安全的已有任务标识，页面绑定这个精确标识读取 `/automations/tasks/output`；不把冲突描述为新命令已受理，也不再次提交、取消或解除后台锁。

原实现丢失 `active_run_id` 后恢复了“执行”按钮，留下“正在执行”的错误提示。没有 Run ID 的旧工具输出并不能证明项目当前没有任务。后续状态必须按具体 Run 显示；读取失败保留追踪身份并明确提示暂时无法同步。

## 验证与发布

共享契约、Agent 路由和 Console 路由测试须覆盖正常初始化、未知或冲突请求拒绝、管理员/独立来源校验和原保存入口。自动化页面须通过实际 JavaScript 状态测试验证冲突后的已有 Run 追踪及结束恢复。

本修复涉及共享接口与两个服务，按[标准 ECS 发布流程](../agent/deploy/publish_to_ecs.md)发布核心。没有数据库迁移或插件业务 payload 变更；签名插件满足逐文件一致时可按发布手册复用。发布后重新打开两个原页，只检查读取请求与页面初始化，不执行真实业务写入。回滚使用本次发布器生成的精确恢复材料，保留新产生的业务数据。
