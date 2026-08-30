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

# 签收管理 页面深拆

> 外部页面快照：内容未在 2026-08-30 重新登录真实页面复验。DOM、接口、字段和页面身份可能已变化，不能直接作为当前自动化合同。

## Summary

- 叶子页数量：`3`
- 分组节点数量：`0`
- 高价值页面：`签收录入`、`是否签收查询`、`签收查询`

## Pages

### 签收录入

**Page Identity**
- 菜单路径：`签收管理 / 签收录入`
- 页面类型：叶子页面
- `pageId`：`Po8wM9WUzx033TwTC9YaGdF3b0Xb512R4jUGws`
- 页面标题：`签收录入-速通`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`到付转月结客户名称`、`未签收运单列表`、`中心已扫发件`、`本站已做到件`、`派件员`、`图片签收`、`回单签名图片`、`运单编号`、`提示：回车保存本地`、`签收网点`、`签收人`、`寄件日期`
- 首屏字段：`DISPATCH_NAME`、`请点击选择文件...`、`BILL_CODE`、`BILL_STATUS`、`SIGN_SITE`、`SIGN_MAN`、`SEND_DATE$text`、`DESTINATION`、`DISPATCH_SITE_CODE$text`、`BILL_WEIGHT`、`SEND_SITE_CODE$text`、`DISPATCH_DATE$text`
- MiniUI text/value 对：`NO_SIGN_BILL` -> value=`NO_SIGN_BILL$value` / submit=`NO_SIGN_BILL`；`DISPATCH_NAME_CODE` -> text=`DISPATCH_NAME_CODE$text` / value=`DISPATCH_NAME_CODE$value` / submit=`DISPATCH_NAME_CODE`；`FILE_NAME` -> value=`FILE_NAME$value`；`FILE_NAME1` -> value=`FILE_NAME1$value`；`BILL_CODE` -> text=`BILL_CODE$text`；`BILL_STATUS` -> text=`BILL_STATUS$text`；`SIGN_SITE_CODE` -> text=`SIGN_SITE_CODE$text` / value=`SIGN_SITE_CODE$value` / submit=`SIGN_SITE_CODE`；`SIGN_MAN` -> text=`SIGN_MAN$text`
- 查询按钮：`查询`、`查询`、`查询`
- 保存类按钮：`提示：回车保存本地`、`上传`、`上传`、`上传`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询`: `#noSignBtn` / `xpath=//*[@id="noSignBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `查询`: `xpath=//*[normalize-space(.)="查询"]`
- `提示：回车保存本地`: `#tip` / `xpath=//*[@id="tip"]` / `xpath=//*[normalize-space(.)="提示：回车保存本地"]`
- `上传`: `#saveBtn` / `xpath=//*[@id="saveBtn"]` / `xpath=//a[normalize-space(.)="上传"]`
- `DISPATCH_NAME`: `#DISPATCH_NAME_CODE$text` / `xpath=//*[@id="DISPATCH_NAME_CODE$text"]` / `xpath=//*[@name="DISPATCH_NAME"]`
- `NO_SIGN_BILL`: value=`NO_SIGN_BILL$value` / submit=`NO_SIGN_BILL`
- `DISPATCH_NAME_CODE`: text=`DISPATCH_NAME_CODE$text` / value=`DISPATCH_NAME_CODE$value` / submit=`DISPATCH_NAME_CODE`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=SIGN_EXCEPTION_TYPE&_=%3CTS%3E`
  - `FIND_SITE_INFO_BY_CODE` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=FIND_SITE_INFO_BY_CODE`
  - `FIND_ALL_SITE_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_ALL_SITE_COMBOBOX&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_SITE_ALL_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_SITE_ALL_COMBOBOX&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_ALL_EMPLOYEE_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&OWNER_SITE_CODE=%3COWNER_SITE_CODE%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_SITE_DISP_CENTRE_SEND_NO_SIGN`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=%3CTOKEN%3E`
- 请求方法：`POST`
- 主查询参数键：`SCAN_DATE`
- 点击查询后额外请求：
  - `FIND_SITE_DISP_CENTRE_SEND_NO_SIGN` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=%3CTOKEN%3E`
