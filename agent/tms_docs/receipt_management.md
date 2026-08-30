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

# 回单管理 页面深拆

> 外部页面快照：内容未在 2026-08-30 重新登录真实页面复验。DOM、接口、字段和页面身份可能已变化，不能直接作为当前自动化合同。

## Summary

- 叶子页数量：`10`
- 分组节点数量：`0`
- 高价值页面：`围板箱管理`、`围板箱调拨发起`、`回单查询`、`派方回箱处理`、`派方回单处理`

## Pages

### 回单查询

**Page Identity**
- 菜单路径：`回单管理 / 回单查询`
- 页面类型：叶子页面
- `pageId`：`xK8RQMEHqPmKQfDJd3DvUVE3LCe2NH6e4ENmI6`
- 页面标题：`回单查询-速通`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`查询条件`、`运单编号`、`回单编号`、`查询时间范围`、`录入时间`、`签收时间`、`寄件网点`、`派件网点`、`回单状态`、`分拨中心`
- 首屏字段：`searchOrderInput`、`SEARCH_DATE_RANGE$text`、`SEND_SITE_CODE$text`、`DISPATCH_SITE_CODE$text`、`STATE$text`、`CENTER_NAME_CODE$text`
- MiniUI text/value 对：`searchBillType` -> value=`searchBillType$value` / submit=`searchBillType`；`searchOrderInput` -> text=`searchOrderInput$text`；`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`SEARCH_DATE_RANGE` -> text=`SEARCH_DATE_RANGE$text` / value=`SEARCH_DATE_RANGE$value` / submit=`SEARCH_DATE_RANGE`；`SEND_SITE_CODE` -> text=`SEND_SITE_CODE$text` / value=`SEND_SITE_CODE$value` / submit=`SEND_SITE_CODE`；`DISPATCH_SITE_CODE` -> text=`DISPATCH_SITE_CODE$text` / value=`DISPATCH_SITE_CODE$value` / submit=`DISPATCH_SITE_CODE`；`STATE` -> text=`STATE$text` / value=`STATE$value` / submit=`STATE`；`CENTER_NAME_CODE` -> text=`CENTER_NAME_CODE$text` / value=`CENTER_NAME_CODE$value` / submit=`CENTER_NAME_CODE`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`
- 导出类按钮：`导出`、`导出`、`导出`、`下载附件`、`下载附件`、`下载附件`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `SEARCH_DATE_RANGE$text`: `#SEARCH_DATE_RANGE$text` / `xpath=//*[@id="SEARCH_DATE_RANGE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `SEND_SITE_CODE$text`: `#SEND_SITE_CODE$text` / `xpath=//*[@id="SEND_SITE_CODE$text"]`
- `searchBillType`: value=`searchBillType$value` / submit=`searchBillType`
- `searchOrderInput`: text=`searchOrderInput$text`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `FIND_TAB_RETURNBILL` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_SITE_ALL_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_SITE_ALL_COMBOBOX&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_SITE_CENTER_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_TAB_WORK_ORDER_TYPE_REMIND` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=%3CTOKEN%3E`
- 主查询接口：`FIND_TAB_RETURNBILL`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_RETURNBILL`
- 请求方法：`POST`
- 主查询参数键：`searchBillType`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`SEND_SITE_CODE`、`DISPATCH_SITE_CODE`、`STATE`、`CENTER_NAME_CODE`、`REGISTER_DATE`、`LOGIN_SITE_CODE`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIND_TAB_RETURNBILL` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_RETURNBILL`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_TAB_RETURNBILL`
- 页面脚本提示接口：
  - `/dataQuery/findAllByCallId?id=FIND_TAB_PIC_SCAN_DATA&BILL_CODE=`

**Field Mapping**
- 用户输入参数：`searchBillType`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`SEND_SITE_CODE`、`DISPATCH_SITE_CODE`、`STATE`、`CENTER_NAME_CODE`
- 页面自动补齐参数：`REGISTER_DATE`
- 登录/站点上下文参数：`LOGIN_SITE_CODE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`searchBillType` -> `searchBillType`；`searchDateType` -> `searchDateType`；`SEARCH_DATE_RANGE` -> `SEARCH_DATE_RANGE`；`SEND_SITE_CODE` -> `SEND_SITE_CODE`；`DISPATCH_SITE_CODE` -> `DISPATCH_SITE_CODE`；`STATE` -> `STATE`；`CENTER_NAME_CODE` -> `CENTER_NAME_CODE`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `deleteBillCode -> //清空查询单号 mini.get("searchOrderInput").setValue("");`
  - `searchBtn -> searchMethod()`
  - `exportBtn -> exportMethod()`
  - `downloadBtn -> downloadFile(e)`
- 前置校验/状态相关 CALL_ID：`FIND_TAB_RETURNBILL`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 页面识别建议：优先用 `pageId=xK8RQMEHqPmKQfDJd3DvUVE3LCe2NH6e4ENmI6` + 页面标题联合识别。

### 寄方回单查询

