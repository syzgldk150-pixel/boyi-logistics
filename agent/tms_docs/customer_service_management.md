---
module: 融辉TMS
type: external_page_snapshot
status: snapshot
captured_at: 2026-03-25
verified_at: 2026-08-30
verification_scope: repository_metadata_only
uncertainty: high
updated: 2026-08-30
---

# 客服管理 页面深拆

> 外部页面快照：内容未在 2026-08-30 重新登录真实页面复验。DOM、接口、字段和页面身份可能已变化，不能直接作为当前自动化合同。

## Summary

- 叶子页数量：`11`
- 分组节点数量：`2`
- 高价值页面：`公告查询`、`客服记录查询`、`费用调整登记`、`快件跟踪新`、`工单登记`
- 分组节点：`工单管理`、`费用调整`

## Pages

### 工单管理

**Page Identity**
- 菜单路径：`客服管理 / 工单管理`
- 页面类型：分组节点
- 分组子项：`工单登记`、`工单接收方查询`
- 说明：该菜单项只负责展开子菜单，live 点击时可能仍停留在上一业务页，不应把当前 iframe 的 `pageId/标题` 误判为该分组节点身份。

**Automation Notes**
- 该项不应直接当成 iframe 页面处理，应该继续点击它的子菜单。

### 工单登记

**Page Identity**
- 菜单路径：`客服管理 / 工单登记`
- 页面类型：叶子页面，上级分组是 `工单管理`
- `pageId`：`PS5LIXZ1oRoqVpNDhvB46MvyeCNDy9dYsVeEae`
- 页面标题：`工单管理`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 页签：`查询结果`、`跟进详情`
- 查询区/标签：`查询条件`、`按运单号查询`、`按工单号查询`、`查询时间范围`、`登记时间`、`工单类型`、`登记网点`、`工单状态`、`响应超时`、`工单类别`、`是否解决`、`责任网点`
- 首屏字段：`searchOrderInput`、`REGISTER_DATE$text`、`WORK_TYPE$text`、`REGISTER_SITE`、`WORK_STATE$text`、`BL_RESPONSE_OVERTIME$text`、`WORK_NAME$text`、`IS_SOLVE$text`、`RESPONSIBLE_PARTY`、`COST_TYPE$text`、`BL_OVER_OVERTIME$text`、`URGE_COUNT$text`
- MiniUI text/value 对：`CODE_TYPE` -> value=`CODE_TYPE$value` / submit=`CODE_TYPE`；`searchOrderInput` -> text=`searchOrderInput$text`；`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`REGISTER_DATE` -> text=`REGISTER_DATE$text` / value=`REGISTER_DATE$value` / submit=`REGISTER_DATE`；`WORK_TYPE` -> text=`WORK_TYPE$text` / value=`WORK_TYPE$value` / submit=`WORK_TYPE`；`REGISTER_SITE` -> text=`REGISTER_SITE$text` / value=`REGISTER_SITE$value` / submit=`REGISTER_SITE`；`WORK_STATE` -> text=`WORK_STATE$text` / value=`WORK_STATE$value` / submit=`WORK_STATE`；`BL_RESPONSE_OVERTIME` -> text=`BL_RESPONSE_OVERTIME$text` / value=`BL_RESPONSE_OVERTIME$value` / submit=`BL_RESPONSE_OVERTIME`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`、`查询结果`
- 保存类按钮：`登记网点`、`响应超时`、`完结超时`、`新增`、`新增`、`新增`、`确认解决`、`确认解决`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `登记网点`: `xpath=//*[normalize-space(.)="登记网点"]`
- `响应超时`: `xpath=//*[normalize-space(.)="响应超时"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `REGISTER_DATE$text`: `#REGISTER_DATE$text` / `xpath=//*[@id="REGISTER_DATE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `WORK_TYPE$text`: `#WORK_TYPE$text` / `xpath=//*[@id="WORK_TYPE$text"]` / `xpath=//*[@placeholder="请选择"]`
- `CODE_TYPE`: value=`CODE_TYPE$value` / submit=`CODE_TYPE`
- `searchOrderInput`: text=`searchOrderInput$text`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `FIND_TAB_WORK_ORDER` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_TAB_SITE_COMBOBOX_ALL` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_TAB_WORK_ORDER_TYPE_TYPE` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_TAB_WORK_ORDER`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_WORK_ORDER`
- 请求方法：`POST`
- 主查询参数键：`CODE_TYPE`、`searchOrderInput`、`searchDateType`、`REGISTER_DATE`、`WORK_TYPE`、`REGISTER_SITE`、`WORK_STATE`、`BL_RESPONSE_OVERTIME`、`WORK_NAME`、`IS_SOLVE`、`RESPONSIBLE_PARTY`、`COST_TYPE`、`BL_OVER_OVERTIME`、`URGE_COUNT`、`BL_VIP`、`ORDER_BY_CREATE_DATE`、`DJF`、`pageIndex`、`pageSize`、`sortField`
- 点击查询后额外请求：
  - `FIND_TAB_WORK_ORDER` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_WORK_ORDER`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_TAB_WORK_ORDER`
- 页面脚本提示接口：
  - `/dataQuery/findAllByCallId?id=FIND_TAB_WORK_ORDER_DETAILS`
  - `/dataQuery/findPageByCallId?id=FIND_TAB_WORK_TYPE&DETAILED_NAME1=`
  - `/dataQuery/findAllByCallId?id=FIND_TAB_WORK_ORDER_TYPE_TYPE`
  - `/dataOperation/saveTables`
  - `//$Z.page.datagridExportExcel(`
  - `/dataQuery/findPageByCallId?id=FIND_TAB_WORK_ORDER_DETAILS&WORK_ORDER_CODE=`
  - `/dataQuery/findAllByCallId?id=FIND_TAB_WORK_ORDER_DETAILS_PIC&ORDER_GUID=`

**Field Mapping**
- 用户输入参数：`CODE_TYPE`、`searchOrderInput`、`searchDateType`、`REGISTER_DATE`、`WORK_TYPE`、`REGISTER_SITE`、`WORK_STATE`、`BL_RESPONSE_OVERTIME`、`WORK_NAME`、`IS_SOLVE`、`RESPONSIBLE_PARTY`、`COST_TYPE`、`BL_OVER_OVERTIME`、`URGE_COUNT`、`BL_VIP`
- 页面自动补齐参数：`ORDER_BY_CREATE_DATE`、`DJF`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`CODE_TYPE` -> `CODE_TYPE`；`searchDateType` -> `searchDateType`；`REGISTER_DATE` -> `REGISTER_DATE`；`WORK_TYPE` -> `WORK_TYPE`；`REGISTER_SITE` -> `REGISTER_SITE`；`WORK_STATE` -> `WORK_STATE`；`BL_RESPONSE_OVERTIME` -> `BL_RESPONSE_OVERTIME`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `searchBtn -> searchMethod();`
  - `urgeBtn -> urgeWork();`
  - `confirmSolveBtn -> confirmSolve();`
  - `checkBtn -> checkFun();`
  - `clearBillCode -> //清空查询单号 mini.get("searchOrderInput").setValue("");`
