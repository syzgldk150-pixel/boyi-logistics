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

# 扫描管理 页面深拆

> 外部页面快照：内容未在 2026-08-30 重新登录真实页面复验。DOM、接口、字段和页面身份可能已变化，不能直接作为当前自动化合同。

## Summary

- 叶子页数量：`19`
- 分组节点数量：`1`
- 高价值页面：`扫描记录查询`、`到件清单`、`有发未到查询`、`有到未发查询`、`发件清单`
- 分组节点：`对比监控`

## Pages

### 收件扫描

**Page Identity**
- 菜单路径：`扫描管理 / 收件扫描`
- 页面类型：叶子页面
- `pageId`：`msH8yuHX0uPkAwhsFq65QSL5CLW6Xp49q6j9FC`
- 页面标题：`收件扫描-速通`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`扫描内容`、`扫描时间`、`快件类型`、`清单编号`、`扫描单号`、`收件员`、`备注`
- 首屏字段：`SCAN_DATE$text`、`FAST_TYPE$text`、`LISTING_CODE`、`BILL_CODE`、`DISPATCH_OR_SEND_MAN`、`REMARK`
- MiniUI text/value 对：`SCAN_DATE` -> text=`SCAN_DATE$text` / value=`SCAN_DATE$value` / submit=`SCAN_DATE`；`FAST_TYPE` -> text=`FAST_TYPE$text` / value=`FAST_TYPE$value` / submit=`FAST_TYPE`；`LISTING_CODE` -> text=`LISTING_CODE$text`；`BILL_CODE` -> text=`BILL_CODE$text`；`DISPATCH_OR_SEND_MAN_CODE` -> text=`DISPATCH_OR_SEND_MAN_CODE$text` / value=`DISPATCH_OR_SEND_MAN_CODE$value` / submit=`DISPATCH_OR_SEND_MAN_CODE`；`REMARK` -> text=`REMARK$text`
- 保存类按钮：`上传`、`上传`、`上传`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `上传`: `#saveAllScanRecBtn` / `xpath=//*[@id="saveAllScanRecBtn"]` / `xpath=//a[normalize-space(.)="上传"]`
- `上传`: `xpath=//*[normalize-space(.)="上传"]`
- `SCAN_DATE$text`: `#SCAN_DATE$text` / `xpath=//*[@id="SCAN_DATE$text"]`
- `FAST_TYPE$text`: `#FAST_TYPE$text` / `xpath=//*[@id="FAST_TYPE$text"]` / `xpath=//*[@placeholder="请选择"]`
- `LISTING_CODE`: `#LISTING_CODE$text` / `xpath=//*[@id="LISTING_CODE$text"]` / `xpath=//*[@name="LISTING_CODE"]`
- `SCAN_DATE`: text=`SCAN_DATE$text` / value=`SCAN_DATE$value` / submit=`SCAN_DATE`
- `FAST_TYPE`: text=`FAST_TYPE$text` / value=`FAST_TYPE$value` / submit=`FAST_TYPE`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=FAST_TYPE&_=%3CTS%3E`
  - `FIND_TAB_EMPLOYEE_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&OWNER_SITE_CODE=%3COWNER_SITE_CODE%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 本次 live 点击未拿到稳定主查询请求，通常是因为页面需要先录入单号、先选择页签，或查询逻辑在脚本内先做前置校验。
- 页面脚本提示接口：
  - `/dataQuery/findPageByCallId?id=FIND_TAB_EMPLOYEE_COMBOBOX&OWNER_SITE_CODE=`
  - `/dataOperation/saveTables`
  - `/dataQuery/findPageByCallId?id=FIND_BILL_BY_BILL_CODE`
  - `/dataQuery/findAllByCallId?id=GET_BILL_BY_BILLCODE_NEW`
  - `/dataQuery/findAllByCallId?id=FIND_TMS_SYS_SHARE_SET`
  - `/dataQuery/findAllByCallId?id=FIND_SCAN_REC_BILL_CODE`
  - `/dataQuery/findAllByCallId?id=FIND_WAYBILL_SIGN_STATE`

**Field Mapping**
- 用户输入参数：本次 live 查询没有稳定提取到，通常是页面先走前置校验、需要先录入单号，或查询不是默认页签。
- text/value 到提交字段：`SCAN_DATE` -> `SCAN_DATE`；`FAST_TYPE` -> `FAST_TYPE`；`DISPATCH_OR_SEND_MAN_CODE` -> `DISPATCH_OR_SEND_MAN_CODE`

**State / Validation Logic**
- 查询入口：没有稳定点击到默认查询按钮，说明此页可能依赖录入单号、页签切换或前置校验。
- 点击绑定（从内联脚本截取）：
  - `saveAllScanRecBtn -> uploadDataMethod();`
  - `delBtn -> deleteRow()`
- 保存/提交操作键：`TAB_SCAN_REC_ADD`
- 前置校验/状态相关 CALL_ID：`FIND_WAYBILL_SIGN_STATE`

**Automation Notes**
- 更适合先走 DOM：当前没有稳定主查询请求，优先把页面交互顺序跑通。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=msH8yuHX0uPkAwhsFq65QSL5CLW6Xp49q6j9FC` + 页面标题联合识别。

### 发件扫描

**Page Identity**
- 菜单路径：`扫描管理 / 发件扫描`
- 页面类型：叶子页面
- `pageId`：`dCUOebh6ICIB4fR75PajnvVQGIRMu2fLuTOnum`
- 页面标题：`发件扫描-速通`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`网点类型`、`扫描信息`、`时效`、`无收、到多发件`、`扫描时间`、`快件类型`、`清单编号`、`扫描单号`、`下一站`、`备注`、`转出单号`、`车牌号`
- 首屏字段：`SCAN_STATUS$text`、`LONG_TIME$text`、`SCAN_DATE$text`、`FAST_TYPE$text`、`LISTING_CODE`、`BILL_CODE`、`PRE_OR_NEXT_STATION`、`REMARK`、`TRANSFERE_BILL_CODE`、`TRUCK_CODE`
- MiniUI text/value 对：`SCAN_STATUS` -> text=`SCAN_STATUS$text` / value=`SCAN_STATUS$value` / submit=`SCAN_STATUS`；`LONG_TIME` -> text=`LONG_TIME$text` / value=`LONG_TIME$value` / submit=`LONG_TIME`；`OTHER_BILL` -> text=`OTHER_BILL$text`；`SCAN_DATE` -> text=`SCAN_DATE$text` / value=`SCAN_DATE$value` / submit=`SCAN_DATE`；`FAST_TYPE` -> text=`FAST_TYPE$text` / value=`FAST_TYPE$value` / submit=`FAST_TYPE`；`LISTING_CODE` -> text=`LISTING_CODE$text`；`BILL_CODE` -> text=`BILL_CODE$text`；`PRE_OR_NEXT_STATION_CODE` -> text=`PRE_OR_NEXT_STATION_CODE$text` / value=`PRE_OR_NEXT_STATION_CODE$value` / submit=`PRE_OR_NEXT_STATION_CODE`
- 保存类按钮：`上传`、`上传`、`上传`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `上传`: `#addBtn` / `xpath=//*[@id="addBtn"]` / `xpath=//a[normalize-space(.)="上传"]`
- `上传`: `xpath=//*[normalize-space(.)="上传"]`
- `SCAN_STATUS$text`: `#SCAN_STATUS$text` / `xpath=//*[@id="SCAN_STATUS$text"]` / `xpath=//*[@placeholder="请选择类型"]`
- `LONG_TIME$text`: `#LONG_TIME$text` / `xpath=//*[@id="LONG_TIME$text"]` / `xpath=//*[@placeholder="请选择"]`
- `SCAN_STATUS`: text=`SCAN_STATUS$text` / value=`SCAN_STATUS$value` / submit=`SCAN_STATUS`
- `LONG_TIME`: text=`LONG_TIME$text` / value=`LONG_TIME$value` / submit=`LONG_TIME`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=FAST_TYPE&_=%3CTS%3E`
  - `FIND_SCAN_SEND_SITE_COMBOBOX_NEW` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 本次 live 点击未拿到稳定主查询请求，通常是因为页面需要先录入单号、先选择页签，或查询逻辑在脚本内先做前置校验。
- 页面脚本提示接口：
  - `/dataQuery/findPageByCallId?id=FIND_SCAN_SEND_SITE_COMBOBOX_BY_CENTER`
  - `///dataQuery/findPageByCallId?id=FIND_SCAN_SEND_SITE_COMBOBOX`
  - `/dataQuery/findPageByCallId?id=FIND_SCAN_SEND_SITE_COMBOBOX`
  - `/dataQuery/findPageByCallId?id=FIND_SITE_ALL_COMBOBOX`
  - `/dataQuery/findPageByCallId?id=FIND_SCAN_SEND_SITE_DIST`
  - `/dataQuery/findAllByCallId?id=GET_SUPERIOR_SITE2_SEND_CODE`
  - `/dataQuery/findPageByCallId?id=FIND_SCAN_SEND_SITE_COMBOBOX&SUPERIOR_SITE2_SEND_CODE=`
  - `/dataQuery/findAllByCallId?id=`
  - `/dataQuery/findAllByCallId?id=GET_BILL_BY_BILLCODE_NEW`
  - `/dataQuery/findAllByCallId?id=FIND_WAYBILL_SIGN_STATE`
  - `/dataOperation/saveTables`
  - `/dataQuery/findAllByCallId?id=FIND_TMS_SYS_SHARE_SET`

**Field Mapping**
- 用户输入参数：本次 live 查询没有稳定提取到，通常是页面先走前置校验、需要先录入单号，或查询不是默认页签。
- text/value 到提交字段：`SCAN_STATUS` -> `SCAN_STATUS`；`LONG_TIME` -> `LONG_TIME`；`SCAN_DATE` -> `SCAN_DATE`；`FAST_TYPE` -> `FAST_TYPE`；`PRE_OR_NEXT_STATION_CODE` -> `PRE_OR_NEXT_STATION_CODE`

**State / Validation Logic**
- 查询入口：没有稳定点击到默认查询按钮，说明此页可能依赖录入单号、页签切换或前置校验。
- 点击绑定（从内联脚本截取）：
  - `addBtn -> addData();`
  - `delBtn -> deleteRow()`
- 保存/提交操作键：`TAB_SCAN_SEND_ADD`
- 前置校验/状态相关 CALL_ID：`FIND_WAYBILL_SIGN_STATE`