**Page Identity**
- 菜单路径：`回单管理 / 寄方回单查询`
- 页面类型：叶子页面
- `pageId`：`TJG3G3KI2iM4UCRsp3mWqJkb0BnlAHnepxGuG1`
- 页面标题：`寄方回单查询`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`查询条件`、`按回单查询`、`收单日期查询（31天内）`、`录入日期`、`寄件日期`、`派件网点`、`货单状态`、`取件员`、`寄件人`
- 首屏字段：`searchOrderInput`、`SEARCH_DATE_RANGE$text`、`DISPATCH_SITE_CODE$text`、`STATE$text`、`SIGN_MAN_CODE`、`SEND_MAN_CODE`
- MiniUI text/value 对：`CODE_TYPE` -> value=`CODE_TYPE$value` / submit=`CODE_TYPE`；`searchOrderInput` -> text=`searchOrderInput$text`；`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`SEARCH_DATE_RANGE` -> text=`SEARCH_DATE_RANGE$text` / value=`SEARCH_DATE_RANGE$value` / submit=`SEARCH_DATE_RANGE`；`DISPATCH_SITE_CODE` -> text=`DISPATCH_SITE_CODE$text` / value=`DISPATCH_SITE_CODE$value` / submit=`DISPATCH_SITE_CODE`；`STATE` -> text=`STATE$text` / value=`STATE$value` / submit=`STATE`；`SIGN_MAN_CODE` -> text=`SIGN_MAN_CODE$text`；`SEND_MAN_CODE` -> text=`SEND_MAN_CODE$text`
- 查询按钮：`收单日期查询（31天内）`、`查询`、`查询`、`查询`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `收单日期查询（31天内）`: `xpath=//*[normalize-space(.)="收单日期查询（31天内）"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `SEARCH_DATE_RANGE$text`: `#SEARCH_DATE_RANGE$text` / `xpath=//*[@id="SEARCH_DATE_RANGE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `DISPATCH_SITE_CODE$text`: `#DISPATCH_SITE_CODE$text` / `xpath=//*[@id="DISPATCH_SITE_CODE$text"]`
- `CODE_TYPE`: value=`CODE_TYPE$value` / submit=`CODE_TYPE`
- `searchOrderInput`: text=`searchOrderInput$text`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `FIND_TAB_RETURNBILL_SEND` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_SITE_ONE_TWO_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_TAB_RETURNBILL_SEND`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- 请求方法：`POST`
- 主查询参数键：`CODE_TYPE`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`DISPATCH_SITE_CODE`、`STATE`、`SIGN_MAN_CODE`、`SEND_MAN_CODE`、`REGISTER_DATE`、`ORDER_BY_CREATE_DATE`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIND_TAB_RETURNBILL_SEND` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_TAB_RETURNBILL_SEND`

**Field Mapping**
- 用户输入参数：`CODE_TYPE`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`DISPATCH_SITE_CODE`、`STATE`、`SIGN_MAN_CODE`、`SEND_MAN_CODE`
- 页面自动补齐参数：`REGISTER_DATE`、`ORDER_BY_CREATE_DATE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`CODE_TYPE` -> `CODE_TYPE`；`searchDateType` -> `searchDateType`；`SEARCH_DATE_RANGE` -> `SEARCH_DATE_RANGE`；`DISPATCH_SITE_CODE` -> `DISPATCH_SITE_CODE`；`STATE` -> `STATE`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `clearBillCode -> //清空查询单号 mini.get("searchOrderInput").setValue("");`
  - `searchBtn -> searchMethod();`
- 前置校验/状态相关 CALL_ID：`FIND_TAB_RETURNBILL_SEND`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 页面识别建议：优先用 `pageId=TJG3G3KI2iM4UCRsp3mWqJkb0BnlAHnepxGuG1` + 页面标题联合识别。

### 派方回单查询

**Page Identity**
- 菜单路径：`回单管理 / 派方回单查询`
- 页面类型：叶子页面
- `pageId`：`QcCvliI1BgzvVmKA33p9J49g0VnNRud3O90x0s`
- 页面标题：`派件回单查询`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`查询条件`、`按回单查询`、`收单日期查询（31天内）`、`录入日期`、`寄件日期`、`寄件网点`、`货单状态`、`派件员`
- 首屏字段：`searchOrderInput`、`SEARCH_DATE_RANGE$text`、`SEND_SITE_CODE$text`、`STATE$text`、`DISPATCH_MAN_CODE`
- MiniUI text/value 对：`CODE_TYPE` -> value=`CODE_TYPE$value` / submit=`CODE_TYPE`；`searchOrderInput` -> text=`searchOrderInput$text`；`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`SEARCH_DATE_RANGE` -> text=`SEARCH_DATE_RANGE$text` / value=`SEARCH_DATE_RANGE$value` / submit=`SEARCH_DATE_RANGE`；`SEND_SITE_CODE` -> text=`SEND_SITE_CODE$text` / value=`SEND_SITE_CODE$value` / submit=`SEND_SITE_CODE`；`STATE` -> text=`STATE$text` / value=`STATE$value` / submit=`STATE`；`DISPATCH_MAN_CODE` -> text=`DISPATCH_MAN_CODE$text`
- 查询按钮：`收单日期查询（31天内）`、`查询`、`查询`、`查询`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `收单日期查询（31天内）`: `xpath=//*[normalize-space(.)="收单日期查询（31天内）"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `SEARCH_DATE_RANGE$text`: `#SEARCH_DATE_RANGE$text` / `xpath=//*[@id="SEARCH_DATE_RANGE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `SEND_SITE_CODE$text`: `#SEND_SITE_CODE$text` / `xpath=//*[@id="SEND_SITE_CODE$text"]`
- `CODE_TYPE`: value=`CODE_TYPE$value` / submit=`CODE_TYPE`
- `searchOrderInput`: text=`searchOrderInput$text`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `FIND_TAB_RETURNBILL_DISP` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_SITE_ONE_TWO_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_TAB_RETURNBILL_DISP`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- 请求方法：`POST`
- 主查询参数键：`CODE_TYPE`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`SEND_SITE_CODE`、`STATE`、`DISPATCH_MAN_CODE`、`REGISTER_DATE`、`ORDER_BY_CREATE_DATE`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIND_TAB_RETURNBILL_DISP` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_TAB_RETURNBILL_DISP`

**Field Mapping**
- 用户输入参数：`CODE_TYPE`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`SEND_SITE_CODE`、`STATE`、`DISPATCH_MAN_CODE`
- 页面自动补齐参数：`REGISTER_DATE`、`ORDER_BY_CREATE_DATE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`CODE_TYPE` -> `CODE_TYPE`；`searchDateType` -> `searchDateType`；`SEARCH_DATE_RANGE` -> `SEARCH_DATE_RANGE`；`SEND_SITE_CODE` -> `SEND_SITE_CODE`；`STATE` -> `STATE`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `clearBillCode -> //清空查询单号 mini.get("searchOrderInput").setValue("");`
  - `searchBtn -> searchMethod();`
