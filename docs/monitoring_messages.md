---
module: 首页消息监控
type: 业务说明
status: active
updated: 2026-11-27
---

# 首页消息与收到工单

首页直接展示融辉、韵达消息正文、来源、时间和状态，支持分类分页及打开原系统。工单与问题件是独立来源，不能相互替代。本文只记录已核验的接口形状，不保存真实账号、运单、正文或业务数量。

## 来源与口径

| 来源 | 已核验读取边界 | 首页展示 |
| --- | --- | --- |
| 融辉公告 | 公告查询上下文；`FIND_TAB_NOTICE_NOT_SEE` | 未读公告的 `GUID/TITLE/CONTENT/CONTENT1/CONTENT2/NOTICE_DATETIME` |
| 融辉收到工单 | “工单接收方查询-工单裁定申诉”；`FIND_TAB_WORK_ORDER`，`ZRF=1`、`IS_DEL1=1`，分别查询 `WORK_STATE=2/3`，每页 50 条读完全部页 | 只保留责任方 `RESPONSIBLE_PARTY` 为邵阳大祥站、邵阳大祥S站、邵阳操作场、邵阳自提部的待责任方响应、待责任方处理工单（用户 2026-11-18 确认）；“已完结”不显示，缺责任方字段或出现其他状态明确失败；`WORK_ORDER_CODE/WORK_NAME/WORK_CONTENT/WORK_STATE/REGISTER_DATE` 及响应、完结期限 |
| 融辉处理记录 | 按工单编号读取 `FIND_TAB_WORK_ORDER_DETAILS`，按 `HANDLE_DATE` 倒序分页 | `HANDLE_CONTENT/HANDLE_STATE/HANDLE_DATE`，最多最新 20 条；原系统未提供回复未读标识，不能称为“新回复数” |
| 韵达问题件 | 问题件查询 `kyproblem.yunda56.com/ky_problem/public/index.php/query/list.html`，`issuer_site=1`（发布给我的），`time=1`（发布时间）与 `time=2`（处理时间）各查一次后合并，`prob_status` 分别为 0 未处理、1 处理中（3 超时未完结不计入，用户确认 2026-11-27） | `prob_main_id/ship_no/prob_type_nm/prob_text/prob_status/site_id/created_time/reply_text/reply_time/over_time`；返回已完结记录明确失败 |
| 韵达仲裁 | 快运仲裁系统 `kyzc.yunda56.com:8090`，先打开 `/kyzc/arbitrament/arbitramentSearch` 经统一登录换取本系统会话，再 `GET .../arbitramentSearch/list`，`searchDate` 分别为 `k.declare_date`（申报时间）与 `g.assignment_update_time`（最新处理时间）后合并，`belongCate=arbitrament`，`disposeState` 传除“结算审核通过、结算审核不通过、中心不受理”外的全部状态代码，`limit/offset` 分页 | `declId/waybillCode/declType/declCategory/disposeState/declareDept/costTypeName/money/createDate/appealDate/assignmentUpdateTime`；返回终态记录明确失败 |
| 韵达调拨件 | 网点运营系统“调拨件确认” `kyinms.yunda56.com/.../replaceLogistics/ReplaceLogisticsConfirm/list.html`，`Replace_Status=0`（待接单，用户确认只显示该状态），`page/rows` 分页 | `Replace_Id/Logistics_Id/Replace_Status/Replace_Status_Code/Apply_Site_Name/Replace_Site_Name/Sender_Address/Create_Time`；`Replace_Status_Code` 非 0 明确失败 |
| 韵达收到工单 | 快运工单系统 `/cssp/vue/index.html#/work-order/deal`；`GET /cssp/comp/sysComplaintSearch/compDealPageQuery` | 处理列表中的 `complaintId/compTypeName/compStatusName/compTime/listId/dealOverTime`，正文从 `GET /cssp/comp/sysCompMain/compInfo/{complaintId}` 的 `compContent` 读取 |
| 韵达催单待回复 | `GET /cssp/collaboration/sysCollaborationManage/cuiPageQuery`；`userId` 只取当前会话 `GET /cssp/sys/user/getUser` 的 `User.userId` | `complaintId/remark/colTime/listId`；保持原系统当前接收范围 |

整个消息中心只显示最近 3 天（今天、昨天、前天）内有动态的未完成记录（用户 2026-11-18 确认），规则单点维护在 `agent/agent/tms_runtime/monitoring_window.py`。动态时间取原系统时间中最新的一个：融辉工单为登记时间与处理记录（含催工单）中最新的时间，融辉公告为发布时间，韵达问题件为发布或处理时间，仲裁为申报或最新处理时间，调拨件为发布时间，快运工单为登记时间，催单待回复为催单时间。记录没有任何可读时间、或融辉处理记录读取失败时保留显示（无法证明已过期），并标明部分读取失败。韵达三个原页一次最多查询 31 天（超过即 HTTP 400），3 天范围在限制内；读完全部分页后按原系统编号去重计数，单类超过 500 条明确失败并提示打开原系统。

2026-11-18 起首页不再读取韵达客户端“待办事项”消息中心（未读公告、消息中心提醒及 `getTypes` 分类）：用户确认该来源更新不及时，且含大量重复推送和已处理消息。

融辉查询通过 `POST /dataQuery/findAllByCallId` 或 `findPageByCallId` 执行只读查询，工单分页使用 `pageIndex/pageSize`。CSSP 分页使用 `offset/limit`。