**Automation Notes**
- 更适合先走 DOM：当前没有稳定主查询请求，优先把页面交互顺序跑通。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=dCUOebh6ICIB4fR75PajnvVQGIRMu2fLuTOnum` + 页面标题联合识别。

### 到件扫描

**Page Identity**
- 菜单路径：`扫描管理 / 到件扫描`
- 页面类型：叶子页面
- `pageId`：`rj4gT693ygKAMUVhcKHB4LrlydDULU46DkEyxu`
- 页面标题：`到件扫描-速通`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`扫描内容`、`时效`、`扫描时间`、`快件类型`、`清单编号`、`扫描单号`、`上一站`、`备注`、`车牌号`
- 首屏字段：`LONG_TIME$text`、`SCAN_DATE$text`、`FAST_TYPE$text`、`LISTING_CODE`、`BILL_CODE`、`PRE_OR_NEXT_STATION`、`REMARK`、`TRUCK_CODE`
- MiniUI text/value 对：`LONG_TIME` -> text=`LONG_TIME$text` / value=`LONG_TIME$value` / submit=`LONG_TIME`；`SCAN_DATE` -> text=`SCAN_DATE$text` / value=`SCAN_DATE$value` / submit=`SCAN_DATE`；`FAST_TYPE` -> text=`FAST_TYPE$text` / value=`FAST_TYPE$value` / submit=`FAST_TYPE`；`LISTING_CODE` -> text=`LISTING_CODE$text`；`BILL_CODE` -> text=`BILL_CODE$text`；`PRE_OR_NEXT_STATION_CODE` -> text=`PRE_OR_NEXT_STATION_CODE$text` / value=`PRE_OR_NEXT_STATION_CODE$value` / submit=`PRE_OR_NEXT_STATION_CODE`；`REMARK` -> text=`REMARK$text`；`TRUCK_CODE` -> text=`TRUCK_CODE$text`
- 保存类按钮：`上传`、`上传`、`上传`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `上传`: `#addBtn` / `xpath=//*[@id="addBtn"]` / `xpath=//a[normalize-space(.)="上传"]`
- `上传`: `xpath=//*[normalize-space(.)="上传"]`
- `LONG_TIME$text`: `#LONG_TIME$text` / `xpath=//*[@id="LONG_TIME$text"]` / `xpath=//*[@placeholder="请选择"]`
- `LONG_TIME`: text=`LONG_TIME$text` / value=`LONG_TIME$value` / submit=`LONG_TIME`
- `SCAN_DATE`: text=`SCAN_DATE$text` / value=`SCAN_DATE$value` / submit=`SCAN_DATE`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=FAST_TYPE&_=%3CTS%3E`
  - `FIND_TAB_SITEALL` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_SITEALL&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 本次 live 点击未拿到稳定主查询请求，通常是因为页面需要先录入单号、先选择页签，或查询逻辑在脚本内先做前置校验。
- 页面脚本提示接口：
  - `/dataQuery/findPageByCallId?id=FIND_SCAN_SITE_COMBOBOX_BY_CENTER`
  - `/dataQuery/findPageByCallId?id=FIND_SITE_ALL_COMBOBOX`
  - `/dataQuery/findPageByCallId?id=FIND_SCAN_COME_SITE_SEND`
  - `/dataQuery/findAllByCallId?id=GET_SUPERIOR_SITE2_DISP_CODE`
  - `/dataQuery/findPageByCallId?id=FIND_SCAN_COME_SITE_COMBOBOX&SUPERIOR_SITE2_DISP_CODE=`
  - `/dataQuery/findPageByCallId?id=FIND_SCAN_COME_SITE_COMBOBOX`
  - `/dataQuery/findAllByCallId?id=FIND_WAYBILL_SIGN_STATE`
  - `/dataOperation/saveTables`
  - `/dataQuery/findAllByCallId?id=FIND_SEND_NO_COME`
  - `/dataQuery/findAllByCallId?id=FIND_SEND_NO_COME2`
  - `/dataQuery/findAllByCallId?id=FIND_TMS_SYS_SHARE_SET`

**Field Mapping**
- 用户输入参数：本次 live 查询没有稳定提取到，通常是页面先走前置校验、需要先录入单号，或查询不是默认页签。
- text/value 到提交字段：`LONG_TIME` -> `LONG_TIME`；`SCAN_DATE` -> `SCAN_DATE`；`FAST_TYPE` -> `FAST_TYPE`；`PRE_OR_NEXT_STATION_CODE` -> `PRE_OR_NEXT_STATION_CODE`

**State / Validation Logic**
- 查询入口：没有稳定点击到默认查询按钮，说明此页可能依赖录入单号、页签切换或前置校验。
- 点击绑定（从内联脚本截取）：
  - `addBtn -> uploadDataMethod()`
  - `deleteBtn -> deleteRow()`
- 保存/提交操作键：`TAB_SCAN_COME_ADD`
- 前置校验/状态相关 CALL_ID：`FIND_WAYBILL_SIGN_STATE`、`FIND_SEND_NO_COME`、`FIND_SEND_NO_COME2`

**Automation Notes**
- 更适合先走 DOM：当前没有稳定主查询请求，优先把页面交互顺序跑通。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=rj4gT693ygKAMUVhcKHB4LrlydDULU46DkEyxu` + 页面标题联合识别。

### 派件扫描

**Page Identity**
- 菜单路径：`扫描管理 / 派件扫描`
- 页面类型：叶子页面
- `pageId`：`wlqNvDFGrUUtZtTgipe5IFaBuU8D3ogUrU2cGk`
- 页面标题：`派件扫描-速通`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`扫描内容`、`时效`、`扫描时间`、`快件类型`、`清单编号`、`扫描单号`、`派件员`、`备注`
- 首屏字段：`LONG_TIME$text`、`SCAN_DATE$text`、`FAST_TYPE$text`、`LISTING_CODE`、`BILL_CODE`、`DISPATCH_OR_SEND_MAN`、`REMARK`
- MiniUI text/value 对：`LONG_TIME` -> text=`LONG_TIME$text` / value=`LONG_TIME$value` / submit=`LONG_TIME`；`SCAN_DATE` -> text=`SCAN_DATE$text` / value=`SCAN_DATE$value` / submit=`SCAN_DATE`；`FAST_TYPE` -> text=`FAST_TYPE$text` / value=`FAST_TYPE$value` / submit=`FAST_TYPE`；`LISTING_CODE` -> text=`LISTING_CODE$text`；`BILL_CODE` -> text=`BILL_CODE$text`；`DISPATCH_OR_SEND_MAN_CODE` -> text=`DISPATCH_OR_SEND_MAN_CODE$text` / value=`DISPATCH_OR_SEND_MAN_CODE$value` / submit=`DISPATCH_OR_SEND_MAN_CODE`；`REMARK` -> text=`REMARK$text`
- 保存类按钮：`上传`、`上传`、`上传`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `上传`: `#addBtn` / `xpath=//*[@id="addBtn"]` / `xpath=//a[normalize-space(.)="上传"]`
- `上传`: `xpath=//*[normalize-space(.)="上传"]`
- `LONG_TIME$text`: `#LONG_TIME$text` / `xpath=//*[@id="LONG_TIME$text"]` / `xpath=//*[@placeholder="请选择"]`
- `LONG_TIME`: text=`LONG_TIME$text` / value=`LONG_TIME$value` / submit=`LONG_TIME`
- `SCAN_DATE`: text=`SCAN_DATE$text` / value=`SCAN_DATE$value` / submit=`SCAN_DATE`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=FAST_TYPE&_=%3CTS%3E`
  - `FIND_EMPLOYEE_ALL_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_TAB_WORK_ORDER_TYPE_REMIND` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=%3CTOKEN%3E`
- 本次 live 点击未拿到稳定主查询请求，通常是因为页面需要先录入单号、先选择页签，或查询逻辑在脚本内先做前置校验。
- 页面脚本提示接口：
  - `/dataQuery/findPageByCallId?id=FIND_EMPLOYEE_ALL_COMBOBOX`
  - `/dataQuery/findAllByCallId?id=FIND_COME_NO_DISP`
  - `/dataOperation/saveTables`
  - `/dataQuery/findAllByCallId?id=FIND_WAYBILL_SIGN_STATE`
  - `/dataQuery/findAllByCallId?id=FIND_TAB_UNITE_ORDERS`
  - `/dataQuery/findAllByCallId?id=FIND_TMS_SYS_SHARE_SET`

**Field Mapping**
- 用户输入参数：本次 live 查询没有稳定提取到，通常是页面先走前置校验、需要先录入单号，或查询不是默认页签。
- text/value 到提交字段：`LONG_TIME` -> `LONG_TIME`；`SCAN_DATE` -> `SCAN_DATE`；`FAST_TYPE` -> `FAST_TYPE`；`DISPATCH_OR_SEND_MAN_CODE` -> `DISPATCH_OR_SEND_MAN_CODE`

**State / Validation Logic**
- 查询入口：没有稳定点击到默认查询按钮，说明此页可能依赖录入单号、页签切换或前置校验。
- 点击绑定（从内联脚本截取）：
  - `addBtn -> uploadDataMethod()`
  - `deleteBtn -> deleteRow()`
- 保存/提交操作键：`TAB_SCAN_DISP_ADD`
- 前置校验/状态相关 CALL_ID：`FIND_WAYBILL_SIGN_STATE`

**Automation Notes**
- 更适合先走 DOM：当前没有稳定主查询请求，优先把页面交互顺序跑通。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=wlqNvDFGrUUtZtTgipe5IFaBuU8D3ogUrU2cGk` + 页面标题联合识别。

### 收件-发件扫描

**Page Identity**
- 菜单路径：`扫描管理 / 收件-发件扫描`
- 页面类型：叶子页面
- `pageId`：`7BCRKeoLRzD6yj4Vpk2j0goWN1ZcDTW95VkFch`
- 页面标题：`收件+发件扫描-速通`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`扫描内容`、`所属省份`、`扫描时间`、`快件类型`、`清单编号`、`扫描单号`、`下一站`、`收件员`、`备注`
- 首屏字段：`SCAN_DATE$text`、`FAST_TYPE$text`、`LISTING_CODE`、`BILL_CODE`、`PRE_OR_NEXT_STATION`、`DISPATCH_OR_SEND_MAN_CODE$text`、`REMARK`
- MiniUI text/value 对：`SCAN_DATE` -> text=`SCAN_DATE$text` / value=`SCAN_DATE$value` / submit=`SCAN_DATE`；`FAST_TYPE` -> text=`FAST_TYPE$text` / value=`FAST_TYPE$value` / submit=`FAST_TYPE`；`LISTING_CODE` -> text=`LISTING_CODE$text`；`BILL_CODE` -> text=`BILL_CODE$text`；`PRE_OR_NEXT_STATION_CODE` -> text=`PRE_OR_NEXT_STATION_CODE$text` / value=`PRE_OR_NEXT_STATION_CODE$value` / submit=`PRE_OR_NEXT_STATION_CODE`；`DISPATCH_OR_SEND_MAN_CODE` -> text=`DISPATCH_OR_SEND_MAN_CODE$text` / value=`DISPATCH_OR_SEND_MAN_CODE$value` / submit=`DISPATCH_OR_SEND_MAN_CODE`；`REMARK` -> text=`REMARK$text`
- 保存类按钮：`上传`、`上传`、`上传`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `上传`: `#addBtn` / `xpath=//*[@id="addBtn"]` / `xpath=//a[normalize-space(.)="上传"]`
- `上传`: `xpath=//*[normalize-space(.)="上传"]`
- `SCAN_DATE$text`: `#SCAN_DATE$text` / `xpath=//*[@id="SCAN_DATE$text"]`
- `FAST_TYPE$text`: `#FAST_TYPE$text` / `xpath=//*[@id="FAST_TYPE$text"]` / `xpath=//*[@placeholder="请选择"]`
- `LISTING_CODE`: `#LISTING_CODE$text` / `xpath=//*[@id="LISTING_CODE$text"]` / `xpath=//*[@name="LISTING_CODE"]`
- `SCAN_DATE`: text=`SCAN_DATE$text` / value=`SCAN_DATE$value` / submit=`SCAN_DATE`
- `FAST_TYPE`: text=`FAST_TYPE$text` / value=`FAST_TYPE$value` / submit=`FAST_TYPE`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=FAST_TYPE&_=%3CTS%3E`
  - `GET_SUPERIOR_SITE2_SEND_CODE` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=%3CTOKEN%3E`
  - `FIND_TAB_EMPLOYEE_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&OWNER_SITE_CODE=%3COWNER_SITE_CODE%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_SCAN_SEND_SITE_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&SUPERIOR_SITE2_SEND_CODE=%3CSUPERIOR_SITE2_SEND_CODE%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 本次 live 点击未拿到稳定主查询请求，通常是因为页面需要先录入单号、先选择页签，或查询逻辑在脚本内先做前置校验。
- 页面脚本提示接口：
  - `/dataQuery/findPageByCallId?id=FIND_TAB_EMPLOYEE_COMBOBOX&OWNER_SITE_CODE=`
  - `/dataQuery/findPageByCallId?id=FIND_SITE_ALL_COMBOBOX`
  - `/dataQuery/findPageByCallId?id=FIND_SCAN_SEND_SITE_DIST`
  - `/dataQuery/findAllByCallId?id=GET_SUPERIOR_SITE2_SEND_CODE`
  - `/dataQuery/findPageByCallId?id=FIND_SCAN_SEND_SITE_COMBOBOX&SUPERIOR_SITE2_SEND_CODE=`
  - `/dataQuery/findAllByCallId?id=FIND_WAYBILL_SIGN_STATE`
  - `/dataOperation/saveTables`
  - `/dataQuery/findAllByCallId?id=FIND_TMS_SYS_SHARE_SET`
  - `/dataQuery/findAllByCallId?id=FIND_SCAN_REC_BILL_CODE`
  - `/dataQuery/findAllByCallId?id=FIND_SCAN_SEND_BILL_CODE`

**Field Mapping**
- 用户输入参数：本次 live 查询没有稳定提取到，通常是页面先走前置校验、需要先录入单号，或查询不是默认页签。
- text/value 到提交字段：`SCAN_DATE` -> `SCAN_DATE`；`FAST_TYPE` -> `FAST_TYPE`；`PRE_OR_NEXT_STATION_CODE` -> `PRE_OR_NEXT_STATION_CODE`；`DISPATCH_OR_SEND_MAN_CODE` -> `DISPATCH_OR_SEND_MAN_CODE`

**State / Validation Logic**
- 查询入口：没有稳定点击到默认查询按钮，说明此页可能依赖录入单号、页签切换或前置校验。
- 点击绑定（从内联脚本截取）：
  - `addBtn -> addData();`
  - `deleteBtn -> deleteRow()`
- 保存/提交操作键：`TAB_SCAN_SEND_ADD`、`TAB_SCAN_REC_ADD`
- 前置校验/状态相关 CALL_ID：`FIND_WAYBILL_SIGN_STATE`

**Automation Notes**
- 更适合先走 DOM：当前没有稳定主查询请求，优先把页面交互顺序跑通。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=7BCRKeoLRzD6yj4Vpk2j0goWN1ZcDTW95VkFch` + 页面标题联合识别。

