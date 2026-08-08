# 运单管理 页面深拆

## Summary

- 叶子页数量：`14`
- 分组节点数量：`1`
- 高价值页面：`运单录入`、`派件运单查询`、`寄件运单查询`、`订单推送查询`、`订单管理`
- 分组节点：`延迟扣款管理`

## Pages

### 运单录入

**Page Identity**
- 菜单路径：`运单管理 / 运单录入`
- 页面类型：叶子页面
- `pageId`：`3Xunbl4w7zKXRvd522bJyYD84YvJykGgJXhKek`
- 页面标题：`运单录入-对公002`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`VIEW_CODE`、`BL_STATUS`、`寄件财务中心`、`目的网点编号`、`派件网点编号`、`派件财务中心`、`派件财务中心编号`、`子单号`、`计费重量`、`目的网点是否允许到付`、`到付款限额`、`订单号`
- 首屏字段：`SEND_DATE$text`、`SEND_SITE`、`REGISTER_SITE`、`TAKE_PIECE_EMPLOYEE`、`BILL_CODE`、`SEND_MAN_ONE`、`SEND_MAN`、`CUSTOMER_NAME`、`SEND_MAN_PHONE`、`SEND_MAN_PHONE_EXT`、`SEND_PROVINCE`、`SEND_CITY`
- MiniUI text/value 对：`GOODS_TYPE` -> text=`GOODS_TYPE$text`；`BL_VIP` -> value=`BL_VIP$value` / submit=`BL_VIP`；`PRODUCT_TYPE` -> text=`PRODUCT_TYPE$text`；`SEND_DATE` -> text=`SEND_DATE$text` / value=`SEND_DATE$value` / submit=`SEND_DATE`；`SEND_SITE_CODE` -> text=`SEND_SITE_CODE$text` / value=`SEND_SITE_CODE$value` / submit=`SEND_SITE_CODE`；`REGISTER_SITE_CODE` -> text=`REGISTER_SITE_CODE$text` / value=`REGISTER_SITE_CODE$value` / submit=`REGISTER_SITE_CODE`；`TAKE_PIECE_EMPLOYEE_CODE` -> text=`TAKE_PIECE_EMPLOYEE_CODE$text` / value=`TAKE_PIECE_EMPLOYEE_CODE$value` / submit=`TAKE_PIECE_EMPLOYEE_CODE`；`BILL_CODE` -> text=`BILL_CODE$text`
- 查询按钮：`寄件人查询`、`收件人查询`
- 保存类按钮：`保存当前勾选项`、`保存当前勾选项`、`保存当前勾选项`、`总代取费开单成本+提货收入，此单为代取件货物，签收后系统将自动返还您开单成本并支付您提货收入`、`保存`、`保存`、`保存`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 子 iframe：存在内嵌 iframe，开发时要先确认是否需要切换子 frame。
- 稳定选择器候选：
- `寄件人查询`: `xpath=//*[normalize-space(.)="寄件人查询"]`
- `收件人查询`: `xpath=//*[normalize-space(.)="收件人查询"]`
- `保存当前勾选项`: `#checkBtn` / `xpath=//*[@id="checkBtn"]` / `xpath=//a[normalize-space(.)="保存当前勾选项"]`
- `保存当前勾选项`: `xpath=//*[normalize-space(.)="保存当前勾选项"]`
- `SEND_DATE$text`: `#SEND_DATE$text` / `xpath=//*[@id="SEND_DATE$text"]`
- `SEND_SITE`: `#SEND_SITE_CODE$text` / `xpath=//*[@id="SEND_SITE_CODE$text"]` / `xpath=//*[@name="SEND_SITE"]`
- `REGISTER_SITE`: `#REGISTER_SITE_CODE$text` / `xpath=//*[@id="REGISTER_SITE_CODE$text"]` / `xpath=//*[@name="REGISTER_SITE"]`
- `GOODS_TYPE`: text=`GOODS_TYPE$text`
- `BL_VIP`: value=`BL_VIP$value` / submit=`BL_VIP`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `FIND_ALL_EMPLOYEE_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&_=%3CTS%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=CARD_TYPE&_=%3CTS%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=TAB_PACKING_TYPE&_=%3CTS%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=TAB_CLASS&_=%3CTS%3E`
  - `FIND_PRODUCT_TYPE` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=FIND_PRODUCT_TYPE&_=%3CTS%3E`
  - `FIND_TAB_SITE_RECORD_` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=FIND_TAB_SITE_RECORD_&SITE_CODE=%3CSITE_CODE%3E&_=%3CTS%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=TAB_DISPATCH_MODE&_=%3CTS%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=VIP_Added_Services&_=%3CTS%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=PAYMENT_TYPE&_=%3CTS%3E`
- 本次 live 点击未拿到稳定主查询请求，通常是因为页面需要先录入单号、先选择页签，或查询逻辑在脚本内先做前置校验。
- 页面脚本提示接口：
  - `/dataQuery/findAllByCallId?id=FIND_PLAN_GOODS_ROUTE`
  - `/dataOperation/saveTables`
  - `/dataQuery/findAllByCallId?id=GET_BILL_DATA_MANAGE`
  - `//printBillNew2(result,`
  - `//printBillNew1(result,`
  - `/dataQuery/findAllByCallId?id=FIND_TAB_GOODS_ROUTE`
  - `/dataQuery/findAllByCallId?id=FIND_TAB_SITE_PRINT_NAME`
  - `/dataQuery/findAllByCallId?id=FIND_TAB_STORAGE_LOCATION2`
  - `/dataQuery/findAllByCallId?id=FIND_TAB_SITEALL`
  - `/dataQuery/findAllByCallId?id=FIND_DESTINATION_BY_NAME`
  - `/dataQuery/findAllByCallId?id=FIND_TAB_DEDICATED_SITE_CONFIG_CBBX1`
  - `/dataQuery/findAllByCallId?id=FIND_CREATE_BILL_DESTINATION`

**Field Mapping**
- 用户输入参数：本次 live 查询没有稳定提取到，通常是页面先走前置校验、需要先录入单号，或查询不是默认页签。
- text/value 到提交字段：`BL_VIP` -> `BL_VIP`；`SEND_DATE` -> `SEND_DATE`；`SEND_SITE_CODE` -> `SEND_SITE_CODE`；`REGISTER_SITE_CODE` -> `REGISTER_SITE_CODE`；`TAKE_PIECE_EMPLOYEE_CODE` -> `TAKE_PIECE_EMPLOYEE_CODE`

**State / Validation Logic**
- 查询入口：没有稳定点击到默认查询按钮，说明此页可能依赖录入单号、页签切换或前置校验。
- 点击绑定（从内联脚本截取）：
  - `checkBtn -> saveCheckFun();`
- 保存/提交操作键：`TAB_BILL_CHECK_ADD`、`TAB_BILL_CHECK_UPT`
- 前置校验/状态相关 CALL_ID：`FIND_TAB_EXCEPTION_REPORT_AUDIT`、`FIND_COUNTY_COMBOBOX_PAGE`、`FIND_BILL_CHECK`、`FIND_SITE_INFO_BY_SITE_CODE`、`FIND_BALANCE_ACCOUNT_ADD_BILL`、`FIND_SITE_EXT_BY_CODE`