各来源计数范围不同，不重复相加。只有明确标记为可合计的独立来源参与总量；无法确认的总量和失败来源数量为 JSON `null`，页面显示“—”，真实成功空列表才显示零。融辉两个待办状态分开读取；已完结历史不计待办。各分类都读全量、按上述规则过滤后，按最新动态时间倒序分页；快运工单只为当前页读取正文详情。

## 首页展示

- 顶部四张卡片只读取对应分类计数，不使用跨口径总数：融辉收到工单（待响应与待处理两个互斥状态相加并分列）、韵达收到工单（另列催单待回复，不相加）、韵达问题件、韵达仲裁与调拨件（两个互斥来源相加并分列）。任一参与分类未知时显示“—”；点击卡片切换到对应分类正文。韵达仲裁原页固定在 8090 端口，“打开原系统”只对 `kyzc.yunda56.com:8090` 放行该端口。
- 消息中心不设分类页签（2026-11-19 用户确认分类只为筛选、主要按平台看）：只有“全部 / 韵达 / 融辉”来源切换，列表合并所有有待办的分类与待确认问题件，按时间倒序，每行标明来源与分类；每类先读第一页，“加载更多”只读取还有下一页的分类的下一页。点顶部卡片可临时只看该卡片的分类，以可清除的筛选标签显示。后台读取的 8 个来源不变。

## 接口与账号边界

- Console 保留 `/monitoring/summary`、`/monitoring/stream`、`/monitoring/detail-link`，新增 `GET /monitoring/items?system=&category_id=&page=&force=`，经签名内部接口 `/internal/v1/admin/monitoring/*` 调用 Agent。汇总对应内部 `snapshot`，正文对应内部 `items`。
- 首页和所有消息接口沿用真实 MySQL 超级管理员权限，不新增普通客服身份权限。Agent 继续实时核验内部服务身份、请求签名及管理员权限。
- 本次范围固定为现有融辉 `default` 与韵达 `yunda` SessionBroker profile。客户端仅提交来源、分类、页码和刷新意图；账号、profile、目标地址由服务器确定。正文固定每页 20 条，页码范围 1–10000。
- 正文响应包含 `system/category_id/title/items/total/page/page_size/has_more/status/status_label/message/updated_at` 及缓存元数据；每条包含标识、正文、时间、来源、状态和安全原系统链接。详情链接只接受 `system/category_id`，忽略浏览器提供的 URL、菜单资源或账号。
- 采集器只调用已核验的只读 HTTP 接口；加载首页、刷新和查看正文不标记已读、不接单、不回复、不处理工单。正文按文本渲染，原始 HTML 不执行。点击“打开原系统”后，原系统自身操作行为由其页面决定。

## 会话、刷新与失败

韵达登录继续先建立寄件会话，再独立初始化报表、问题件、待办消息与 CSSP。消息页以实际 iframe 中隐藏的 `input#notice_readStatus` 已附加到 DOM 确认页面加载，消息认证仍由实际接口响应判断。CSSP 在同一浏览器 context 的客户端首页等待精确“快运工单系统”应用链接可见，再向该链接派发 `click` 事件打开临时页面；不点击或确认遮挡鼠标的公告、资料填写等业务提醒。只有 `getUser` 明确返回成功及 `User.userId` 才记为就绪；临时页面关闭后，既有保存步骤统一保存会话。四个可选来源各最多 10 秒、共享 40 秒预算，并受现有 staged 登录总期限约束；单个子系统失败不清除已建立的寄件登录态。快运仲裁系统不在登录初始化中：每次读取先打开仲裁查询页，跟随韵达统一登录的授权跳转换取本系统会话；最终页面不是仲裁查询页即按登录失效处理，不提交任何表单。

`query_session_initialization` 只持久化上述四个子系统的固定 `status/code`，正常账号状态刷新保留，退出登录时清除；不保存任意原页响应或认证数据。

升级后应在后台韵达账号管理中重新登录一次，以初始化新增子系统会话。已有会话缺少消息/CSSP Cookie 时明确显示该来源不可用，不用其他账号或旧成功值掩盖失败。凭据和 Cookie 只由现有 SessionBroker 管理，消息模块不保存凭据，不将正文写入数据库或运行文件。

首页保留 60 秒刷新及手动刷新；服务端缓存有效期 55 秒。正文缓存按 `(system, profile, category_id, page)` 隔离、仅保存在进程内；失败不缓存成成功。来源单独显示错误；页面保留旧内容时必须显示更新时间和过期状态。手动 `force` 只刷新目标来源，不调用全能力健康矩阵或触发业务写操作。

首页不再展示“今日应签未签”统计，也不请求或轮询飞书应签明细；手动刷新、自动刷新和页面重新可见时均只更新消息与工单。应签数据直接在飞书表查看，每日应签插件及其同步定时由原自动化项目管理。

## 维护与验收

聚合、缓存和入口位于 `agent/agent/tms_runtime/monitoring.py`；平台协议分别位于 `monitoring_ronghui.py`、`monitoring_yunda.py`。Console 代理位于 `console/services/monitoring_finance.py`，页面与浏览器交互位于 `console/templates/portal.html`、`console/static/monitoring.js`。新增来源必须先核验真实原页字段、接收范围及状态，再修改适配器。

离线测试覆盖解析失败、登录失效、部分来源失败、分页、重复记录、未知数量、缓存范围、浏览器参数注入和只读请求边界。真实账号验收应在同一时间和筛选范围核对原页的正文、状态、数量与链接；不能用固定业务数量作为长期测试期望。

本次源码交付不包含 ECS 部署。生产发布需用户另外授权，按既有发布入口执行；未发布及后台重新登录前，不能宣称线上首页已采用新采集链路。