### 到件-派件扫描

**Page Identity**
- 菜单路径：`扫描管理 / 到件-派件扫描`
- 页面类型：叶子页面
- `pageId`：`9Ki8urrggOoNHya7qLrEhk3OSkCab2nbOKs8Og`
- 页面标题：`到件+派件扫描-速通`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`所属省份`、`扫描内容`、`扫描时间`、`快件类型`、`清单编号`、`扫描单号`、`上一站`、`派件员`、`备注`
- 首屏字段：`SCAN_DATE$text`、`FAST_TYPE$text`、`LISTING_CODE`、`BILL_CODE`、`PRE_OR_NEXT_STATION`、`DISPATCH_OR_SEND_MAN_CODE$text`、`REMARK`
- MiniUI text/value 对：`SCAN_DATE` -> text=`SCAN_DATE$text` / value=`SCAN_DATE$value` / submit=`SCAN_DATE`；`FAST_TYPE` -> text=`FAST_TYPE$text` / value=`FAST_TYPE$value` / submit=`FAST_TYPE`；`LISTING_CODE` -> text=`LISTING_CODE$text`；`BILL_CODE` -> text=`BILL_CODE$text`；`PRE_OR_NEXT_STATION_CODE` -> text=`PRE_OR_NEXT_STATION_CODE$text` / value=`PRE_OR_NEXT_STATION_CODE$value` / submit=`PRE_OR_NEXT_STATION_CODE`；`DISPATCH_OR_SEND_MAN_CODE` -> text=`DISPATCH_OR_SEND_MAN_CODE$text` / value=`DISPATCH_OR_SEND_MAN_CODE$value` / submit=`DISPATCH_OR_SEND_MAN_CODE`；`REMARK` -> text=`REMARK$text`
- 保存类按钮：`上传`、`上传`、`上传`、`上传-新`、`上传-新`、`上传-新`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `上传`: `#addBtn` / `xpath=//*[@id="addBtn"]` / `xpath=//a[normalize-space(.)="上传"]`
- `上传`: `xpath=//*[normalize-space(.)="上传"]`
- `SCAN_DATE$text`: `#SCAN_DATE$text` / `xpath=//*[@id="SCAN_DATE$text"]`
- `FAST_TYPE$text`: `#FAST_TYPE$text` / `xpath=//*[@id="FAST_TYPE$text"]` / `xpath=//*[@placeholder="请选择"]`
- `LISTING_CODE`: `#LISTING_CODE$text` / `xpath=//*[@id="LISTING_CODE$text"]` / `xpath=//*[@name="LISTING_CODE"]`
- `SCAN_DATE`: text=`SCAN_DATE$text` / value=`SCAN_DATE$value` / submit=`SCAN_DATE`
- `FAST_TYPE`: text=`FAST_TYPE$text` / value=`FAST_TYPE$value` / submit=`FAST_TYPE`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=FAST_TYPE&_=%3CTS%3E`
  - `GET_SUPERIOR_SITE2_DISP_CODE` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=%3CTOKEN%3E`
  - `FIND_SCAN_COME_SITE_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&SUPERIOR_SITE2_DISP_CODE=%3CSUPERIOR_SITE2_DISP_CODE%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_TAB_EMPLOYEE_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&OWNER_SITE_CODE=%3COWNER_SITE_CODE%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 本次 live 点击未拿到稳定主查询请求，通常是因为页面需要先录入单号、先选择页签，或查询逻辑在脚本内先做前置校验。
- 页面脚本提示接口：
  - `/dataQuery/findPageByCallId?id=FIND_TAB_EMPLOYEE_COMBOBOX&OWNER_SITE_CODE=`
  - `/dataQuery/findPageByCallId?id=FIND_SITE_ALL_COMBOBOX`
  - `/dataQuery/findPageByCallId?id=FIND_SCAN_COME_SITE_SEND`
  - `/dataQuery/findAllByCallId?id=GET_SUPERIOR_SITE2_DISP_CODE`
  - `/dataQuery/findPageByCallId?id=FIND_SCAN_COME_SITE_COMBOBOX&SUPERIOR_SITE2_DISP_CODE=`
  - `/dataQuery/findAllByCallId?id=FIND_WAYBILL_SIGN_STATE`
  - `/dataQuery/findAllByCallId?id=FIND_TAB_UNITE_ORDERS`
  - `/dataOperation/saveTables`
  - `/dataQuery/findAllByCallId?id=FIND_TMS_SYS_SHARE_SET`

**Field Mapping**
- 用户输入参数：本次 live 查询没有稳定提取到，通常是页面先走前置校验、需要先录入单号，或查询不是默认页签。
- text/value 到提交字段：`SCAN_DATE` -> `SCAN_DATE`；`FAST_TYPE` -> `FAST_TYPE`；`PRE_OR_NEXT_STATION_CODE` -> `PRE_OR_NEXT_STATION_CODE`；`DISPATCH_OR_SEND_MAN_CODE` -> `DISPATCH_OR_SEND_MAN_CODE`

**State / Validation Logic**
- 查询入口：没有稳定点击到默认查询按钮，说明此页可能依赖录入单号、页签切换或前置校验。
- 点击绑定（从内联脚本截取）：
  - `addBtn -> uploadDataMethod();`
  - `deleteBtn -> deleteRow()`
  - `Uplaod-New -> uploadDataMethod1();`
- 保存/提交操作键：`TAB_SCAN_DISP_ADD`、`TAB_SCAN_COME_ADD`
- 前置校验/状态相关 CALL_ID：`FIND_WAYBILL_SIGN_STATE`

**Automation Notes**
- 更适合先走 DOM：当前没有稳定主查询请求，优先把页面交互顺序跑通。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=9Ki8urrggOoNHya7qLrEhk3OSkCab2nbOKs8Og` + 页面标题联合识别。

### 扫描记录查询

**Page Identity**
- 菜单路径：`扫描管理 / 扫描记录查询`
- 页面类型：叶子页面
- `pageId`：`WQeAp6msHiQly2tCOFFqssneUw8eyScnVCGOjr`
- 页面标题：`扫描记录查询-速通`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`查询条件`、`运单编号`、`袋号/主单号`、`子单号`、`清单编号`、`转出单号`、`扫描类型`、`扫描时间`、`备注`、`过滤子单/回单`、`扫描站点`、`上/下一网点`
- 首屏字段：`searchOrderInput`、`SCAN_TYPE$text`、`SEARCH_DATE_RANGE$text`、`REMARK`、`SCAN_SITE_CODE$text`、`PRE_OR_NEXT_STATION_CODE$text`、`CLASSTYPE`、`DESTINATION_CODE$text`、`DISPATCH_OR_SEND_MAN_CODE$text`、`SCAN_MAN_CODE$text`、`SEND_SITE_CODE$text`、`AREA_NAME_SEND$text`
- MiniUI text/value 对：`searchOrderType` -> value=`searchOrderType$value` / submit=`searchOrderType`；`searchOrderInput` -> text=`searchOrderInput$text`；`SCAN_TYPE` -> text=`SCAN_TYPE$text` / value=`SCAN_TYPE$value` / submit=`SCAN_TYPE`；`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`SEARCH_DATE_RANGE` -> text=`SEARCH_DATE_RANGE$text` / value=`SEARCH_DATE_RANGE$value` / submit=`SEARCH_DATE_RANGE`；`REMARK` -> text=`REMARK$text`；`BL_SUB_RECEIPT` -> value=`BL_SUB_RECEIPT$value` / submit=`BL_SUB_RECEIPT`；`SCAN_SITE_CODE` -> text=`SCAN_SITE_CODE$text` / value=`SCAN_SITE_CODE$value` / submit=`SCAN_SITE_CODE`
- 查询按钮：`查询`、`查询`、`查询`
- 导出类按钮：`导出`、`导出`、`导出`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `查询`: `xpath=//*[normalize-space(.)="查询"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `SCAN_TYPE$text`: `#SCAN_TYPE$text` / `xpath=//*[@id="SCAN_TYPE$text"]` / `xpath=//*[@placeholder="请选择"]`
- `SEARCH_DATE_RANGE$text`: `#SEARCH_DATE_RANGE$text` / `xpath=//*[@id="SEARCH_DATE_RANGE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `searchOrderType`: value=`searchOrderType$value` / submit=`searchOrderType`
- `searchOrderInput`: text=`searchOrderInput$text`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=TAB_SCAN_TYPE&_=%3CTS%3E`
  - `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_ALL_SITE_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_ALL_SITE_COMBOBOX&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_DESTINATION_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&BL_OPEN=1&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_TAB_EMPLOYEE_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_SITE_ALL_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_SITE_ALL_COMBOBOX&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_AREA_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_AREA_COMBOBOX&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_SCAN_ALL_BYBILLCODE`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- 请求方法：`POST`
- 主查询参数键：`searchOrderType`、`searchOrderInput`、`SCAN_TYPE`、`searchDateType`、`SEARCH_DATE_RANGE`、`REMARK`、`BL_SUB_RECEIPT`、`SCAN_SITE_CODE`、`PRE_OR_NEXT_STATION_CODE`、`CLASSTYPE`、`DESTINATION_CODE`、`DISPATCH_OR_SEND_MAN_CODE`、`SCAN_MAN_CODE`、`SEND_SITE_CODE`、`AREA_NAME_SEND`、`AREA_NAME_DISP`、`SEND_CENTER_CODE`、`DISP_CENTER_CODE`、`SCAN_DATE`、`LOGIN_SITE_CODE`
- 点击查询后额外请求：
  - `FIND_SCAN_ALL_BYBILLCODE` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- 页面脚本提示接口：
  - `/dataQuery/findPageByCallId?id=FIND_REC_SCAN_RECORD`
  - `/dataQuery/findPageByCallId?id=FIND_SEND_SCAN_RECORD`
  - `/dataQuery/findPageByCallId?id=FIND_COME_SCAN_RECORD`
  - `/dataQuery/findPageByCallId?id=FIND_DISP_SCAN_RECORD`
  - `/dataQuery/findPageByCallId?id=FIND_OTHER_SCAN_RECORD`
  - `/dataQuery/findPageByCallId?id=FIND_SCAN_ALL_BYBILLCODE`
  - `/dataQuery/findAllByCallId?id=FIND_BILL_TYPE`
  - `/dataOperation/saveTables`

**Field Mapping**
- 用户输入参数：`searchOrderType`、`searchOrderInput`、`SCAN_TYPE`、`searchDateType`、`SEARCH_DATE_RANGE`、`REMARK`、`BL_SUB_RECEIPT`、`SCAN_SITE_CODE`、`PRE_OR_NEXT_STATION_CODE`、`CLASSTYPE`、`DESTINATION_CODE`、`DISPATCH_OR_SEND_MAN_CODE`、`SCAN_MAN_CODE`、`SEND_SITE_CODE`、`AREA_NAME_SEND`、`AREA_NAME_DISP`、`SEND_CENTER_CODE`、`DISP_CENTER_CODE`
- 页面自动补齐参数：`SCAN_DATE`
- 登录/站点上下文参数：`LOGIN_SITE_CODE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`searchOrderType` -> `searchOrderType`；`SCAN_TYPE` -> `SCAN_TYPE`；`searchDateType` -> `searchDateType`；`SEARCH_DATE_RANGE` -> `SEARCH_DATE_RANGE`；`BL_SUB_RECEIPT` -> `BL_SUB_RECEIPT`；`SCAN_SITE_CODE` -> `SCAN_SITE_CODE`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `clearBillCode -> //清空查询单号 mini.get("searchOrderInput").setValue("");`
  - `searchBtn -> searchMethod()`
  - `exportBtn -> exportMethod()`