**Automation Notes**
- 更适合先走 DOM：当前没有稳定主查询请求，优先把页面交互顺序跑通。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=3Xunbl4w7zKXRvd522bJyYD84YvJykGgJXhKek` + 页面标题联合识别。

### 订单推送查询

**Page Identity**
- 菜单路径：`运单管理 / 订单推送查询`
- 页面类型：叶子页面
- `pageId`：`Gqyny3iwzbHBXhPZA0900jstLSv728HAyTmit4`
- 页面标题：`第三方订单推送查询`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`查询条件`、`按运单查询`、`查询时间范围`、`寄件网点`、`派件网点`、`推送时间`、`寄件时间`、`寄件人`、`收件人`、`寄件客户`、`派件省份`、`推送状态`
- 首屏字段：`searchOrderInput`、`SEND_SITE_CODE$text`、`DISPATCH_SITE_CODE$text`、`SEND_MAN$text`、`ACCEPT_MAN$text`、`SEARCH_DATE_RANGE$text`、`CUSTOMER_CODE$text`、`ACCEPT_PROVINCE$text`、`PUSH_STATUS$text`、`SEND_PROVINCE$text`、`ORDER_CHANNEL$text`
- MiniUI text/value 对：`CODE_TYPE` -> value=`CODE_TYPE$value` / submit=`CODE_TYPE`；`searchOrderInput` -> text=`searchOrderInput$text`；`SEND_SITE_CODE` -> text=`SEND_SITE_CODE$text` / value=`SEND_SITE_CODE$value` / submit=`SEND_SITE_CODE`；`DISPATCH_SITE_CODE` -> text=`DISPATCH_SITE_CODE$text` / value=`DISPATCH_SITE_CODE$value` / submit=`DISPATCH_SITE_CODE`；`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`SEND_MAN` -> text=`SEND_MAN$text` / value=`SEND_MAN$value` / submit=`SEND_MAN`；`ACCEPT_MAN` -> text=`ACCEPT_MAN$text` / value=`ACCEPT_MAN$value` / submit=`ACCEPT_MAN`；`SEARCH_DATE_RANGE` -> text=`SEARCH_DATE_RANGE$text` / value=`SEARCH_DATE_RANGE$value` / submit=`SEARCH_DATE_RANGE`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`
- 导出类按钮：`导出`、`导出`、`导出`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `SEND_SITE_CODE$text`: `#SEND_SITE_CODE$text` / `xpath=//*[@id="SEND_SITE_CODE$text"]`
- `DISPATCH_SITE_CODE$text`: `#DISPATCH_SITE_CODE$text` / `xpath=//*[@id="DISPATCH_SITE_CODE$text"]`
- `CODE_TYPE`: value=`CODE_TYPE$value` / submit=`CODE_TYPE`
- `searchOrderInput`: text=`searchOrderInput$text`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `FIND_PROVINCE` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=FIND_PROVINCE&_=%3CTS%3E`
  - `FIND_TAB_BILL_PUSH_SJA` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_SITE_ALL_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_SITE_ALL_COMBOBOX&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_CUSTOMER_SEND_LIST` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_CUSTOMER_SEND_LIST&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_CUSTOMER_DISP_LIST` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_CUSTOMER_DISP_LIST&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_CUSTOMER_AND_SITE` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_CUSTOMER_AND_SITE&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_TAB_BILL_PUSH_SJA`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_BILL_PUSH_SJA`
- 请求方法：`POST`
- 主查询参数键：`CODE_TYPE`、`searchOrderInput`、`SEND_SITE_CODE`、`DISPATCH_SITE_CODE`、`searchDateType`、`SEND_MAN`、`ACCEPT_MAN`、`SEARCH_DATE_RANGE`、`CUSTOMER_CODE`、`ACCEPT_PROVINCE`、`PUSH_STATUS`、`SEND_PROVINCE`、`ORDER_CHANNEL`、`PUSH_TIME`、`PREPARE_SITE_CODE`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIND_TAB_BILL_PUSH_SJA` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_BILL_PUSH_SJA`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_TAB_BILL_PUSH_SJA`
- 页面脚本提示接口：
  - `/dataOperation/saveTables`
  - `/dataQuery/findAllByCallId`
  - `/dataQuery/findPageByCallId?id=FIND_TAB_EMPLOYEE&OWNER_SITE_CODE=`

**Field Mapping**
- 用户输入参数：`CODE_TYPE`、`searchOrderInput`、`SEND_SITE_CODE`、`DISPATCH_SITE_CODE`、`searchDateType`、`SEND_MAN`、`ACCEPT_MAN`、`SEARCH_DATE_RANGE`、`CUSTOMER_CODE`、`ACCEPT_PROVINCE`、`PUSH_STATUS`、`SEND_PROVINCE`、`ORDER_CHANNEL`
- 页面自动补齐参数：`PUSH_TIME`、`PREPARE_SITE_CODE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`CODE_TYPE` -> `CODE_TYPE`；`SEND_SITE_CODE` -> `SEND_SITE_CODE`；`DISPATCH_SITE_CODE` -> `DISPATCH_SITE_CODE`；`searchDateType` -> `searchDateType`；`SEND_MAN` -> `SEND_MAN`；`ACCEPT_MAN` -> `ACCEPT_MAN`；`SEARCH_DATE_RANGE` -> `SEARCH_DATE_RANGE`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `edit_push -> dbClickDetail(e);`
  - `searchBtn -> searchMethod();`
  - `exportBtn -> var data=getQueryParam(); $Z.page.datagridExportExcel("datagrid","FIND_TAB_BILL_PUSH_SJA_EXCEL","数据导出",data, "async");`
  - `PUSH_TWO -> push_tow();`
- 保存/提交操作键：`TAB_BILL_PUSH_SJA_DEL`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=Gqyny3iwzbHBXhPZA0900jstLSv728HAyTmit4` + 页面标题联合识别。

### 订单管理

**Page Identity**
- 菜单路径：`运单管理 / 订单管理`
- 页面类型：叶子页面
- `pageId`：`cjeoSnhnEqLf0T5cRyoRl5BzkyiKll36wNqW04`
- 页面标题：`订单管理-小程序`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 页签：`查询结果`、`跟进详情`、`推送记录`
- 查询区/标签：`查询条件`、`queryOrderType`、`按运单查询`、`按订单查询`、`查询时间范围`、`签收状态`、`寄件客户`、`下单时间`、`寄件时间`、`派件时间`、`寄件网点`、`支付类型`
- 首屏字段：`searchOrderInput`、`BL_SIGN$text`、`CUSTOMER_CODE$text`、`SEND_SITE_CODE$text`、`PAYMENT_TYPE$text`、`SEARCH_DATE_RANGE$text`、`DISPATCH_SITE_CODE$text`、`CLASS_TYPE$text`、`STATUS$text`、`DISPATCH_MODE$text`、`ORDER_TYPE$text`
- MiniUI text/value 对：`CODE_TYPE` -> value=`CODE_TYPE$value` / submit=`CODE_TYPE`；`searchOrderInput` -> text=`searchOrderInput$text`；`BL_SIGN` -> text=`BL_SIGN$text` / value=`BL_SIGN$value` / submit=`BL_SIGN`；`CUSTOMER_CODE` -> text=`CUSTOMER_CODE$text` / value=`CUSTOMER_CODE$value` / submit=`CUSTOMER_CODE`；`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`SEND_SITE_CODE` -> text=`SEND_SITE_CODE$text` / value=`SEND_SITE_CODE$value` / submit=`SEND_SITE_CODE`；`PAYMENT_TYPE` -> text=`PAYMENT_TYPE$text` / value=`PAYMENT_TYPE$value` / submit=`PAYMENT_TYPE`；`SEARCH_DATE_RANGE` -> text=`SEARCH_DATE_RANGE$text` / value=`SEARCH_DATE_RANGE$value` / submit=`SEARCH_DATE_RANGE`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`、`查询结果`
- 保存类按钮：`撤销登记`、`撤销登记`、`撤销登记`
- 导出类按钮：`导出`、`导出`、`导出`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `撤销登记`: `#backBtn` / `xpath=//*[@id="backBtn"]` / `xpath=//a[normalize-space(.)="撤销登记"]`
- `撤销登记`: `xpath=//*[normalize-space(.)="撤销登记"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `BL_SIGN$text`: `#BL_SIGN$text` / `xpath=//*[@id="BL_SIGN$text"]` / `xpath=//*[@placeholder="请选择"]`
- `CUSTOMER_CODE$text`: `#CUSTOMER_CODE$text` / `xpath=//*[@id="CUSTOMER_CODE$text"]`
- `CODE_TYPE`: value=`CODE_TYPE$value` / submit=`CODE_TYPE`
- `searchOrderInput`: text=`searchOrderInput$text`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=TAB_CLASS&_=%3CTS%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=TAB_DISPATCH_MODE&_=%3CTS%3E`
  - `FIND_TAB_UNITE_ORDERS` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_CUSTOMER_AND_SITE` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_CUSTOMER_AND_SITE&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_SITE_ALL_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_SITE_ALL_COMBOBOX&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_TAB_UNITE_ORDERS`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_UNITE_ORDERS`