- 前置校验/状态相关 CALL_ID：`FIND_TAB_RETURNBILL_DISP`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 页面识别建议：优先用 `pageId=QcCvliI1BgzvVmKA33p9J49g0VnNRud3O90x0s` + 页面标题联合识别。

### 寄方回单跟踪

**Page Identity**
- 菜单路径：`回单管理 / 寄方回单跟踪`
- 页面类型：叶子页面
- `pageId`：`WGCUtjj1FIh69G5VO34SyVY9uSeAVOl1Rp1w15`
- 页面标题：`寄方回单跟踪`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 页签：`查询结果`、`处理记录`
- 查询区/标签：`查询条件`、`按回单号查询`、`按运单号查询`、`查询时间范围`、`寄件时间`、`签收时间`、`返回寄件时间`、`返回签收时间`、`目的网点`、`回单状态`
- 首屏字段：`searchOrderInput`、`SEARCH_DATE_RANGE$text`、`DESTINATION_CODE$text`、`RETURNBILL_STATUS$text`
- MiniUI text/value 对：`CODE_TYPE` -> value=`CODE_TYPE$value` / submit=`CODE_TYPE`；`searchOrderInput` -> text=`searchOrderInput$text`；`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`SEARCH_DATE_RANGE` -> text=`SEARCH_DATE_RANGE$text` / value=`SEARCH_DATE_RANGE$value` / submit=`SEARCH_DATE_RANGE`；`DESTINATION_CODE` -> text=`DESTINATION_CODE$text` / value=`DESTINATION_CODE$value` / submit=`DESTINATION_CODE`；`RETURNBILL_STATUS` -> text=`RETURNBILL_STATUS$text` / value=`RETURNBILL_STATUS$value` / submit=`RETURNBILL_STATUS`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`、`查询结果`
- 保存类按钮：`登记`、`登记`、`登记`、`审核`、`审核`、`审核`
- 导出类按钮：`导出`、`导出`、`导出`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `登记`: `#addBtn` / `xpath=//*[@id="addBtn"]` / `xpath=//a[normalize-space(.)="登记"]`
- `登记`: `xpath=//*[normalize-space(.)="登记"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `SEARCH_DATE_RANGE$text`: `#SEARCH_DATE_RANGE$text` / `xpath=//*[@id="SEARCH_DATE_RANGE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `DESTINATION_CODE$text`: `#DESTINATION_CODE$text` / `xpath=//*[@id="DESTINATION_CODE$text"]`
- `CODE_TYPE`: value=`CODE_TYPE$value` / submit=`CODE_TYPE`
- `searchOrderInput`: text=`searchOrderInput$text`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=RETURNBILL_STATUS&_=%3CTS%3E`
  - `FIND_SEND_RETURN_PROCESS` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_ALL_ONE_TOW_SITE_COMBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_SEND_RETURN_PROCESS`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- 请求方法：`POST`
- 主查询参数键：`CODE_TYPE`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`DESTINATION_CODE`、`RETURNBILL_STATUS`、`SEND_DATE`、`LOGIN_SITE_CODE`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIND_SEND_RETURN_PROCESS` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_SEND_RETURN_PROCESS`
- 页面脚本提示接口：
  - `/dataQuery/findAllByCallId?id=FIND_TAB_PROCESS_RECORD`
  - `/dataQuery/findAllByCallId?id=`

**Field Mapping**
- 用户输入参数：`CODE_TYPE`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`DESTINATION_CODE`、`RETURNBILL_STATUS`
- 页面自动补齐参数：`SEND_DATE`
- 登录/站点上下文参数：`LOGIN_SITE_CODE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`CODE_TYPE` -> `CODE_TYPE`；`searchDateType` -> `searchDateType`；`SEARCH_DATE_RANGE` -> `SEARCH_DATE_RANGE`；`DESTINATION_CODE` -> `DESTINATION_CODE`；`RETURNBILL_STATUS` -> `RETURNBILL_STATUS`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `searchBtn -> searchMethod()`
  - `addBtn -> addMethod();`
  - `auditBtn -> auditMethod();`
  - `exportBtn -> exportMethod()`
  - `clearBillCode -> //清空查询单号 mini.get("searchOrderInput").setValue("");`