- 保存/提交操作键：`TAB_SCAN_OTHER_DEL`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=WQeAp6msHiQly2tCOFFqssneUw8eyScnVCGOjr` + 页面标题联合识别。

### 到件预报查询

**Page Identity**
- 菜单路径：`扫描管理 / 到件预报查询`
- 页面类型：叶子页面
- `pageId`：`BwdUddm7vDXnYLYLCE3P2XHtVVcA3UBs6UNMVI`
- 页面标题：`到件预报查询-速通`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`查询条件`、`查询时间范围`、`扫描时间`、`上一网点`、`寄件网点`、`订单来源`、`目的地`、`去掉重复`、`线路类型`
- 首屏字段：`SEARCH_DATE_RANGE$text`、`PRE_OR_NEXT_STATION_CODE$text`、`SEND_SITE_CODE$text`、`ORDER_TYPE$text`、`DESTINATION_CODE$text`、`SITE_TYPE$text`
- MiniUI text/value 对：`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`SEARCH_DATE_RANGE` -> text=`SEARCH_DATE_RANGE$text` / value=`SEARCH_DATE_RANGE$value` / submit=`SEARCH_DATE_RANGE`；`PRE_OR_NEXT_STATION_CODE` -> text=`PRE_OR_NEXT_STATION_CODE$text` / value=`PRE_OR_NEXT_STATION_CODE$value` / submit=`PRE_OR_NEXT_STATION_CODE`；`SEND_SITE_CODE` -> text=`SEND_SITE_CODE$text` / value=`SEND_SITE_CODE$value` / submit=`SEND_SITE_CODE`；`ORDER_TYPE` -> text=`ORDER_TYPE$text` / value=`ORDER_TYPE$value` / submit=`ORDER_TYPE`；`DESTINATION_CODE` -> text=`DESTINATION_CODE$text` / value=`DESTINATION_CODE$value` / submit=`DESTINATION_CODE`；`BL_REPEAT` -> value=`BL_REPEAT$value` / submit=`BL_REPEAT`；`SITE_TYPE` -> text=`SITE_TYPE$text` / value=`SITE_TYPE$value` / submit=`SITE_TYPE`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`
- 导出类按钮：`导出`、`导出`、`导出`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `SEARCH_DATE_RANGE$text`: `#SEARCH_DATE_RANGE$text` / `xpath=//*[@id="SEARCH_DATE_RANGE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `PRE_OR_NEXT_STATION_CODE$text`: `#PRE_OR_NEXT_STATION_CODE$text` / `xpath=//*[@id="PRE_OR_NEXT_STATION_CODE$text"]`
- `SEND_SITE_CODE$text`: `#SEND_SITE_CODE$text` / `xpath=//*[@id="SEND_SITE_CODE$text"]`
- `searchDateType`: value=`searchDateType$value` / submit=`searchDateType`
- `SEARCH_DATE_RANGE`: text=`SEARCH_DATE_RANGE$text` / value=`SEARCH_DATE_RANGE$value` / submit=`SEARCH_DATE_RANGE`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_ALL_SITE_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_ALL_SITE_COMBOBOX&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_SCAN_SEND_NO_COME`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_SCAN_SEND_NO_COME`
- 请求方法：`POST`
- 主查询参数键：`searchDateType`、`SEARCH_DATE_RANGE`、`PRE_OR_NEXT_STATION_CODE`、`SEND_SITE_CODE`、`ORDER_TYPE`、`DESTINATION_CODE`、`BL_REPEAT`、`SITE_TYPE`、`SCAN_DATE`、`LOGIN_SITE_CODE`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIND_SCAN_SEND_NO_COME` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_SCAN_SEND_NO_COME`
- 页面脚本提示接口：
  - `/dataQuery/findPageByCallId?id=FIND_SCAN_SEND_NO_COME`
  - `/dataQuery/findPageByCallId?id=FIND_SCAN_SEND_NO_COME_DISTINCT`

**Field Mapping**
- 用户输入参数：`searchDateType`、`SEARCH_DATE_RANGE`、`PRE_OR_NEXT_STATION_CODE`、`SEND_SITE_CODE`、`ORDER_TYPE`、`DESTINATION_CODE`、`BL_REPEAT`、`SITE_TYPE`
- 页面自动补齐参数：`SCAN_DATE`
- 登录/站点上下文参数：`LOGIN_SITE_CODE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`searchDateType` -> `searchDateType`；`SEARCH_DATE_RANGE` -> `SEARCH_DATE_RANGE`；`PRE_OR_NEXT_STATION_CODE` -> `PRE_OR_NEXT_STATION_CODE`；`SEND_SITE_CODE` -> `SEND_SITE_CODE`；`ORDER_TYPE` -> `ORDER_TYPE`；`DESTINATION_CODE` -> `DESTINATION_CODE`；`BL_REPEAT` -> `BL_REPEAT`；`SITE_TYPE` -> `SITE_TYPE`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `searchBtn -> searchMethod();`
  - `exportBtn -> exportMethod();`
- 前置校验/状态相关 CALL_ID：`FIND_SCAN_SEND_NO_COME`、`FIND_SCAN_SEND_NO_COME_DISTINCT`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 页面识别建议：优先用 `pageId=BwdUddm7vDXnYLYLCE3P2XHtVVcA3UBs6UNMVI` + 页面标题联合识别。

### 派件预报

**Page Identity**
- 菜单路径：`扫描管理 / 派件预报`
- 页面类型：叶子页面
- `pageId`：`etE5MW4NMTCrfM9MrDgpxtkhECdbmXbCZX2Ndx`
- 页面标题：`派件预报-速通`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`打印模板`、`查询条件`、`按运单查询`、`查询时间范围`、`扫描时间`、`派件网点`、`发件中心`、`订单来源`、`中心到件预报`、`中心发件预报`、`网点到件预报`、`网点录单预报`
- 首屏字段：`searchOrderInput`、`SEARCH_DATE_RANGE$text`、`PRE_OR_NEXT_STATION_CODE$text`、`SCAN_SITE_CODE$text`、`PRINT_TYPE_VIEW2$text`、`ORDER_TYPE$text`、`WAITNOTIFY_SEND$text`、`WAITNOTIFY_SEND_STATUS$text`
- MiniUI text/value 对：`PRINT_TYPE_VIEW` -> text=`PRINT_TYPE_VIEW$text` / value=`PRINT_TYPE_VIEW$value` / submit=`PRINT_TYPE_VIEW`；`CODE_TYPE` -> value=`CODE_TYPE$value` / submit=`CODE_TYPE`；`searchOrderInput` -> text=`searchOrderInput$text`；`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`SEARCH_DATE_RANGE` -> text=`SEARCH_DATE_RANGE$text` / value=`SEARCH_DATE_RANGE$value` / submit=`SEARCH_DATE_RANGE`；`PRE_OR_NEXT_STATION_CODE` -> text=`PRE_OR_NEXT_STATION_CODE$text` / value=`PRE_OR_NEXT_STATION_CODE$value` / submit=`PRE_OR_NEXT_STATION_CODE`；`SCAN_SITE_CODE` -> text=`SCAN_SITE_CODE$text` / value=`SCAN_SITE_CODE$value` / submit=`SCAN_SITE_CODE`；`PRINT_TYPE_VIEW2` -> text=`PRINT_TYPE_VIEW2$text` / value=`PRINT_TYPE_VIEW2$value` / submit=`PRINT_TYPE_VIEW2`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`
- 保存类按钮：`打印模板`、`打印`、`打印`、`打印`、`打印预览`、`打印预览`、`打印预览`
- 导出类按钮：`导出`、`导出`、`导出`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `打印模板`: `xpath=//*[normalize-space(.)="打印模板"]`
- `打印`: `#printBtn` / `xpath=//*[@id="printBtn"]` / `xpath=//a[normalize-space(.)="打印"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `SEARCH_DATE_RANGE$text`: `#SEARCH_DATE_RANGE$text` / `xpath=//*[@id="SEARCH_DATE_RANGE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `PRE_OR_NEXT_STATION_CODE$text`: `#PRE_OR_NEXT_STATION_CODE$text` / `xpath=//*[@id="PRE_OR_NEXT_STATION_CODE$text"]`
- `PRINT_TYPE_VIEW`: text=`PRINT_TYPE_VIEW$text` / value=`PRINT_TYPE_VIEW$value` / submit=`PRINT_TYPE_VIEW`
- `CODE_TYPE`: value=`CODE_TYPE$value` / submit=`CODE_TYPE`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `FIND_DISPATCH_FORECAST_BILL` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_SITE_ALL_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_SITE_ALL_COMBOBOX&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_SITE_CENTER_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_DISPATCH_FORECAST_CENTER`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- 请求方法：`POST`
- 主查询参数键：`PRINT_TYPE_VIEW`、`CODE_TYPE`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`PRE_OR_NEXT_STATION_CODE`、`SCAN_SITE_CODE`、`PRINT_TYPE_VIEW2`、`ORDER_TYPE`、`FORECAST`、`WAITNOTIFY_SEND`、`WAITNOTIFY_SEND_STATUS`、`BL_VIP`、`EXCLUDE_RETURNBILL`、`encryption`、`isViewLogo`、`freight_type`、`SCAN_DATE`、`LOGIN_SITE_CODE`、`pageIndex`
- 点击查询后额外请求：
  - `FIND_DISPATCH_FORECAST_CENTER` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_DISPATCH_FORECAST_BILL`
- 页面脚本提示接口：
  - `//直接打印时printCount返回的是true，阅览打印时printCount返回的是打印次数`
  - `/dataQuery/findPageByCallId?id=FIND_DISPATCH_FORECAST_CENTER`
  - `/dataQuery/findPageByCallId?id=FIND_DISPATCH_FORECAST_COME`
  - `/dataQuery/findPageByCallId?id=FIND_DISPATCH_FORECAST_COME1`
  - `/dataQuery/findPageByCallId?id=FIND_DISPATCH_FORECAST_BILL`
  - `/dataQuery/findAllByCallId?id=FIND_PRINT_TEMPLATE`
  - `/dataOperation/saveTables`
  - `//LODOP.SET_PRINT_STYLEA(0,`
  - `/dataQuery/findAllByCallId?id=FIND_PRINT_LOG_COUNT&BILL_CODE=`

**Field Mapping**
- 用户输入参数：`PRINT_TYPE_VIEW`、`CODE_TYPE`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`PRE_OR_NEXT_STATION_CODE`、`SCAN_SITE_CODE`、`PRINT_TYPE_VIEW2`、`ORDER_TYPE`、`FORECAST`、`WAITNOTIFY_SEND`、`WAITNOTIFY_SEND_STATUS`、`BL_VIP`、`EXCLUDE_RETURNBILL`、`encryption`、`isViewLogo`、`freight_type`
- 页面自动补齐参数：`SCAN_DATE`
- 登录/站点上下文参数：`LOGIN_SITE_CODE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`PRINT_TYPE_VIEW` -> `PRINT_TYPE_VIEW`；`CODE_TYPE` -> `CODE_TYPE`；`searchDateType` -> `searchDateType`；`SEARCH_DATE_RANGE` -> `SEARCH_DATE_RANGE`；`PRE_OR_NEXT_STATION_CODE` -> `PRE_OR_NEXT_STATION_CODE`；`SCAN_SITE_CODE` -> `SCAN_SITE_CODE`；`PRINT_TYPE_VIEW2` -> `PRINT_TYPE_VIEW2`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `searchBtn -> searchMethod();`
  - `exportBtn -> exportFun();`
  - `printBtn -> printMethodNew(1);`
  - `zoomoutBtn -> printMethodNew(2);`
- 保存/提交操作键：`TAB_PRINT_LOG_ADD`、`TAB_PRINT_COUNT_ADD`
- 前置校验/状态相关 CALL_ID：`FIND_PRINT_LOG_COUNT`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=etE5MW4NMTCrfM9MrDgpxtkhECdbmXbCZX2Ndx` + 页面标题联合识别。

### 收件清单