- 请求方法：`POST`
- 主查询参数键：`queryOrderType`、`CODE_TYPE`、`searchOrderInput`、`BL_SIGN`、`CUSTOMER_CODE`、`searchDateType`、`SEND_SITE_CODE`、`PAYMENT_TYPE`、`SEARCH_DATE_RANGE`、`DISPATCH_SITE_CODE`、`CLASS_TYPE`、`STATUS`、`DISPATCH_MODE`、`ORDER_TYPE`、`CREATE_DATE`、`LOGIN_SITE_CODE`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`
- 点击查询后额外请求：
  - `FIND_TAB_UNITE_ORDERS` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_UNITE_ORDERS`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_TAB_UNITE_ORDERS`
- 页面脚本提示接口：
  - `/dataQuery/findAllByCallId?id=FIND_TAB_ORDER_REVOKE`
  - `/dataQuery/findAllByCallId?id=FIND_TAB_BILL_BY_CODE`
  - `/dataOperation/saveTables`
  - `/dataQuery/findPageByCallId?id=FIND_TAB_SEND_BILL_TEMU`
  - `/dataQuery/findAllByCallId?id=FIND_TAB_SEND_BILL_TEMU`
  - `/dataQuery/findAllByCallId`
  - `/dataQuery/findPageByCallId?id=FIND_SCAN_FOCUS_IN`
  - `/dataQuery/findAllByCallId?id=FIND_ALL_EMPLOYEE_COMBOBOX2&OWNER_SITE_CODE=`
  - `/dataQuery/findAllByCallId?id=FIND_SOON_TYPE_COMBOBOX&CLASS_TYPE=`
  - `/dataQuery/findPageByCallId?id=FIND_TIME_LIMIT_COMBOBOX&LINE_NAME=`
  - `/dataQuery/findPageByCallId?id=FIND_TAB_CUSTOMER&CUSTOMER_OWNER_SITE=`
  - `/dataQuery/findPageByCallId?id=FIND_TAB_CUSTOMER`

**Field Mapping**
- 用户输入参数：`CODE_TYPE`、`searchOrderInput`、`BL_SIGN`、`CUSTOMER_CODE`、`searchDateType`、`SEND_SITE_CODE`、`PAYMENT_TYPE`、`SEARCH_DATE_RANGE`、`DISPATCH_SITE_CODE`、`CLASS_TYPE`、`STATUS`、`DISPATCH_MODE`、`ORDER_TYPE`
- 页面自动补齐参数：`queryOrderType`、`CREATE_DATE`
- 登录/站点上下文参数：`LOGIN_SITE_CODE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`CODE_TYPE` -> `CODE_TYPE`；`BL_SIGN` -> `BL_SIGN`；`CUSTOMER_CODE` -> `CUSTOMER_CODE`；`searchDateType` -> `searchDateType`；`SEND_SITE_CODE` -> `SEND_SITE_CODE`；`PAYMENT_TYPE` -> `PAYMENT_TYPE`；`SEARCH_DATE_RANGE` -> `SEARCH_DATE_RANGE`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `collectBtn1 -> collectMethodNew()`
  - `getOrderBtn -> getOrderFun();`
  - `backBtn -> backFun();`
  - `repushBtn -> repushFun();`
  - `collectBtn6 -> collectMethodNew1()`
  - `supplementDataBtn -> supplementDataFun();`
  - `getLJDateBtn -> getLJDateFun();`
  - `searchBtn -> searchMethod();`
- 保存/提交操作键：`TAB_UNITE_ORDERS_UPT`
- 前置校验/状态相关 CALL_ID：`FIND_TAB_BILL_BY_CODE`、`FIND_COUNTRY_COMBOBOX`、`FIND_COUNTY_COMBOBOX`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=cjeoSnhnEqLf0T5cRyoRl5BzkyiKll36wNqW04` + 页面标题联合识别。

### 运单基础资料修改

**Page Identity**
- 菜单路径：`运单管理 / 运单基础资料修改`
- 页面类型：叶子页面
- `pageId`：`IZfoAolO49XM1HXWFjJ6HeJeheKzhaSRFsmFeW`
- 页面标题：`运单基础资料修改`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`查询条件`、`按运单查询`、`查询时间范围`、`寄件日期`
- 首屏字段：`searchOrderInput`、`SEARCH_DATE_RANGE$text`
- MiniUI text/value 对：`CODE_TYPE` -> value=`CODE_TYPE$value` / submit=`CODE_TYPE`；`searchOrderInput` -> text=`searchOrderInput$text`；`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`SEARCH_DATE_RANGE` -> text=`SEARCH_DATE_RANGE$text` / value=`SEARCH_DATE_RANGE$value` / submit=`SEARCH_DATE_RANGE`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `SEARCH_DATE_RANGE$text`: `#SEARCH_DATE_RANGE$text` / `xpath=//*[@id="SEARCH_DATE_RANGE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `CODE_TYPE`: value=`CODE_TYPE$value` / submit=`CODE_TYPE`
- `searchOrderInput`: text=`searchOrderInput$text`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `FIND_BILL_SEND_UPT_SECONDS` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
- 主查询接口：`FIND_BILL_SEND_UPT_SECONDS`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- 请求方法：`POST`
- 主查询参数键：`CODE_TYPE`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`SEND_DATE`、`ORDER_BY_CREATE_DATE`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIND_BILL_SEND_UPT_SECONDS` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_BILL_SEND_UPT_SECONDS`
- 页面脚本提示接口：
  - `/dataQuery/findAllByCallId?id=FIND_SCAN_ALL_BYBILLCODE2`
  - `/dataQuery/findAllByCallId?id=FIND_BILL_TYPE`
  - `/dataOperation/saveTables`
  - `/dataQuery/findAllByCallId?id=FIND_TAB_SCAN_SEND_BY_BILL_CODE`
  - `/dataQuery/findAllByCallId?id=FIND_TAB_BILL_MODIFY_COUNT`
  - `/dataQuery/findAllByCallId?id=FIND_TAB_SIGN_COUNT`
  - `/dataQuery/findAllByCallId?id=FIND_TAB_SCAN_COME`

**Field Mapping**
- 用户输入参数：`CODE_TYPE`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`
- 页面自动补齐参数：`SEND_DATE`、`ORDER_BY_CREATE_DATE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`CODE_TYPE` -> `CODE_TYPE`；`searchDateType` -> `searchDateType`；`SEARCH_DATE_RANGE` -> `SEARCH_DATE_RANGE`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `clearBillCode -> //清空查询单号 mini.get("searchOrderInput").setValue("");`
  - `searchBtn -> searchMethod();`
  - `deleteBtn -> deleteFun();`
  - `editBtn -> editMethodNew3();`
  - `editBtn2 -> editMethodNew4();`