- 前置校验/状态相关 CALL_ID：`FIND_TAB_PROCESS_RECORD`、`FIND_SEND_RETURN_PROCESS`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 页面识别建议：优先用 `pageId=WGCUtjj1FIh69G5VO34SyVY9uSeAVOl1Rp1w15` + 页面标题联合识别。

### 派方回单处理

**Page Identity**
- 菜单路径：`回单管理 / 派方回单处理`
- 页面类型：叶子页面
- `pageId`：`z2rp3P3pAOx95yjHr2E6SkIFNzXJfpdtbbKcSx`
- 页面标题：`派方回单处理`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 页签：`查询结果`、`处理记录`
- 查询区/标签：`查询条件`、`按回单号查询`、`按运单号查询`、`查询时间范围`、`寄件时间`、`签收时间`、`返回寄件时间`、`返回签收时间`、`寄件网点`、`回单状态`
- 首屏字段：`searchOrderInput`、`SEARCH_DATE_RANGE$text`、`SEND_SITE_CODE$text`、`RETURNBILL_STATUS$text`
- MiniUI text/value 对：`CODE_TYPE` -> value=`CODE_TYPE$value` / submit=`CODE_TYPE`；`searchOrderInput` -> text=`searchOrderInput$text`；`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`SEARCH_DATE_RANGE` -> text=`SEARCH_DATE_RANGE$text` / value=`SEARCH_DATE_RANGE$value` / submit=`SEARCH_DATE_RANGE`；`SEND_SITE_CODE` -> text=`SEND_SITE_CODE$text` / value=`SEND_SITE_CODE$value` / submit=`SEND_SITE_CODE`；`RETURNBILL_STATUS` -> text=`RETURNBILL_STATUS$text` / value=`RETURNBILL_STATUS$value` / submit=`RETURNBILL_STATUS`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`、`查询结果`
- 保存类按钮：`登记`、`登记`、`登记`、`开单`、`开单`、`开单`
- 导出类按钮：`导出`、`导出`、`导出`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `登记`: `#addBtn` / `xpath=//*[@id="addBtn"]` / `xpath=//a[normalize-space(.)="登记"]`
- `登记`: `xpath=//*[normalize-space(.)="登记"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `SEARCH_DATE_RANGE$text`: `#SEARCH_DATE_RANGE$text` / `xpath=//*[@id="SEARCH_DATE_RANGE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `SEND_SITE_CODE$text`: `#SEND_SITE_CODE$text` / `xpath=//*[@id="SEND_SITE_CODE$text"]`
- `CODE_TYPE`: value=`CODE_TYPE$value` / submit=`CODE_TYPE`
- `searchOrderInput`: text=`searchOrderInput$text`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=RETURNBILL_STATUS&_=%3CTS%3E`
  - `FIND_DISP_RETURN_PROCESS` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_ALL_ONE_TOW_SITE_COMBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_DISP_RETURN_PROCESS`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- 请求方法：`POST`
- 主查询参数键：`CODE_TYPE`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`SEND_SITE_CODE`、`RETURNBILL_STATUS`、`SEND_DATE`、`LOGIN_SITE_CODE`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIND_DISP_RETURN_PROCESS` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_DISP_RETURN_PROCESS`
- 页面脚本提示接口：
  - `/dataQuery/findAllByCallId?id=FIND_TAB_PROCESS_RECORD`
  - `/dataQuery/findAllByCallId?id=`

**Field Mapping**
- 用户输入参数：`CODE_TYPE`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`SEND_SITE_CODE`、`RETURNBILL_STATUS`
- 页面自动补齐参数：`SEND_DATE`
- 登录/站点上下文参数：`LOGIN_SITE_CODE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`CODE_TYPE` -> `CODE_TYPE`；`searchDateType` -> `searchDateType`；`SEARCH_DATE_RANGE` -> `SEARCH_DATE_RANGE`；`SEND_SITE_CODE` -> `SEND_SITE_CODE`；`RETURNBILL_STATUS` -> `RETURNBILL_STATUS`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `searchBtn -> searchMethod()`
  - `addBtn -> addMethod();`
  - `billBtn -> newRtBillMethod();`
  - `exportBtn -> exportMethod()`
  - `clearBillCode -> //清空查询单号 mini.get("searchOrderInput").setValue("");`
- 前置校验/状态相关 CALL_ID：`FIND_TAB_PROCESS_RECORD`、`FIND_DISP_RETURN_PROCESS`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 页面识别建议：优先用 `pageId=z2rp3P3pAOx95yjHr2E6SkIFNzXJfpdtbbKcSx` + 页面标题联合识别。

### 围板箱管理