**Page Identity**
- 菜单路径：`扫描管理 / 收件清单`
- 页面类型：叶子页面
- `pageId`：`RVD6fSecRITQjPHnyPj40PKv6K0f4KKttOnUB8`
- 页面标题：`收件清单-速通`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`查询条件`、`按运单查询`、`查询时间范围`、`扫描时间`、`总件数`、`扫描网点`、`上一网点`、`总重量`、`快件类型`、`查询结果过滤子单`、`子单总重量`
- 首屏字段：`searchOrderInput`、`SEARCH_DATE_RANGE$text`、`SUM_PIC`、`SCAN_SITE_CODE$text`、`PRE_OR_NEXT_STATION_CODE$text`、`SUM_W`、`FAST_TYPE$text`、`SUM_SUB_W`
- MiniUI text/value 对：`searchOrderType` -> value=`searchOrderType$value` / submit=`searchOrderType`；`searchOrderInput` -> text=`searchOrderInput$text`；`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`SEARCH_DATE_RANGE` -> text=`SEARCH_DATE_RANGE$text` / value=`SEARCH_DATE_RANGE$value` / submit=`SEARCH_DATE_RANGE`；`SUM_PIC` -> text=`SUM_PIC$text`；`SCAN_SITE_CODE` -> text=`SCAN_SITE_CODE$text` / value=`SCAN_SITE_CODE$value` / submit=`SCAN_SITE_CODE`；`PRE_OR_NEXT_STATION_CODE` -> text=`PRE_OR_NEXT_STATION_CODE$text` / value=`PRE_OR_NEXT_STATION_CODE$value` / submit=`PRE_OR_NEXT_STATION_CODE`；`SUM_W` -> text=`SUM_W$text`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`
- 导出类按钮：`导出`、`导出`、`导出`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `SEARCH_DATE_RANGE$text`: `#SEARCH_DATE_RANGE$text` / `xpath=//*[@id="SEARCH_DATE_RANGE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `SUM_PIC`: `#SUM_PIC$text` / `xpath=//*[@id="SUM_PIC$text"]` / `xpath=//*[@name="SUM_PIC"]`
- `searchOrderType`: value=`searchOrderType$value` / submit=`searchOrderType`
- `searchOrderInput`: text=`searchOrderInput$text`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=FAST_TYPE&_=%3CTS%3E`
  - `FIND_SCAN_REC_LIST` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_SITE_ALL_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_SITE_ALL_COMBOBOX&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_MAIN_SUB_SITE_COMBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&LOGIN_SITE_CODE=&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_SCAN_REC_LIST_SUM`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=FIND_SCAN_REC_LIST_SUM`
- 请求方法：`POST`
- 主查询参数键：`searchOrderType`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`SUM_PIC`、`SCAN_SITE_CODE`、`PRE_OR_NEXT_STATION_CODE`、`SUM_W`、`FAST_TYPE`、`BL_SUB_BILL`、`SUM_SUB_W`、`SCAN_DATE`、`LOGIN_SITE_CODE`、`pageIndex`
- 点击查询后额外请求：
  - `FIND_SCAN_REC_LIST_SUM` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=FIND_SCAN_REC_LIST_SUM`
  - `FIND_SCAN_REC_LIST` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_SCAN_REC_LIST`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_SCAN_REC_LIST`
- 页面脚本提示接口：
  - `/dataQuery/findPageByCallId?id=FIND_MAIN_SUB_SITE_COMBOX&LOGIN_SITE_CODE`
  - `/dataQuery/findAllByCallId?id=FIND_SCAN_REC_LIST_SUM`

**Field Mapping**
- 用户输入参数：`searchOrderType`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`SUM_PIC`、`SCAN_SITE_CODE`、`PRE_OR_NEXT_STATION_CODE`、`SUM_W`、`FAST_TYPE`、`BL_SUB_BILL`、`SUM_SUB_W`
- 页面自动补齐参数：`SCAN_DATE`
- 登录/站点上下文参数：`LOGIN_SITE_CODE`
- 分页技术参数：`pageIndex`
- text/value 到提交字段：`searchOrderType` -> `searchOrderType`；`searchDateType` -> `searchDateType`；`SEARCH_DATE_RANGE` -> `SEARCH_DATE_RANGE`；`SCAN_SITE_CODE` -> `SCAN_SITE_CODE`；`PRE_OR_NEXT_STATION_CODE` -> `PRE_OR_NEXT_STATION_CODE`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `deleteList -> //清空查询单号 mini.get("searchOrderInput").setValue("");`
  - `searchBtn -> searchMethod()`
  - `exportBtn -> exportMethod();`
- 前置校验/状态相关 CALL_ID：`FIND_SCAN_REC_LIST_SUM`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 页面识别建议：优先用 `pageId=RVD6fSecRITQjPHnyPj40PKv6K0f4KKttOnUB8` + 页面标题联合识别。

### 发件清单

**Page Identity**
- 菜单路径：`扫描管理 / 发件清单`
- 页面类型：叶子页面
- `pageId`：`topn53wYMMASGjg0St7v9bAcOR0ygeu6eazCuO`
- 页面标题：`发件清单-速通`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`查询条件`、`按运单查询`、`查询时间范围`、`扫描时间`、`查主单记录`、`过滤重复单号`、`仅查重复`、`总件数`、`扫描员`、`下一网点`、`是否录单`、`查询结果过滤回单`
- 首屏字段：`searchOrderInput`、`SEARCH_DATE_RANGE$text`、`SUM_PIC`、`SCAN_MAN_CODE$text`、`PRE_OR_NEXT_STATION_CODE$text`、`BL_REGISTER$text`、`SUM_W`、`PRODUCT_CODE$text`、`OPERATE_CODE$text`、`SCAN_SITE_CODE$text`、`CLASS$text`、`LISTING_CODE`
- MiniUI text/value 对：`searchOrderType` -> value=`searchOrderType$value` / submit=`searchOrderType`；`searchOrderInput` -> text=`searchOrderInput$text`；`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`SEARCH_DATE_RANGE` -> text=`SEARCH_DATE_RANGE$text` / value=`SEARCH_DATE_RANGE$value` / submit=`SEARCH_DATE_RANGE`；`BL_MAIN_BILL` -> value=`BL_MAIN_BILL$value` / submit=`BL_MAIN_BILL`；`BL_REPEAT` -> value=`BL_REPEAT$value` / submit=`BL_REPEAT`；`BL_NOT_REPEAT` -> value=`BL_NOT_REPEAT$value` / submit=`BL_NOT_REPEAT`；`SUM_PIC` -> text=`SUM_PIC$text`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`
- 导出类按钮：`导出`、`导出`、`导出`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `SEARCH_DATE_RANGE$text`: `#SEARCH_DATE_RANGE$text` / `xpath=//*[@id="SEARCH_DATE_RANGE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `SUM_PIC`: `#SUM_PIC$text` / `xpath=//*[@id="SUM_PIC$text"]` / `xpath=//*[@name="SUM_PIC"]`
- `searchOrderType`: value=`searchOrderType$value` / submit=`searchOrderType`
- `searchOrderInput`: text=`searchOrderInput$text`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `FIND_PRODUCT_TYPE` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=FIND_PRODUCT_TYPE&_=%3CTS%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=FAST_TYPE&_=%3CTS%3E`
  - `FIND_SCAN_SEND_LIST_NEW` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_TAB_EMPLOYEE_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&OWNER_SITE_CODE=%3COWNER_SITE_CODE%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_TAB_EMPLOYEE_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_SITE_ALL_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_SITE_ALL_COMBOBOX&SUPERIOR_SITE_CODE=%3CSUPERIOR_SITE_CODE%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_SITE_ALL_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_SITE_ALL_COMBOBOX&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_SCAN_SEND_LIST_NEW`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_SCAN_SEND_LIST_NEW`
- 请求方法：`POST`
- 主查询参数键：`searchOrderType`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`BL_MAIN_BILL`、`BL_REPEAT`、`BL_NOT_REPEAT`、`SUM_PIC`、`SCAN_MAN_CODE`、`PRE_OR_NEXT_STATION_CODE`、`BL_REGISTER`、`R_BILLCODE`、`BL_SUB_BILL`、`SUM_W`、`PRODUCT_CODE`、`OPERATE_CODE`、`SCAN_SITE_CODE`、`CLASS`、`LISTING_CODE`、`BL_SIGNS_MARKING`
- 点击查询后额外请求：
  - `FIND_SCAN_SEND_LIST_NEW` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_SCAN_SEND_LIST_NEW`
  - `FIND_SCAN_SEND_LIST_SUM` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=FIND_SCAN_SEND_LIST_SUM`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_SCAN_SEND_LIST_NEW`
- 页面脚本提示接口：
  - `/dataQuery/findPageByCallId?id=FIND_MAIN_SUB_SITE_COMBOX`
  - `/dataQuery/findPageByCallId?id=FIND_SITE_ALL_COMBOBOX`
  - `/dataQuery/findPageByCallId?id=FIND_TAB_EMPLOYEE_COMBOBOX&OWNER_SITE_CODE=`
  - `/dataQuery/findPageByCallId?id=FIND_SITE_ALL_COMBOBOX&SUPERIOR_SITE_CODE=`
  - `/dataQuery/findPageByCallId?id=FIND_SCAN_SEND_LIST_NEW`
  - `/dataQuery/findPageByCallId?id=FIND_SCAN_SEND_LIST_DISTINCT`
  - `/dataQuery/findAllByCallId?id=FIND_SCAN_SEND_LIST_SUM`

**Field Mapping**
- 用户输入参数：`searchOrderType`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`BL_MAIN_BILL`、`BL_REPEAT`、`BL_NOT_REPEAT`、`SUM_PIC`、`SCAN_MAN_CODE`、`PRE_OR_NEXT_STATION_CODE`、`BL_REGISTER`、`R_BILLCODE`、`BL_SUB_BILL`、`SUM_W`、`PRODUCT_CODE`、`OPERATE_CODE`、`SCAN_SITE_CODE`、`CLASS`、`LISTING_CODE`、`BL_SIGNS_MARKING`、`SUM_SUB_W`
- 页面自动补齐参数：`SCAN_DATE`
- 登录/站点上下文参数：`LOGIN_SITE_CODE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`searchOrderType` -> `searchOrderType`；`searchDateType` -> `searchDateType`；`SEARCH_DATE_RANGE` -> `SEARCH_DATE_RANGE`；`BL_MAIN_BILL` -> `BL_MAIN_BILL`；`BL_REPEAT` -> `BL_REPEAT`；`BL_NOT_REPEAT` -> `BL_NOT_REPEAT`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `searchBtn -> searchMethod()`
  - `exportBtn -> exportMethod();`
- 前置校验/状态相关 CALL_ID：`FIND_SCAN_SEND_LIST_SUM`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 页面识别建议：优先用 `pageId=topn53wYMMASGjg0St7v9bAcOR0ygeu6eazCuO` + 页面标题联合识别。

### 到件清单

**Page Identity**
- 菜单路径：`扫描管理 / 到件清单`
- 页面类型：叶子页面
- `pageId`：`mutOV8kYaRxJ4dYmXSgqNkBX3IQd76ljNKowGH`
- 页面标题：`到件清单-速通`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`查询条件`、`按运单查询`、`查询时间范围`、`扫描时间`、`查主单记录`、`过滤重复单号`、`总票数`、`扫描网点`、`上一网点`、`快件类型`、`查询结果过滤回单`、`查子单记录`
- 首屏字段：`searchOrderInput`、`SEARCH_DATE_RANGE$text`、`SUM_PIC`、`SCAN_SITE_CODE$text`、`PRE_OR_NEXT_STATION_CODE$text`、`CLASS$text`、`SUM_W`、`BL_REGISTER$text`、`SCAN_MAN_CODE$text`、`OPERATE_CODE$text`、`LISTING_CODE`、`SUM_SUB_W`
- MiniUI text/value 对：`searchOrderType` -> value=`searchOrderType$value` / submit=`searchOrderType`；`searchOrderInput` -> text=`searchOrderInput$text`；`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`SEARCH_DATE_RANGE` -> text=`SEARCH_DATE_RANGE$text` / value=`SEARCH_DATE_RANGE$value` / submit=`SEARCH_DATE_RANGE`；`BL_MAIN_BILL` -> value=`BL_MAIN_BILL$value` / submit=`BL_MAIN_BILL`；`BL_REPEAT` -> value=`BL_REPEAT$value` / submit=`BL_REPEAT`；`SUM_PIC` -> text=`SUM_PIC$text`；`SCAN_SITE_CODE` -> text=`SCAN_SITE_CODE$text` / value=`SCAN_SITE_CODE$value` / submit=`SCAN_SITE_CODE`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`
- 导出类按钮：`导出`、`导出`、`导出`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `SEARCH_DATE_RANGE$text`: `#SEARCH_DATE_RANGE$text` / `xpath=//*[@id="SEARCH_DATE_RANGE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `SUM_PIC`: `#SUM_PIC$text` / `xpath=//*[@id="SUM_PIC$text"]` / `xpath=//*[@name="SUM_PIC"]`
- `searchOrderType`: value=`searchOrderType$value` / submit=`searchOrderType`
- `searchOrderInput`: text=`searchOrderInput$text`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=FAST_TYPE&_=%3CTS%3E`
  - `FIND_PRODUCT_TYPE` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=FIND_PRODUCT_TYPE&_=%3CTS%3E`
  - `FIND_SCAN_COME_LIST_NEW` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_SITE_ALL_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_SITE_ALL_COMBOBOX&SUPERIOR_SITE_CODE=%3CSUPERIOR_SITE_CODE%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_SITE_ALL_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_SITE_ALL_COMBOBOX&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_TAB_EMPLOYEE_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&OWNER_SITE_CODE=%3COWNER_SITE_CODE%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_TAB_EMPLOYEE_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_SCAN_COME_LIST_SUM`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=FIND_SCAN_COME_LIST_SUM`