- 保存/提交操作键：`TAB_BILL_DEL`
- 前置校验/状态相关 CALL_ID：`FIND_TAB_BILL_MODIFY_COUNT`、`FIND_TAB_SIGN_COUNT`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=IZfoAolO49XM1HXWFjJ6HeJeheKzhaSRFsmFeW` + 页面标题联合识别。

### 寄件运单查询

**Page Identity**
- 菜单路径：`运单管理 / 寄件运单查询`
- 页面类型：叶子页面
- `pageId`：`jBclHk405Grg82ojV9WJwZ4TosPMF67JX7nyal`
- 页面标题：`寄件运单查询`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`查询条件`、`按运单查询`、`查询时间范围`、`寄件日期`、`派件日期`、`自联填单时间`、`签收状态`、`寄件网点`、`寄货片区`、`寄件客户`、`收件业务员`、`寄件录入网点`
- 首屏字段：`searchOrderInput`、`SEARCH_DATE_RANGE2$text`、`BL_SIGNS_MARKING$text`、`SEND_SITE_CODE$text`、`SEND_AREA_CODE$text`、`CUSTOMER_NAME`、`TAKE_PIECE_EMPLOYEE_CODE$text`、`REGISTER_SITE_CODE$text`、`SEND_PROVINCE$text`、`ORDER_TYPE$text`、`DISPATCH_SITE_CODE$text`、`WAITNOTIFY_SEND$text`
- MiniUI text/value 对：`CODE_TYPE` -> value=`CODE_TYPE$value` / submit=`CODE_TYPE`；`searchOrderInput` -> text=`searchOrderInput$text`；`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`SEARCH_DATE_RANGE1` -> text=`SEARCH_DATE_RANGE1$text` / value=`SEARCH_DATE_RANGE1$value` / submit=`SEARCH_DATE_RANGE1`；`SEARCH_DATE_RANGE2` -> text=`SEARCH_DATE_RANGE2$text` / value=`SEARCH_DATE_RANGE2$value` / submit=`SEARCH_DATE_RANGE2`；`BL_SIGNS_MARKING` -> text=`BL_SIGNS_MARKING$text` / value=`BL_SIGNS_MARKING$value` / submit=`BL_SIGNS_MARKING`；`SEND_SITE_CODE` -> text=`SEND_SITE_CODE$text` / value=`SEND_SITE_CODE$value` / submit=`SEND_SITE_CODE`；`SEND_AREA_CODE` -> text=`SEND_AREA_CODE$text` / value=`SEND_AREA_CODE$value` / submit=`SEND_AREA_CODE`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`、`查询测试`、`查询测试`
- 导出类按钮：`导出`、`导出`、`导出`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `SEARCH_DATE_RANGE2$text`: `#SEARCH_DATE_RANGE2$text` / `xpath=//*[@id="SEARCH_DATE_RANGE2$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `BL_SIGNS_MARKING$text`: `#BL_SIGNS_MARKING$text` / `xpath=//*[@id="BL_SIGNS_MARKING$text"]` / `xpath=//*[@placeholder="请选择"]`
- `CODE_TYPE`: value=`CODE_TYPE$value` / submit=`CODE_TYPE`
- `searchOrderInput`: text=`searchOrderInput$text`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `FIND_AREA_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=FIND_AREA_COMBOBOX&_=%3CTS%3E`
  - `FIND_PROVINCE_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=FIND_PROVINCE_COMBOBOX&_=%3CTS%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=TAB_DISPATCH_MODE&_=%3CTS%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=PAYMENT_TYPE&_=%3CTS%3E`
  - `FIND_PRODUCT_TYPE` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=FIND_PRODUCT_TYPE&_=%3CTS%3E`
  - `FIND_BILL_SEND` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_SITE_ONE_TWO_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 本次 live 点击未拿到稳定主查询请求，通常是因为页面需要先录入单号、先选择页签，或查询逻辑在脚本内先做前置校验。
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_BILL_SEND`
- 页面脚本提示接口：
  - `/dataQuery/findAllByCallId?id=FIND_BILL_SEND`
  - `/dataQuery/findAllByCallId?id=FIND_BILL_SEND_TEST`
  - `/dataQuery/findPageByCallId?id=FIND_TAB_BILL`
  - `/dataOperation/saveTables`
  - `/dataQuery/findAllByCallId?id=FIND_BILL_SEND_SUM`
  - `/dataQuery/findAllByCallId?id=FIND_BILL_ROUTE_STOCK_DETAIL_QRY&`

**Field Mapping**
- 用户输入参数：本次 live 查询没有稳定提取到，通常是页面先走前置校验、需要先录入单号，或查询不是默认页签。
- text/value 到提交字段：`CODE_TYPE` -> `CODE_TYPE`；`searchDateType` -> `searchDateType`；`SEARCH_DATE_RANGE1` -> `SEARCH_DATE_RANGE1`；`SEARCH_DATE_RANGE2` -> `SEARCH_DATE_RANGE2`；`BL_SIGNS_MARKING` -> `BL_SIGNS_MARKING`；`SEND_SITE_CODE` -> `SEND_SITE_CODE`；`SEND_AREA_CODE` -> `SEND_AREA_CODE`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `searchBtn -> searchMethod();`
  - `exportBtn -> exportMethod();`
  - `sbtn -> searchMethodTest();`
  - `notifyBtn -> notifyFun()`
  - `refreshBtn -> transportRefresh();`
- 保存/提交操作键：`TAB_BILL`
- 前置校验/状态相关 CALL_ID：`FIND_BILL_SEND_SUM`、`FIND_BILL_ROUTE_STOCK_DETAIL_QRY`

**Automation Notes**
- 更适合先走 DOM：当前没有稳定主查询请求，优先把页面交互顺序跑通。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=jBclHk405Grg82ojV9WJwZ4TosPMF67JX7nyal` + 页面标题联合识别。

### 派件运单查询

**Page Identity**
- 菜单路径：`运单管理 / 派件运单查询`
- 页面类型：叶子页面
- `pageId`：`hgRk9iksizvNRSBhaLryD41Cn8FRrjsAgu8Krc`
- 页面标题：`派件运单查询`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`查询条件`、`按运单查询`、`查询时间范围`、`寄件日期`、`签收日期`、`自联填单时间`、`寄件人`、`签收状态`、`产品类型`、`寄件网点`、`寄件客户`、`派件业务员`
- 首屏字段：`searchOrderInput`、`SEARCH_DATE_RANGE2$text`、`SEND_MAN$text`、`BL_SIGNS_MARKING$text`、`PRODUCT_CODE$text`、`SEND_SITE_CODE$text`、`CUSTOMER_NAME`、`DISPATCH_MAN_CODE$text`、`DISPATCH_UNDERLING_SITE_CODE$text`、`CLASS_TYPE$text`、`SEND_PROVINCE$text`、`ORDER_TYPE$text`
- MiniUI text/value 对：`CODE_TYPE` -> value=`CODE_TYPE$value` / submit=`CODE_TYPE`；`searchOrderInput` -> text=`searchOrderInput$text`；`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`SEARCH_DATE_RANGE1` -> text=`SEARCH_DATE_RANGE1$text` / value=`SEARCH_DATE_RANGE1$value` / submit=`SEARCH_DATE_RANGE1`；`SEARCH_DATE_RANGE2` -> text=`SEARCH_DATE_RANGE2$text` / value=`SEARCH_DATE_RANGE2$value` / submit=`SEARCH_DATE_RANGE2`；`SEND_MAN` -> text=`SEND_MAN$text` / value=`SEND_MAN$value` / submit=`SEND_MAN`；`BL_SIGNS_MARKING` -> text=`BL_SIGNS_MARKING$text` / value=`BL_SIGNS_MARKING$value` / submit=`BL_SIGNS_MARKING`；`PRODUCT_CODE` -> text=`PRODUCT_CODE$text` / value=`PRODUCT_CODE$value` / submit=`PRODUCT_CODE`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`
- 导出类按钮：`导出`、`导出`、`导出`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `SEARCH_DATE_RANGE2$text`: `#SEARCH_DATE_RANGE2$text` / `xpath=//*[@id="SEARCH_DATE_RANGE2$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `SEND_MAN$text`: `#SEND_MAN$text` / `xpath=//*[@id="SEND_MAN$text"]`
- `CODE_TYPE`: value=`CODE_TYPE$value` / submit=`CODE_TYPE`
- `searchOrderInput`: text=`searchOrderInput$text`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `FIND_PRODUCT_TYPE` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=FIND_PRODUCT_TYPE&_=%3CTS%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=TAB_CLASS&_=%3CTS%3E`
  - `FIND_PROVINCE_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=FIND_PROVINCE_COMBOBOX&_=%3CTS%3E`
  - `FIND_AREA_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=FIND_AREA_COMBOBOX&_=%3CTS%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=TAB_DISPATCH_MODE&_=%3CTS%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=PAYMENT_TYPE&_=%3CTS%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=SITE_LEVELS&_=%3CTS%3E`
  - `FIND_BILL_DISP` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
- 本次 live 点击未拿到稳定主查询请求，通常是因为页面需要先录入单号、先选择页签，或查询逻辑在脚本内先做前置校验。
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_BILL_DISP`
- 页面脚本提示接口：
  - `/dataQuery/findAllByCallId?id=FIND_BILL_DISP`
  - `/dataOperation/saveTables`
  - `/dataQuery/findAllByCallId?id=FIND_BILL_DISP_SUM`

**Field Mapping**
- 用户输入参数：本次 live 查询没有稳定提取到，通常是页面先走前置校验、需要先录入单号，或查询不是默认页签。
- text/value 到提交字段：`CODE_TYPE` -> `CODE_TYPE`；`searchDateType` -> `searchDateType`；`SEARCH_DATE_RANGE1` -> `SEARCH_DATE_RANGE1`；`SEARCH_DATE_RANGE2` -> `SEARCH_DATE_RANGE2`；`SEND_MAN` -> `SEND_MAN`；`BL_SIGNS_MARKING` -> `BL_SIGNS_MARKING`；`PRODUCT_CODE` -> `PRODUCT_CODE`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `searchBtn -> searchMethod();`
  - `exportBtn -> exportMethod();`
- 保存/提交操作键：`TAB_BILL`
- 前置校验/状态相关 CALL_ID：`FIND_BILL_DISP_SUM`