- 页面脚本提示接口：
  - `/dataQuery/findPageByCallId?id=FIND_ALL_EMPLOYEE_COMBOBOX&OWNER_SITE_CODE=`
  - `/dataQuery/findAllByCallId?id=FIND_SITE_INFO_BY_CODE`
  - `/dataQuery/findAllByCallId?id=FIND_TAB_RETURNBILL_BY_CODE`
  - `/file/upload?sysFileUploadId=IMG`
  - `/dataQuery/findAllByCallId?id=FIND_LOGIN_SITE_SCAN_COME`
  - `/dataQuery/findAllByCallId?id=FIND_TAB_GOODS_BY_BILL_CODE`
  - `/dataQuery/findAllByCallId?id=FIND_SCAN_COME_BY_CODE`
  - `/dataQuery/findAllByCallId?id=FIND_SCAN_DISP_BY_CODE`
  - `/dataQuery/findAllByCallId?id=CHECK_SIGN_DISPATCH_SITE_CODE2`
  - `/dataQuery/findAllByCallId?id=CHECK_SIGN_DISPATCH_SITE_CODE`
  - `/dataQuery/findAllByCallId?id=`
  - `/dataQuery/findAllByCallId?id=GET_BILL_BY_BILLCODE`

**Field Mapping**
- 用户输入参数：本次 live 查询没有稳定提取到，通常是页面先走前置校验、需要先录入单号，或查询不是默认页签。
- 页面自动补齐参数：`SCAN_DATE`
- text/value 到提交字段：`NO_SIGN_BILL` -> `NO_SIGN_BILL`；`DISPATCH_NAME_CODE` -> `DISPATCH_NAME_CODE`；`SIGN_SITE_CODE` -> `SIGN_SITE_CODE`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `noSignBtn`。
- 点击绑定（从内联脚本截取）：
  - `saveBtn -> uploadMethod();`
  - `removeBtn -> deleteRow()`
  - `noSignBtn -> queryNoSign()`
