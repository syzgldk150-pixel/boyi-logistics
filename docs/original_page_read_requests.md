# 原页初始化读取与已有任务追踪

## 当前运单录入入口（2026-10-07）

韵达内部页签现直接加载 `https://kyinms.yunda56.com/ky_inms/public/index.php/business/waybill/entry/indexNew.html?page=tab&p=nil`，不再调用原页代理，也不改写原站脚本、接口或打印行为。它使用当前浏览器的韵达登录态，与 ECS 业务账号会话独立。登录失效时可从页签提示条打开原站登录，原页助手登记的辅助窗口在登录成功后自动返回并仅重新加载发起登录的页签。

真实跨站验证：在博益 HTTPS 来源内加载韵达录入页，客户检索直接 POST 原站 `like/getCustomer.html`，query 为 `class/info/type/IdNumber`，正文为 `keyword/ShareSiteCode/page/rows`；响应为 `total/rows`，实测返回 8 条记录。仅记录结构和数量，未保存客户资料或凭据，未执行领号、保存、上传或打印。原站完整保存与本机打印仍需真实业务验收，不能由查询成功推定。

博益报价入口对两个平台都明确“打开原站（手动填写）”；录单壳已移除旧预填缓存读写、重试、字段映射与消息桥接，不向跨站页面注入博益资料。原站保存不经过博益代理的即时入库及本地打印桥接，运单数据由现有寄件同步取得。

融辉按用户最新要求也改为直接嵌入 `https://tms.ronghuiwl.com/module/index?mv=index`，原页助手在登录后通过原站菜单进入“运单录入”，优先激活已有页签。真实录单页地址含临时鉴权参数，不将其复制、固化或通过 Console 拼接。每个融辉页签均提供原站新窗口入口、重新加载及跨站登录限制提示，不再加载代理。本轮真实跨站验证中，浏览器以 `SchemefulSameSiteUnspecifiedTreatedAsLax` 阻止其登录 Cookie，随后登录跳转使用 HTTP，在博益 HTTPS 页面中又被混合内容策略阻止。未安装扩展时仍可能出现内嵌空白或无法登录，不能把 iframe load 当成业务可用。

下文的共享请求契约及会话规则适用于仍保留的旧代理链路，不进入当前两个运单录入页签，也不约束浏览器直连原站。此次切换仅更新 Console，不修改 Agent 契约或原站业务行为。

## 原页助手（0.4.0）

源码单点位于 `console/static/browser_extensions/ronghui/`，安装说明随包提供。已登录博益的用户可从韵达或融辉提示栏下载 `/ocr/browser-extension.zip`，服务端只把固定的已发布源码文件打成 ZIP，不打包凭据、会话或运行态。此下载由 `console/routes/ocr.py` 和 `console/services/browser_extension_package.py` 提供；无需修改发布白名单或把生成 ZIP 纳入 Git。

扩展在用户浏览器本机适配融辉 Cookie 的 SameSite/Secure 属性，把已观察到的 HTTP 登录跳转转为 HTTPS；值不输出、不存文件、不上传。收起侧栏、顶部栏和重复页签后仍使用原站当前的 iframe/文档，通过“原站菜单 / 仅显示录单”切换；同一系统页面只自动进入一次，菜单往返不重新申请单号；初次进入仍可能按原站设置领号。布局只作用于博益内嵌的融辉系统入口，不改变登录页和独立原站标签。没有账号隔离，同一浏览器共享融辉账号；删除扩展不会立即还原已有 Cookie 属性，直到原站重新设置或用户正常退出/清理站点数据。

Chrome 153 实测：内嵌登录、刷新保持、寄件人/收件人历史查询；展开录单后可用面积从 1198×510 增至 1410×599 CSS 像素，菜单往返保持 iframe、文档、地址和已填业务字段，寄件人下拉框可见、底部保存按钮可滚动到。只验证显示和读取，没有提交运单、上传或打印，也没有为验证额外申请单号。Edge/Firefox 仍待实测。ECS 同步仅发布源码及下载入口，不会自动安装到其他电脑，浏览器安装/更新后才启用。

### 0.4.0 的登录与扫码修复

韵达真实飞书包装页为 `/public/feishu/login`，外层 250×294 且禁滚动，标题默认边距使内部 250px 窗口溢出。精确页面 CSS 将标题设为 24px 行高与 12px 下间距，完整窗口高度为 286px，二维码本身不缩放、不读取或改写。

登录辅助窗口由扩展显式打开，`storage.session` 只登记平台、来源浏览器标签和录单页签。真实系统页面就绪后，后台核对精确来源、路径、顶层 frame 和已登记辅助页，只通知发起登录的页签重新加载，返回主站并关闭辅助页；其他原站页面与正在填写的页签不受影响。独立原站不自动开单。韵达内嵌登录后的 `/index/index.html` 跳转既有录单地址；融辉只点击真实 `#mainMenu` 中唯一且非重复折叠菜单的“运单录入”，不拼接含鉴权参数的地址。