- 请求方法：`POST`
- 主查询参数键：`searchOrderType`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`BL_MAIN_BILL`、`BL_REPEAT`、`SUM_PIC`、`SCAN_SITE_CODE`、`PRE_OR_NEXT_STATION_CODE`、`CLASS`、`R_BILLCODE`、`BL_SUB_BILL`、`SUM_W`、`BL_REGISTER`、`SCAN_MAN_CODE`、`OPERATE_CODE`、`LISTING_CODE`、`SUM_SUB_W`、`PRODUCT_CODE`、`BL_VIP`
- 点击查询后额外请求：
  - `FIND_SCAN_COME_LIST_SUM` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=FIND_SCAN_COME_LIST_SUM`
  - `FIND_TAB_BILL_TOTAL_WEIGHT` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=%3CTOKEN%3E`
  - `FIND_SCAN_COME_LIST_NEW` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_SCAN_COME_LIST_NEW`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_SCAN_COME_LIST_NEW`
- 页面脚本提示接口：
  - `/dataQuery/findPageByCallId?id=FIND_TAB_EMPLOYEE_COMBOBOX&OWNER_SITE_CODE=`
  - `/dataQuery/findPageByCallId?id=FIND_SITE_ALL_COMBOBOX&SUPERIOR_SITE_CODE=`
  - `/dataQuery/findPageByCallId?id=FIND_SCAN_COME_LIST_NEW`
  - `/dataQuery/findPageByCallId?id=FIND_SCAN_COME_LIST_DISTINCT`
  - `/dataQuery/findAllByCallId?id=FIND_SCAN_COME_LIST_SUM`
  - `/dataQuery/findAllByCallId?id=FIND_TAB_BILL_TOTAL_WEIGHT`

**Field Mapping**
- 用户输入参数：`searchOrderType`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`BL_MAIN_BILL`、`BL_REPEAT`、`SUM_PIC`、`SCAN_SITE_CODE`、`PRE_OR_NEXT_STATION_CODE`、`CLASS`、`R_BILLCODE`、`BL_SUB_BILL`、`SUM_W`、`BL_REGISTER`、`SCAN_MAN_CODE`、`OPERATE_CODE`、`LISTING_CODE`、`SUM_SUB_W`、`PRODUCT_CODE`、`BL_VIP`
- 页面自动补齐参数：`SCAN_DATE`
- 登录/站点上下文参数：`LOGIN_SITE_CODE`
- 分页技术参数：`pageIndex`
- text/value 到提交字段：`searchOrderType` -> `searchOrderType`；`searchDateType` -> `searchDateType`；`SEARCH_DATE_RANGE` -> `SEARCH_DATE_RANGE`；`BL_MAIN_BILL` -> `BL_MAIN_BILL`；`BL_REPEAT` -> `BL_REPEAT`；`SCAN_SITE_CODE` -> `SCAN_SITE_CODE`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `deleteList -> //清空查询单号 mini.get("searchOrderInput").setValue("");`
  - `searchBtn -> searchMethod()`
  - `exportBtn -> exportMethod();`
- 前置校验/状态相关 CALL_ID：`FIND_SCAN_COME_LIST_SUM`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 页面识别建议：优先用 `pageId=mutOV8kYaRxJ4dYmXSgqNkBX3IQd76ljNKowGH` + 页面标题联合识别。

### 留仓件清单

**Page Identity**
- 菜单路径：`扫描管理 / 留仓件清单`
- 页面类型：叶子页面
- `pageId`：`vRlJ5Z6Nbes1U2LVFDMhROmovQi2kcD0WigVM4`
- 页面标题：`留仓扫描清单new`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`查询条件`、`按运单查询`、`查询时间范围`、`扫描时间`、`总件数`、`扫描网点`、`快件类型`、`查询过滤回单`、`查子单记录`、`总重量`、`是否录单`、`扫描员`
- 首屏字段：`searchOrderInput`、`SEARCH_DATE_RANGE$text`、`SUM_PIC`、`SCAN_SITE_CODE$text`、`CLASS$text`、`SUM_W`、`BL_REGISTER$text`、`SCAN_MAN_CODE$text`、`SUM_SUB_W`
- MiniUI text/value 对：`searchOrderType` -> value=`searchOrderType$value` / submit=`searchOrderType`；`searchOrderInput` -> text=`searchOrderInput$text`；`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`SEARCH_DATE_RANGE` -> text=`SEARCH_DATE_RANGE$text` / value=`SEARCH_DATE_RANGE$value` / submit=`SEARCH_DATE_RANGE`；`SUM_PIC` -> text=`SUM_PIC$text`；`SCAN_SITE_CODE` -> text=`SCAN_SITE_CODE$text` / value=`SCAN_SITE_CODE$value` / submit=`SCAN_SITE_CODE`；`CLASS` -> text=`CLASS$text` / value=`CLASS$value` / submit=`CLASS`；`R_BILLCODE` -> value=`R_BILLCODE$value` / submit=`R_BILLCODE`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`
- 导出类按钮：`导出`、`导出`、`导出`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `SEARCH_DATE_RANGE$text`: `#SEARCH_DATE_RANGE$text` / `xpath=//*[@id="SEARCH_DATE_RANGE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `SUM_PIC`: `#SUM_PIC$text` / `xpath=//*[@id="SUM_PIC$text"]` / `xpath=//*[@name="SUM_PIC"]`
- `searchOrderType`: value=`searchOrderType$value` / submit=`searchOrderType`
- `searchOrderInput`: text=`searchOrderInput$text`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=FAST_TYPE&_=%3CTS%3E`
  - `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_SITE_ALL_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_SITE_ALL_COMBOBOX&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_TAB_EMPLOYEE_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `https://tms.ronghuiwl.com/dataOperation/execProcedure`
  - `FIND_TAB_WORK_ORDER_TYPE_REMIND` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=%3CTOKEN%3E`
- 主查询接口：`FIND_SCAN_LEAVE_LIST_SUM`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=%3CTOKEN%3E`
- 请求方法：`POST`
- 主查询参数键：`searchOrderType`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`SUM_PIC`、`SCAN_SITE_CODE`、`CLASS`、`R_BILLCODE`、`BL_SUB_BILL`、`SUM_W`、`BL_REGISTER`、`SCAN_MAN_CODE`、`BL_MAIN_BILL`、`BL_REPEAT`、`SUM_SUB_W`、`SCAN_DATE`、`LOGIN_SITE_CODE`、`pageIndex`
- 点击查询后额外请求：
  - `FIND_SCAN_LEAVE_LIST_SUM` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=%3CTOKEN%3E`
  - `FIND_SCAN_LEAVE_LIST_NEW` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- 页面脚本提示接口：
  - `/dataQuery/findPageByCallId?id=FIND_SCAN_LEAVE_LIST_NEW`
  - `/dataQuery/findPageByCallId?id=FIND_SCAN_COME_LIST_DISTINCT`
  - `/dataQuery/findAllByCallId?id=FIND_SCAN_LEAVE_LIST_SUM`

**Field Mapping**
- 用户输入参数：`searchOrderType`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`SUM_PIC`、`SCAN_SITE_CODE`、`CLASS`、`R_BILLCODE`、`BL_SUB_BILL`、`SUM_W`、`BL_REGISTER`、`SCAN_MAN_CODE`、`BL_MAIN_BILL`、`BL_REPEAT`、`SUM_SUB_W`
- 页面自动补齐参数：`SCAN_DATE`
- 登录/站点上下文参数：`LOGIN_SITE_CODE`
- 分页技术参数：`pageIndex`
- text/value 到提交字段：`searchOrderType` -> `searchOrderType`；`searchDateType` -> `searchDateType`；`SEARCH_DATE_RANGE` -> `SEARCH_DATE_RANGE`；`SCAN_SITE_CODE` -> `SCAN_SITE_CODE`；`CLASS` -> `CLASS`；`R_BILLCODE` -> `R_BILLCODE`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `deleteList -> //清空查询单号 mini.get("searchOrderInput").setValue("");`
  - `searchBtn -> searchMethod()`
  - `exportBtn -> exportMethod();`
- 前置校验/状态相关 CALL_ID：`FIND_SCAN_LEAVE_LIST_SUM`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 页面识别建议：优先用 `pageId=vRlJ5Z6Nbes1U2LVFDMhROmovQi2kcD0WigVM4` + 页面标题联合识别。

### 派件清单

**Page Identity**
- 菜单路径：`扫描管理 / 派件清单`
- 页面类型：叶子页面
- `pageId`：`djFt6V6p3q6cEpmTbuCYGZO12Y1El7Gw9CMJau`
- 页面标题：`派件清单-速通`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`查询条件`、`按运单查询`、`查询时间范围`、`扫描时间`、`总件数`、`派件员`、`快件类型`、`查询结果过滤子单`、`总重量`、`是否vip服务`、`子单总重量`
- 首屏字段：`searchOrderInput`、`SEARCH_DATE_RANGE$text`、`SUM_PIC`、`SCAN_MAN_CODE$text`、`CLASS$text`、`SUM_W`、`SUM_SUB_W`
- MiniUI text/value 对：`searchOrderType` -> value=`searchOrderType$value` / submit=`searchOrderType`；`searchOrderInput` -> text=`searchOrderInput$text`；`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`SEARCH_DATE_RANGE` -> text=`SEARCH_DATE_RANGE$text` / value=`SEARCH_DATE_RANGE$value` / submit=`SEARCH_DATE_RANGE`；`SUM_PIC` -> text=`SUM_PIC$text`；`SCAN_MAN_CODE` -> text=`SCAN_MAN_CODE$text` / value=`SCAN_MAN_CODE$value` / submit=`SCAN_MAN_CODE`；`CLASS` -> text=`CLASS$text` / value=`CLASS$value` / submit=`CLASS`；`BL_SUB_BILL` -> value=`BL_SUB_BILL$value` / submit=`BL_SUB_BILL`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`
- 导出类按钮：`导出`、`导出`、`导出`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `SEARCH_DATE_RANGE$text`: `#SEARCH_DATE_RANGE$text` / `xpath=//*[@id="SEARCH_DATE_RANGE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `SUM_PIC`: `#SUM_PIC$text` / `xpath=//*[@id="SUM_PIC$text"]` / `xpath=//*[@name="SUM_PIC"]`
- `searchOrderType`: value=`searchOrderType$value` / submit=`searchOrderType`
- `searchOrderInput`: text=`searchOrderInput$text`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=FAST_TYPE&_=%3CTS%3E`
  - `FIND_SCAN_DISP_LIST` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_TAB_EMPLOYEE_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_SCAN_DISP_LIST_SUM`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=FIND_SCAN_DISP_LIST_SUM`
- 请求方法：`POST`
- 主查询参数键：`searchOrderType`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`SUM_PIC`、`SCAN_MAN_CODE`、`CLASS`、`BL_SUB_BILL`、`SUM_W`、`BL_VIP`、`SUM_SUB_W`、`SCAN_DATE`、`LOGIN_SITE_CODE`、`pageIndex`
- 点击查询后额外请求：
  - `FIND_SCAN_DISP_LIST_SUM` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=FIND_SCAN_DISP_LIST_SUM`
  - `FIND_SCAN_DISP_LIST` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_SCAN_DISP_LIST`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_SCAN_DISP_LIST`
- 页面脚本提示接口：
  - `/dataQuery/findAllByCallId?id=FIND_SCAN_DISP_LIST_SUM`

**Field Mapping**
- 用户输入参数：`searchOrderType`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`SUM_PIC`、`SCAN_MAN_CODE`、`CLASS`、`BL_SUB_BILL`、`SUM_W`、`BL_VIP`、`SUM_SUB_W`
- 页面自动补齐参数：`SCAN_DATE`
- 登录/站点上下文参数：`LOGIN_SITE_CODE`
- 分页技术参数：`pageIndex`
- text/value 到提交字段：`searchOrderType` -> `searchOrderType`；`searchDateType` -> `searchDateType`；`SEARCH_DATE_RANGE` -> `SEARCH_DATE_RANGE`；`SCAN_MAN_CODE` -> `SCAN_MAN_CODE`；`CLASS` -> `CLASS`；`BL_SUB_BILL` -> `BL_SUB_BILL`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `deleteList -> //清空查询单号 mini.get("searchOrderInput").setValue("");`
  - `searchBtn -> searchMethod()`
  - `exportBtn -> exportMethod();`