**Page Identity**
- 菜单路径：`回单管理 / 围板箱管理`
- 页面类型：叶子页面
- `pageId`：`lzDh3ldqwRXALboIhDu8NBSDmU69yvAFCfnrPs`
- 页面标题：`围板箱管理`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 页签：`围板箱明细`、`库存查询`
- 查询区/标签：`基础信息`、`CURRENT_LOGIN_SITE`、`围板箱编号`、`网点`、`回箱状态`、`围板箱状态`
- 首屏字段：`COLLAR_NUMBER_LIST`、`BELONG_SITE_CODE$text`、`COLLAR_STATUS$text`
- MiniUI text/value 对：`COLLAR_NUMBER_LIST` -> text=`COLLAR_NUMBER_LIST$text`；`BELONG_SITE_CODE` -> text=`BELONG_SITE_CODE$text` / value=`BELONG_SITE_CODE$value` / submit=`BELONG_SITE_CODE`；`COLLAR_STATUS` -> text=`COLLAR_STATUS$text` / value=`COLLAR_STATUS$value` / submit=`COLLAR_STATUS`
- 查询按钮：`查询`、`查询`、`查询`、`库存查询`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询`: `#searchBtn1` / `xpath=//*[@id="searchBtn1"]` / `xpath=//a[normalize-space(.)="查询"]`
- `查询`: `xpath=//*[normalize-space(.)="查询"]`
- `COLLAR_NUMBER_LIST`: `#COLLAR_NUMBER_LIST$text` / `xpath=//*[@id="COLLAR_NUMBER_LIST$text"]` / `xpath=//*[@name="COLLAR_NUMBER_LIST"]`
- `BELONG_SITE_CODE$text`: `#BELONG_SITE_CODE$text` / `xpath=//*[@id="BELONG_SITE_CODE$text"]`
- `COLLAR_STATUS$text`: `#COLLAR_STATUS$text` / `xpath=//*[@id="COLLAR_STATUS$text"]` / `xpath=//*[@placeholder="请选择"]`
- `COLLAR_NUMBER_LIST`: text=`COLLAR_NUMBER_LIST$text`
- `BELONG_SITE_CODE`: text=`BELONG_SITE_CODE$text` / value=`BELONG_SITE_CODE$value` / submit=`BELONG_SITE_CODE`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=RETURNBOX_STATUS&_=%3CTS%3E`
  - `FIND_TAB_COLLAR` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_COLLAR`
  - `FIND_TAB_COLLAR_COUNT` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_COLLAR_COUNT`
  - `FIND_SITE_BOX_ALL` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_SITE_BOX_ALL&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_TAB_COLLAR`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_COLLAR`
- 请求方法：`POST`
- 主查询参数键：`CURRENT_LOGIN_SITE`、`COLLAR_NUMBER_LIST`、`BELONG_SITE_CODE`、`COLLAR_STATUS`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIND_TAB_COLLAR` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_COLLAR`
  - `FIND_TAB_COLLAR_COUNT` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_COLLAR_COUNT`
- 页面脚本提示接口：
  - `/dataOperation/saveTables`

**Field Mapping**
- 用户输入参数：`COLLAR_NUMBER_LIST`、`BELONG_SITE_CODE`、`COLLAR_STATUS`
- 页面自动补齐参数：`CURRENT_LOGIN_SITE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`BELONG_SITE_CODE` -> `BELONG_SITE_CODE`；`COLLAR_STATUS` -> `COLLAR_STATUS`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn1`。
- 点击绑定（从内联脚本截取）：
  - `searchBtn1 -> query();`
  - `editBut -> edit1();`
- 保存/提交操作键：`TAB_IRONARMY_QUOTE_DEL`
- 前置校验/状态相关 CALL_ID：`FIND_TAB_COLLAR_COUNT`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=lzDh3ldqwRXALboIhDu8NBSDmU69yvAFCfnrPs` + 页面标题联合识别。

### 围板箱调拨发起

**Page Identity**
- 菜单路径：`回单管理 / 围板箱调拨发起`
- 页面类型：叶子页面
- `pageId`：`U42tlpWwhExbGnRIKcJPUVn4SfsIhQY7dOYIi0`
- 页面标题：`围板箱调拨发起`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 页签：`调拨单`、`明细查询`
- 查询区/标签：`基础信息`、`按调拨单号查询`、`按运单号查询`、`调出网点`、`调拨类型`、`调入网点`、`单据类型`
- 首屏字段：`searchOrderInput`、`ALLOCATE_OUT_SITE_CODE$text`、`ALLOCATE_TYPE$text`、`ALLOCATE_IN_SITE_CODE$text`、`ORDER_TYPE$text`
- MiniUI text/value 对：`CODE_TYPE` -> value=`CODE_TYPE$value` / submit=`CODE_TYPE`；`searchOrderInput` -> text=`searchOrderInput$text`；`ALLOCATE_OUT_SITE_CODE` -> text=`ALLOCATE_OUT_SITE_CODE$text` / value=`ALLOCATE_OUT_SITE_CODE$value` / submit=`ALLOCATE_OUT_SITE_CODE`；`ALLOCATE_TYPE` -> text=`ALLOCATE_TYPE$text` / value=`ALLOCATE_TYPE$value` / submit=`ALLOCATE_TYPE`；`ALLOCATE_IN_SITE_CODE` -> text=`ALLOCATE_IN_SITE_CODE$text` / value=`ALLOCATE_IN_SITE_CODE$value` / submit=`ALLOCATE_IN_SITE_CODE`；`ORDER_TYPE` -> text=`ORDER_TYPE$text` / value=`ORDER_TYPE$value` / submit=`ORDER_TYPE`
- 查询按钮：`查询`、`查询`、`查询`、`明细查询`
- 保存类按钮：`开单`、`开单`、`开单`
- 导出类按钮：`导出`、`导出`、`导出`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询`: `#searchBtn1` / `xpath=//*[@id="searchBtn1"]` / `xpath=//a[normalize-space(.)="查询"]`
- `查询`: `xpath=//*[normalize-space(.)="查询"]`
- `开单`: `#billBtn` / `xpath=//*[@id="billBtn"]` / `xpath=//a[normalize-space(.)="开单"]`
- `开单`: `xpath=//*[normalize-space(.)="开单"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `ALLOCATE_OUT_SITE_CODE$text`: `#ALLOCATE_OUT_SITE_CODE$text` / `xpath=//*[@id="ALLOCATE_OUT_SITE_CODE$text"]`
- `ALLOCATE_TYPE$text`: `#ALLOCATE_TYPE$text` / `xpath=//*[@id="ALLOCATE_TYPE$text"]` / `xpath=//*[@placeholder="请选择"]`
- `CODE_TYPE`: value=`CODE_TYPE$value` / submit=`CODE_TYPE`
- `searchOrderInput`: text=`searchOrderInput$text`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `TAB_COLLAR_ALLOCATE` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=TAB_COLLAR_ALLOCATE`
  - `FIND_SITE_BOX_ALL` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_SITE_BOX_ALL&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`TAB_COLLAR_ALLOCATE`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=TAB_COLLAR_ALLOCATE`
