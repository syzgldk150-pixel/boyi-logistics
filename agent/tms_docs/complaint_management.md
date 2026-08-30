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

# 投诉管理 页面深拆

> 外部页面快照：内容未在 2026-08-30 重新登录真实页面复验。DOM、接口、字段和页面身份可能已变化，不能直接作为当前自动化合同。

## Summary

- 叶子页数量：`3`
- 分组节点数量：`0`
- 高价值页面：`被投诉方查询`、`投诉方登记`、`申诉登记`

## Pages

### 投诉方登记

**Page Identity**
- 菜单路径：`投诉管理 / 投诉方登记`
- 页面类型：叶子页面
- `pageId`：`ZCpQns3p9bgy5ojxah4jdRGP0O1XLEqicNrUPO`
- 页面标题：`投诉方登记-融辉`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 页签：`查询结果`、`投诉信息`、`申诉详情`、`理赔资料补充`、`处理记录`
- 查询区/标签：`查询条件`、`按运单查询`、`查询时间范围`、`投诉时间`、`投诉类型`、`被投诉部门`、`被投诉网点`、`投诉类别`、`仲裁状态`、`投诉状态`、`申诉状态`、`是否自主理赔`
- 首屏字段：`searchOrderInput`、`SEARCH_DATE_RANGE$text`、`EXCEPTION_TYPE$text`、`EXCEPTION_DEPT_SIDE_CODE$text`、`EXCEPTIONSITE_SIDE_CODE$text`、`CATEGORY$text`、`ARBITRATE_STATUS$text`、`STATUS$text`、`APPEAL_STATUS$text`
- MiniUI text/value 对：`1212` -> value=`1212$value`；`CODE_TYPE` -> value=`CODE_TYPE$value` / submit=`CODE_TYPE`；`searchOrderInput` -> text=`searchOrderInput$text`；`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`SEARCH_DATE_RANGE` -> text=`SEARCH_DATE_RANGE$text` / value=`SEARCH_DATE_RANGE$value` / submit=`SEARCH_DATE_RANGE`；`EXCEPTION_TYPE` -> text=`EXCEPTION_TYPE$text` / value=`EXCEPTION_TYPE$value` / submit=`EXCEPTION_TYPE`；`EXCEPTION_DEPT_SIDE_CODE` -> text=`EXCEPTION_DEPT_SIDE_CODE$text` / value=`EXCEPTION_DEPT_SIDE_CODE$value` / submit=`EXCEPTION_DEPT_SIDE_CODE`；`EXCEPTIONSITE_SIDE_CODE` -> text=`EXCEPTIONSITE_SIDE_CODE$text` / value=`EXCEPTIONSITE_SIDE_CODE$value` / submit=`EXCEPTIONSITE_SIDE_CODE`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`、`查询结果`
- 保存类按钮：`新增`、`新增`、`新增`
- 导出类按钮：`下载投诉方附件`、`下载投诉方附件`、`下载投诉方附件`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `新增`: `#addBtn` / `xpath=//*[@id="addBtn"]` / `xpath=//a[normalize-space(.)="新增"]`
- `新增`: `xpath=//*[normalize-space(.)="新增"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `SEARCH_DATE_RANGE$text`: `#SEARCH_DATE_RANGE$text` / `xpath=//*[@id="SEARCH_DATE_RANGE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `EXCEPTION_TYPE$text`: `#EXCEPTION_TYPE$text` / `xpath=//*[@id="EXCEPTION_TYPE$text"]`
- `1212`: value=`1212$value`
- `CODE_TYPE`: value=`CODE_TYPE$value` / submit=`CODE_TYPE`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `FIND_TAB_EXCEPTION_TYPE_CATEGORYBOX` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=%3CTOKEN%3E&_=%3CTS%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=ARBITRATE_STATE&_=%3CTS%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=COMPLAINT_STATE&_=%3CTS%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=APPEAL_STATE&_=%3CTS%3E`
  - `FIND_TAB_EXCEPTION_TYPE_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_TAB_DEPT_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_DEPT_COMBOBOX&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_SITE_INFO_ON_EXCEPTION_COMBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_TAB_EXCEPTION_REGISTER_CS`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- 请求方法：`POST`
- 主查询参数键：`CODE_TYPE`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`EXCEPTION_TYPE`、`EXCEPTION_DEPT_SIDE_CODE`、`EXCEPTIONSITE_SIDE_CODE`、`CATEGORY`、`ARBITRATE_STATUS`、`STATUS`、`APPEAL_STATUS`、`BL_SETTLEMENT`、`EXCEPTION_DATE`、`LOGIN_SITE_CODE`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIND_TAB_EXCEPTION_REGISTER_CS` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- 页面脚本提示接口：
  - `/dataQuery/findAllByCallId?id=FIND_TAB_BILL_BY_CODE`
  - `/dataOperation/saveTables`
  - `/dataQuery/findAllByCallId?id=FIND_EXCEPTION_PATH`
  - `/file/upload?sysFileUploadId=IMG`
  - `/dataQuery/findAllByCallId?id=FIND_TAB_EXCEPTION_REGISTER`
  - `/dataQuery/findAllByCallId?id=FIND_TAB_PROCESS_RECORD_PATH`
  - `/dataQuery/findAllByCallId?id=FIND_EXCEPTION_PATH&EXCEPTION_ID=`