- 前置校验/状态相关 CALL_ID：`FIND_SCAN_DISP_LIST_SUM`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 页面识别建议：优先用 `pageId=djFt6V6p3q6cEpmTbuCYGZO12Y1El7Gw9CMJau` + 页面标题联合识别。

### 对比监控

**Page Identity**
- 菜单路径：`扫描管理 / 对比监控`
- 页面类型：分组节点
- 分组子项：`有到未发查询`、`有发未到查询`、`应到件查询`
- 说明：该菜单项只负责展开子菜单，live 点击时可能仍停留在上一业务页，不应把当前 iframe 的 `pageId/标题` 误判为该分组节点身份。

**Automation Notes**
- 该项不应直接当成 iframe 页面处理，应该继续点击它的子菜单。

### 有到未发查询

**Page Identity**
- 菜单路径：`扫描管理 / 有到未发查询`
- 页面类型：叶子页面，上级分组是 `对比监控`
- `pageId`：`TMthw7WoTOg4qjw74kbatEynEijjlOwGV9J5c7`
- 页面标题：`有到未发件查询-速通`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`查询条件`、`按运单查询`、`查询时间范围`、`扫描时间`、`扫描网点`、`物品类别`、`寄件网点`、`上一站`、`班次`、`快件类型`、`目的网点`、`单号类型`
- 首屏字段：`searchOrderInput`、`SEARCH_DATE_RANGE$text`、`SCAN_SITE_CODE$text`、`GOODS_TYPE$text`、`SEND_SITE_CODE$text`、`PRE_OR_NEXT_STATION_CODE$text`、`CLASSTYPE$text`、`FAST_TYPE$text`、`DESTINATION_CODE$text`、`BL_BILL_TYPE$text`
- MiniUI text/value 对：`searchOrderType` -> value=`searchOrderType$value` / submit=`searchOrderType`；`searchOrderInput` -> text=`searchOrderInput$text`；`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`SEARCH_DATE_RANGE` -> text=`SEARCH_DATE_RANGE$text` / value=`SEARCH_DATE_RANGE$value` / submit=`SEARCH_DATE_RANGE`；`SCAN_SITE_CODE` -> text=`SCAN_SITE_CODE$text` / value=`SCAN_SITE_CODE$value` / submit=`SCAN_SITE_CODE`；`GOODS_TYPE` -> text=`GOODS_TYPE$text` / value=`GOODS_TYPE$value` / submit=`GOODS_TYPE`；`SEND_SITE_CODE` -> text=`SEND_SITE_CODE$text` / value=`SEND_SITE_CODE$value` / submit=`SEND_SITE_CODE`；`PRE_OR_NEXT_STATION_CODE` -> text=`PRE_OR_NEXT_STATION_CODE$text` / value=`PRE_OR_NEXT_STATION_CODE$value` / submit=`PRE_OR_NEXT_STATION_CODE`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `SEARCH_DATE_RANGE$text`: `#SEARCH_DATE_RANGE$text` / `xpath=//*[@id="SEARCH_DATE_RANGE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `SCAN_SITE_CODE$text`: `#SCAN_SITE_CODE$text` / `xpath=//*[@id="SCAN_SITE_CODE$text"]`
- `searchOrderType`: value=`searchOrderType$value` / submit=`searchOrderType`
- `searchOrderInput`: text=`searchOrderInput$text`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=GOODS_TYPE&_=%3CTS%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=CLASS_INFO&_=%3CTS%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=FAST_TYPE&_=%3CTS%3E`
  - `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_SITE_ALL_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_SITE_ALL_COMBOBOX&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_SCAN_COME_NO_SEND`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_SCAN_COME_NO_SEND`
- 请求方法：`POST`
- 主查询参数键：`searchOrderType`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`SCAN_SITE_CODE`、`GOODS_TYPE`、`SEND_SITE_CODE`、`PRE_OR_NEXT_STATION_CODE`、`CLASSTYPE`、`FAST_TYPE`、`DESTINATION_CODE`、`BL_BILL_TYPE`、`DIS_BILL`、`SCAN_DATE`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIND_SCAN_COME_NO_SEND` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_SCAN_COME_NO_SEND`
- 页面脚本提示接口：
  - `/dataQuery/findPageByCallId?id=FIND_SCAN_COME_NO_SEND_LOGIN_DISTINCT`
  - `/dataQuery/findPageByCallId?id=FIND_SCAN_COME_NO_SEND_LOGIN`
  - `/dataQuery/findPageByCallId?id=FIND_SCAN_COME_NO_SEND_DISTINCT`
  - `/dataQuery/findPageByCallId?id=FIND_SCAN_COME_NO_SEND`

**Field Mapping**
- 用户输入参数：`searchOrderType`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`SCAN_SITE_CODE`、`GOODS_TYPE`、`SEND_SITE_CODE`、`PRE_OR_NEXT_STATION_CODE`、`CLASSTYPE`、`FAST_TYPE`、`DESTINATION_CODE`、`BL_BILL_TYPE`、`DIS_BILL`
- 页面自动补齐参数：`SCAN_DATE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`searchOrderType` -> `searchOrderType`；`searchDateType` -> `searchDateType`；`SEARCH_DATE_RANGE` -> `SEARCH_DATE_RANGE`；`SCAN_SITE_CODE` -> `SCAN_SITE_CODE`；`GOODS_TYPE` -> `GOODS_TYPE`；`SEND_SITE_CODE` -> `SEND_SITE_CODE`；`PRE_OR_NEXT_STATION_CODE` -> `PRE_OR_NEXT_STATION_CODE`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `deleteList -> //清空查询单号 mini.get("searchOrderInput").setValue("");`
  - `searchBtn -> searchMethod()`
- 前置校验/状态相关 CALL_ID：`FIND_SCAN_COME_NO_SEND_LOGIN_DISTINCT`、`FIND_SCAN_COME_NO_SEND_LOGIN`、`FIND_SCAN_COME_NO_SEND_DISTINCT`、`FIND_SCAN_COME_NO_SEND`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 页面识别建议：优先用 `pageId=TMthw7WoTOg4qjw74kbatEynEijjlOwGV9J5c7` + 页面标题联合识别。

### 有发未到查询

**Page Identity**
- 菜单路径：`扫描管理 / 有发未到查询`
- 页面类型：叶子页面，上级分组是 `对比监控`
- `pageId`：`7yZc3tYywXZUHRV7RFl62Sej1QUFR9f1HdSdDy`
- 页面标题：`有发未到件查询-速通`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`查询条件`、`按运单查询`、`查询时间范围`、`扫描时间`、`扫描网点`、`物品类别`、`寄件网点`、`上一站`、`班次`、`快件类型`、`目的网点`、`单号类型`
- 首屏字段：`searchOrderInput`、`SEARCH_DATE_RANGE$text`、`SCAN_SITE_CODE$text`、`GOODS_TYPE$text`、`SEND_SITE_CODE$text`、`PRE_OR_NEXT_STATION_CODE$text`、`CLASSTYPE$text`、`FAST_TYPE$text`、`DESTINATION_CODE$text`、`BL_BILL_TYPE$text`
- MiniUI text/value 对：`searchOrderType` -> value=`searchOrderType$value` / submit=`searchOrderType`；`searchOrderInput` -> text=`searchOrderInput$text`；`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`SEARCH_DATE_RANGE` -> text=`SEARCH_DATE_RANGE$text` / value=`SEARCH_DATE_RANGE$value` / submit=`SEARCH_DATE_RANGE`；`SCAN_SITE_CODE` -> text=`SCAN_SITE_CODE$text` / value=`SCAN_SITE_CODE$value` / submit=`SCAN_SITE_CODE`；`GOODS_TYPE` -> text=`GOODS_TYPE$text` / value=`GOODS_TYPE$value` / submit=`GOODS_TYPE`；`SEND_SITE_CODE` -> text=`SEND_SITE_CODE$text` / value=`SEND_SITE_CODE$value` / submit=`SEND_SITE_CODE`；`PRE_OR_NEXT_STATION_CODE` -> text=`PRE_OR_NEXT_STATION_CODE$text` / value=`PRE_OR_NEXT_STATION_CODE$value` / submit=`PRE_OR_NEXT_STATION_CODE`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `SEARCH_DATE_RANGE$text`: `#SEARCH_DATE_RANGE$text` / `xpath=//*[@id="SEARCH_DATE_RANGE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `SCAN_SITE_CODE$text`: `#SCAN_SITE_CODE$text` / `xpath=//*[@id="SCAN_SITE_CODE$text"]`
- `searchOrderType`: value=`searchOrderType$value` / submit=`searchOrderType`
- `searchOrderInput`: text=`searchOrderInput$text`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=GOODS_TYPE&_=%3CTS%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=CLASS_INFO&_=%3CTS%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=FAST_TYPE&_=%3CTS%3E`
  - `FIND_SCAN_SEND_NO_COME_ALL` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_SITE_ALL_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_SITE_ALL_COMBOBOX&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_SCAN_SEND_NO_COME_LOGIN`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- 请求方法：`POST`
- 主查询参数键：`searchOrderType`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`SCAN_SITE_CODE`、`GOODS_TYPE`、`SEND_SITE_CODE`、`PRE_OR_NEXT_STATION_CODE`、`CLASSTYPE`、`FAST_TYPE`、`DESTINATION_CODE`、`BL_BILL_TYPE`、`DIS_BILL`、`SCAN_DATE`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIND_SCAN_SEND_NO_COME_LOGIN` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_SCAN_SEND_NO_COME_ALL`
- 页面脚本提示接口：
  - `/dataQuery/findPageByCallId?id=FIND_SCAN_SEND_NO_COME_LOGIN_DISTINCT`
  - `/dataQuery/findPageByCallId?id=FIND_SCAN_SEND_NO_COME_LOGIN`
  - `/dataQuery/findPageByCallId?id=FIND_SCAN_SEND_NO_COME_ALL_ISTINCT`
  - `/dataQuery/findPageByCallId?id=FIND_SCAN_SEND_NO_COME_ALL`

**Field Mapping**
- 用户输入参数：`searchOrderType`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`SCAN_SITE_CODE`、`GOODS_TYPE`、`SEND_SITE_CODE`、`PRE_OR_NEXT_STATION_CODE`、`CLASSTYPE`、`FAST_TYPE`、`DESTINATION_CODE`、`BL_BILL_TYPE`、`DIS_BILL`
- 页面自动补齐参数：`SCAN_DATE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`searchOrderType` -> `searchOrderType`；`searchDateType` -> `searchDateType`；`SEARCH_DATE_RANGE` -> `SEARCH_DATE_RANGE`；`SCAN_SITE_CODE` -> `SCAN_SITE_CODE`；`GOODS_TYPE` -> `GOODS_TYPE`；`SEND_SITE_CODE` -> `SEND_SITE_CODE`；`PRE_OR_NEXT_STATION_CODE` -> `PRE_OR_NEXT_STATION_CODE`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `deleteList -> //清空查询单号 mini.get("searchOrderInput").setValue("");`
  - `searchBtn -> searchMethod()`
- 前置校验/状态相关 CALL_ID：`FIND_SCAN_SEND_NO_COME_LOGIN_DISTINCT`、`FIND_SCAN_SEND_NO_COME_LOGIN`、`FIND_SCAN_SEND_NO_COME_ALL_ISTINCT`、`FIND_SCAN_SEND_NO_COME_ALL`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 页面识别建议：优先用 `pageId=7yZc3tYywXZUHRV7RFl62Sej1QUFR9f1HdSdDy` + 页面标题联合识别。

### 应到件查询