- 请求方法：`POST`
- 主查询参数键：`CODE_TYPE`、`searchOrderInput`、`ALLOCATE_OUT_SITE_CODE`、`ALLOCATE_TYPE`、`ALLOCATE_IN_SITE_CODE`、`ORDER_TYPE`、`ORDER_CODE_LIST`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `TAB_COLLAR_ALLOCATE` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=TAB_COLLAR_ALLOCATE`
- 页面脚本提示接口：
  - `/dataQuery/findAllByCallId?id=FIND_TAB_ELECTRONIC_STOCK_WBX`
  - `/dataOperation/saveTables`

**Field Mapping**
- 用户输入参数：`CODE_TYPE`、`searchOrderInput`、`ALLOCATE_OUT_SITE_CODE`、`ALLOCATE_TYPE`、`ALLOCATE_IN_SITE_CODE`、`ORDER_TYPE`
- 页面自动补齐参数：`ORDER_CODE_LIST`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`CODE_TYPE` -> `CODE_TYPE`；`ALLOCATE_OUT_SITE_CODE` -> `ALLOCATE_OUT_SITE_CODE`；`ALLOCATE_TYPE` -> `ALLOCATE_TYPE`；`ALLOCATE_IN_SITE_CODE` -> `ALLOCATE_IN_SITE_CODE`；`ORDER_TYPE` -> `ORDER_TYPE`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn1`。
- 点击绑定（从内联脚本截取）：
  - `searchBtn2 -> add();`
  - `searchBtn3 -> edit();`
  - `billBtn -> newRtBillMethod();`
  - `bohui -> chexiao();`
  - `daochu -> exportData();`
  - `searchBtn1 -> query();`
- 保存/提交操作键：`TAB_COLLAR_ALLOCATE_UPT`、`TAB_COLLAR_ALLOCATE`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=U42tlpWwhExbGnRIKcJPUVn4SfsIhQY7dOYIi0` + 页面标题联合识别。

### 围板箱调拨确认

**Page Identity**
- 菜单路径：`回单管理 / 围板箱调拨确认`
- 页面类型：叶子页面
- `pageId`：`BgTSBBZf3BKxPelzhw1JftN6xi8L07iB11xIAz`
- 页面标题：`围板箱调拨确认`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 页签：`调拨单`、`明细查询`
- 查询区/标签：`基础信息`、`调拨单号`、`调出网点`、`调拨类型`、`调入网点`、`单据类型`
- 首屏字段：`ORDER_CODE_LIST`、`ALLOCATE_OUT_SITE_CODE$text`、`COLLAR_STATUS$text`、`ALLOCATE_IN_SITE_CODE$text`
- MiniUI text/value 对：`ORDER_CODE_LIST` -> text=`ORDER_CODE_LIST$text`；`ALLOCATE_OUT_SITE_CODE` -> text=`ALLOCATE_OUT_SITE_CODE$text` / value=`ALLOCATE_OUT_SITE_CODE$value` / submit=`ALLOCATE_OUT_SITE_CODE`；`COLLAR_STATUS` -> text=`COLLAR_STATUS$text` / value=`COLLAR_STATUS$value` / submit=`COLLAR_STATUS`；`ALLOCATE_IN_SITE_CODE` -> text=`ALLOCATE_IN_SITE_CODE$text` / value=`ALLOCATE_IN_SITE_CODE$value` / submit=`ALLOCATE_IN_SITE_CODE`
- 查询按钮：`查询`、`查询`、`查询`、`明细查询`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询`: `#searchBtn1` / `xpath=//*[@id="searchBtn1"]` / `xpath=//a[normalize-space(.)="查询"]`
- `查询`: `xpath=//*[normalize-space(.)="查询"]`
- `ORDER_CODE_LIST`: `#ORDER_CODE_LIST$text` / `xpath=//*[@id="ORDER_CODE_LIST$text"]` / `xpath=//*[@name="ORDER_CODE_LIST"]`
- `ALLOCATE_OUT_SITE_CODE$text`: `#ALLOCATE_OUT_SITE_CODE$text` / `xpath=//*[@id="ALLOCATE_OUT_SITE_CODE$text"]`
- `COLLAR_STATUS$text`: `#COLLAR_STATUS$text` / `xpath=//*[@id="COLLAR_STATUS$text"]` / `xpath=//*[@placeholder="请选择"]`
- `ORDER_CODE_LIST`: text=`ORDER_CODE_LIST$text`
- `ALLOCATE_OUT_SITE_CODE`: text=`ALLOCATE_OUT_SITE_CODE$text` / value=`ALLOCATE_OUT_SITE_CODE$value` / submit=`ALLOCATE_OUT_SITE_CODE`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `TAB_COLLAR_ALLOCATE_SH` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=TAB_COLLAR_ALLOCATE_SH`
  - `FIND_SITE_BOX_ALL` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_SITE_BOX_ALL&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_SITE_ALL_AREA` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_SITE_ALL_AREA&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`TAB_COLLAR_ALLOCATE_SH`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=TAB_COLLAR_ALLOCATE_SH`
- 请求方法：`POST`
- 主查询参数键：`ORDER_CODE_LIST`、`ALLOCATE_OUT_SITE_CODE`、`COLLAR_STATUS`、`ALLOCATE_IN_SITE_CODE`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `TAB_COLLAR_ALLOCATE_SH` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=TAB_COLLAR_ALLOCATE_SH`