- 保存/提交操作键：`TAB_SIGN_ADD`
- 前置校验/状态相关 CALL_ID：`FIND_SITE_INFO_BY_CODE`、`FIND_TAB_RETURNBILL_BY_CODE`、`FIND_SCAN_COME_BY_CODE`、`FIND_SCAN_DISP_BY_CODE`、`CHECK_SIGN_DISPATCH_SITE_CODE2`、`CHECK_SIGN_DISPATCH_SITE_CODE`、`FIND_BALANCE_ACCOUNT_ADD_BILL`、`FIND_TAB_SITE_BY_CODE`、`FIND_TAB_FEE_APPLY_SUM_1`、`FIND_SITE_DISP_CENTRE_SEND_NO_SIGN`、`FIND_SITE_DISP_CENTRE_COME_NO_SIGN`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=Po8wM9WUzx033TwTC9YaGdF3b0Xb512R4jUGws` + 页面标题联合识别。

### 签收查询

**Page Identity**
- 菜单路径：`签收管理 / 签收查询`
- 页面类型：叶子页面
- `pageId`：`GFRhlBMnNsC33c1uUWk7zto4Pn9j2outsVhQd8`
- 页面标题：`签收查询-速通`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 页签：`总部汇总`、`汇总数据`、`明细数据`
- 查询区/标签：`查询条件`、`按运单查询`、`查询时间范围`、`签收日期`、`是否到付件`、`签收网点和派件网点是否一致`、`片区名称`、`派件员`、`录入网点`、`是否代签收`、`签收类型`、`异常类型`
- 首屏字段：`searchOrderInput`、`SEARCH_DATE_RANGE$text`、`AREA_NAME$text`、`DISPATCH_NAME_CODE$text`、`RECORD_SITE_CODE$text`、`BL_REPLACE$text`、`STATE_NAME$text`、`EXCEPTION_TYPE$text`
- MiniUI text/value 对：`searchOrderType` -> value=`searchOrderType$value` / submit=`searchOrderType`；`searchOrderInput` -> text=`searchOrderInput$text`；`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`SEARCH_DATE_RANGE` -> text=`SEARCH_DATE_RANGE$text` / value=`SEARCH_DATE_RANGE$value` / submit=`SEARCH_DATE_RANGE`；`PAYMENT_TYPE` -> value=`PAYMENT_TYPE$value` / submit=`PAYMENT_TYPE`；`BL_DISP_SIGN` -> value=`BL_DISP_SIGN$value` / submit=`BL_DISP_SIGN`；`AREA_NAME` -> text=`AREA_NAME$text` / value=`AREA_NAME$value` / submit=`AREA_NAME`；`DISPATCH_NAME_CODE` -> text=`DISPATCH_NAME_CODE$text` / value=`DISPATCH_NAME_CODE$value` / submit=`DISPATCH_NAME_CODE`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`
- 导出类按钮：`导出`、`导出`、`导出`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `SEARCH_DATE_RANGE$text`: `#SEARCH_DATE_RANGE$text` / `xpath=//*[@id="SEARCH_DATE_RANGE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `AREA_NAME$text`: `#AREA_NAME$text` / `xpath=//*[@id="AREA_NAME$text"]`
- `searchOrderType`: value=`searchOrderType$value` / submit=`searchOrderType`
- `searchOrderInput`: text=`searchOrderInput$text`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=SIGN_EXCEPTION_TYPE&_=%3CTS%3E`
  - `FIND_SIGNED_TOTAL` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_AREA_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_AREA_COMBOBOX&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_TAB_EMPLOYEE_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_SITE_ALL_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_SITE_ALL_COMBOBOX&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 本站汇总查询接口：`FIND_SIGNED_TOTAL`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- 请求方法：`POST`
- 主查询参数键：`searchOrderType`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`PAYMENT_TYPE`、`BL_DISP_SIGN`、`AREA_NAME`、`DISPATCH_NAME_CODE`、`RECORD_SITE_CODE`、`BL_REPLACE`、`STATE_NAME`、`EXCEPTION_TYPE`、`SIGN_DATE`、`LOGIN_SITE_CODE`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击“汇总数据”查询后请求：
  - `FIND_SIGNED_TOTAL` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
  - `FIND_TAB_WORK_ORDER_TYPE_REMIND` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=%3CTOKEN%3E`
- 双击本站汇总行进入逐票明细后请求：
  - `FIND_SIGNED_DETAIL_ALL_EXCEL` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
  - 在原查询参数上增加汇总行返回的 `SIGN_SITE_CODE`、`AREA_NAME`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_SIGNED_TOTAL`
- `FIND_SIGNED_DETAIL_ALL` 只用于总部账号的特殊明细分支；本站账号不得直接用它替代上述“汇总 → 双击本站行 → 明细”的页面链路，否则日期条件可能不生效。

**Field Mapping**
- 用户输入参数：`searchOrderType`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`PAYMENT_TYPE`、`BL_DISP_SIGN`、`AREA_NAME`、`DISPATCH_NAME_CODE`、`RECORD_SITE_CODE`、`BL_REPLACE`、`STATE_NAME`、`EXCEPTION_TYPE`
- 页面自动补齐参数：`SIGN_DATE`
- 登录/站点上下文参数：`LOGIN_SITE_CODE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`searchOrderType` -> `searchOrderType`；`searchDateType` -> `searchDateType`；`SEARCH_DATE_RANGE` -> `SEARCH_DATE_RANGE`；`PAYMENT_TYPE` -> `PAYMENT_TYPE`；`BL_DISP_SIGN` -> `BL_DISP_SIGN`；`AREA_NAME` -> `AREA_NAME`；`DISPATCH_NAME_CODE` -> `DISPATCH_NAME_CODE`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `searchBtn -> searchMethod();`
  - `exportBtn -> exportMethod();`