**Field Mapping**
- 用户输入参数：`CODE_TYPE`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`EXCEPTION_TYPE`、`EXCEPTION_DEPT_SIDE_CODE`、`EXCEPTIONSITE_SIDE_CODE`、`CATEGORY`、`ARBITRATE_STATUS`、`STATUS`、`APPEAL_STATUS`、`BL_SETTLEMENT`、`EXCEPTION_DATE`
- 登录/站点上下文参数：`LOGIN_SITE_CODE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`CODE_TYPE` -> `CODE_TYPE`；`searchDateType` -> `searchDateType`；`SEARCH_DATE_RANGE` -> `SEARCH_DATE_RANGE`；`EXCEPTION_TYPE` -> `EXCEPTION_TYPE`；`EXCEPTION_DEPT_SIDE_CODE` -> `EXCEPTION_DEPT_SIDE_CODE`；`EXCEPTIONSITE_SIDE_CODE` -> `EXCEPTIONSITE_SIDE_CODE`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `searchBtn -> searchMethod()`
  - `addBtn -> addMethodCs5()`
  - `editBtn -> editMethodCs();`
  - `cancelBtn -> deleteMethod();`
  - `downwardBtn -> downloadFile();`
- 保存逻辑：脚本里显式出现 `/dataOperation/saveTables`，说明页面存在保存链路。
- 前置校验/状态相关 CALL_ID：`FIND_TAB_BILL_BY_CODE`、`FIND_EXCEPTION_PATH`、`FIND_TAB_EXCEPTION_REGISTER`、`FIND_TAB_PROCESS_RECORD_PATH`、`FIND_TAB_EXCEPTION_TYPE_CATEGORYBOX`、`FIND_TAB_EXCEPTION_TYPE_COMBOBOX`、`FIND_SITE_INFO_ON_EXCEPTION_COMBOX`、`FIND_TAB_EXCEPTION_REGISTER_CS`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=ZCpQns3p9bgy5ojxah4jdRGP0O1XLEqicNrUPO` + 页面标题联合识别。

### 被投诉方查询

