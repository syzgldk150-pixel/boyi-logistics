# 首页消息与收到工单

首页直接展示融辉、韵达消息正文、来源、时间和状态，支持分类分页及打开原系统。工单与问题件是独立来源，不能相互替代。本文只记录已核验的接口形状，不保存真实账号、运单、正文或业务数量。

## 来源与口径

| 来源 | 已核验读取边界 | 首页展示 |
| --- | --- | --- |
| 融辉公告 | 公告查询上下文；`FIND_TAB_NOTICE_NOT_SEE` | 未读公告的 `GUID/TITLE/CONTENT/CONTENT1/CONTENT2/NOTICE_DATETIME` |
| 融辉收到工单 | “工单接收方查询-工单裁定申诉”；`FIND_TAB_WORK_ORDER`，`ZRF=1`、`IS_DEL1=1`，分别查询 `WORK_STATE=2/3` | 所有日期待责任方响应、待责任方处理的工单；`WORK_ORDER_CODE/WORK_NAME/WORK_CONTENT/WORK_STATE/REGISTER_DATE` 及响应、完结期限 |
| 融辉处理记录 | 按工单编号读取 `FIND_TAB_WORK_ORDER_DETAILS`，按 `HANDLE_DATE` 倒序分页 | `HANDLE_CONTENT/HANDLE_STATE/HANDLE_DATE`，最多最新 20 条；原系统未提供回复未读标识，不能称为“新回复数” |
| 韵达待办消息 | 客户端资源 `21144` 的“待办事项” iframe：`ky-export.yunda56.com/ydky_rabbit_web/ui/view/common/kyimport_message.html` | `msgId/msgTitle/msgContent/createdTime/readStatus/handleStatus/logisticsId/typeName` |
| 韵达收到工单 | 快运工单系统 `/cssp/vue/index.html#/work-order/deal`；`GET /cssp/comp/sysComplaintSearch/compDealPageQuery` | 处理列表中的 `complaintId/compTypeName/compStatusName/compTime/listId/dealOverTime`，正文从 `GET /cssp/comp/sysCompMain/compInfo/{complaintId}` 的 `compContent` 读取 |
| 韵达催单待回复 | `GET /cssp/collaboration/sysCollaborationManage/cuiPageQuery`；`userId` 只取当前会话 `GET /cssp/sys/user/getUser` 的 `User.userId` | `complaintId/remark/colTime/listId`；保持原系统当前接收范围 |

融辉查询通过 `POST /dataQuery/findAllByCallId` 或 `findPageByCallId` 执行只读查询，工单分页使用 `pageIndex/pageSize`。韵达消息中心使用 `/ydky_rabbit_web/actions/message/*.do`：`getTypes` 读取顶层 `rows` 分类；`getNoticeReadByStatus` 按字符串 `readStatus="0"/showModel="0"` 查询未读公告；`getAdviseReadByStatus` 按字符串 `handleStatus="0"/showModel="1"` 查询待处理通知，不能将筛选值改成数字零。消息查询中的 `user` 只取当前客户端 `details.username`，不由浏览器提交。CSSP 分页使用 `offset/limit`。

消息原页请求的查询参数为 `actionId` 和字面字符串 `ssid="null"`。实际会话由 `ky-export.yunda56.com` 域的 `ydky_rabbit_web_cip_sessionid` Cookie 承载；后台要求该 Cookie 有唯一非空值，缺失或冲突明确返回登录失效。不能以通用 `cip_sessionid` 替代，也不能把实际会话值写入 URL；来源接口的业务认证失败仍按实际响应处理。

分类汇总、通知、公告和工单的计数范围可能重叠，不能把它们重复相加。只有明确标记为可合计的独立来源参与总量；无法确认的总量和失败来源数量为 JSON `null`，页面显示“—”，真实成功空列表才显示零。融辉两个待办状态分开读取；已完结历史不计待办。融辉沿用原系统登记时间分页，仅在当前页内按最新处理时间排序，不保证全局最新回复置顶。韵达仅展示 `getTypes` 顶层分类，不展开字段形状不同且可能复用类型编号的 `chidList`；通知、公告、收到工单和催单待回复支持正文分页，其他分类仅显示原系统计数并提供原系统入口。