- 保存/提交操作键：`TAB_WORK_ORDER_UPT`
- 前置校验/状态相关 CALL_ID：`FIND_TAB_WORK_ORDER_DETAILS`、`FIND_TAB_WORK_ORDER_DETAILS_PIC`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=PS5LIXZ1oRoqVpNDhvB46MvyeCNDy9dYsVeEae` + 页面标题联合识别。

### 工单接收方查询

**Page Identity**
- 菜单路径：`客服管理 / 工单接收方查询`
- 页面类型：叶子页面，上级分组是 `工单管理`
- `pageId`：`BnK7vim9BFA9FNrCK1TKodn66rCTo4Tzkb2OGR`
- 页面标题：`工单管理-责任方查询`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 页签：`查询结果`、`跟进详情`
- 查询区/标签：`查询条件`、`按运单号查询`、`按工单号查询`、`查询时间范围`、`登记时间`、`工单类型`、`登记网点`、`工单状态`、`响应超时`、`工单类别`、`是否解决`、`责任网点`
- 首屏字段：`searchOrderInput`、`REGISTER_DATE$text`、`WORK_TYPE$text`、`REGISTER_SITE`、`WORK_STATE$text`、`BL_RESPONSE_OVERTIME$text`、`WORK_NAME$text`、`IS_SOLVE$text`、`RESPONSIBLE_PARTY`、`COST_TYPE$text`、`BL_OVER_OVERTIME$text`、`URGE_COUNT$text`
- MiniUI text/value 对：`CODE_TYPE` -> value=`CODE_TYPE$value` / submit=`CODE_TYPE`；`searchOrderInput` -> text=`searchOrderInput$text`；`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`REGISTER_DATE` -> text=`REGISTER_DATE$text` / value=`REGISTER_DATE$value` / submit=`REGISTER_DATE`；`WORK_TYPE` -> text=`WORK_TYPE$text` / value=`WORK_TYPE$value` / submit=`WORK_TYPE`；`REGISTER_SITE` -> text=`REGISTER_SITE$text` / value=`REGISTER_SITE$value` / submit=`REGISTER_SITE`；`WORK_STATE` -> text=`WORK_STATE$text` / value=`WORK_STATE$value` / submit=`WORK_STATE`；`BL_RESPONSE_OVERTIME` -> text=`BL_RESPONSE_OVERTIME$text` / value=`BL_RESPONSE_OVERTIME$value` / submit=`BL_RESPONSE_OVERTIME`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`、`查询结果`
- 保存类按钮：`登记网点`、`响应超时`、`完结超时`、`响应`、`响应`、`响应`、`完结`、`完结`
- 导出类按钮：`导出`、`导出`、`导出`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `登记网点`: `xpath=//*[normalize-space(.)="登记网点"]`
- `响应超时`: `xpath=//*[normalize-space(.)="响应超时"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `REGISTER_DATE$text`: `#REGISTER_DATE$text` / `xpath=//*[@id="REGISTER_DATE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `WORK_TYPE$text`: `#WORK_TYPE$text` / `xpath=//*[@id="WORK_TYPE$text"]` / `xpath=//*[@placeholder="请选择"]`
- `CODE_TYPE`: value=`CODE_TYPE$value` / submit=`CODE_TYPE`
- `searchOrderInput`: text=`searchOrderInput$text`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `FIND_TAB_WORK_ORDER` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_TAB_WORK_ORDER` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_WORK_ORDER`
  - `FIND_TAB_SITE_COMBOBOX_ALL` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_TAB_WORK_ORDER_TYPE_TYPE` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_TAB_SITE_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_SITE_COMBOBOX&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_TAB_WORK_ORDER`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_WORK_ORDER`
- 请求方法：`POST`
- 主查询参数键：`CODE_TYPE`、`searchOrderInput`、`searchDateType`、`REGISTER_DATE`、`WORK_TYPE`、`REGISTER_SITE`、`WORK_STATE`、`BL_RESPONSE_OVERTIME`、`WORK_NAME`、`IS_SOLVE`、`RESPONSIBLE_PARTY`、`COST_TYPE`、`BL_OVER_OVERTIME`、`URGE_COUNT`、`APPEAL_STATUS`、`BL_VIP`、`ORDER_BY_CREATE_DATE`、`ZRF`、`pageIndex`、`pageSize`
- 点击查询后额外请求：
  - `FIND_TAB_WORK_ORDER` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_WORK_ORDER`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_TAB_WORK_ORDER`
- 页面脚本提示接口：
  - `/dataQuery/findAllByCallId?id=FIND_TAB_WORK_ORDER_DETAILS`
  - `/dataQuery/findAllByCallId?id=FIND_TAB_WORK_ORDER`
  - `/dataQuery/findPageByCallId?id=FIND_TAB_WORK_TYPE&DETAILED_NAME1=`
  - `//$Z.page.datagridExportExcel(`
  - `/dataQuery/findPageByCallId?id=FIND_TAB_WORK_ORDER_DETAILS&WORK_ORDER_CODE=`
  - `/dataQuery/findAllByCallId?id=FIND_TAB_WORK_ORDER_DETAILS_PIC&ORDER_GUID=`

**Field Mapping**
- 用户输入参数：`CODE_TYPE`、`searchOrderInput`、`searchDateType`、`REGISTER_DATE`、`WORK_TYPE`、`REGISTER_SITE`、`WORK_STATE`、`BL_RESPONSE_OVERTIME`、`WORK_NAME`、`IS_SOLVE`、`RESPONSIBLE_PARTY`、`COST_TYPE`、`BL_OVER_OVERTIME`、`URGE_COUNT`、`APPEAL_STATUS`、`BL_VIP`
- 页面自动补齐参数：`ORDER_BY_CREATE_DATE`、`ZRF`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`CODE_TYPE` -> `CODE_TYPE`；`searchDateType` -> `searchDateType`；`REGISTER_DATE` -> `REGISTER_DATE`；`WORK_TYPE` -> `WORK_TYPE`；`REGISTER_SITE` -> `REGISTER_SITE`；`WORK_STATE` -> `WORK_STATE`；`BL_RESPONSE_OVERTIME` -> `BL_RESPONSE_OVERTIME`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `searchBtn -> searchMethod();`
  - `genjinBut -> answer();`
  - `completeBtn -> clickend();`
  - `searchForm -> exportMethod();`
  - `urgeBtn -> urgeWork();`
  - `clearBillCode -> //清空查询单号 mini.get("searchOrderInput").setValue("");`
- 前置校验/状态相关 CALL_ID：`FIND_TAB_WORK_ORDER_DETAILS`、`FIND_TAB_WORK_ORDER_DETAILS_PIC`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 页面识别建议：优先用 `pageId=BnK7vim9BFA9FNrCK1TKodn66rCTo4Tzkb2OGR` + 页面标题联合识别。

### 快件跟踪