**Page Identity**
- 菜单路径：`投诉管理 / 被投诉方查询`
- 页面类型：叶子页面
- `pageId`：`QNSZV8eQ5fIUR55ntV1HE3OHKTfrLWRF4ycaOd`
- 页面标题：`被投诉方查询-融辉`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 页签：`查询结果`、`投诉信息`、`申诉详情`、`处理记录`
- 查询区/标签：`被投诉类型`、`查询条件`、`按运单查询`、`查询时间范围`、`投诉时间`、`投诉类型`、`仲裁状态`、`投诉类别`、`被投诉部门`、`被投诉网点`、`投诉状态`、`申诉状态`
- 首屏字段：`searchOrderInput`、`SEARCH_DATE_RANGE$text`、`EXCEPTION_TYPE$text`、`ARBITRATE_STATUS$text`、`CATEGORY$text`、`EXCEPTION_DEPT_SIDE_CODE$text`、`EXCEPTIONSITE_SIDE_CODE$text`、`STATUS$text`、`APPEAL_STATUS$text`、`APPEAL_TYPE$text`
- MiniUI text/value 对：`112` -> text=`112$text`；`1212` -> value=`1212$value`；`123123` -> text=`123123$text`；`OBJECT_TYPE` -> text=`OBJECT_TYPE$text` / value=`OBJECT_TYPE$value` / submit=`OBJECT_TYPE`；`CODE_TYPE` -> value=`CODE_TYPE$value` / submit=`CODE_TYPE`；`searchOrderInput` -> text=`searchOrderInput$text`；`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`SEARCH_DATE_RANGE` -> text=`SEARCH_DATE_RANGE$text` / value=`SEARCH_DATE_RANGE$value` / submit=`SEARCH_DATE_RANGE`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`、`查询结果`
- 保存类按钮：`新增申诉`、`新增申诉`、`新增申诉`
- 导出类按钮：`导出`、`导出`、`导出`、`下载投诉方附件`、`下载投诉方附件`、`下载投诉方附件`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `新增申诉`: `#addBtn` / `xpath=//*[@id="addBtn"]` / `xpath=//a[normalize-space(.)="新增申诉"]`
- `新增申诉`: `xpath=//*[normalize-space(.)="新增申诉"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `SEARCH_DATE_RANGE$text`: `#SEARCH_DATE_RANGE$text` / `xpath=//*[@id="SEARCH_DATE_RANGE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `EXCEPTION_TYPE$text`: `#EXCEPTION_TYPE$text` / `xpath=//*[@id="EXCEPTION_TYPE$text"]`
- `112`: text=`112$text`
- `1212`: value=`1212$value`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=EXCEPTION_TYPE&_=%3CTS%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=ARBITRATE_STATE&_=%3CTS%3E`
  - `FIND_TAB_EXCEPTION_TYPE_CATEGORYBOX` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=%3CTOKEN%3E&_=%3CTS%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=COMPLAINT_STATE&_=%3CTS%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=APPEAL_STATE&_=%3CTS%3E`
  - `GET_EXCEPTION_TYPE_DISTINCT` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=%3CTOKEN%3E&_=%3CTS%3E`
  - `FIND_TAB_EXCEPTION_SIDE` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_TAB_EXCEPTION_TYPE_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_TAB_DEPT_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_DEPT_COMBOBOX&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_TAB_EXCEPTION_SIDE`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_EXCEPTION_SIDE`
- 请求方法：`POST`
- 主查询参数键：`CODE_TYPE`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`EXCEPTION_TYPE`、`ARBITRATE_STATUS`、`CATEGORY`、`EXCEPTION_DEPT_SIDE_CODE`、`EXCEPTIONSITE_SIDE_CODE`、`STATUS`、`APPEAL_STATUS`、`APPEAL_TYPE`、`BL_SETTLEMENT`、`EXCEPTION_DATE`、`LOGIN_SITE_CODE`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIND_TAB_EXCEPTION_SIDE` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_EXCEPTION_SIDE`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_TAB_EXCEPTION_SIDE`
- 页面脚本提示接口：
  - `/dataQuery/findAllByCallId?id=FIND_EXCEPTION_PATH`
  - `/dataQuery/findAllByCallId?id=FIND_EXCEPTION_PATH&EXCEPTION_ID=`
  - `/dataQuery/findAllByCallId?id=FIND_TAB_BILL_BY_CODE`
  - `/dataOperation/saveTables`
  - `/dataQuery/findAllByCallId?id=FIND_TAB_PROCESS_RECORD_PATH`

**Field Mapping**
- 用户输入参数：`CODE_TYPE`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`EXCEPTION_TYPE`、`ARBITRATE_STATUS`、`CATEGORY`、`EXCEPTION_DEPT_SIDE_CODE`、`EXCEPTIONSITE_SIDE_CODE`、`STATUS`、`APPEAL_STATUS`、`APPEAL_TYPE`、`BL_SETTLEMENT`、`EXCEPTION_DATE`
- 登录/站点上下文参数：`LOGIN_SITE_CODE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`OBJECT_TYPE` -> `OBJECT_TYPE`；`CODE_TYPE` -> `CODE_TYPE`；`searchDateType` -> `searchDateType`；`SEARCH_DATE_RANGE` -> `SEARCH_DATE_RANGE`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `exportBtn -> exportMethod()`
  - `downwardBtn -> downloadFile();`
  - `addBtn -> addMethod();`
  - `replyBtn -> addReplyMethod();`
  - `searchBtn -> searchMethod()`
- 保存逻辑：脚本里显式出现 `/dataOperation/saveTables`，说明页面存在保存链路。
- 前置校验/状态相关 CALL_ID：`FIND_EXCEPTION_PATH`、`FIND_TAB_BILL_BY_CODE`、`FIND_TAB_PROCESS_RECORD_PATH`、`FIND_TAB_EXCEPTION_TYPE_CATEGORYBOX`、`GET_EXCEPTION_TYPE_DISTINCT`、`FIND_TAB_EXCEPTION_SIDE`、`FIND_TAB_EXCEPTION_TYPE_COMBOBOX`、`FIND_SITE_INFO_ON_EXCEPTION_COMBOX`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=QNSZV8eQ5fIUR55ntV1HE3OHKTfrLWRF4ycaOd` + 页面标题联合识别。