**Automation Notes**
- 更适合先走 DOM：当前没有稳定主查询请求，优先把页面交互顺序跑通。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=hgRk9iksizvNRSBhaLryD41Cn8FRrjsAgu8Krc` + 页面标题联合识别。

### 延迟扣款管理

**Page Identity**
- 菜单路径：`运单管理 / 延迟扣款管理`
- 页面类型：分组节点
- 分组子项：`延迟扣款申请`、`延迟扣款确认`
- 说明：该菜单项只负责展开子菜单，live 点击时可能仍停留在上一业务页，不应把当前 iframe 的 `pageId/标题` 误判为该分组节点身份。

**Automation Notes**
- 该项不应直接当成 iframe 页面处理，应该继续点击它的子菜单。

### 延迟扣款申请

**Page Identity**
- 菜单路径：`运单管理 / 延迟扣款申请`
- 页面类型：叶子页面，上级分组是 `延迟扣款管理`
- `pageId`：`KSjDBmruZFxO4gzlC2FNpQceiv9AlHVJZ9PYPn`
- 页面标题：`延迟管理-申请`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`查询条件`、`按运单查询`、`查询时间范围`、`创建时间`、`审核时间`、`申请状态`、`申请网点`、`审核确认网点`、`延迟类型`
- 首屏字段：`APPLICATION_STATUS$text`、`APPLY_SITE_CODE$text`、`searchOrderInput`、`DATE$text`、`REVIEW_SITE$text`、`DELAY_TYPE$text`
- MiniUI text/value 对：`searchOrderType` -> value=`searchOrderType$value` / submit=`searchOrderType`；`DATE_TYPE` -> value=`DATE_TYPE$value` / submit=`DATE_TYPE`；`APPLICATION_STATUS` -> text=`APPLICATION_STATUS$text` / value=`APPLICATION_STATUS$value` / submit=`APPLICATION_STATUS`；`APPLY_SITE_CODE` -> text=`APPLY_SITE_CODE$text` / value=`APPLY_SITE_CODE$value` / submit=`APPLY_SITE_CODE`；`searchOrderInput` -> text=`searchOrderInput$text`；`DATE` -> text=`DATE$text` / value=`DATE$value` / submit=`DATE`；`REVIEW_SITE` -> text=`REVIEW_SITE$text` / value=`REVIEW_SITE$value` / submit=`REVIEW_SITE`；`DELAY_TYPE` -> text=`DELAY_TYPE$text` / value=`DELAY_TYPE$value` / submit=`DELAY_TYPE`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`
- 保存类按钮：`审核确认网点`
- 导出类按钮：`导出`、`导出`、`导出`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `审核确认网点`: `xpath=//*[normalize-space(.)="审核确认网点"]`
- `APPLICATION_STATUS$text`: `#APPLICATION_STATUS$text` / `xpath=//*[@id="APPLICATION_STATUS$text"]` / `xpath=//*[@placeholder="请选择"]`
- `APPLY_SITE_CODE$text`: `#APPLY_SITE_CODE$text` / `xpath=//*[@id="APPLY_SITE_CODE$text"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `searchOrderType`: value=`searchOrderType$value` / submit=`searchOrderType`
- `DATE_TYPE`: value=`DATE_TYPE$value` / submit=`DATE_TYPE`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=EXTENSION_TYPE&_=%3CTS%3E`
  - `FIND_SITE_ALL_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_SITE_ALL_COMBOBOX&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_TAB_BILL_EXTENSION_PAGE`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- 请求方法：`POST`
- 主查询参数键：`searchOrderType`、`DATE_TYPE`、`APPLICATION_STATUS`、`APPLY_SITE_CODE`、`searchOrderInput`、`DATE`、`REVIEW_SITE`、`DELAY_TYPE`、`CREATE_TIME`、`LOGIN_SITE_CODE`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIND_TAB_BILL_EXTENSION_PAGE` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- 页面脚本提示接口：
  - `/dataOperation/saveTables`

**Field Mapping**
- 用户输入参数：`searchOrderType`、`DATE_TYPE`、`APPLICATION_STATUS`、`APPLY_SITE_CODE`、`searchOrderInput`、`DATE`、`REVIEW_SITE`、`DELAY_TYPE`
- 页面自动补齐参数：`CREATE_TIME`
- 登录/站点上下文参数：`LOGIN_SITE_CODE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`searchOrderType` -> `searchOrderType`；`DATE_TYPE` -> `DATE_TYPE`；`APPLICATION_STATUS` -> `APPLICATION_STATUS`；`APPLY_SITE_CODE` -> `APPLY_SITE_CODE`；`DATE` -> `DATE`；`REVIEW_SITE` -> `REVIEW_SITE`；`DELAY_TYPE` -> `DELAY_TYPE`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `COD_del -> mini.get('CODE').setValue('');`
  - `searchBtn -> searchMethod();`
  - `exportSave -> SaveMethod()`
  - `exportBtn -> exportMethod()`
- 保存/提交操作键：`TAB_BILL_EXTENSION_UPT`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=KSjDBmruZFxO4gzlC2FNpQceiv9AlHVJZ9PYPn` + 页面标题联合识别。

### 延迟扣款确认

**Page Identity**
- 菜单路径：`运单管理 / 延迟扣款确认`
- 页面类型：叶子页面，上级分组是 `延迟扣款管理`
- `pageId`：`dkla9vIs4h3K7kftFbS9xtf5NbFb8RvSTLlAlb`
- 页面标题：`延迟扣款确认`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`查询条件`、`按运单查询`、`查询时间范围`、`创建时间`、`审核时间`、`申请状态`、`申请网点`、`审核确认网点`、`延迟类型`
- 首屏字段：`APPLICATION_STATUS$text`、`APPLY_SITE_CODE$text`、`searchOrderInput`、`DATE$text`、`REVIEW_SITE$text`、`DELAY_TYPE$text`
- MiniUI text/value 对：`searchOrderType` -> value=`searchOrderType$value` / submit=`searchOrderType`；`DATE_TYPE` -> value=`DATE_TYPE$value` / submit=`DATE_TYPE`；`APPLICATION_STATUS` -> text=`APPLICATION_STATUS$text` / value=`APPLICATION_STATUS$value` / submit=`APPLICATION_STATUS`；`APPLY_SITE_CODE` -> text=`APPLY_SITE_CODE$text` / value=`APPLY_SITE_CODE$value` / submit=`APPLY_SITE_CODE`；`searchOrderInput` -> text=`searchOrderInput$text`；`DATE` -> text=`DATE$text` / value=`DATE$value` / submit=`DATE`；`REVIEW_SITE` -> text=`REVIEW_SITE$text` / value=`REVIEW_SITE$value` / submit=`REVIEW_SITE`；`DELAY_TYPE` -> text=`DELAY_TYPE$text` / value=`DELAY_TYPE$value` / submit=`DELAY_TYPE`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`
- 保存类按钮：`审核确认网点`、`审核确认`、`审核确认`、`审核确认`
- 导出类按钮：`导出`、`导出`、`导出`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `审核确认网点`: `xpath=//*[normalize-space(.)="审核确认网点"]`
- `审核确认`: `#exportupt` / `xpath=//*[@id="exportupt"]` / `xpath=//a[normalize-space(.)="审核确认"]`
- `APPLICATION_STATUS$text`: `#APPLICATION_STATUS$text` / `xpath=//*[@id="APPLICATION_STATUS$text"]` / `xpath=//*[@placeholder="请选择"]`
- `APPLY_SITE_CODE$text`: `#APPLY_SITE_CODE$text` / `xpath=//*[@id="APPLY_SITE_CODE$text"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `searchOrderType`: value=`searchOrderType$value` / submit=`searchOrderType`
- `DATE_TYPE`: value=`DATE_TYPE$value` / submit=`DATE_TYPE`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=EXTENSION_TYPE&_=%3CTS%3E`
  - `FIND_SITE_ALL_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_SITE_ALL_COMBOBOX&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_TAB_BILL_EXTENSION_CONFIRM`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- 请求方法：`POST`
- 主查询参数键：`searchOrderType`、`DATE_TYPE`、`APPLICATION_STATUS`、`APPLY_SITE_CODE`、`searchOrderInput`、`DATE`、`REVIEW_SITE`、`DELAY_TYPE`、`CREATE_TIME`、`LOGIN_SITE_CODE`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIND_TAB_BILL_EXTENSION_CONFIRM` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
  - `FIND_TAB_WORK_ORDER_TYPE_REMIND` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=%3CTOKEN%3E`