## 接口与账号边界

- Console 保留 `/monitoring/summary`、`/monitoring/stream`、`/monitoring/detail-link`，新增 `GET /monitoring/items?system=&category_id=&page=&force=`，经签名内部接口 `/internal/v1/admin/monitoring/*` 调用 Agent。汇总对应内部 `snapshot`，正文对应内部 `items`。
- 首页和所有消息接口沿用真实 MySQL 超级管理员权限，不新增普通客服身份权限。Agent 继续实时核验内部服务身份、请求签名及管理员权限。
- 本次范围固定为现有融辉 `default` 与韵达 `yunda` SessionBroker profile。客户端仅提交来源、分类、页码和刷新意图；账号、profile、目标地址由服务器确定。正文固定每页 20 条，页码范围 1–10000。
- 正文响应包含 `system/category_id/title/items/total/page/page_size/has_more/status/status_label/message/updated_at` 及缓存元数据；每条包含标识、正文、时间、来源、状态和安全原系统链接。详情链接只接受 `system/category_id`，忽略浏览器提供的 URL、菜单资源或账号。
- 采集器只调用已核验的只读 HTTP 接口；加载首页、刷新和查看正文不标记已读、不接单、不回复、不处理工单。正文按文本渲染，原始 HTML 不执行。点击“打开原系统”后，原系统自身操作行为由其页面决定。

## 会话、刷新与失败

韵达登录继续先建立寄件会话，再独立初始化报表、问题件、待办消息与 CSSP。消息页以实际 iframe 中隐藏的 `input#notice_readStatus` 已附加到 DOM 确认页面加载，消息认证仍由实际接口响应判断。CSSP 在同一浏览器 context 的客户端首页等待精确“快运工单系统”应用链接可见，再向该链接派发 `click` 事件打开临时页面；不点击或确认遮挡鼠标的公告、资料填写等业务提醒。只有 `getUser` 明确返回成功及 `User.userId` 才记为就绪；临时页面关闭后，既有保存步骤统一保存会话。四个可选来源各最多 10 秒、共享 40 秒预算，并受现有 staged 登录总期限约束；单个子系统失败不清除已建立的寄件登录态。

`query_session_initialization` 只持久化上述四个子系统的固定 `status/code`，正常账号状态刷新保留，退出登录时清除；不保存任意原页响应或认证数据。

升级后应在后台韵达账号管理中重新登录一次，以初始化新增子系统会话。已有会话缺少消息/CSSP Cookie 时明确显示该来源不可用，不用其他账号或旧成功值掩盖失败。凭据和 Cookie 只由现有 SessionBroker 管理，消息模块不保存凭据，不将正文写入数据库或运行文件。

首页保留 60 秒刷新及手动刷新；服务端缓存有效期 55 秒。正文缓存按 `(system, profile, category_id, page)` 隔离、仅保存在进程内；失败不缓存成成功。来源单独显示错误；页面保留旧内容时必须显示更新时间和过期状态。手动 `force` 只刷新目标来源，不调用全能力健康矩阵或触发业务写操作。

“今日应签未签”继续使用既有飞书应签明细和定时刷新口径，本次不改其业务计算及试点切换条件。

## 维护与验收

聚合、缓存和入口位于 `agent/agent/tms_runtime/monitoring.py`；平台协议分别位于 `monitoring_ronghui.py`、`monitoring_yunda.py`。Console 代理位于 `console/services/monitoring_finance.py`，页面与浏览器交互位于 `console/templates/portal.html`、`console/static/monitoring.js`。新增来源必须先核验真实原页字段、接收范围及状态，再修改适配器。

离线测试覆盖解析失败、登录失效、部分来源失败、分页、重复记录、未知数量、缓存范围、浏览器参数注入和只读请求边界。真实账号验收应在同一时间和筛选范围核对原页的正文、状态、数量与链接；不能用固定业务数量作为长期测试期望。

本次源码交付不包含 ECS 部署。生产发布需用户另外授权，按既有发布入口执行；未发布及后台重新登录前，不能宣称线上首页已采用新采集链路。