**Page Identity**
- 菜单路径：`扫描管理 / 应到件查询`
- 页面类型：叶子页面，上级分组是 `对比监控`
- `pageId`：`yWykgvBl6aSWchRSJEi1AsNIdW3XmYnl5SKQGH`
- 页面标题：`应到件查询-速通`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 页签：`汇总数据`、`明细数据`
- 查询区/标签：`查询条件`、`按运单查询`、`查询时间范围`、`扫描时间`、`按相同网点查询`、`只查主单`、`发件站`、`物品类别`、`寄件网点`、`应到网点`、`快件类型`、`目的网点`
- 首屏字段：`searchOrderInput`、`SEARCH_DATE_RANGE$text`、`SENT_SITE_CODE$text`、`GOODS_TYPE$text`、`SEND_SITE_CODE$text`、`COME_SITE_CODE$text`、`FAST_TYPE$text`、`DESTINATION_CODE$text`
- MiniUI text/value 对：`searchOrderType` -> value=`searchOrderType$value` / submit=`searchOrderType`；`searchOrderInput` -> text=`searchOrderInput$text`；`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`SEARCH_DATE_RANGE` -> text=`SEARCH_DATE_RANGE$text` / value=`SEARCH_DATE_RANGE$value` / submit=`SEARCH_DATE_RANGE`；`BL_SAME` -> value=`BL_SAME$value` / submit=`BL_SAME`；`BL_BILL_TYPE` -> value=`BL_BILL_TYPE$value` / submit=`BL_BILL_TYPE`；`SENT_SITE_CODE` -> text=`SENT_SITE_CODE$text` / value=`SENT_SITE_CODE$value` / submit=`SENT_SITE_CODE`；`GOODS_TYPE` -> text=`GOODS_TYPE$text` / value=`GOODS_TYPE$value` / submit=`GOODS_TYPE`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`
- 导出类按钮：`导出`、`导出`、`导出`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `SEARCH_DATE_RANGE$text`: `#SEARCH_DATE_RANGE$text` / `xpath=//*[@id="SEARCH_DATE_RANGE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `SENT_SITE_CODE$text`: `#SENT_SITE_CODE$text` / `xpath=//*[@id="SENT_SITE_CODE$text"]`
- `searchOrderType`: value=`searchOrderType$value` / submit=`searchOrderType`
- `searchOrderInput`: text=`searchOrderInput$text`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=GOODS_TYPE&_=%3CTS%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=FAST_TYPE&_=%3CTS%3E`
  - `FIND_SHOULD_COME_TOTAL` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_SITE_ALL_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_SITE_ALL_COMBOBOX&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_SHOULD_COME_TOTAL`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_SHOULD_COME_TOTAL`
- 请求方法：`POST`
- 主查询参数键：`searchOrderType`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`BL_SAME`、`BL_BILL_TYPE`、`SENT_SITE_CODE`、`GOODS_TYPE`、`SEND_SITE_CODE`、`COME_SITE_CODE`、`FAST_TYPE`、`DESTINATION_CODE`、`SCAN_DATE`、`LOGIN_SITE_CODE`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIND_SHOULD_COME_TOTAL` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_SHOULD_COME_TOTAL`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_SHOULD_COME_TOTAL`
- 页面脚本提示接口：
  - `/dataQuery/findPageByCallId?id=FIND_SHOULD_COME_DETAIL_MIN`
  - `/dataQuery/findPageByCallId?id=FIND_SHOULD_COME_DETAIL`

**Field Mapping**
- 用户输入参数：`searchOrderType`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`BL_SAME`、`BL_BILL_TYPE`、`SENT_SITE_CODE`、`GOODS_TYPE`、`SEND_SITE_CODE`、`COME_SITE_CODE`、`FAST_TYPE`、`DESTINATION_CODE`
- 页面自动补齐参数：`SCAN_DATE`
- 登录/站点上下文参数：`LOGIN_SITE_CODE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`searchOrderType` -> `searchOrderType`；`searchDateType` -> `searchDateType`；`SEARCH_DATE_RANGE` -> `SEARCH_DATE_RANGE`；`BL_SAME` -> `BL_SAME`；`BL_BILL_TYPE` -> `BL_BILL_TYPE`；`SENT_SITE_CODE` -> `SENT_SITE_CODE`；`GOODS_TYPE` -> `GOODS_TYPE`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `deleteList -> //清空查询单号 mini.get("searchOrderInput").setValue("");`
  - `searchBtn -> searchMethod();`
  - `exportBtn -> exportMethod();`
- 前置校验/状态相关 CALL_ID：`FIND_SHOULD_COME_DETAIL_MIN`、`FIND_SHOULD_COME_DETAIL`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 页面识别建议：优先用 `pageId=yWykgvBl6aSWchRSJEi1AsNIdW3XmYnl5SKQGH` + 页面标题联合识别。

### 网点到离港时间

**Page Identity**
- 菜单路径：`扫描管理 / 网点到离港时间`
- 页面类型：叶子页面
- `pageId`：`UwvjfzV9ix7C6q8EwcBXcJJjMGlyN5IoobaNKX`
- 页面标题：`网点到离港时间`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`基础信息`、`CENTER_CODE`、`L2_SITE_CODE`、`L1_SITE_CODE`、`网点名称`、`打卡类型`、`打卡分拨`
- 首屏字段：`SITE_CODE$text`、`CLOCK_IN_TYPE$text`、`SITE_FB_CODE$text`
- MiniUI text/value 对：`SITE_CODE` -> text=`SITE_CODE$text` / value=`SITE_CODE$value` / submit=`SITE_CODE`；`CLOCK_IN_TYPE` -> text=`CLOCK_IN_TYPE$text` / value=`CLOCK_IN_TYPE$value` / submit=`CLOCK_IN_TYPE`；`SITE_FB_CODE` -> text=`SITE_FB_CODE$text` / value=`SITE_FB_CODE$value` / submit=`SITE_FB_CODE`
- 查询按钮：`查询`、`查询`、`查询`
- 导出类按钮：`导出`、`导出`、`导出`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询`: `#queryBtn` / `xpath=//*[@id="queryBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `查询`: `xpath=//*[normalize-space(.)="查询"]`
- `SITE_CODE$text`: `#SITE_CODE$text` / `xpath=//*[@id="SITE_CODE$text"]`
- `CLOCK_IN_TYPE$text`: `#CLOCK_IN_TYPE$text` / `xpath=//*[@id="CLOCK_IN_TYPE$text"]` / `xpath=//*[@placeholder="请选择"]`
- `SITE_FB_CODE$text`: `#SITE_FB_CODE$text` / `xpath=//*[@id="SITE_FB_CODE$text"]`
- `SITE_CODE`: text=`SITE_CODE$text` / value=`SITE_CODE$value` / submit=`SITE_CODE`
- `CLOCK_IN_TYPE`: text=`CLOCK_IN_TYPE$text` / value=`CLOCK_IN_TYPE$value` / submit=`CLOCK_IN_TYPE`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `FIND_TAB_REACH_OR_LEAVE_PORT_DATE` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
  - `FIND_SITE_ONE_TWO_FB_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_SITE_CENTER_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_TAB_REACH_OR_LEAVE_PORT_DATE`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- 请求方法：`POST`
- 主查询参数键：`CENTER_CODE`、`L2_SITE_CODE`、`L1_SITE_CODE`、`SITE_CODE`、`CLOCK_IN_TYPE`、`SITE_FB_CODE`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIND_TAB_REACH_OR_LEAVE_PORT_DATE` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- 页面脚本提示接口：
  - `/dataOperation/saveTables`

**Field Mapping**
- 用户输入参数：`CLOCK_IN_TYPE`、`SITE_FB_CODE`
- 登录/站点上下文参数：`CENTER_CODE`、`L2_SITE_CODE`、`L1_SITE_CODE`、`SITE_CODE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`SITE_CODE` -> `SITE_CODE`；`CLOCK_IN_TYPE` -> `CLOCK_IN_TYPE`；`SITE_FB_CODE` -> `SITE_FB_CODE`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `queryBtn`。
- 点击绑定（从内联脚本截取）：
  - `queryBtn -> query();`
  - `exportBtn -> exportFun();`
- 保存/提交操作键：`TAB_REACH_OR_LEAVE_PORT_DATE_DEL`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=UwvjfzV9ix7C6q8EwcBXcJJjMGlyN5IoobaNKX` + 页面标题联合识别。

### 网点到离港记录-新

**Page Identity**
- 菜单路径：`扫描管理 / 网点到离港记录-新`
- 页面类型：叶子页面
- `pageId`：`xpSVE7Ra8abSiketBd3vuleiUgOO4wGKRVbzvH`
- 页面标题：`网点到离港记录查询-测试-陈浩`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 页签：`网点查询`、`打卡记录`
- 查询区/标签：`查询条件`、`CENTER_CODE`、`L1_SITE_CODE`、`L2_SITE_CODE`、`规定时间`、`打卡时间`、`网点名称`、`打卡类型`、`打卡结果`、`网点类型`、`打卡分拨`
- 首屏字段：`SITE_CODE$text`、`REACH_OR_LEAVE_PORT_TYPE$text`、`CLOCK_IN_TYPE$text`、`SEARCH_DATE_RANGE$text`、`SITE_TYPE$text`、`SITE_FB_CODE$text`
- MiniUI text/value 对：`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`SITE_CODE` -> text=`SITE_CODE$text` / value=`SITE_CODE$value` / submit=`SITE_CODE`；`REACH_OR_LEAVE_PORT_TYPE` -> text=`REACH_OR_LEAVE_PORT_TYPE$text` / value=`REACH_OR_LEAVE_PORT_TYPE$value` / submit=`REACH_OR_LEAVE_PORT_TYPE`；`CLOCK_IN_TYPE` -> text=`CLOCK_IN_TYPE$text` / value=`CLOCK_IN_TYPE$value` / submit=`CLOCK_IN_TYPE`；`SEARCH_DATE_RANGE` -> text=`SEARCH_DATE_RANGE$text` / value=`SEARCH_DATE_RANGE$value` / submit=`SEARCH_DATE_RANGE`；`SITE_TYPE` -> text=`SITE_TYPE$text` / value=`SITE_TYPE$value` / submit=`SITE_TYPE`；`SITE_FB_CODE` -> text=`SITE_FB_CODE$text` / value=`SITE_FB_CODE$value` / submit=`SITE_FB_CODE`
- 查询按钮：`查询`、`查询`、`查询`、`网点查询`
- 导出类按钮：`导出`、`导出`、`导出`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询`: `#queryBtn` / `xpath=//*[@id="queryBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `查询`: `xpath=//*[normalize-space(.)="查询"]`
- `SITE_CODE$text`: `#SITE_CODE$text` / `xpath=//*[@id="SITE_CODE$text"]`
- `REACH_OR_LEAVE_PORT_TYPE$text`: `#REACH_OR_LEAVE_PORT_TYPE$text` / `xpath=//*[@id="REACH_OR_LEAVE_PORT_TYPE$text"]` / `xpath=//*[@placeholder="请选择"]`
- `CLOCK_IN_TYPE$text`: `#CLOCK_IN_TYPE$text` / `xpath=//*[@id="CLOCK_IN_TYPE$text"]` / `xpath=//*[@placeholder="请选择"]`
- `searchDateType`: value=`searchDateType$value` / submit=`searchDateType`
- `SITE_CODE`: text=`SITE_CODE$text` / value=`SITE_CODE$value` / submit=`SITE_CODE`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `FIND_REACH_OR_LEAVE_PORT_DETNEW_ONE` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
  - `FIND_SITE_ONE_TWO_FB_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_SITE_CENTER_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_REACH_OR_LEAVE_PORT_DETNEW_ONE`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- 请求方法：`POST`
- 主查询参数键：`CENTER_CODE`、`L1_SITE_CODE`、`L2_SITE_CODE`、`searchDateType`、`SITE_CODE`、`REACH_OR_LEAVE_PORT_TYPE`、`CLOCK_IN_TYPE`、`SEARCH_DATE_RANGE`、`SITE_TYPE`、`SITE_FB_CODE`、`HD_DATE`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIND_REACH_OR_LEAVE_PORT_DETNEW_ONE` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- 页面脚本提示接口：
  - `/dataOperation/saveTables`

**Field Mapping**
- 用户输入参数：`searchDateType`、`REACH_OR_LEAVE_PORT_TYPE`、`CLOCK_IN_TYPE`、`SEARCH_DATE_RANGE`、`SITE_TYPE`、`SITE_FB_CODE`
- 页面自动补齐参数：`HD_DATE`
- 登录/站点上下文参数：`CENTER_CODE`、`L1_SITE_CODE`、`L2_SITE_CODE`、`SITE_CODE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`searchDateType` -> `searchDateType`；`SITE_CODE` -> `SITE_CODE`；`REACH_OR_LEAVE_PORT_TYPE` -> `REACH_OR_LEAVE_PORT_TYPE`；`CLOCK_IN_TYPE` -> `CLOCK_IN_TYPE`；`SEARCH_DATE_RANGE` -> `SEARCH_DATE_RANGE`；`SITE_TYPE` -> `SITE_TYPE`；`SITE_FB_CODE` -> `SITE_FB_CODE`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `queryBtn`。
- 点击绑定（从内联脚本截取）：
  - `queryBtn -> query();`
  - `exportBtn -> exportFun();`
- 保存/提交操作键：`TAB_REACH_OR_LEAVE_PORT_DETNEW_DEL`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=xpSVE7Ra8abSiketBd3vuleiUgOO4wGKRVbzvH` + 页面标题联合识别。