- 页面脚本提示接口：
  - `/dataOperation/saveTables`

**Field Mapping**
- 用户输入参数：`searchOrderType`、`DATE_TYPE`、`APPLICATION_STATUS`、`APPLY_SITE_CODE`、`searchOrderInput`、`DATE`、`REVIEW_SITE`、`DELAY_TYPE`
- 页面自动补齐参数：`CREATE_TIME`
- 登录/站点上下文参数：`LOGIN_SITE_CODE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`searchOrderType` -> `searchOrderType`；`DATE_TYPE` -> `DATE_TYPE`；`APPLICATION_STATUS` -> `APPLICATION_STATUS`；`APPLY_SITE_CODE` -> `APPLY_SITE_CODE`；`DATE` -> `DATE`；`REVIEW_SITE` -> `REVIEW_SITE`；`DELAY_TYPE` -> `DELAY_TYPE`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `COD_del -> mini.get('CODE').setValue('');`
  - `searchBtn -> searchMethod();`
  - `exportupt -> SaveMethod()`
  - `exportBtn -> exportMethod()`
- 保存/提交操作键：`TAB_BILL_EXTENSION_UPT`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=dkla9vIs4h3K7kftFbS9xtf5NbFb8RvSTLlAlb` + 页面标题联合识别。

### 主面单打印

**Page Identity**
- 菜单路径：`运单管理 / 主面单打印`
- 页面类型：叶子页面
- `pageId`：`LjydkmFTWgW4Hu270DXrG7GJj8KNhtfTrGNt6x`
- 页面标题：`面单打印模块`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`打印模板`、`查询条件`、`按运单查询`、`查询时间范围`、`寄件日期`、`派件日期`、`到件日期`、`打印运费`、`手机号加密`、`是否显示LOGO`、`收件人加密`
- 首屏字段：`searchOrderInput`、`SEARCH_DATE_RANGE$text`、`PRINT_TYPE_VIEW2$text`
- MiniUI text/value 对：`PRINT_TYPE_VIEW` -> text=`PRINT_TYPE_VIEW$text` / value=`PRINT_TYPE_VIEW$value` / submit=`PRINT_TYPE_VIEW`；`CODE_TYPE` -> value=`CODE_TYPE$value` / submit=`CODE_TYPE`；`searchOrderInput` -> text=`searchOrderInput$text`；`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`SEARCH_DATE_RANGE` -> text=`SEARCH_DATE_RANGE$text` / value=`SEARCH_DATE_RANGE$value` / submit=`SEARCH_DATE_RANGE`；`PRINT_TYPE_VIEW2` -> text=`PRINT_TYPE_VIEW2$text` / value=`PRINT_TYPE_VIEW2$value` / submit=`PRINT_TYPE_VIEW2`；`freight_type` -> value=`freight_type$value` / submit=`freight_type`；`encryption` -> value=`encryption$value` / submit=`encryption`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`
- 保存类按钮：`打印模板`、`打印面单`、`打印面单`、`打印面单`、`打印预览`、`打印预览`、`打印预览`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `打印模板`: `xpath=//*[normalize-space(.)="打印模板"]`
- `打印面单`: `#printBtn` / `xpath=//*[@id="printBtn"]` / `xpath=//a[normalize-space(.)="打印面单"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `SEARCH_DATE_RANGE$text`: `#SEARCH_DATE_RANGE$text` / `xpath=//*[@id="SEARCH_DATE_RANGE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `PRINT_TYPE_VIEW2$text`: `#PRINT_TYPE_VIEW2$text` / `xpath=//*[@id="PRINT_TYPE_VIEW2$text"]` / `xpath=//*[@placeholder="请选择"]`
- `PRINT_TYPE_VIEW`: text=`PRINT_TYPE_VIEW$text` / value=`PRINT_TYPE_VIEW$value` / submit=`PRINT_TYPE_VIEW`
- `CODE_TYPE`: value=`CODE_TYPE$value` / submit=`CODE_TYPE`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `FIDN_FACE_SINGLE_PRINT_ONE3` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
- 主查询接口：`FIDN_FACE_SINGLE_PRINT_ONE3`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- 请求方法：`POST`
- 主查询参数键：`PRINT_TYPE_VIEW`、`CODE_TYPE`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`PRINT_TYPE_VIEW2`、`freight_type`、`encryption`、`isViewLogo`、`recipientmd5`、`SEND_DATE`、`ORDER_BY_CREATE_DATE`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIDN_FACE_SINGLE_PRINT_ONE3` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIDN_FACE_SINGLE_PRINT_ONE3`
- 页面脚本提示接口：
  - `//直接打印时printCount返回的是true，阅览打印时printCount返回的是打印次数`
  - `/dataQuery/findAllByCallId?id=FIDN_FACE_SINGLE_PRINT_ONE4`
  - `/dataQuery/findAllByCallId?id=FIND_PRINT_TEMPLATE`
  - `/dataQuery/findAllByCallId?id=FIND_PRINT_LOG_COUNT&BILL_CODE=`
  - `/dataOperation/saveTables`
  - `//LODOP.SET_PRINT_STYLEA(0,`

**Field Mapping**
- 用户输入参数：`PRINT_TYPE_VIEW`、`CODE_TYPE`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`PRINT_TYPE_VIEW2`、`freight_type`、`encryption`、`isViewLogo`、`recipientmd5`
- 页面自动补齐参数：`SEND_DATE`、`ORDER_BY_CREATE_DATE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`PRINT_TYPE_VIEW` -> `PRINT_TYPE_VIEW`；`CODE_TYPE` -> `CODE_TYPE`；`searchDateType` -> `searchDateType`；`SEARCH_DATE_RANGE` -> `SEARCH_DATE_RANGE`；`PRINT_TYPE_VIEW2` -> `PRINT_TYPE_VIEW2`；`freight_type` -> `freight_type`；`encryption` -> `encryption`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `searchBtn -> searchMethod();`
  - `printBtn -> printMethodNew(1);`
  - `zoomoutBtn -> printMethodNew(2);`
  - `clearBillCode -> mini.get("searchOrderInput").setValue(""); mini.get("searchOrderInput").doValueChanged();`
- 保存/提交操作键：`TAB_PRINT_LOG_ADD`、`TAB_PRINT_COUNT_ADD`
- 前置校验/状态相关 CALL_ID：`FIND_PRINT_LOG_COUNT`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=LjydkmFTWgW4Hu270DXrG7GJj8KNhtfTrGNt6x` + 页面标题联合识别。

### 电子标签打印

**Page Identity**
- 菜单路径：`运单管理 / 电子标签打印`
- 页面类型：叶子页面
- `pageId`：`qHxIpF7wG91xv8QnG1bRFzHc0Y3Cm8JjQV5Ker`
- 页面标题：`电子标签打印-测试`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`打印模板`、`查询条件`、`按运单查询`、`查询时间范围`、`寄件日期`、`手机号加密`、`是否显示LOGO`
- 首屏字段：`searchOrderInput`、`SEARCH_DATE_RANGE$text`
- MiniUI text/value 对：`PRINT_TYPE_VIEW` -> text=`PRINT_TYPE_VIEW$text` / value=`PRINT_TYPE_VIEW$value` / submit=`PRINT_TYPE_VIEW`；`CODE_TYPE` -> value=`CODE_TYPE$value` / submit=`CODE_TYPE`；`searchOrderInput` -> text=`searchOrderInput$text`；`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`SEARCH_DATE_RANGE` -> text=`SEARCH_DATE_RANGE$text` / value=`SEARCH_DATE_RANGE$value` / submit=`SEARCH_DATE_RANGE`；`encryption` -> value=`encryption$value` / submit=`encryption`；`isViewLogo` -> value=`isViewLogo$value` / submit=`isViewLogo`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`
- 保存类按钮：`打印面单`、`打印面单`、`打印面单`、`win7打印面单`、`win7打印面单`、`win7打印面单`、`打印面单-预览`、`打印面单-预览`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `打印面单`: `#printBtn1` / `xpath=//*[@id="printBtn1"]` / `xpath=//a[normalize-space(.)="打印面单"]`
- `打印面单`: `xpath=//*[normalize-space(.)="打印面单"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `SEARCH_DATE_RANGE$text`: `#SEARCH_DATE_RANGE$text` / `xpath=//*[@id="SEARCH_DATE_RANGE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `PRINT_TYPE_VIEW`: text=`PRINT_TYPE_VIEW$text` / value=`PRINT_TYPE_VIEW$value` / submit=`PRINT_TYPE_VIEW`
- `CODE_TYPE`: value=`CODE_TYPE$value` / submit=`CODE_TYPE`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `FIDN_FACE_SINGLE_PRINT` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
- 主查询接口：`FIDN_FACE_SINGLE_PRINT`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIDN_FACE_SINGLE_PRINT`
- 请求方法：`POST`
- 主查询参数键：`PRINT_TYPE_VIEW`、`CODE_TYPE`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`encryption`、`isViewLogo`、`SEND_DATE`、`COME_DATE`、`ORDER_BY_CREATE_DATE`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIDN_FACE_SINGLE_PRINT` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIDN_FACE_SINGLE_PRINT`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIDN_FACE_SINGLE_PRINT`
- 页面脚本提示接口：
  - `//直接打印时printCount返回的是true，阅览打印时printCount返回的是打印次数`
  - `/dataQuery/findAllByCallId?id=FIDN_FACE_SINGLE_PRINT`
  - `/dataQuery/findAllByCallId?id=FIND_PRINT_TEMPLATE`
  - `/dataQuery/findAllByCallId?id=FIND_TAB_GOODS_ROUTE`
  - `/dataQuery/findAllByCallId?id=FIND_TAB_SITE_PRINT_NAME`
  - `/dataQuery/findAllByCallId?id=FIND_TAB_STORAGE_LOCATION3`
  - `/dataOperation/saveTables`
  - `//LODOP.SET_PRINT_STYLEA(0,`
  - `//LODOP.ADD_PRINT_BARCODE((obj.top),`