**Page Identity**
- 菜单路径：`客服管理 / 快件跟踪`
- 页面类型：叶子页面
- `pageId`：`PykASzJQaYswjo70ivG09b48hYj79D58dB7BM3`
- 页面标题：`快件跟踪-重构`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`基础信息`、`查询条件`、`按单号查询`、`只查主单`、`录入查询情况`、`来电方受理人`、`来电类型`、`来电网点`、`来电客户`、`来电电话号码`、`受理类别`、`受理类型`
- 首屏字段：`BILL_CODE`、`DISPOSAL_CALLER`、`ACCEPTANCE_SOURCE$text`、`SEND_SITE`、`PHONE_CODE`、`ACCEPTANCE_NAME$text`、`ACCEPTANCE_TYPE$text`、`ACCEPTANCE_CANAL$text`、`QUERY_RESULT`
- MiniUI text/value 对：`BILL_CODE` -> text=`BILL_CODE$text`；`BL_MAIN` -> value=`BL_MAIN$value`；`DISPOSAL_CALLER` -> text=`DISPOSAL_CALLER$text`；`ACCEPTANCE_SOURCE` -> text=`ACCEPTANCE_SOURCE$text` / value=`ACCEPTANCE_SOURCE$value` / submit=`ACCEPTANCE_SOURCE`；`SITE_SEND_SITE_CODE` -> text=`SITE_SEND_SITE_CODE$text` / value=`SITE_SEND_SITE_CODE$value` / submit=`SEND_SITE_CODE`；`CUST_SEND_SITE` -> text=`CUST_SEND_SITE$text` / value=`CUST_SEND_SITE$value` / submit=`SEND_SITE`；`PHONE_CODE` -> text=`PHONE_CODE$text`；`ACCEPTANCE_NAME` -> text=`ACCEPTANCE_NAME$text` / value=`ACCEPTANCE_NAME$value` / submit=`ACCEPTANCE_NAME`
- 查询按钮：`按单号查询`、`查询`、`查询`、`查询`
- 保存类按钮：`保存`、`保存`、`保存`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `按单号查询`: `xpath=//*[normalize-space(.)="按单号查询"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `保存`: `#saveBtn` / `xpath=//*[@id="saveBtn"]` / `xpath=//a[normalize-space(.)="保存"]`
- `保存`: `xpath=//*[normalize-space(.)="保存"]`
- `BILL_CODE`: `#BILL_CODE$text` / `xpath=//*[@id="BILL_CODE$text"]` / `xpath=//*[@name="BILL_CODE"]`
- `DISPOSAL_CALLER`: `#DISPOSAL_CALLER$text` / `xpath=//*[@id="DISPOSAL_CALLER$text"]` / `xpath=//*[@name="DISPOSAL_CALLER"]`
- `ACCEPTANCE_SOURCE$text`: `#ACCEPTANCE_SOURCE$text` / `xpath=//*[@id="ACCEPTANCE_SOURCE$text"]` / `xpath=//*[@placeholder="请选择"]`
- `BILL_CODE`: text=`BILL_CODE$text`
- `BL_MAIN`: value=`BL_MAIN$value`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=ACCEPTANCE_NAME&_=%3CTS%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=ACCEPTANCE_TYPE&_=%3CTS%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=ACCEPTANCE_CANAL&_=%3CTS%3E`
  - `FIND_SITE_ONE_TWO_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 本次 live 点击未拿到稳定主查询请求，通常是因为页面需要先录入单号、先选择页签，或查询逻辑在脚本内先做前置校验。
- 页面脚本提示接口：
  - `/dataOperation/saveTables`
  - `/dataQuery/findAllByCallId?id=FIND_SACN_TRACK_BY_CODE`
  - `/dataQuery/findAllByCallId?id=FIND_SACN_TRACK_BY_CODE_MAIN`
  - `/dataQuery/findAllByCallId?id=FIND_BILL_BY_CODE`
  - `/billEntity/getBillByCode`
  - `/dataQuery/findAllByCallId?id=FIND_PROBLEM_BY_CODE`
  - `/dataQuery/findAllByCallId?id=FIND_STOCK_GOODS_DETAIL`

**Field Mapping**
- 用户输入参数：本次 live 查询没有稳定提取到，通常是页面先走前置校验、需要先录入单号，或查询不是默认页签。
- text/value 到提交字段：`ACCEPTANCE_SOURCE` -> `ACCEPTANCE_SOURCE`；`SITE_SEND_SITE_CODE` -> `SEND_SITE_CODE`；`CUST_SEND_SITE` -> `SEND_SITE`；`ACCEPTANCE_NAME` -> `ACCEPTANCE_NAME`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `saveBtn -> saveLog();`
  - `searchBtn -> search();`
  - `clearBtn -> mini.get("BILL_CODE").setValue("")`
- 保存/提交操作键：`TAB_SERVICE_LOG_ADD`
- 前置校验/状态相关 CALL_ID：`FIND_SACN_TRACK_BY_CODE`、`FIND_SACN_TRACK_BY_CODE_MAIN`、`FIND_BILL_BY_CODE`、`FIND_PROBLEM_BY_CODE`、`FIND_STOCK_GOODS_DETAIL`

**Automation Notes**
- 更适合先走 DOM：当前没有稳定主查询请求，优先把页面交互顺序跑通。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=PykASzJQaYswjo70ivG09b48hYj79D58dB7BM3` + 页面标题联合识别。

### 快件跟踪新

**Page Identity**
- 菜单路径：`客服管理 / 快件跟踪新`
- 页面类型：叶子页面
- `pageId`：`9hy36DRPSGHS0ogX3aci7mxmXqtanu913Lb2uz`
- 页面标题：`快件跟踪-修改`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`基础信息`、`查询条件`、`按单号查询`、`只查主单`、`录入查询情况`、`来电方受理人`、`来电类型`、`来电网点`、`来电客户`、`来电电话号码`、`受理类别`、`受理类型`
- 首屏字段：`BILL_CODE`、`DISPOSAL_CALLER`、`ACCEPTANCE_SOURCE$text`、`SEND_SITE`、`PHONE_CODE`、`ACCEPTANCE_NAME$text`、`ACCEPTANCE_TYPE$text`、`RESP_SITE`、`ACCEPTANCE_CANAL$text`、`QUERY_RESULT`
- MiniUI text/value 对：`BILL_CODE` -> text=`BILL_CODE$text`；`BL_MAIN` -> value=`BL_MAIN$value`；`DISPOSAL_CALLER` -> text=`DISPOSAL_CALLER$text`；`ACCEPTANCE_SOURCE` -> text=`ACCEPTANCE_SOURCE$text` / value=`ACCEPTANCE_SOURCE$value` / submit=`ACCEPTANCE_SOURCE`；`SITE_SEND_SITE_CODE` -> text=`SITE_SEND_SITE_CODE$text` / value=`SITE_SEND_SITE_CODE$value` / submit=`SEND_SITE_CODE`；`CUST_SEND_SITE` -> text=`CUST_SEND_SITE$text` / value=`CUST_SEND_SITE$value` / submit=`SEND_SITE`；`PHONE_CODE` -> text=`PHONE_CODE$text`；`ACCEPTANCE_NAME` -> text=`ACCEPTANCE_NAME$text` / value=`ACCEPTANCE_NAME$value` / submit=`ACCEPTANCE_NAME`
- 查询按钮：`按单号查询`、`查询`、`查询`、`查询`
- 保存类按钮：`保存`、`保存`、`保存`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `按单号查询`: `xpath=//*[normalize-space(.)="按单号查询"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `保存`: `#saveBtn` / `xpath=//*[@id="saveBtn"]` / `xpath=//a[normalize-space(.)="保存"]`
- `保存`: `xpath=//*[normalize-space(.)="保存"]`
- `BILL_CODE`: `#BILL_CODE$text` / `xpath=//*[@id="BILL_CODE$text"]` / `xpath=//*[@name="BILL_CODE"]`
- `DISPOSAL_CALLER`: `#DISPOSAL_CALLER$text` / `xpath=//*[@id="DISPOSAL_CALLER$text"]` / `xpath=//*[@name="DISPOSAL_CALLER"]`
- `ACCEPTANCE_SOURCE$text`: `#ACCEPTANCE_SOURCE$text` / `xpath=//*[@id="ACCEPTANCE_SOURCE$text"]` / `xpath=//*[@placeholder="请选择"]`
- `BILL_CODE`: text=`BILL_CODE$text`
- `BL_MAIN`: value=`BL_MAIN$value`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=ACCEPTANCE_NAME&_=%3CTS%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=ACCEPTANCE_TYPE&_=%3CTS%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=ACCEPTANCE_CANAL&_=%3CTS%3E`
  - `FIND_SITE_ONE_TWO_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_ALL_SITE_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_ALL_SITE_COMBOBOX&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 本次 live 点击未拿到稳定主查询请求，通常是因为页面需要先录入单号、先选择页签，或查询逻辑在脚本内先做前置校验。