**Field Mapping**
- 用户输入参数：`ORDER_CODE_LIST`、`ALLOCATE_OUT_SITE_CODE`、`COLLAR_STATUS`、`ALLOCATE_IN_SITE_CODE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`ALLOCATE_OUT_SITE_CODE` -> `ALLOCATE_OUT_SITE_CODE`；`COLLAR_STATUS` -> `COLLAR_STATUS`；`ALLOCATE_IN_SITE_CODE` -> `ALLOCATE_IN_SITE_CODE`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn1`。
- 点击绑定（从内联脚本截取）：
  - `searchBtn1 -> query();`
  - `searchBtn2 -> shenhe();`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 页面识别建议：优先用 `pageId=BgTSBBZf3BKxPelzhw1JftN6xi8L07iB11xIAz` + 页面标题联合识别。

### 寄方回箱跟踪

**Page Identity**
- 菜单路径：`回单管理 / 寄方回箱跟踪`
- 页面类型：叶子页面
- `pageId`：`FTKtCTpPqNa1ovA9MLZ0cCd7xfVk5aWHYAmjgz`
- 页面标题：`寄方回箱跟踪`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 页签：`查询结果`
- 查询区/标签：`查询条件`、`按运单号查询`、`查询时间范围`、`寄件时间`、`签收时间`、`返回寄件时间`、`返回签收时间`、`目的网点`、`回单状态`
- 首屏字段：`searchOrderInput`、`SEARCH_DATE_RANGE$text`、`DESTINATION_CODE$text`、`RETURNBILL_STATUS$text`
- MiniUI text/value 对：`CODE_TYPE` -> value=`CODE_TYPE$value` / submit=`CODE_TYPE`；`searchOrderInput` -> text=`searchOrderInput$text`；`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`SEARCH_DATE_RANGE` -> text=`SEARCH_DATE_RANGE$text` / value=`SEARCH_DATE_RANGE$value` / submit=`SEARCH_DATE_RANGE`；`DESTINATION_CODE` -> text=`DESTINATION_CODE$text` / value=`DESTINATION_CODE$value` / submit=`DESTINATION_CODE`；`RETURNBILL_STATUS` -> text=`RETURNBILL_STATUS$text` / value=`RETURNBILL_STATUS$value` / submit=`RETURNBILL_STATUS`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`、`查询结果`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `SEARCH_DATE_RANGE$text`: `#SEARCH_DATE_RANGE$text` / `xpath=//*[@id="SEARCH_DATE_RANGE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `DESTINATION_CODE$text`: `#DESTINATION_CODE$text` / `xpath=//*[@id="DESTINATION_CODE$text"]`
- `CODE_TYPE`: value=`CODE_TYPE$value` / submit=`CODE_TYPE`
- `searchOrderInput`: text=`searchOrderInput$text`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=RETURNBOX_STATUS&_=%3CTS%3E`
  - `FIND_SEND_RETURN_PROCESS_HX` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_ALL_ONE_TOW_SITE_COMBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_SEND_RETURN_PROCESS_HX`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- 请求方法：`POST`