**Field Mapping**
- 用户输入参数：`PRINT_TYPE_VIEW`、`CODE_TYPE`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`encryption`、`isViewLogo`
- 页面自动补齐参数：`SEND_DATE`、`COME_DATE`、`ORDER_BY_CREATE_DATE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`PRINT_TYPE_VIEW` -> `PRINT_TYPE_VIEW`；`CODE_TYPE` -> `CODE_TYPE`；`searchDateType` -> `searchDateType`；`SEARCH_DATE_RANGE` -> `SEARCH_DATE_RANGE`；`encryption` -> `encryption`；`isViewLogo` -> `isViewLogo`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `searchBtn -> searchMethod();`
  - `printBtn1 -> printMethodNew1(1);`
  - `printBtn4 -> printMethodNew(1);`
  - `printBtn2 -> printMethodNew1(2);`
  - `printBtn3 -> printMethodNew(2);`
  - `clearBillCode -> //清空查询单号 mini.get("searchOrderInput").setValue(""); mini.get("searchOrderInput").doValueChanged();`
- 保存/提交操作键：`TAB_PRINT_COUNT_ADD`、`TAB_PRINT_LOG_ADD`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=qHxIpF7wG91xv8QnG1bRFzHc0Y3Cm8JjQV5Ker` + 页面标题联合识别。

### 回单打印

**Page Identity**
- 菜单路径：`运单管理 / 回单打印`
- 页面类型：叶子页面
- `pageId`：`qtFeyGS3p6YNQudStIAVuusjNKsfgkZ49w1RwG`
- 页面标题：`电子回单打印`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`查询条件`、`按运单查询`、`按回单查询`、`查询时间范围`、`寄件日期`、`打印模板`、`是否显示LOGO`
- 首屏字段：`searchOrderInput`、`SEARCH_DATE_RANGE$text`、`PRINT_TYPE_VIEW$text`、`text$text`
- MiniUI text/value 对：`CODE_TYPE` -> value=`CODE_TYPE$value` / submit=`CODE_TYPE`；`searchOrderInput` -> text=`searchOrderInput$text`；`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`SEARCH_DATE_RANGE` -> text=`SEARCH_DATE_RANGE$text` / value=`SEARCH_DATE_RANGE$value` / submit=`SEARCH_DATE_RANGE`；`PRINT_TYPE_VIEW` -> text=`PRINT_TYPE_VIEW$text` / value=`PRINT_TYPE_VIEW$value` / submit=`PRINT_TYPE_VIEW`；`text` -> text=`text$text`；`isViewLogo` -> value=`isViewLogo$value` / submit=`isViewLogo`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`
- 保存类按钮：`打印模板`、`打印回单`、`打印回单`、`打印回单`、`打印预览`、`打印预览`、`打印预览`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `打印模板`: `xpath=//*[normalize-space(.)="打印模板"]`
- `打印回单`: `#printBtn` / `xpath=//*[@id="printBtn"]` / `xpath=//a[normalize-space(.)="打印回单"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `SEARCH_DATE_RANGE$text`: `#SEARCH_DATE_RANGE$text` / `xpath=//*[@id="SEARCH_DATE_RANGE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `PRINT_TYPE_VIEW$text`: `#PRINT_TYPE_VIEW$text` / `xpath=//*[@id="PRINT_TYPE_VIEW$text"]` / `xpath=//*[@placeholder="请选择"]`
- `CODE_TYPE`: value=`CODE_TYPE$value` / submit=`CODE_TYPE`
- `searchOrderInput`: text=`searchOrderInput$text`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `FIDN_FACE_SINGLE_PRINT_RETURN` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIDN_FACE_SINGLE_PRINT_RETURN` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- 主查询接口：`FIDN_FACE_SINGLE_PRINT_RETURN`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- 请求方法：`POST`
- 主查询参数键：`CODE_TYPE`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`PRINT_TYPE_VIEW`、`isViewLogo`、`SEND_DATE`、`ORDER_BY_CREATE_DATE`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIDN_FACE_SINGLE_PRINT_RETURN` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIDN_FACE_SINGLE_PRINT_RETURN`
- 页面脚本提示接口：
  - `/dataQuery/findAllByCallId?id=FIDN_FACE_SINGLE_PRINT`
  - `/dataQuery/findAllByCallId?id=FIND_PRINT_TEMPLATE`
  - `/dataQuery/findAllByCallId?id=FIND_PRINT_LOG_COUNT&BILL_CODE=`
  - `//直接打印时printCount返回的是true，阅览打印时printCount返回的是打印次数`
  - `/dataOperation/saveTables`

**Field Mapping**
- 用户输入参数：`CODE_TYPE`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`PRINT_TYPE_VIEW`、`isViewLogo`
- 页面自动补齐参数：`SEND_DATE`、`ORDER_BY_CREATE_DATE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`CODE_TYPE` -> `CODE_TYPE`；`searchDateType` -> `searchDateType`；`SEARCH_DATE_RANGE` -> `SEARCH_DATE_RANGE`；`PRINT_TYPE_VIEW` -> `PRINT_TYPE_VIEW`；`isViewLogo` -> `isViewLogo`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `searchBtn -> searchMethod();`
  - `printBtn -> printMethod(1);`
  - `zoomoutBtb -> printMethod(2);`
  - `clearBillCode -> //清空查询单号 mini.get("searchOrderInput").setValue(""); mini.get("searchOrderInput").doValueChanged();`
- 保存/提交操作键：`TAB_PRINT_LOG_ADD`、`TAB_PRINT_COUNT_ADD`
- 前置校验/状态相关 CALL_ID：`FIND_PRINT_LOG_COUNT`、`FIDN_FACE_SINGLE_PRINT_RETURN`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=qtFeyGS3p6YNQudStIAVuusjNKsfgkZ49w1RwG` + 页面标题联合识别。

### 子单绑定主单查询

**Page Identity**
- 菜单路径：`运单管理 / 子单绑定主单查询`
- 页面类型：叶子页面
- `pageId`：`VrEqMUt0lg26IcWkxbO8SIlQTvsajDVLJDyYG0`
- 页面标题：`查询是否子单绑定主单`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`查询条件`、`单号`
- 首屏字段：`searchOrderInput`
- MiniUI text/value 对：`searchOrderInput` -> text=`searchOrderInput$text`
- 查询按钮：`查询`、`查询`、`查询`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `查询`: `xpath=//*[normalize-space(.)="查询"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `searchOrderInput`: text=`searchOrderInput$text`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
- 本次 live 点击未拿到稳定主查询请求，通常是因为页面需要先录入单号、先选择页签，或查询逻辑在脚本内先做前置校验。