- 逐票签收证据必须按页面真实链路先查 `FIND_SIGNED_TOTAL`，再按每个汇总行查 `FIND_SIGNED_DETAIL_ALL_EXCEL`，并校验每组 `TOTAL_NUM` 与明细 `total` 相等。
- 长历史范围按连续、无重叠的 31 天窗口分片查询；每个窗口均独立完成汇总与明细分页并校验，片间按 `BILL_CODE + SIGN_DATE` 去重且冲突显式失败。日常增量窗口不足 31 天时只发起一个分片。

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 页面识别建议：优先用 `pageId=GFRhlBMnNsC33c1uUWk7zto4Pn9j2outsVhQd8` + 页面标题联合识别。

### 是否签收查询

**Page Identity**
- 菜单路径：`签收管理 / 是否签收查询`
- 页面类型：叶子页面
- `pageId`：`645n0cgg0AoLoXDy3pUXIjD6wH4ff2S9q6sBO4`
- 页面标题：`是否签收查询-速通`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 页签：`汇总数据`、`明细数据`
- 查询区/标签：`查询条件`、`查询时间范围`、`扫描时间`、`扫描网点`、`上下网点`、`扫描类型`、`扫描人`、`寄件片区`、`派件片区`、`寄件分拨`、`派件分拨`、`只看主单`
- 首屏字段：`SEARCH_DATE_RANGE$text`、`SCAN_SITE_CODE$text`、`PRE_OR_NEXT_STATION_CODE$text`、`SCAN_TYPE$text`、`SCAN_MAN_CODE$text`、`AREA_NAME_SEND$text`、`AREA_NAME_DISP$text`、`SEND_CENTER_CODE$text`、`DISP_CENTER_CODE$text`
- MiniUI text/value 对：`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`SEARCH_DATE_RANGE` -> text=`SEARCH_DATE_RANGE$text` / value=`SEARCH_DATE_RANGE$value` / submit=`SEARCH_DATE_RANGE`；`SCAN_SITE_CODE` -> text=`SCAN_SITE_CODE$text` / value=`SCAN_SITE_CODE$value` / submit=`SCAN_SITE_CODE`；`PRE_OR_NEXT_STATION_CODE` -> text=`PRE_OR_NEXT_STATION_CODE$text` / value=`PRE_OR_NEXT_STATION_CODE$value` / submit=`PRE_OR_NEXT_STATION_CODE`；`SCAN_TYPE` -> text=`SCAN_TYPE$text` / value=`SCAN_TYPE$value` / submit=`SCAN_TYPE`；`SCAN_MAN_CODE` -> text=`SCAN_MAN_CODE$text` / value=`SCAN_MAN_CODE$value` / submit=`SCAN_MAN_CODE`；`AREA_NAME_SEND` -> text=`AREA_NAME_SEND$text` / value=`AREA_NAME_SEND$value` / submit=`AREA_NAME_SEND`；`AREA_NAME_DISP` -> text=`AREA_NAME_DISP$text` / value=`AREA_NAME_DISP$value` / submit=`AREA_NAME_DISP`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`
- 导出类按钮：`导出`、`导出`、`导出`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `SEARCH_DATE_RANGE$text`: `#SEARCH_DATE_RANGE$text` / `xpath=//*[@id="SEARCH_DATE_RANGE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `SCAN_SITE_CODE$text`: `#SCAN_SITE_CODE$text` / `xpath=//*[@id="SCAN_SITE_CODE$text"]`
- `PRE_OR_NEXT_STATION_CODE$text`: `#PRE_OR_NEXT_STATION_CODE$text` / `xpath=//*[@id="PRE_OR_NEXT_STATION_CODE$text"]`
- `searchDateType`: value=`searchDateType$value` / submit=`searchDateType`
- `SEARCH_DATE_RANGE`: text=`SEARCH_DATE_RANGE$text` / value=`SEARCH_DATE_RANGE$value` / submit=`SEARCH_DATE_RANGE`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=TAB_SCAN_TYPE&_=%3CTS%3E`
  - `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_ALL_SITE_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_ALL_SITE_COMBOBOX&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_TAB_EMPLOYEE_SUB_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_AREA_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_AREA_COMBOBOX&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_SITE_CENTER_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_SEND_SIGN_RATE_TOTAL`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- 请求方法：`POST`
- 主查询参数键：`searchDateType`、`SEARCH_DATE_RANGE`、`SCAN_SITE_CODE`、`PRE_OR_NEXT_STATION_CODE`、`SCAN_TYPE`、`SCAN_MAN_CODE`、`AREA_NAME_SEND`、`AREA_NAME_DISP`、`SEND_CENTER_CODE`、`DISP_CENTER_CODE`、`BL_DISP_SIGN`、`SCAN_DATE`、`LOGIN_SITE_CODE`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIND_SEND_SIGN_RATE_TOTAL` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- 页面脚本提示接口：
  - `/dataQuery/findPageByCallId?id=FIND_REC_SIGN_RATE_TOTAL`
  - `/dataQuery/findPageByCallId?id=FIND_REC_SIGN_RATE_DETAIL`
  - `/dataQuery/findPageByCallId?id=FIND_SEND_SIGN_RATE_TOTAL`
  - `/dataQuery/findPageByCallId?id=FIND_SEND_SIGN_RATE_DETAIL`
  - `/dataQuery/findPageByCallId?id=FIND_COME_SIGN_RATE_TOTAL`
  - `/dataQuery/findPageByCallId?id=FIND_COME_SIGN_RATE_DETAIL`
  - `/dataQuery/findPageByCallId?id=FIND_DISP_SIGN_RATE_TOTAL`
  - `/dataQuery/findPageByCallId?id=FIND_DISP_SIGN_RATE_DETAIL`
  - `/dataQuery/findPageByCallId?id=FIND_OTHER_SIGN_RATE_TOTAL`
  - `/dataQuery/findPageByCallId?id=FIND_OTHER_SIGN_RATE_DETAIL`