- 主查询参数键：`CODE_TYPE`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`DESTINATION_CODE`、`RETURNBILL_STATUS`、`SEND_DATE`、`LOGIN_SITE_CODE`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIND_SEND_RETURN_PROCESS_HX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_SEND_RETURN_PROCESS_HX`
- 页面脚本提示接口：
  - `/dataQuery/findAllByCallId?id=FIND_TAB_PROCESS_RECORD_HX`
  - `/dataQuery/findAllByCallId?id=`

**Field Mapping**
- 用户输入参数：`CODE_TYPE`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`DESTINATION_CODE`、`RETURNBILL_STATUS`
- 页面自动补齐参数：`SEND_DATE`
- 登录/站点上下文参数：`LOGIN_SITE_CODE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`CODE_TYPE` -> `CODE_TYPE`；`searchDateType` -> `searchDateType`；`SEARCH_DATE_RANGE` -> `SEARCH_DATE_RANGE`；`DESTINATION_CODE` -> `DESTINATION_CODE`；`RETURNBILL_STATUS` -> `RETURNBILL_STATUS`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `clearBillCode -> //清空查询单号 mini.get("searchOrderInput").setValue("");`
  - `searchBtn -> searchMethod()`
- 前置校验/状态相关 CALL_ID：`FIND_TAB_PROCESS_RECORD_HX`、`FIND_SEND_RETURN_PROCESS_HX`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 页面识别建议：优先用 `pageId=FTKtCTpPqNa1ovA9MLZ0cCd7xfVk5aWHYAmjgz` + 页面标题联合识别。

### 派方回箱处理

**Page Identity**
- 菜单路径：`回单管理 / 派方回箱处理`
- 页面类型：叶子页面
- `pageId`：`Ay3AuDwBDbp5zd2asEEl9sdx3IcbqdQaU6D7jF`
- 页面标题：`派方回箱处理`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 页签：`查询结果`
- 查询区/标签：`查询条件`、`按运单号查询`、`查询时间范围`、`寄件时间`、`签收时间`、`返回寄件时间`、`返回签收时间`、`寄件网点`、`回单状态`
- 首屏字段：`searchOrderInput`、`SEARCH_DATE_RANGE$text`、`SEND_SITE_CODE$text`、`RETURNBILL_STATUS$text`
- MiniUI text/value 对：`CODE_TYPE` -> value=`CODE_TYPE$value` / submit=`CODE_TYPE`；`searchOrderInput` -> text=`searchOrderInput$text`；`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`SEARCH_DATE_RANGE` -> text=`SEARCH_DATE_RANGE$text` / value=`SEARCH_DATE_RANGE$value` / submit=`SEARCH_DATE_RANGE`；`SEND_SITE_CODE` -> text=`SEND_SITE_CODE$text` / value=`SEND_SITE_CODE$value` / submit=`SEND_SITE_CODE`；`RETURNBILL_STATUS` -> text=`RETURNBILL_STATUS$text` / value=`RETURNBILL_STATUS$value` / submit=`RETURNBILL_STATUS`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`、`查询结果`
- 保存类按钮：`开单`、`开单`、`开单`
- 导出类按钮：`导出`、`导出`、`导出`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `开单`: `#billBtn` / `xpath=//*[@id="billBtn"]` / `xpath=//a[normalize-space(.)="开单"]`
- `开单`: `xpath=//*[normalize-space(.)="开单"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `SEARCH_DATE_RANGE$text`: `#SEARCH_DATE_RANGE$text` / `xpath=//*[@id="SEARCH_DATE_RANGE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `SEND_SITE_CODE$text`: `#SEND_SITE_CODE$text` / `xpath=//*[@id="SEND_SITE_CODE$text"]`
- `CODE_TYPE`: value=`CODE_TYPE$value` / submit=`CODE_TYPE`
- `searchOrderInput`: text=`searchOrderInput$text`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=RETURNBOX_STATUS&_=%3CTS%3E`
  - `FIND_DISP_RETURN_PROCESS_HX` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_ALL_ONE_TOW_SITE_COMBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_DISP_RETURN_PROCESS_HX`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- 请求方法：`POST`
- 主查询参数键：`CODE_TYPE`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`SEND_SITE_CODE`、`RETURNBILL_STATUS`、`SEND_DATE`、`LOGIN_SITE_CODE`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIND_DISP_RETURN_PROCESS_HX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
  - `FIND_TAB_WORK_ORDER_TYPE_REMIND` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=%3CTOKEN%3E`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_DISP_RETURN_PROCESS_HX`
- 页面脚本提示接口：
  - `/dataQuery/findAllByCallId?id=FIND_TAB_PROCESS_RECORD`
  - `/dataQuery/findAllByCallId?id=`

**Field Mapping**
- 用户输入参数：`CODE_TYPE`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`SEND_SITE_CODE`、`RETURNBILL_STATUS`
- 页面自动补齐参数：`SEND_DATE`
- 登录/站点上下文参数：`LOGIN_SITE_CODE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`CODE_TYPE` -> `CODE_TYPE`；`searchDateType` -> `searchDateType`；`SEARCH_DATE_RANGE` -> `SEARCH_DATE_RANGE`；`SEND_SITE_CODE` -> `SEND_SITE_CODE`；`RETURNBILL_STATUS` -> `RETURNBILL_STATUS`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `searchBtn -> searchMethod()`
  - `billBtn -> newRtBillMethod();`
  - `exportBtn -> exportMethod();`
  - `clearBillCode -> //清空查询单号 mini.get("searchOrderInput").setValue("");`
- 前置校验/状态相关 CALL_ID：`FIND_TAB_PROCESS_RECORD`、`FIND_DISP_RETURN_PROCESS_HX`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 页面识别建议：优先用 `pageId=Ay3AuDwBDbp5zd2asEEl9sdx3IcbqdQaU6D7jF` + 页面标题联合识别。