**Field Mapping**
- 用户输入参数：本次 live 查询没有稳定提取到，通常是页面先走前置校验、需要先录入单号，或查询不是默认页签。

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `clearBtn -> mini.get("searchOrderInput").setValue("")`
  - `searchBtn -> query();`

**Automation Notes**
- 更适合先走 DOM：当前没有稳定主查询请求，优先把页面交互顺序跑通。
- 页面识别建议：优先用 `pageId=VrEqMUt0lg26IcWkxbO8SIlQTvsajDVLJDyYG0` + 页面标题联合识别。

### 打印日志查询

**Page Identity**
- 菜单路径：`运单管理 / 打印日志查询`
- 页面类型：叶子页面
- `pageId`：`uDjVBftIy5dXVF83x7Y9A6fVs6b5YlVbEIN8tz`
- 页面标题：`打印日志查询`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`查询条件`、`按单号查询`、`查询时间范围`、`打印时间`、`打印网点`、`打印数据来源`、`打印类型`、`打印单类型`
- 首屏字段：`searchOrderInput`、`SEARCH_DATE_RANGE$text`、`CREATE_SITE_CODE$text`、`DATA_FROM$text`、`BILL_TYPE$text`、`PRINT_TYPE$text`
- MiniUI text/value 对：`CODE_TYPE` -> value=`CODE_TYPE$value` / submit=`CODE_TYPE`；`searchOrderInput` -> text=`searchOrderInput$text`；`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`SEARCH_DATE_RANGE` -> text=`SEARCH_DATE_RANGE$text` / value=`SEARCH_DATE_RANGE$value` / submit=`SEARCH_DATE_RANGE`；`CREATE_SITE_CODE` -> text=`CREATE_SITE_CODE$text` / value=`CREATE_SITE_CODE$value` / submit=`CREATE_SITE_CODE`；`DATA_FROM` -> text=`DATA_FROM$text` / value=`DATA_FROM$value` / submit=`DATA_FROM`；`BILL_TYPE` -> text=`BILL_TYPE$text` / value=`BILL_TYPE$value` / submit=`BILL_TYPE`；`PRINT_TYPE` -> text=`PRINT_TYPE$text` / value=`PRINT_TYPE$value` / submit=`PRINT_TYPE`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`
- 保存类按钮：`打印网点`、`打印数据来源`、`打印类型`、`打印单类型`
- 导出类按钮：`导出`、`导出`、`导出`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `打印网点`: `xpath=//*[normalize-space(.)="打印网点"]`
- `打印数据来源`: `xpath=//*[normalize-space(.)="打印数据来源"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `SEARCH_DATE_RANGE$text`: `#SEARCH_DATE_RANGE$text` / `xpath=//*[@id="SEARCH_DATE_RANGE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `CREATE_SITE_CODE$text`: `#CREATE_SITE_CODE$text` / `xpath=//*[@id="CREATE_SITE_CODE$text"]`
- `CODE_TYPE`: value=`CODE_TYPE$value` / submit=`CODE_TYPE`
- `searchOrderInput`: text=`searchOrderInput$text`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `FIND_TAB_PRINT_COUNT` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_ALL_SITE_INFO` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_ALL_SITE_INFO&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_TAB_PRINT_COUNT`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_PRINT_COUNT`
- 请求方法：`POST`
- 主查询参数键：`CODE_TYPE`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`CREATE_SITE_CODE`、`DATA_FROM`、`BILL_TYPE`、`PRINT_TYPE`、`CREATE_DATE`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIND_TAB_PRINT_COUNT` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_PRINT_COUNT`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_TAB_PRINT_COUNT`

**Field Mapping**
- 用户输入参数：`CODE_TYPE`、`searchOrderInput`、`searchDateType`、`SEARCH_DATE_RANGE`、`CREATE_SITE_CODE`、`DATA_FROM`、`BILL_TYPE`、`PRINT_TYPE`
- 页面自动补齐参数：`CREATE_DATE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`CODE_TYPE` -> `CODE_TYPE`；`searchDateType` -> `searchDateType`；`SEARCH_DATE_RANGE` -> `SEARCH_DATE_RANGE`；`CREATE_SITE_CODE` -> `CREATE_SITE_CODE`；`DATA_FROM` -> `DATA_FROM`；`BILL_TYPE` -> `BILL_TYPE`；`PRINT_TYPE` -> `PRINT_TYPE`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `clearBillCode -> //清空查询单号 mini.get("searchOrderInput").setValue("");`
  - `searchBtn -> searchMethod()`
  - `exportBtn -> exportMethod()`
- 前置校验/状态相关 CALL_ID：`FIND_TAB_PRINT_COUNT`、`FIND_ALL_SITE_INFO`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 页面识别建议：优先用 `pageId=uDjVBftIy5dXVF83x7Y9A6fVs6b5YlVbEIN8tz` + 页面标题联合识别。

### 运单数据修记录

**Page Identity**
- 菜单路径：`运单管理 / 运单数据修记录`
- 页面类型：叶子页面
- `pageId`：`ndxUN27BL8NN6rvcFP6VLLtJNHJLhdHpQSInLg`
- 页面标题：`数据修改记录查询-运单修改查询`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`查询条件`、`按运单查询`、`按回单查询`、`修改时间范围`、`修改类型`、`修改网点`、`修改人`
- 首屏字段：`searchOrderInput`、`OPERATION_DATE$text`、`OPERATION_TYPE$text`、`MODIFY_SITE_CODE$text`、`MODIFIER$text`
- MiniUI text/value 对：`searchOrderType` -> value=`searchOrderType$value` / submit=`searchOrderType`；`searchOrderInput` -> text=`searchOrderInput$text`；`OPERATION_DATE` -> text=`OPERATION_DATE$text` / value=`OPERATION_DATE$value` / submit=`OPERATION_DATE`；`OPERATION_TYPE` -> text=`OPERATION_TYPE$text` / value=`OPERATION_TYPE$value` / submit=`OPERATION_TYPE`；`MODIFY_SITE_CODE` -> text=`MODIFY_SITE_CODE$text` / value=`MODIFY_SITE_CODE$value` / submit=`MODIFY_SITE_CODE`；`MODIFIER` -> text=`MODIFIER$text` / value=`MODIFIER$value` / submit=`MODIFIER`
- 查询按钮：`查询`、`查询`、`查询`
- 导出类按钮：`导出`、`导出`、`导出`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `查询`: `xpath=//*[normalize-space(.)="查询"]`
- `searchOrderInput`: `#searchOrderInput$text` / `xpath=//*[@id="searchOrderInput$text"]` / `xpath=//*[@name="searchOrderInput"]`
- `OPERATION_DATE$text`: `#OPERATION_DATE$text` / `xpath=//*[@id="OPERATION_DATE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `OPERATION_TYPE$text`: `#OPERATION_TYPE$text` / `xpath=//*[@id="OPERATION_TYPE$text"]` / `xpath=//*[@placeholder="全部"]`
- `searchOrderType`: value=`searchOrderType$value` / submit=`searchOrderType`
- `searchOrderInput`: text=`searchOrderInput$text`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `FIND_TAB_MODIFY_BILL` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_TAB_SITE_COMBOBOX_LIMIT` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_TAB_EMPLOYEE_SYSTEM_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_TAB_MODIFY_BILL`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_MODIFY_BILL`
- 请求方法：`POST`
- 主查询参数键：`searchOrderType`、`searchOrderInput`、`OPERATION_DATE`、`OPERATION_TYPE`、`MODIFY_SITE_CODE`、`MODIFIER`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIND_TAB_MODIFY_BILL` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_MODIFY_BILL`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_TAB_MODIFY_BILL`

**Field Mapping**
- 用户输入参数：`searchOrderType`、`searchOrderInput`、`OPERATION_DATE`、`OPERATION_TYPE`、`MODIFY_SITE_CODE`、`MODIFIER`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`searchOrderType` -> `searchOrderType`；`OPERATION_DATE` -> `OPERATION_DATE`；`OPERATION_TYPE` -> `OPERATION_TYPE`；`MODIFY_SITE_CODE` -> `MODIFY_SITE_CODE`；`MODIFIER` -> `MODIFIER`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `deleteBillCode -> //清空查询单号 mini.get("searchOrderInput").setValue("");`
  - `searchBtn -> searchMethod();`
  - `exportBtn -> exportMethod()`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 页面识别建议：优先用 `pageId=ndxUN27BL8NN6rvcFP6VLLtJNHJLhdHpQSInLg` + 页面标题联合识别。