- 页面脚本提示接口：
  - `/dataOperation/saveTables`
  - `/dataQuery/findAllByCallId?id=FIND_BY_ERROR_SITE_CODES`
  - `/dataQuery/findAllByCallId?id=FIND_BY_SEND_SITE_RECORD`

**Field Mapping**
- 用户输入参数：本次 live 查询没有稳定提取到，通常是页面先走前置校验、需要先录入单号，或查询不是默认页签。
- text/value 到提交字段：`ACCEPTANCE_SOURCE` -> `ACCEPTANCE_SOURCE`；`SITE_SEND_SITE_CODE` -> `SEND_SITE_CODE`；`CUST_SEND_SITE` -> `SEND_SITE`；`ACCEPTANCE_NAME` -> `ACCEPTANCE_NAME`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `saveBtn -> saveLog();`
  - `searchBtn -> searchMethod();`
  - `clearBtn -> mini.get("BILL_CODE").setValue("")`
- 保存/提交操作键：`TAB_SERVICE_LOG_ADD`

**Automation Notes**
- 更适合先走 DOM：当前没有稳定主查询请求，优先把页面交互顺序跑通。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=9hy36DRPSGHS0ogX3aci7mxmXqtanu913Lb2uz` + 页面标题联合识别。

### 费用调整

**Page Identity**
- 菜单路径：`客服管理 / 费用调整`
- 页面类型：分组节点
- 分组子项：`费用调整登记`、`费用调整接收方查询`
- 说明：该菜单项只负责展开子菜单，live 点击时可能仍停留在上一业务页，不应把当前 iframe 的 `pageId/标题` 误判为该分组节点身份。

**Automation Notes**
- 该项不应直接当成 iframe 页面处理，应该继续点击它的子菜单。

### 费用调整登记

**Page Identity**
- 菜单路径：`客服管理 / 费用调整登记`
- 页面类型：叶子页面，上级分组是 `费用调整`
- `pageId`：`nImusLyN8oieRCXATl3SegnxX99ALcqdBgRKmr`
- 页面标题：`费用调整管理`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`查询条件`、`按运单查询`、`查询时间范围`、`创建日期`、`已申请已确认超时确认有问题已审核已拒绝已扣款已取消`、`调整类型`、`申请网点`、`审核状态`、`接收网点`
- 首屏字段：`searchOrderInput`、`SEARCH_DATE_RANGE$text`、`TRIM_TYPE$text`、`APPLY_SITE_CODE$text`、`BL_DEST_AUDIT$text`、`DEST_SITE_CODE$text`
- MiniUI text/value 对：`CODE_TYPE` -> value=`CODE_TYPE$value` / submit=`CODE_TYPE`；`searchOrderInput` -> text=`searchOrderInput$text`；`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`SEARCH_DATE_RANGE` -> text=`SEARCH_DATE_RANGE$text` / value=`SEARCH_DATE_RANGE$value` / submit=`SEARCH_DATE_RANGE`；`TRIM_TYPE` -> text=`TRIM_TYPE$text` / value=`TRIM_TYPE$value` / submit=`TRIM_TYPE`；`APPLY_SITE_CODE` -> text=`APPLY_SITE_CODE$text` / value=`APPLY_SITE_CODE$value` / submit=`APPLY_SITE_CODE`；`BL_DEST_AUDIT` -> text=`BL_DEST_AUDIT$text` / value=`BL_DEST_AUDIT$value` / submit=`BL_DEST_AUDIT`；`DEST_SITE_CODE` -> text=`DEST_SITE_CODE$text` / value=`DEST_SITE_CODE$value` / submit=`DEST_SITE_CODE`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`
- 保存类按钮：`已申请已确认超时确认有问题已审核已拒绝已扣款已取消`、`已确认`、`超时确认`、`已审核`、`审核状态`、`增值调整登记`、`增值调整登记`、`增值调整登记`
- 导出类按钮：`导出`、`导出`、`导出`、`下载导入模板`、`下载导入模板`、`下载导入模板`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `已申请已确认超时确认有问题已审核已拒绝已扣款已取消`: `#desc` / `xpath=//*[@id="desc"]` / `xpath=//*[normalize-space(.)="已申请已确认超时确认有问题已审核已拒绝已扣款已取消"]`
- `已确认`: `xpath=//*[normalize-space(.)="已确认"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `SEARCH_DATE_RANGE$text`: `#SEARCH_DATE_RANGE$text` / `xpath=//*[@id="SEARCH_DATE_RANGE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `TRIM_TYPE$text`: `#TRIM_TYPE$text` / `xpath=//*[@id="TRIM_TYPE$text"]` / `xpath=//*[@placeholder="全部"]`
- `CODE_TYPE`: value=`CODE_TYPE$value` / submit=`CODE_TYPE`
- `searchOrderInput`: text=`searchOrderInput$text`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `FIND_TAB_FEE_APPLY` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_TAB_FEE_APPLY` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_FEE_APPLY`
  - `FIND_SITE_ONE_TWO_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_SITE_ONE_TWO_COMBOBOX5` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_TAB_FEE_APPLY`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_FEE_APPLY`
- 请求方法：`POST`
- 主查询参数键：`CODE_TYPE`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`TRIM_TYPE`、`APPLY_SITE_CODE`、`BL_DEST_AUDIT`、`DEST_SITE_CODE`、`APPLY_DATE`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIND_TAB_FEE_APPLY` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_FEE_APPLY`
  - `https://tms.ronghuiwl.com/dataOperation/execProcedure`
  - `FIND_TAB_WORK_ORDER_TYPE_REMIND` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=%3CTOKEN%3E`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_TAB_FEE_APPLY`
- 页面脚本提示接口：
  - `/dataOperation/saveTables`