2026-10-07 Chrome 154 已安装 0.4.0 实测：真实韵达二维码包装页可视高度 294px、内容无溢出、二维码窗口底边 286px；融辉在博益 iframe 自动打开并激活唯一录单页，菜单往返保持同一个表单 iframe。已登录状态下，两种原站辅助窗口都自动关闭并回到对应录单页，等待标记清除，其他页签保留；韵达实际路径为既有 `entry/indexNew.html`，融辉在 `mainTabs` 打开 `/widget/home` 并展开。本轮没有保存、上传或打印；从未登录状态重新扫码的完整流程尚未复测，业务写后核验不由页面打开成功替代。

## 原页 POST 读取

韵达、融辉原页在初始化时使用 POST 读取数据。只允许 GET 与运单保存 POST 的旧代理规则，会在页面尚未填单时返回 `MANUAL_PROXY_WRITE_DISABLED`，使库存、模板和网点规则无法加载。

`shared/manual_entry_contracts.py` 是 Console 与 Agent 共用的请求判定入口。只读 POST 使用真实登录页面核实的精确路径；融辉共用查询入口还要求唯一、明确的查询编号。不能将所有 POST、整个 `/dataQuery/` 或所有 `FIND_*` 编号视为只读。

本轮核实范围：

- 韵达：电子面单库存 `elecStock.html`、录单模板列表 `business/waybill/entry/getTemplateList.html`、费用提示 `getCostInfoPrompt.html`。
- 2026-09-16 在已登录韵达真实录入页补充核实初始化 POST：`getCurrentTime.html` 无参数，返回 `info/data`；`Region/province.html` 仅 query `state`，`Region/city.html` 与 `Region/county.html` 为 query `state` 加正文 `bm`，返回区域选项；`checkBoxIsDiscount.html` 与 `checkTextIsDiscount.html` 正文均为 `CreatedDotCode/SettlementTotalNumber`，分别返回优惠开关显示状态与优惠说明数据。全部取得 HTTP 200，未领号、保存或修改运单。共享契约只增加这些精确读取路径和字段集合，额外操作、重复参数与其他未核实 POST 继续拒绝。
- 融辉 `/dataQuery/findAllByCallId`：`FIND_SYS_DATE`、`FIND_SITE_INFO_BY_SITE_CODE`、`FIND_SITE_AND_CENTER`、`FIND_TAB_SITE_BY_AGENT`、`FIND_TAB_QUOTE_SWITCH_SITE`、`FIND_TMS_SYS_SHARE_SET`、`FIND_TAB_SITE_BUSINESS_TYPE`、`FIND_BILL_CHECK`、`FIND_TAB_COLLAR_CURRENT_SITE`。
- 融辉 `/minic/combobox`：`optionCode=WEIGHT_RATIO`。
- 融辉领号后的存在性检查：`/dataQuery/findAllByCallId`，`id=GET_BILL_BY_BILLCODE`，正文仅非空 `BILL_CODE`。

判定必须使用实际转发的 query、正文和 Content-Type；重复查询编号、query/body 冲突、无法解析的正文不能被放行。认证仍由独立原页 capability、真实管理员会话与 Agent 验签承担。原有精确运单保存入口保留原权限约束；未知写入和已退役的同源原页入口继续拒绝。

后续增加读取接口时，先在真实登录原页记录方法、路径、query/body 字段、响应结构及用途，再更新这份共享契约及两端测试。不得保存凭据或业务原始值。页面初始化验证不等于已验证真实保存、领号、上传、删除或打印。

## 韵达电子单号领号

2026-09-29 线上访问日志确认“获取电子单号”的 POST 请求被原页代理返回 405；不是初始化读取或登录失败。已在登录后的韵达真实录入页核实按钮处理器，并在发送前终止请求，取得请求结构：`/ky_inms/public/index.php/joinlgs/MakeLogisticsApi/getLogisticsNum.html`，无 query，表单正文仅 `CreatedDotCode/UserCode`。页面以响应 `info` 判断结果，并从 `logistics` 取单号；此次检查未实际申请单号，也未保存运单。

该接口归入受审手工写入，共享契约只允许精确 POST 路径及上述两个非空字符串字段，拒绝额外字段、重复字段、query/body 冲突与 GET 领号。账号与来源权限继续由独立原页 capability、管理员身份、Origin 和 Agent 签名校验承担；不进入只读初始化清单。Console 与真实 Agent 路由/代理运输测试使用合成字段验证放行及参数原样转发，不替代生产真实领号验收。

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