### 申诉登记

**Page Identity**
- 菜单路径：`投诉管理 / 申诉登记`
- 页面类型：叶子页面
- `pageId`：`391ZgCPsBLgNgGkF0uOEnaWR7HXIXfDRsFl4uh`
- 页面标题：`申诉登记-速通`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`查询条件`、`按运单查询`、`查询时间范围`、`创建时间`、`申诉类型`
- 首屏字段：`searchOrderInput`、`SEARCH_DATE_RANGE$text`、`APPEAL_TYPE$text`
- MiniUI text/value 对：`searchOrderType` -> value=`searchOrderType$value` / submit=`searchOrderType`；`searchOrderInput` -> text=`searchOrderInput$text`；`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`SEARCH_DATE_RANGE` -> text=`SEARCH_DATE_RANGE$text` / value=`SEARCH_DATE_RANGE$value` / submit=`SEARCH_DATE_RANGE`；`APPEAL_TYPE` -> text=`APPEAL_TYPE$text` / value=`APPEAL_TYPE$value` / submit=`APPEAL_TYPE`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`
- 保存类按钮：`新增`、`新增`、`新增`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `新增`: `#addBtn` / `xpath=//*[@id="addBtn"]` / `xpath=//a[normalize-space(.)="新增"]`
- `新增`: `xpath=//*[normalize-space(.)="新增"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `SEARCH_DATE_RANGE$text`: `#SEARCH_DATE_RANGE$text` / `xpath=//*[@id="SEARCH_DATE_RANGE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `APPEAL_TYPE$text`: `#APPEAL_TYPE$text` / `xpath=//*[@id="APPEAL_TYPE$text"]` / `xpath=//*[@placeholder="请选择"]`
- `searchOrderType`: value=`searchOrderType$value` / submit=`searchOrderType`
- `searchOrderInput`: text=`searchOrderInput$text`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `FIND_APPEAL_TYPE_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=%3CTOKEN%3E&_=%3CTS%3E`
  - `FIND_TAB_EXCEPTION_APPEAL` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
- 主查询接口：`FIND_TAB_EXCEPTION_APPEAL`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- 请求方法：`POST`
- 主查询参数键：`searchOrderType`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`APPEAL_TYPE`、`REGISTER_DATE`、`LOGIN_SITE_CODE`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIND_TAB_EXCEPTION_APPEAL` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_TAB_EXCEPTION_APPEAL`
- 页面脚本提示接口：
  - `/dataQuery/findAllByCallId?id=FIND_EXCEPTION_PATH&EXCEPTION_ID=`

**Field Mapping**
- 用户输入参数：`searchOrderType`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`APPEAL_TYPE`
- 页面自动补齐参数：`REGISTER_DATE`
- 登录/站点上下文参数：`LOGIN_SITE_CODE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`searchOrderType` -> `searchOrderType`；`searchDateType` -> `searchDateType`；`SEARCH_DATE_RANGE` -> `SEARCH_DATE_RANGE`；`APPEAL_TYPE` -> `APPEAL_TYPE`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `searchBtn -> searchMethod();`
  - `addBtn -> addMethod1();`
  - `refreshBtn -> reloadSelect()`
- 前置校验/状态相关 CALL_ID：`FIND_EXCEPTION_PATH`、`FIND_TAB_EXCEPTION_APPEAL`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 页面识别建议：优先用 `pageId=391ZgCPsBLgNgGkF0uOEnaWR7HXIXfDRsFl4uh` + 页面标题联合识别。