**Field Mapping**
- 用户输入参数：`searchDateType`、`SEARCH_DATE_RANGE`、`SCAN_SITE_CODE`、`PRE_OR_NEXT_STATION_CODE`、`SCAN_TYPE`、`SCAN_MAN_CODE`、`AREA_NAME_SEND`、`AREA_NAME_DISP`、`SEND_CENTER_CODE`、`DISP_CENTER_CODE`、`BL_DISP_SIGN`
- 页面自动补齐参数：`SCAN_DATE`
- 登录/站点上下文参数：`LOGIN_SITE_CODE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`searchDateType` -> `searchDateType`；`SEARCH_DATE_RANGE` -> `SEARCH_DATE_RANGE`；`SCAN_SITE_CODE` -> `SCAN_SITE_CODE`；`PRE_OR_NEXT_STATION_CODE` -> `PRE_OR_NEXT_STATION_CODE`；`SCAN_TYPE` -> `SCAN_TYPE`；`SCAN_MAN_CODE` -> `SCAN_MAN_CODE`；`AREA_NAME_SEND` -> `AREA_NAME_SEND`；`AREA_NAME_DISP` -> `AREA_NAME_DISP`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `searchBtn -> searchMethod();`
  - `exportBtn -> exportMethod();`
- 前置校验/状态相关 CALL_ID：`FIND_REC_SIGN_RATE_DETAIL`、`FIND_SEND_SIGN_RATE_DETAIL`、`FIND_COME_SIGN_RATE_DETAIL`、`FIND_DISP_SIGN_RATE_DETAIL`、`FIND_OTHER_SIGN_RATE_DETAIL`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 页面识别建议：优先用 `pageId=645n0cgg0AoLoXDy3pUXIjD6wH4ff2S9q6sBO4` + 页面标题联合识别。
