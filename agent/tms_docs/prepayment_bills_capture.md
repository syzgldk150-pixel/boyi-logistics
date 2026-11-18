---
module: 财务模块
type: external_page_snapshot
status: snapshot
captured_at: 2026-11-18
verified_at: 2026-11-18
verification_scope: ecs_saved_sessions_read_only
uncertainty: low
updated: 2026-11-18
---

# 融辉 / 韵达预付款对账单接口核实（2026-11-18）

> 外部页面快照：2026-11-18 在 ECS 用业务账号已保存的登录态只读核实（只发查询请求，未做任何写入）。平台改版后以真实页面为准；代码实现见 `../agent/tms_runtime/platform_prepay_bills.py`。文中不记录真实账号、网点编码或金额。

## 融辉：预付款管理 / 结算明细查询

- 入口：`/menuTreeExtend/loadMenu` 中菜单路径 `预付款管理 / 结算明细查询` 的 `/widget/home?authenticationKey=…&pageId=…`。三个取数网点账号均有该菜单。
- 必须先 GET 该 widget 页，再以同一会话 POST `/dataQuery/findPageByCallId?id=<调用ID>`，请求头带 `authenticationKey`、`pageId`、`Referer`=widget 页；否则返回 `{"success": false, "message": "非法的请求"}`。
- 页面有三个页签：预付款余额查询、明细查询、统计查询（统计查询内再分“汇总数据”“明细数据”）。

| 用途 | 表格 id | 调用 ID | 关键参数 |
|---|---|---|---|
| 预付款余额 | `accountDatagrid` | `FIND_BALANCE_ACCOUNT_INFO` | `SITE_NAME_CODE`=登录网点 |
| 统计（汇总数据） | `datagridTJ` | `FIND_BALANCE_QRY_TJ_WST` | `DATE_TYPE`、`DATE`、同名日期口径字段、`CENTER_NAME_CODE/SITE_NAME_CODE/TURN_NAME_CODE/BALANCE_TYPE` 为空 |
| 统计行双击（明细数据） | `datagridTJDetails` | `FIND_BALANCE_QRY_TJ_DETAIL` | 同上，另带所选行的 `CENTER_NAME_CODE`、`SITE_NAME_CODE`、`BALANCE_TYPE` |
| 明细查询 | `datagrid` | `FIND_BALANCE_QRY_WST_WITH_SITE`（指定网点时；不指定为 `FIND_BALANCE_QRY_WST`） | `DATE_TYPE`、`DATE`、日期口径字段、`CODE/SOURCE_TYPE/CONFIRM_STATUS/CONFIRM_STATUSNEW` 为空、`SITE_NAME_CODE`=登录网点 |

- 日期口径 `DATE_TYPE`：`BALANCE_DATE`（结算日期）或 `FINANCE_DATE`（账单日期）；区间为 JSON `{"start": "YYYY-MM-DD 00:00:00", "end": "YYYY-MM-DD 23:59:59"}`，同时放在 `DATE` 和口径同名字段；日期控件 `maxSpan=32` 天。
- 分页 `pageIndex`（从 0）、`pageSize`（页面可选 100/200/300/500）；返回 `{total, data, totalInfo, …}`，金额为 JSON 数字。
- 列标题与字段取自页面表格内 `<div header="…" field="…">` 声明（统计 7 列；明细数据 40 余列，含前余额、金额、后余额、运单编号、结算类型等）。

## 韵达：快运预付款系统（KY_YFK）

- 客户端应用 `/client/user/app` 中“快运预付款系统”指向 `ky-yfkweb.yunda56.com`；其菜单（`/ydkyyfk/menus/getResources.action`）里“账户交易 / 交易明细及汇总查询”与“分部预付款 / 账户余额(二级网点)”实际都打开 `ky-appept.yunda56.com` 的通用报表模板系统。
- 会话：用已保存登录态 GET `https://ky-appept.yunda56.com/export/logins?templateCode=<菜单>&applicationCode=PCTab@SYS:KY_YFK@PARAM:<base64 JSON>`，经 `ky-sso` OAuth 跳转后落在 `/export/public/index.html`；未登录会停在 ky-sso 登录页。无需浏览器初始化。
- 身份：`GET /export/author/financialSysAuthor/getAuth` → `dataMap.username` 作为后续 `userId`。
- 请求格式：POST multipart，表单字段 `message` 为 JSON。

| 用途 | 接口 | 要点 |
|---|---|---|
| 页签列表 | `author/dynamicPcTabBasic/selectMenuTab` | `ac_trade_sum_query` 有 `ac_trade_detail_es`（交易明细）、`ac_trade_sum_day`（交易汇总-按天）、`ac_trade_sum_query`（交易汇总查询） |
| 列 | `author/dynamicPcTabBasic/selectDynamicFileds` | `template_code`=页签；`skipFieldStatus=1` 的列带 `skipData`（参数名）与 `skipTab`（目标页签） |
| 交易数据 | `author/dynamicPcTabInterface/selectInterface` | `{menu_code, tab_code, APPVALUE:"KY_YFK", userId, interface_skip:1, message:<内层 JSON 字符串>}`；内层含 `template_code/page/rows/start_time/end_time` 与条件字段。返回三层 JSON 字符串，最内层 `{total, footer:["收入:",…,"支出:",…,"收支差(收-支):",…], rows}`；超过 31 天返回“只可查询最长31天数据”；`rows` 1000 可用 |
| 分部余额 | `author/dynamicPcTabBasic/selectFiledsData` | `menu_code=template_code=account_amount_fb`、`interface_skip:0`、`workType:"branch"`；下拉条件 `auth_type/branch_status/send_status/account_status` 空值必须传一个空格，否则报错 |

- 下钻（与页面一致）：交易汇总行“操作1”→按天、“操作2”→交易明细；按天行“操作”→交易明细；分部余额“账号”→交易汇总查询（新窗口）。参数名为列 `skipData` 的键，值优先取所点行同名字段，行里没有的沿用当前查询条件（分部跳转带 `account`、`sub_data_display`、`time_selsect` 与 `workType=branch`）。