**Field Mapping**
- 用户输入参数：`CODE_TYPE`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`TRIM_TYPE`、`APPLY_SITE_CODE`、`BL_DEST_AUDIT`、`DEST_SITE_CODE`
- 页面自动补齐参数：`APPLY_DATE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`CODE_TYPE` -> `CODE_TYPE`；`searchDateType` -> `searchDateType`；`SEARCH_DATE_RANGE` -> `SEARCH_DATE_RANGE`；`TRIM_TYPE` -> `TRIM_TYPE`；`APPLY_SITE_CODE` -> `APPLY_SITE_CODE`；`BL_DEST_AUDIT` -> `BL_DEST_AUDIT`；`DEST_SITE_CODE` -> `DEST_SITE_CODE`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `editBtn1 -> clickCancel();`
  - `searchBtn -> searchMethod();`
  - `delBtn -> clickCancel();`
  - `exportBtn -> var data=getQueryParam(); $Z.page.datagridExportExcel("datagrid","FIND_TAB_FEE_APPLY_EXCEL","费用调整数据导出",data, "async");`
  - `downImportTemplateBtn -> $Z.page.downImportTempldate("TAB_FEE_APPLY");`
  - `importBtn -> var userInfo = $Z.user.getUserInfo(); $Z.Page.prototype.openImportWindow("TAB_FEE_APPLY", {`
- 保存/提交操作键：`TAB_FEE_APPLY_UPT`、`TAB_FEE_APPLY`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=nImusLyN8oieRCXATl3SegnxX99ALcqdBgRKmr` + 页面标题联合识别。

### 费用调整接收方查询

**Page Identity**
- 菜单路径：`客服管理 / 费用调整接收方查询`
- 页面类型：叶子页面，上级分组是 `费用调整`
- `pageId`：`bzppZFQdCANqUOjJHIT8hViM8VEc516ZPVrldm`
- 页面标题：`费用调整接收方查询`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`查询条件`、`按运单查询`、`查询时间范围`、`创建日期`、`确认日期`、`调整类型`、`申请网点`、`调整明细类型`、`审核状态`、`接收网点`、`已申请已确认超时确认有问题已审核已拒绝已扣款已取消`
- 首屏字段：`searchOrderInput`、`SEARCH_DATE_RANGE$text`、`TRIM_TYPE$text`、`APPLY_SITE_CODE$text`、`APPLY_TYPE$text`、`BL_DEST_AUDIT$text`、`DEST_SITE_CODE$text`
- MiniUI text/value 对：`CODE_TYPE` -> value=`CODE_TYPE$value` / submit=`CODE_TYPE`；`searchOrderInput` -> text=`searchOrderInput$text`；`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`SEARCH_DATE_RANGE` -> text=`SEARCH_DATE_RANGE$text` / value=`SEARCH_DATE_RANGE$value` / submit=`SEARCH_DATE_RANGE`；`TRIM_TYPE` -> text=`TRIM_TYPE$text` / value=`TRIM_TYPE$value` / submit=`TRIM_TYPE`；`APPLY_SITE_CODE` -> text=`APPLY_SITE_CODE$text` / value=`APPLY_SITE_CODE$value` / submit=`APPLY_SITE_CODE`；`APPLY_TYPE` -> text=`APPLY_TYPE$text` / value=`APPLY_TYPE$value` / submit=`APPLY_TYPE`；`BL_DEST_AUDIT` -> text=`BL_DEST_AUDIT$text` / value=`BL_DEST_AUDIT$value` / submit=`BL_DEST_AUDIT`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`
- 保存类按钮：`审核状态`、`确认`、`确认`、`确认`、`批量确认`、`批量确认`、`批量确认`、`已申请已确认超时确认有问题已审核已拒绝已扣款已取消`
- 导出类按钮：`导出`、`导出`、`导出`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `审核状态`: `xpath=//*[normalize-space(.)="审核状态"]`
- `确认`: `#editBtn` / `xpath=//*[@id="editBtn"]` / `xpath=//a[normalize-space(.)="确认"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `SEARCH_DATE_RANGE$text`: `#SEARCH_DATE_RANGE$text` / `xpath=//*[@id="SEARCH_DATE_RANGE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `TRIM_TYPE$text`: `#TRIM_TYPE$text` / `xpath=//*[@id="TRIM_TYPE$text"]` / `xpath=//*[@placeholder="全部"]`
- `CODE_TYPE`: value=`CODE_TYPE$value` / submit=`CODE_TYPE`
- `searchOrderInput`: text=`searchOrderInput$text`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `FIND_TAB_FEE_TYPE_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=%3CTOKEN%3E&_=%3CTS%3E`
  - `FIND_SITE_ALL` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=FIND_SITE_ALL`
  - `FIND_TAB_FEE_APPLY_DEST` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_TAB_FEE_APPLY_DEST` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_FEE_APPLY_DEST`
  - `FIND_SITE_ONE_TWO_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_SITE_ONE_TWO_COMBOBOX5` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_TAB_FEE_APPLY_DEST`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_FEE_APPLY_DEST`
- 请求方法：`POST`
- 主查询参数键：`CODE_TYPE`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`TRIM_TYPE`、`APPLY_SITE_CODE`、`APPLY_TYPE`、`BL_DEST_AUDIT`、`DEST_SITE_CODE`、`APPLY_DATE`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIND_TAB_FEE_APPLY_DEST` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_FEE_APPLY_DEST`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_TAB_FEE_APPLY_DEST`
- 页面脚本提示接口：
  - `/dataQuery/findAllByCallId?id=FIND_SITE_ALL`
  - `/dataQuery/findAllByCallId?id=FIND_TAB_BALANCE_ACCOUNT_SITE`
  - `/dataQuery/findAllByCallId?id=FIND_TAB_FEE_APPLY_SUM`

**Field Mapping**
- 用户输入参数：`CODE_TYPE`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`TRIM_TYPE`、`APPLY_SITE_CODE`、`APPLY_TYPE`、`BL_DEST_AUDIT`、`DEST_SITE_CODE`
- 页面自动补齐参数：`APPLY_DATE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`CODE_TYPE` -> `CODE_TYPE`；`searchDateType` -> `searchDateType`；`SEARCH_DATE_RANGE` -> `SEARCH_DATE_RANGE`；`TRIM_TYPE` -> `TRIM_TYPE`；`APPLY_SITE_CODE` -> `APPLY_SITE_CODE`；`APPLY_TYPE` -> `APPLY_TYPE`；`BL_DEST_AUDIT` -> `BL_DEST_AUDIT`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `searchBtn -> searchMethod();`
  - `editBtn -> confirmFun();`
  - `editBtnAll -> confirmBatchFun();`
  - `exportBtn -> exportMethod();`
  - `clearBillCode -> //清空查询单号 mini.get("searchOrderInput").setValue("");`
- 前置校验/状态相关 CALL_ID：`FIND_TAB_BALANCE_ACCOUNT_SITE`、`FIND_TAB_FEE_APPLY_SUM`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 页面识别建议：优先用 `pageId=bzppZFQdCANqUOjJHIT8hViM8VEc516ZPVrldm` + 页面标题联合识别。

### 公告查询

**Page Identity**
- 菜单路径：`客服管理 / 公告查询`
- 页面类型：叶子页面
- `pageId`：`ydEdzXOcf7JSERBjWPIRrG5mOUpqxTHmWzolZe`
- 页面标题：`公告查询`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 页签：`未查看的消息`、`接收消息查询`、`已发送消息查询`、`已发送消息统计`、`公司文件`
- 查询区/标签：`基础信息`、`消息类型`、`以下显示的是您从未查看过的消息，双击数据可弹窗查看消息内容`、`回复`、`发布时间范围（7天以内）`、`发布网点`、`标题`、`必看`、`双击数据可弹窗查看消息内容`、`接收网点`、`接收类型`、`双击数据可查看统计明细`
- 首屏字段：`NOTICE_NAME_ONE$text`
- MiniUI text/value 对：`NOTICE_NAME_ONE` -> text=`NOTICE_NAME_ONE$text` / value=`NOTICE_NAME_ONE$value` / submit=`NOTICE_NAME`；`SEARCH_NOTICE_DATETIME` -> text=`SEARCH_NOTICE_DATETIME$text` / value=`SEARCH_NOTICE_DATETIME$value` / submit=`NOTICE_DATETIME`；`SEARCH_SEND_SITE_CODE` -> text=`SEARCH_SEND_SITE_CODE$text` / value=`SEARCH_SEND_SITE_CODE$value` / submit=`SEND_SITE_CODE`；`searchTitle` -> text=`searchTitle$text`；`searchShowReviewOrMust` -> value=`searchShowReviewOrMust$value` / submit=`searchShowReviewOrMust`；`NOTICE_NAME_TWO` -> text=`NOTICE_NAME_TWO$text` / value=`NOTICE_NAME_TWO$value` / submit=`NOTICE_NAME`；`SEARCH_NOTICE_DATETIME_THREE` -> text=`SEARCH_NOTICE_DATETIME_THREE$text` / value=`SEARCH_NOTICE_DATETIME_THREE$value` / submit=`NOTICE_DATETIME`；`REC_SITE_CODE_THREE` -> text=`REC_SITE_CODE_THREE$text` / value=`REC_SITE_CODE_THREE$value` / submit=`REC_SITE_CODE`
- 查询按钮：`接收消息查询`、`已发送消息查询`、`查询`、`查询`、`查询`
- 导出类按钮：`附件下载`、`附件下载`、`附件下载`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 子 iframe：存在内嵌 iframe，开发时要先确认是否需要切换子 frame。
- 稳定选择器候选：
- `接收消息查询`: `xpath=//*[normalize-space(.)="接收消息查询"]`
- `已发送消息查询`: `xpath=//*[normalize-space(.)="已发送消息查询"]`
- `NOTICE_NAME_ONE$text`: `#NOTICE_NAME_ONE$text` / `xpath=//*[@id="NOTICE_NAME_ONE$text"]` / `xpath=//*[@placeholder="全部"]`
- `NOTICE_NAME_ONE`: text=`NOTICE_NAME_ONE$text` / value=`NOTICE_NAME_ONE$value` / submit=`NOTICE_NAME`
- `SEARCH_NOTICE_DATETIME`: text=`SEARCH_NOTICE_DATETIME$text` / value=`SEARCH_NOTICE_DATETIME$value` / submit=`NOTICE_DATETIME`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `FIND_CONSTANT_DATA` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=FIND_CONSTANT_DATA`
  - `FIND_TAB_NOTICE_NOT_SEE` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_TAB_NOTICE_REC` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_TAB_NOTICE_SEND` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_NOTICE_STATISTICS` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_TAB_NOTICE_DOC` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_ALL_SITE_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_ALL_SITE_COMBOBOX&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `https://tms.ronghuiwl.com/ueditor/config?action=config&noCache=%3CNUM%3E`
- 主查询接口：`FIND_TAB_NOTICE_NOT_SEE`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_NOTICE_NOT_SEE&LOGIN_EMP_CODE=%3CNUM%3E&LOGIN_EMP_SITE_CODE=%3CNUM%3E`
- 请求方法：`POST`
- 主查询参数键：`NOTICE_NAME`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIND_TAB_NOTICE_NOT_SEE` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_NOTICE_NOT_SEE&LOGIN_EMP_CODE=%3CNUM%3E&LOGIN_EMP_SITE_CODE=%3CNUM%3E`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_TAB_NOTICE_NOT_SEE&LOGIN_EMP_CODE=73900040001&LOGIN_EMP_SITE_CODE=7390004`
  - `/dataQuery/findPageByCallId?id=FIND_TAB_NOTICE_REC&LOGIN_EMP_CODE=73900040001&LOGIN_EMP_SITE_CODE=7390004`
  - `/dataQuery/findPageByCallId?id=FIND_TAB_NOTICE_SEND&LOGIN_EMP_CODE=73900040001`
  - `/dataQuery/findPageByCallId?id=FIND_NOTICE_STATISTICS&LOGIN_EMP_CODE=73900040001`
  - `/dataQuery/findPageByCallId?id=FIND_TAB_NOTICE_DOC&LOGIN_EMP_CODE=73900040001&LOGIN_EMP_SITE_CODE=7390004`
- 页面脚本提示接口：
  - `/dataQuery/findPageByCallId?id=FIND_TAB_NOTICE_NOT_SEE&LOGIN_EMP_CODE=`
  - `/dataQuery/findPageByCallId?id=FIND_TAB_NOTICE_REC&LOGIN_EMP_CODE=`
  - `/dataQuery/findPageByCallId?id=FIND_TAB_NOTICE_SEND&LOGIN_EMP_CODE=`
  - `/dataQuery/findPageByCallId?id=FIND_NOTICE_STATISTICS&LOGIN_EMP_CODE=`
  - `/dataQuery/findPageByCallId?id=FIND_TAB_NOTICE_DOC&LOGIN_EMP_CODE=`
  - `/dataQuery/findAllByCallId?id=FIND_CONSTANT_DATA`
  - `/dataOperation/saveTables`

**Field Mapping**
- 用户输入参数：`NOTICE_NAME`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`NOTICE_NAME_ONE` -> `NOTICE_NAME`；`SEARCH_NOTICE_DATETIME` -> `NOTICE_DATETIME`；`SEARCH_SEND_SITE_CODE` -> `SEND_SITE_CODE`；`searchShowReviewOrMust` -> `searchShowReviewOrMust`；`NOTICE_NAME_TWO` -> `NOTICE_NAME`；`SEARCH_NOTICE_DATETIME_THREE` -> `NOTICE_DATETIME`；`REC_SITE_CODE_THREE` -> `REC_SITE_CODE`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `searchBtn -> searchNoSeeMethod();`
  - `downloadBtn -> documentDownload("notSeeDatagrid");`
  - `sendBtn -> replyMethod();`
- 保存/提交操作键：`TAB_NOTICE_DEL`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=ydEdzXOcf7JSERBjWPIRrG5mOUpqxTHmWzolZe` + 页面标题联合识别。

### 无头件登记

**Page Identity**
- 菜单路径：`客服管理 / 无头件登记`
- 页面类型：叶子页面
- `pageId`：未稳定捕获到。
- 页面标题：`未显式设置标题`
- iframe URL：`/module/index?mv=customerservice%2Fno_bill&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`申报人CODE`、`申报网点CODE`、`申报时间`、`图片路径`、`无头件登记`、`无头件编号`、`上一站`、`申报人`、`申报网点`、`下一站`、`上传图片1`、`上传图片2`
- 首屏字段：`BILL_NUMBER`、`showDate`、`DIRECTION$text`、`DECLARER_MAN`、`DECLARE_SITE`、`ANASTIGMATIC$text`、`selectFile1$text`、`selectFile2$text`、`selectFile3$text`、`SHOW_CONTENT`
- MiniUI text/value 对：`BILL_NUMBER` -> text=`BILL_NUMBER$text`；`showDate` -> text=`showDate$text`；`DIRECTION` -> text=`DIRECTION$text` / value=`DIRECTION$value` / submit=`DIRECTION`；`DECLARER_MAN` -> text=`DECLARER_MAN$text`；`DECLARE_SITE` -> text=`DECLARE_SITE$text`；`ANASTIGMATIC` -> text=`ANASTIGMATIC$text` / value=`ANASTIGMATIC$value` / submit=`ANASTIGMATIC`；`selectFile1` -> text=`selectFile1$text`；`selectFile2` -> text=`selectFile2$text`
- 保存类按钮：`上传图片1`、`上传图片2`、`上传图片3`、`保存`、`保存`、`保存`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `上传图片1`: `xpath=//*[normalize-space(.)="上传图片1"]`
- `上传图片2`: `xpath=//*[normalize-space(.)="上传图片2"]`
- `BILL_NUMBER`: `#BILL_NUMBER$text` / `xpath=//*[@id="BILL_NUMBER$text"]` / `xpath=//*[@name="BILL_NUMBER"]`
- `showDate`: `#showDate$text` / `xpath=//*[@id="showDate$text"]` / `xpath=//*[@name="showDate"]`
- `DIRECTION$text`: `#DIRECTION$text` / `xpath=//*[@id="DIRECTION$text"]`
- `BILL_NUMBER`: text=`BILL_NUMBER$text`
- `showDate`: text=`showDate$text`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/module/index?mv=customerservice%2Fno_bill&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `FIND_SITE_COMBOBOX_OTHER` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 本次 live 点击未拿到稳定主查询请求，通常是因为页面需要先录入单号、先选择页签，或查询逻辑在脚本内先做前置校验。

**Field Mapping**
- 用户输入参数：本次 live 查询没有稳定提取到，通常是页面先走前置校验、需要先录入单号，或查询不是默认页签。
- text/value 到提交字段：`DIRECTION` -> `DIRECTION`；`ANASTIGMATIC` -> `ANASTIGMATIC`

**State / Validation Logic**
- 查询入口：没有稳定点击到默认查询按钮，说明此页可能依赖录入单号、页签切换或前置校验。
- 特殊页：没有稳定标题/pageId，更适合按 DOM 控件和保存按钮去驱动。

**Automation Notes**
- 更适合先走 DOM：当前没有稳定主查询请求，优先把页面交互顺序跑通。
- 页面识别建议：优先用菜单路径 + 关键按钮/字段来识别。

### 无头件认领

**Page Identity**
- 菜单路径：`客服管理 / 无头件认领`
- 页面类型：叶子页面
- `pageId`：`JRk5QCAPn46OccCo8JGmj11W4VgBXFZRAi7Bm4`
- 页面标题：`无头件查询`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`查询条件`、`申报时间范围(31天)`、`申报网点`、`认领状态`、`无头件编号`、`未认领`、`已认领`
- 首屏字段：`DECLARE_DATE$text`、`DECLARE_SITE_CODE$text`、`BL_CLAIM$text`、`BILL_NUMBER`
- MiniUI text/value 对：`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`DECLARE_DATE` -> text=`DECLARE_DATE$text` / value=`DECLARE_DATE$value` / submit=`DECLARE_DATE`；`DECLARE_SITE_CODE` -> text=`DECLARE_SITE_CODE$text` / value=`DECLARE_SITE_CODE$value` / submit=`DECLARE_SITE_CODE`；`BL_CLAIM` -> text=`BL_CLAIM$text` / value=`BL_CLAIM$value` / submit=`BL_CLAIM`；`BILL_NUMBER` -> text=`BILL_NUMBER$text`
- 查询按钮：`查询`、`查询`、`查询`
- 保存类按钮：`认领状态`、`认领`、`认领`、`认领`、`未认领`、`已认领`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `查询`: `xpath=//*[normalize-space(.)="查询"]`
- `认领状态`: `xpath=//*[normalize-space(.)="认领状态"]`
- `认领`: `#confirmBtn` / `xpath=//*[@id="confirmBtn"]` / `xpath=//a[normalize-space(.)="认领"]`
- `DECLARE_DATE$text`: `#DECLARE_DATE$text` / `xpath=//*[@id="DECLARE_DATE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `DECLARE_SITE_CODE$text`: `#DECLARE_SITE_CODE$text` / `xpath=//*[@id="DECLARE_SITE_CODE$text"]`
- `BL_CLAIM$text`: `#BL_CLAIM$text` / `xpath=//*[@id="BL_CLAIM$text"]` / `xpath=//*[@placeholder="全部"]`
- `searchDateType`: value=`searchDateType$value` / submit=`searchDateType`
- `DECLARE_DATE`: text=`DECLARE_DATE$text` / value=`DECLARE_DATE$value` / submit=`DECLARE_DATE`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `FIND_TAB_NOBILL` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_ALL_SITE_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_ALL_SITE_COMBOBOX&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_TAB_NOBILL`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_NOBILL`
- 请求方法：`POST`
- 主查询参数键：`searchDateType`、`DECLARE_DATE`、`DECLARE_SITE_CODE`、`BL_CLAIM`、`BILL_NUMBER`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIND_TAB_NOBILL` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_NOBILL`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_TAB_NOBILL`
- 页面脚本提示接口：
  - `/dataOperation/saveTables`

**Field Mapping**
- 用户输入参数：`searchDateType`、`DECLARE_DATE`、`DECLARE_SITE_CODE`、`BL_CLAIM`、`BILL_NUMBER`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`searchDateType` -> `searchDateType`；`DECLARE_DATE` -> `DECLARE_DATE`；`DECLARE_SITE_CODE` -> `DECLARE_SITE_CODE`；`BL_CLAIM` -> `BL_CLAIM`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `searchBtn -> searchMethod();`
  - `confirmBtn -> confirmMethod();`
- 保存/提交操作键：`TAB_NOBILL_DEL`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=JRk5QCAPn46OccCo8JGmj11W4VgBXFZRAi7Bm4` + 页面标题联合识别。

### 客服记录查询

**Page Identity**
- 菜单路径：`客服管理 / 客服记录查询`
- 页面类型：叶子页面
- `pageId`：`EwGsmpA8CcuqNCNzyloQeJUfgKVWAAC7PWH1VW`
- 页面标题：`客服查件管理`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
- 共页关系：和 `客服管理 / 云呼系统` 共用同一 `pageId`，需要结合菜单行为判断是同页入口还是视觉复用。

**DOM Structure**
- 查询区/标签：`查询条件`、`按运单查询`、`查询时间范围`、`录入日期`、`处理完毕`、`处理结果`、`来电类型`、`来电网点所属分拨中心`、`责任方网点`、`登记网点`、`受理类别`、`来电网点`
- 首屏字段：`searchOrderInput`、`SEARCH_DATE_RANGE$text`、`BL_OVER$text`、`DEAL_RESULT`、`ACCEPTANCE_SOURCE$text`、`SEND_SUPERIOR_SITE_CODE$text`、`RESP_SITE_CODE$text`、`REGISTER_SITE_CODE$text`、`ACCEPTANCE_NAME$text`、`SEND_SITE`、`PHONE_CODE`、`REGISTER_MAN_CODE$text`
- MiniUI text/value 对：`CODE_TYPE` -> value=`CODE_TYPE$value` / submit=`CODE_TYPE`；`searchOrderInput` -> text=`searchOrderInput$text`；`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`SEARCH_DATE_RANGE` -> text=`SEARCH_DATE_RANGE$text` / value=`SEARCH_DATE_RANGE$value` / submit=`SEARCH_DATE_RANGE`；`BL_OVER` -> text=`BL_OVER$text` / value=`BL_OVER$value` / submit=`BL_OVER`；`DEAL_RESULT` -> text=`DEAL_RESULT$text`；`ACCEPTANCE_SOURCE` -> text=`ACCEPTANCE_SOURCE$text` / value=`ACCEPTANCE_SOURCE$value` / submit=`ACCEPTANCE_SOURCE`；`SEND_SUPERIOR_SITE_CODE` -> text=`SEND_SUPERIOR_SITE_CODE$text` / value=`SEND_SUPERIOR_SITE_CODE$value` / submit=`SEND_SUPERIOR_SITE_CODE`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`
- 保存类按钮：`登记网点`、`登记人`
- 导出类按钮：`导出`、`导出`、`导出`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `登记网点`: `xpath=//*[normalize-space(.)="登记网点"]`
- `登记人`: `xpath=//*[normalize-space(.)="登记人"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `SEARCH_DATE_RANGE$text`: `#SEARCH_DATE_RANGE$text` / `xpath=//*[@id="SEARCH_DATE_RANGE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `BL_OVER$text`: `#BL_OVER$text` / `xpath=//*[@id="BL_OVER$text"]` / `xpath=//*[@placeholder="全部"]`
- `CODE_TYPE`: value=`CODE_TYPE$value` / submit=`CODE_TYPE`
- `searchOrderInput`: text=`searchOrderInput$text`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=ACCEPTANCE_NAME&_=%3CTS%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=ACCEPTANCE_TYPE&_=%3CTS%3E`
  - `FIND_TAB_SERVICE_LOG_PAGE` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_SITE_CENTER_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_SITE_ONE_TWO_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_TAB_EMPLOYEE_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 本次 live 点击未拿到稳定主查询请求，通常是因为页面需要先录入单号、先选择页签，或查询逻辑在脚本内先做前置校验。
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_TAB_SERVICE_LOG_PAGE`
- 页面脚本提示接口：
  - `/dataOperation/saveTables`

**Field Mapping**
- 用户输入参数：本次 live 查询没有稳定提取到，通常是页面先走前置校验、需要先录入单号，或查询不是默认页签。
- text/value 到提交字段：`CODE_TYPE` -> `CODE_TYPE`；`searchDateType` -> `searchDateType`；`SEARCH_DATE_RANGE` -> `SEARCH_DATE_RANGE`；`BL_OVER` -> `BL_OVER`；`ACCEPTANCE_SOURCE` -> `ACCEPTANCE_SOURCE`；`SEND_SUPERIOR_SITE_CODE` -> `SEND_SUPERIOR_SITE_CODE`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `searchBtn -> searchMethod()`
  - `exportBtn -> var data=$Z.page.getFormData("searchForm"); $Z.page.datagridExportExcel("datagrid","FIND_TAB_SERVICE_LOG_PAGE_EXCEL","客服查件数据导出", getQueryParam(), "async");`
- 保存/提交操作键：`TAB_SERVICE_LOG_DEL`

**Automation Notes**
- 更适合先走 DOM：当前没有稳定主查询请求，优先把页面交互顺序跑通。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=EwGsmpA8CcuqNCNzyloQeJUfgKVWAAC7PWH1VW` + 页面标题联合识别。

### 云呼系统

**Page Identity**
- 菜单路径：`客服管理 / 云呼系统`
- 页面类型：叶子页面
- `pageId`：`EwGsmpA8CcuqNCNzyloQeJUfgKVWAAC7PWH1VW`
- 页面标题：`客服查件管理`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
- 共页关系：和 `客服管理 / 客服记录查询` 共用同一 `pageId`，需要结合菜单行为判断是同页入口还是视觉复用。

**DOM Structure**
- 查询区/标签：`查询条件`、`按运单查询`、`查询时间范围`、`录入日期`、`处理完毕`、`处理结果`、`来电类型`、`来电网点所属分拨中心`、`责任方网点`、`登记网点`、`受理类别`、`来电网点`
- 首屏字段：`searchOrderInput`、`SEARCH_DATE_RANGE$text`、`BL_OVER$text`、`DEAL_RESULT`、`ACCEPTANCE_SOURCE$text`、`SEND_SUPERIOR_SITE_CODE$text`、`RESP_SITE_CODE$text`、`REGISTER_SITE_CODE$text`、`ACCEPTANCE_NAME$text`、`SEND_SITE`、`PHONE_CODE`、`REGISTER_MAN_CODE$text`
- MiniUI text/value 对：`CODE_TYPE` -> value=`CODE_TYPE$value` / submit=`CODE_TYPE`；`searchOrderInput` -> text=`searchOrderInput$text`；`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`SEARCH_DATE_RANGE` -> text=`SEARCH_DATE_RANGE$text` / value=`SEARCH_DATE_RANGE$value` / submit=`SEARCH_DATE_RANGE`；`BL_OVER` -> text=`BL_OVER$text` / value=`BL_OVER$value` / submit=`BL_OVER`；`DEAL_RESULT` -> text=`DEAL_RESULT$text`；`ACCEPTANCE_SOURCE` -> text=`ACCEPTANCE_SOURCE$text` / value=`ACCEPTANCE_SOURCE$value` / submit=`ACCEPTANCE_SOURCE`；`SEND_SUPERIOR_SITE_CODE` -> text=`SEND_SUPERIOR_SITE_CODE$text` / value=`SEND_SUPERIOR_SITE_CODE$value` / submit=`SEND_SUPERIOR_SITE_CODE`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`
- 保存类按钮：`登记网点`、`登记人`
- 导出类按钮：`导出`、`导出`、`导出`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `登记网点`: `xpath=//*[normalize-space(.)="登记网点"]`
- `登记人`: `xpath=//*[normalize-space(.)="登记人"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `SEARCH_DATE_RANGE$text`: `#SEARCH_DATE_RANGE$text` / `xpath=//*[@id="SEARCH_DATE_RANGE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `BL_OVER$text`: `#BL_OVER$text` / `xpath=//*[@id="BL_OVER$text"]` / `xpath=//*[@placeholder="全部"]`
- `CODE_TYPE`: value=`CODE_TYPE$value` / submit=`CODE_TYPE`
- `searchOrderInput`: text=`searchOrderInput$text`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
- 本次 live 点击未拿到稳定主查询请求，通常是因为页面需要先录入单号、先选择页签，或查询逻辑在脚本内先做前置校验。
- 页面脚本提示接口：
  - `/dataOperation/saveTables`

**Field Mapping**
- 用户输入参数：本次 live 查询没有稳定提取到，通常是页面先走前置校验、需要先录入单号，或查询不是默认页签。
- text/value 到提交字段：`CODE_TYPE` -> `CODE_TYPE`；`searchDateType` -> `searchDateType`；`SEARCH_DATE_RANGE` -> `SEARCH_DATE_RANGE`；`BL_OVER` -> `BL_OVER`；`ACCEPTANCE_SOURCE` -> `ACCEPTANCE_SOURCE`；`SEND_SUPERIOR_SITE_CODE` -> `SEND_SUPERIOR_SITE_CODE`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `searchBtn -> searchMethod()`
  - `exportBtn -> var data=$Z.page.getFormData("searchForm"); $Z.page.datagridExportExcel("datagrid","FIND_TAB_SERVICE_LOG_PAGE_EXCEL","客服查件数据导出", getQueryParam(), "async");`
- 保存/提交操作键：`TAB_SERVICE_LOG_DEL`
- 特殊页：当前 live 落在和 `客服记录查询` 相同的 `pageId`，后续开发前要再次确认是否存在系统内跳转或延迟重定向。

**Automation Notes**
- 更适合先走 DOM：当前没有稳定主查询请求，优先把页面交互顺序跑通。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=EwGsmpA8CcuqNCNzyloQeJUfgKVWAAC7PWH1VW` + 页面标题联合识别。
