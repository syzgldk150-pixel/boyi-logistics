# 预付款管理 页面深拆

## Summary

- 叶子页数量：`11`
- 分组节点数量：`0`
- 高价值页面：`结算明细查询`、`预付款账户管理`、`网商结算账户维护`、`固定费用设置`、`预付款余额查询`

## Pages

### 预付款账户管理

**Page Identity**
- 菜单路径：`预付款管理 / 预付款账户管理`
- 页面类型：叶子页面
- `pageId`：`vA6RjsBp90OqceeRGdXY8uHmmetkAefiK7whRF`
- 页面标题：`预付款账户管理`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`查询条件`、`开通时间`、`余额不足时锁机`、`是`、`财务中心`、`网点名称`、`所属分拨中心`、`所属片区`、`账户币种`、`余额状态`、`启用状态`、`只查一级网点`
- 首屏字段：`START_DATE$text`、`CENTER_NAME_CODE$text`、`SITE_NAME_CODE$text`、`SUPERIOR_SITE_CODE$text`、`AREA_NAME$text`、`CURRENCY$text`、`CONFIRM_MONEY$text`、`BL_OPEN$text`
- MiniUI text/value 对：`START_DATE` -> text=`START_DATE$text` / value=`START_DATE$value` / submit=`START_DATE`；`BL_LOCK` -> value=`BL_LOCK$value` / submit=`BL_LOCK`；`CENTER_NAME_CODE` -> text=`CENTER_NAME_CODE$text` / value=`CENTER_NAME_CODE$value` / submit=`CENTER_NAME_CODE`；`SITE_NAME_CODE` -> text=`SITE_NAME_CODE$text` / value=`SITE_NAME_CODE$value` / submit=`SITE_NAME_CODE`；`SUPERIOR_SITE_CODE` -> text=`SUPERIOR_SITE_CODE$text` / value=`SUPERIOR_SITE_CODE$value` / submit=`SUPERIOR_SITE_CODE`；`AREA_NAME` -> text=`AREA_NAME$text` / value=`AREA_NAME$value` / submit=`AREA_NAME`；`CURRENCY` -> text=`CURRENCY$text` / value=`CURRENCY$value` / submit=`CURRENCY`；`CONFIRM_MONEY` -> text=`CONFIRM_MONEY$text` / value=`CONFIRM_MONEY$value` / submit=`CONFIRM_MONEY`
- 查询按钮：`查询`、`查询`、`查询`
- 保存类按钮：`新增`、`新增`、`新增`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `查询`: `xpath=//*[normalize-space(.)="查询"]`
- `新增`: `#addBtn` / `xpath=//*[@id="addBtn"]` / `xpath=//a[normalize-space(.)="新增"]`
- `新增`: `xpath=//*[normalize-space(.)="新增"]`
- `START_DATE$text`: `#START_DATE$text` / `xpath=//*[@id="START_DATE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `CENTER_NAME_CODE$text`: `#CENTER_NAME_CODE$text` / `xpath=//*[@id="CENTER_NAME_CODE$text"]`
- `SITE_NAME_CODE$text`: `#SITE_NAME_CODE$text` / `xpath=//*[@id="SITE_NAME_CODE$text"]`
- `START_DATE`: text=`START_DATE$text` / value=`START_DATE$value` / submit=`START_DATE`
- `BL_LOCK`: value=`BL_LOCK$value` / submit=`BL_LOCK`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=ACCOUNT_CURRENCY&_=%3CTS%3E`
  - `FIND_BALANCE_ACCOUNT_INFO` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_TAB_SITE_COMBOBOX_ALL` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_SITE_CENTER_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_AREA_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_AREA_COMBOBOX&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_SITE_FINANCE_CENTER_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&LOGIN_SITE_CODE=%3CLOGIN_SITE_CODE%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_BALANCE_ACCOUNT_INFO`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- 请求方法：`POST`
- 主查询参数键：`START_DATE`、`BL_LOCK`、`CENTER_NAME_CODE`、`SITE_NAME_CODE`、`SUPERIOR_SITE_CODE`、`AREA_NAME`、`CURRENCY`、`CONFIRM_MONEY`、`BL_OPEN`、`SITE_CODE_ONE`、`SUPERIOR_DIRECTLY`、`PARENT_NAME_CODE`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIND_BALANCE_ACCOUNT_INFO` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_BALANCE_ACCOUNT_INFO`
- 页面脚本提示接口：
  - `/dataQuery/findPageByCallId?id=FIND_SITE_FINANCE_CENTER_COMBOBOX&LOGIN_SITE_CODE=`
  - `/dataQuery/findPageByCallId?id=FIND_SITE_FINANCE_CENTER_COMBOBOX`
  - `/dataQuery/findAllByCallId?id=FIND_SITE_IS_RIGHT&SITE_CODE=`
  - `/dataQuery/findAllByCallId?id=FIND_OTHER_ACC&SITE_NAME=`
  - `/dataOperation/saveTables`
  - `/dataQuery/findPageByCallId?id=FIND_ACCOUNT_FROZEN_INFO`

**Field Mapping**
- 用户输入参数：`START_DATE`、`BL_LOCK`、`CENTER_NAME_CODE`、`SITE_NAME_CODE`、`AREA_NAME`、`CURRENCY`、`CONFIRM_MONEY`、`BL_OPEN`、`SITE_CODE_ONE`、`SUPERIOR_DIRECTLY`
- 登录/站点上下文参数：`SUPERIOR_SITE_CODE`、`PARENT_NAME_CODE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`START_DATE` -> `START_DATE`；`BL_LOCK` -> `BL_LOCK`；`CENTER_NAME_CODE` -> `CENTER_NAME_CODE`；`SITE_NAME_CODE` -> `SITE_NAME_CODE`；`SUPERIOR_SITE_CODE` -> `SUPERIOR_SITE_CODE`；`AREA_NAME` -> `AREA_NAME`；`CURRENCY` -> `CURRENCY`；`CONFIRM_MONEY` -> `CONFIRM_MONEY`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `searchBtn -> searchMethod();`
  - `addBtn -> addMethod();`
  - `editBtn -> editMethod();`
  - `resetBtn -> resetMethod();`
- 保存/提交操作键：`TAB_BALANCE_ACCOUNT_UPT`
- 前置校验/状态相关 CALL_ID：`FIND_ACCOUNT_FROZEN_INFO`、`FIND_BALANCE_ACCOUNT_INFO`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=vA6RjsBp90OqceeRGdXY8uHmmetkAefiK7whRF` + 页面标题联合识别。

### 预付款余额查询

**Page Identity**
- 菜单路径：`预付款管理 / 预付款余额查询`
- 页面类型：叶子页面
- `pageId`：`qzY0aHTy6c0qVe5DglmNuGpkrHhEv4MiweU9PQ`
- 页面标题：`预付款余额查询`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`查询条件`、`查询时间`、`结算日期`、`财务中心`、`所属分拨中心`、`网点名称`、`所属片区`、`账户币种`、`只查一级网点`、`启用状态`、`只查直营财务中心`
- 首屏字段：`BALANCE_DATE$text`、`CENTER_NAME_CODE$text`、`SUPERIOR_SITE_CODE$text`、`SITE_NAME_CODE$text`、`AREA_NAME$text`、`CURRENCY$text`、`BL_OPEN$text`
- MiniUI text/value 对：`searchDateType` -> value=`searchDateType$value` / submit=`searchDateType`；`BALANCE_DATE` -> text=`BALANCE_DATE$text` / value=`BALANCE_DATE$value` / submit=`BALANCE_DATE`；`CENTER_NAME_CODE` -> text=`CENTER_NAME_CODE$text` / value=`CENTER_NAME_CODE$value` / submit=`CENTER_NAME_CODE`；`SUPERIOR_SITE_CODE` -> text=`SUPERIOR_SITE_CODE$text` / value=`SUPERIOR_SITE_CODE$value` / submit=`SUPERIOR_SITE_CODE`；`SITE_NAME_CODE` -> text=`SITE_NAME_CODE$text` / value=`SITE_NAME_CODE$value` / submit=`SITE_NAME_CODE`；`AREA_NAME` -> text=`AREA_NAME$text` / value=`AREA_NAME$value` / submit=`AREA_NAME`；`CURRENCY` -> text=`CURRENCY$text` / value=`CURRENCY$value` / submit=`CURRENCY`；`SITE_CODE_ONE` -> value=`SITE_CODE_ONE$value` / submit=`SITE_CODE_ONE`
- 查询按钮：`查询时间`、`查询`、`查询`、`查询`
- 导出类按钮：`导出`、`导出`、`导出`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间`: `xpath=//*[normalize-space(.)="查询时间"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `BALANCE_DATE$text`: `#BALANCE_DATE$text` / `xpath=//*[@id="BALANCE_DATE$text"]`
- `CENTER_NAME_CODE$text`: `#CENTER_NAME_CODE$text` / `xpath=//*[@id="CENTER_NAME_CODE$text"]`
- `SUPERIOR_SITE_CODE$text`: `#SUPERIOR_SITE_CODE$text` / `xpath=//*[@id="SUPERIOR_SITE_CODE$text"]`
- `searchDateType`: value=`searchDateType$value` / submit=`searchDateType`
- `BALANCE_DATE`: text=`BALANCE_DATE$text` / value=`BALANCE_DATE$value` / submit=`BALANCE_DATE`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=ACCOUNT_CURRENCY&_=%3CTS%3E`
  - `FIND_BALANCE_ACCOUNT_INFO_C` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_SITE_CENTER_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_TAB_SITE_COMBOBOX_NO_FINCENTER` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_AREA_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_AREA_COMBOBOX&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_SITE_FINANCE_CENTER_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&LOGIN_SITE_CODE=%3CLOGIN_SITE_CODE%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_BALANCE_ACCOUNT_INFO_C`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- 请求方法：`POST`
- 主查询参数键：`searchDateType`、`BALANCE_DATE`、`CENTER_NAME_CODE`、`SUPERIOR_SITE_CODE`、`SITE_NAME_CODE`、`AREA_NAME`、`CURRENCY`、`SITE_CODE_ONE`、`BL_OPEN`、`SUPERIOR_DIRECTLY`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIND_BALANCE_ACCOUNT_INFO_C` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_BALANCE_ACCOUNT_INFO_C`
- 页面脚本提示接口：
  - `/dataQuery/findPageByCallId?id=FIND_SITE_FINANCE_CENTER_COMBOBOX&LOGIN_SITE_CODE=`
  - `/dataQuery/findPageByCallId?id=FIND_SITE_FINANCE_CENTER_COMBOBOX`
  - `/dataQuery/findPageByCallId?id=FIND_BALANCE_ACCOUNT_INFO_CNEW`
  - `/dataQuery/findPageByCallId?id=FIND_BALANCE_ACCOUNT_INFO_C`

**Field Mapping**
- 用户输入参数：`searchDateType`、`BALANCE_DATE`、`CENTER_NAME_CODE`、`SITE_NAME_CODE`、`AREA_NAME`、`CURRENCY`、`SITE_CODE_ONE`、`BL_OPEN`、`SUPERIOR_DIRECTLY`
- 登录/站点上下文参数：`SUPERIOR_SITE_CODE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`searchDateType` -> `searchDateType`；`BALANCE_DATE` -> `BALANCE_DATE`；`CENTER_NAME_CODE` -> `CENTER_NAME_CODE`；`SUPERIOR_SITE_CODE` -> `SUPERIOR_SITE_CODE`；`SITE_NAME_CODE` -> `SITE_NAME_CODE`；`AREA_NAME` -> `AREA_NAME`；`CURRENCY` -> `CURRENCY`；`SITE_CODE_ONE` -> `SITE_CODE_ONE`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `searchBtn -> searchMethod();`
  - `exportBtn -> exportMethod();`
- 前置校验/状态相关 CALL_ID：`FIND_BALANCE_ACCOUNT_INFO_CNEW`、`FIND_BALANCE_ACCOUNT_INFO_C`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 页面识别建议：优先用 `pageId=qzY0aHTy6c0qVe5DglmNuGpkrHhEv4MiweU9PQ` + 页面标题联合识别。

### 结算明细录入

**Page Identity**
- 菜单路径：`预付款管理 / 结算明细录入`
- 页面类型：叶子页面
- `pageId`：`OSYzdI24xjQQ2xOp6HDpFnHmPMlnVz6iSZ8ONW`
- 页面标题：`结算明细录入`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`数据操作`、`异步审核标识`、`网点编号`、`财务中心编号`、`数据来源`、`所属财务中心`、`结算人编号`、`确认金额`、`结算网点编号`、`结算网点`、`SITE_TYPE`、`结算类型的收付类型`
- 首屏字段：`SITE_NAME`、`CENTER_NAME`、`BALANCE_MAN`、`BALANCE_TYPE`、`BALANCE_CUR_MONEY`、`BILL_CODE`、`REMARK`
- MiniUI text/value 对：`SITE_NAME_CODE` -> text=`SITE_NAME_CODE$text` / value=`SITE_NAME_CODE$value` / submit=`SITE_NAME_CODE`；`CENTER_NAME_CODE` -> text=`CENTER_NAME_CODE$text` / value=`CENTER_NAME_CODE$value` / submit=`CENTER_NAME_CODE`；`BALANCE_MAN` -> text=`BALANCE_MAN$text`；`BALANCE_TYPE` -> text=`BALANCE_TYPE$text` / value=`BALANCE_TYPE$value` / submit=`BALANCE_TYPE`；`BALANCE_CUR_MONEY` -> text=`BALANCE_CUR_MONEY$text`；`BILL_CODE` -> text=`BILL_CODE$text`；`REMARK` -> text=`REMARK$text`
- 保存类按钮：`保存`、`保存`、`保存`
- 导出类按钮：`下载导入模板`、`下载导入模板`、`下载导入模板`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `保存`: `#saveBtn` / `xpath=//*[@id="saveBtn"]` / `xpath=//a[normalize-space(.)="保存"]`
- `保存`: `xpath=//*[normalize-space(.)="保存"]`
- `SITE_NAME`: `#SITE_NAME_CODE$text` / `xpath=//*[@id="SITE_NAME_CODE$text"]` / `xpath=//*[@name="SITE_NAME"]`
- `CENTER_NAME`: `#CENTER_NAME_CODE$text` / `xpath=//*[@id="CENTER_NAME_CODE$text"]` / `xpath=//*[@name="CENTER_NAME"]`
- `BALANCE_MAN`: `#BALANCE_MAN$text` / `xpath=//*[@id="BALANCE_MAN$text"]` / `xpath=//*[@name="BALANCE_MAN"]`
- `SITE_NAME_CODE`: text=`SITE_NAME_CODE$text` / value=`SITE_NAME_CODE$value` / submit=`SITE_NAME_CODE`
- `CENTER_NAME_CODE`: text=`CENTER_NAME_CODE$text` / value=`CENTER_NAME_CODE$value` / submit=`CENTER_NAME_CODE`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_BALANCE_TYPE_DETAIL_COMBOX` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=%3CTOKEN%3E&BL_SITE=1&_=%3CTS%3E`
  - `FIND_TAB_SITE_HM2` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_SITE_HM2&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_BALANCE_ADD_SC_SITE_WST` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 本次 live 点击未拿到稳定主查询请求，通常是因为页面需要先录入单号、先选择页签，或查询逻辑在脚本内先做前置校验。
- 页面脚本提示接口：
  - `/dataQuery/findPageByCallId?id=FIND_TAB_SITEALL_BOOT`
  - `/dataQuery/findPageByCallId?id=FIND_BALANCE_ADD_SC_SITE_WST`
  - `/dataQuery/findAllByCallId?id=FIND_BALANCE_ADD_BAC_TYPE_WST_NEW`
  - `/dataQuery/findAllByCallId?id=FIND_BALANCE_TYPE_DETAIL_COMBOX&BL_USE=1`
  - `/dataQuery/findAllByCallId?id=FIND_BALANCE_TYPE_DETAIL_COMBOX&BL_SITE=1`
  - `/dataQuery/findAllByCallId?id=FIND_BALANCE_ADD_CK_ACC_WST`
  - `/dataQuery/findAllByCallId?id=FIND_BALANCE_ACCOUNT_INFO`
  - `/dataQuery/findAllByCallId?id=FIND_BILL_TRANSFER_CODE`
  - `/dataQuery/findAllByCallId`
  - `/dataQuery/findAllByCallId?id=FIND_BALANCE_ADD_BAC_TYPE_WST&BL_INPUT=1`
  - `/dataOperation/saveTables`

**Field Mapping**
- 用户输入参数：本次 live 查询没有稳定提取到，通常是页面先走前置校验、需要先录入单号，或查询不是默认页签。
- text/value 到提交字段：`SITE_NAME_CODE` -> `SITE_NAME_CODE`；`CENTER_NAME_CODE` -> `CENTER_NAME_CODE`；`BALANCE_TYPE` -> `BALANCE_TYPE`

**State / Validation Logic**
- 查询入口：没有稳定点击到默认查询按钮，说明此页可能依赖录入单号、页签切换或前置校验。
- 点击绑定（从内联脚本截取）：
  - `delBtn -> opData("_GRID_DEL");`
  - `saveBtn -> saveData();`
  - `downloadBtn -> $Z.page.downImportTempldate("TAB_BALANCE_DETAIL");`
  - `selectUploadFile -> fileSelect();`
- 保存/提交操作键：`TAB_BALANCE_DETAIL_ADD`、`TAB_BALANCE_DETAIL`
- 前置校验/状态相关 CALL_ID：`FIND_BALANCE_TYPE_DETAIL_COMBOX`、`FIND_BALANCE_ACCOUNT_INFO`

**Automation Notes**
- 更适合先走 DOM：当前没有稳定主查询请求，优先把页面交互顺序跑通。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=OSYzdI24xjQQ2xOp6HDpFnHmPMlnVz6iSZ8ONW` + 页面标题联合识别。

### 结算明细查询

**Page Identity**
- 菜单路径：`预付款管理 / 结算明细查询`
- 页面类型：叶子页面
- `pageId`：`TV8EsFNSS0OktABJguiq9MMmG7BIVCn390MJB4`
- 页面标题：`结算明细查询`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 页签：`预付款余额查询`、`明细查询`、`统计查询`、`汇总数据`、`明细数据`
- 查询区/标签：`查询条件`、`财务中心`、`网点名称`、`网点类型`、`所属片区`、`启用状态`、`所属分拨中心`、`只查一级网点`、`只查直营财务中心`、`按运单查询`、`查询时间范围`、`结算日期`
- 首屏字段：`SUM_CENTER_NAME_CODE$text`、`SUM_SITE_NAME_CODE$text`、`SUM_SITE_TYPE$text`、`AREA_NAME$text`、`BL_OPEN$text`、`SUPERIOR_SITE_CODE$text`
- MiniUI text/value 对：`SUM_CENTER_NAME_CODE` -> text=`SUM_CENTER_NAME_CODE$text` / value=`SUM_CENTER_NAME_CODE$value` / submit=`CENTER_NAME_CODE`；`SUM_SITE_NAME_CODE` -> text=`SUM_SITE_NAME_CODE$text` / value=`SUM_SITE_NAME_CODE$value` / submit=`SITE_NAME_CODE`；`SUM_SITE_TYPE` -> text=`SUM_SITE_TYPE$text` / value=`SUM_SITE_TYPE$value` / submit=`SITE_TYPE`；`AREA_NAME` -> text=`AREA_NAME$text` / value=`AREA_NAME$value` / submit=`AREA_NAME`；`BL_OPEN` -> text=`BL_OPEN$text` / value=`BL_OPEN$value` / submit=`BL_OPEN`；`SUPERIOR_SITE_CODE` -> text=`SUPERIOR_SITE_CODE$text` / value=`SUPERIOR_SITE_CODE$value` / submit=`SUPERIOR_SITE_CODE`；`SITE_CODE_ONE` -> value=`SITE_CODE_ONE$value` / submit=`SITE_CODE_ONE`；`SUPERIOR_DIRECTLY` -> value=`SUPERIOR_DIRECTLY$value` / submit=`SUPERIOR_DIRECTLY`
- 查询按钮：`预付款余额查询`、`明细查询`、`统计查询`、`查询`、`查询`、`查询`
- 导出类按钮：`导出`、`导出`、`导出`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `预付款余额查询`: `xpath=//*[normalize-space(.)="预付款余额查询"]`
- `明细查询`: `xpath=//*[normalize-space(.)="明细查询"]`
- `SUM_CENTER_NAME_CODE$text`: `#SUM_CENTER_NAME_CODE$text` / `xpath=//*[@id="SUM_CENTER_NAME_CODE$text"]`
- `SUM_SITE_NAME_CODE$text`: `#SUM_SITE_NAME_CODE$text` / `xpath=//*[@id="SUM_SITE_NAME_CODE$text"]`
- `SUM_SITE_TYPE$text`: `#SUM_SITE_TYPE$text` / `xpath=//*[@id="SUM_SITE_TYPE$text"]` / `xpath=//*[@placeholder="全部"]`
- `SUM_CENTER_NAME_CODE`: text=`SUM_CENTER_NAME_CODE$text` / value=`SUM_CENTER_NAME_CODE$value` / submit=`CENTER_NAME_CODE`
- `SUM_SITE_NAME_CODE`: text=`SUM_SITE_NAME_CODE$text` / value=`SUM_SITE_NAME_CODE$value` / submit=`SITE_NAME_CODE`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=SITE_TYPE&_=%3CTS%3E`
  - `FIND_AREA_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=FIND_AREA_COMBOBOX&_=%3CTS%3E`
  - `FIND_BALANCE_ADD_BAC_TYPE_WST` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=%3CTOKEN%3E&_=%3CTS%3E`
  - `FIND_BALANCE_ACCOUNT_INFO` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_BALANCE_QRY_WST` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_BALANCE_QRY_TJ_WST` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
- 主查询接口：`FIND_BALANCE_ACCOUNT_INFO`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- 请求方法：`POST`
- 主查询参数键：`CENTER_NAME_CODE`、`SITE_NAME_CODE`、`SITE_TYPE`、`AREA_NAME`、`BL_OPEN`、`SUPERIOR_SITE_CODE`、`SITE_CODE_ONE`、`SUPERIOR_DIRECTLY`、`PARENT_NAME_CODE`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIND_BALANCE_ACCOUNT_INFO` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_BALANCE_ACCOUNT_INFO`
  - `/dataQuery/findPageByCallId?id=FIND_BALANCE_QRY_WST`
  - `/dataQuery/findPageByCallId?id=FIND_BALANCE_QRY_TJ_WST`
  - `/dataQuery/findPageByCallId?id=FIND_BALANCE_QRY_TJ_DETAIL`
- 页面脚本提示接口：
  - `/dataQuery/findPageByCallId?id=FIND_BALANCE_SC_SITE_WST_COMBOX2&LOGIN_SITE_CODE=`
  - `/dataQuery/findPageByCallId?id=FIND_CETER_AND_SITE&SITE_CODE=`
  - `/dataQuery/findPageByCallId?id=FIND_SITE_DATA_ONE&SITE_CODE=`
  - `/dataQuery/findPageByCallId?id=FIND_BALANCE_SC_SITE_WST_COMBOX&LOGIN_SITE_CODE=`
  - `/dataOperation/saveTables`
  - `/dataQuery/findAllByCallId?id=FIND_BALANCE_QRY_WST_SUM_WITH_SITE`
  - `/dataQuery/findAllByCallId?id=FIND_BALANCE_QRY_WST_SUM`
  - `/dataQuery/findPageByCallId?id=FIND_BALANCE_QRY_WST_WITH_SITE`
  - `/dataQuery/findPageByCallId?id=FIND_BALANCE_QRY_WST`

**Field Mapping**
- 用户输入参数：`CENTER_NAME_CODE`、`SITE_NAME_CODE`、`SITE_TYPE`、`AREA_NAME`、`BL_OPEN`、`SITE_CODE_ONE`、`SUPERIOR_DIRECTLY`
- 登录/站点上下文参数：`SUPERIOR_SITE_CODE`、`PARENT_NAME_CODE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`SUM_CENTER_NAME_CODE` -> `CENTER_NAME_CODE`；`SUM_SITE_NAME_CODE` -> `SITE_NAME_CODE`；`SUM_SITE_TYPE` -> `SITE_TYPE`；`AREA_NAME` -> `AREA_NAME`；`BL_OPEN` -> `BL_OPEN`；`SUPERIOR_SITE_CODE` -> `SUPERIOR_SITE_CODE`；`SITE_CODE_ONE` -> `SITE_CODE_ONE`；`SUPERIOR_DIRECTLY` -> `SUPERIOR_DIRECTLY`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchSumBtn`。
- 点击绑定（从内联脚本截取）：
  - `searchSumBtn -> query1();`
  - `exportSumBtn -> var data=$Z.page.getFormData("searchAccountForm"); if($Z.user.getUserInfo().loginSiteType!='总部'){ data.PARENT_NAME_CODE=$Z.user.getUserInfo().loginSiteCode;`
- 保存/提交操作键：`TAB_BALANCE_DETAIL_UPT`
- 前置校验/状态相关 CALL_ID：`FIND_BALANCE_QRY_WST_SUM_WITH_SITE`、`FIND_BALANCE_QRY_WST_SUM`、`FIND_BALANCE_ACCOUNT_INFO`、`FIND_BALANCE_QRY_TJ_DETAIL`、`FIND_BALANCE_SUM_NAME`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=TV8EsFNSS0OktABJguiq9MMmG7BIVCn390MJB4` + 页面标题联合识别。

### 固定费用设置

**Page Identity**
- 菜单路径：`预付款管理 / 固定费用设置`
- 页面类型：叶子页面
- `pageId`：`zadfJDcOkgzUbirE5KjIchzXEsEa8ZImx2GmMs`
- 页面标题：`固定费用设置`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`查询条件`、`使用网点`、`扣款方式`、`扣款类型`、`扣款网点`、`是否启用`、`分拨中心`
- 首屏字段：`CENTER_NAME_CODE$text`、`FREQUENCY$text`、`FEETYPE$text`、`SITE_CODE$text`、`BL_OPEN$text`、`DISTRIBUTION_CENTER_CODE$text`
- MiniUI text/value 对：`CENTER_NAME_CODE` -> text=`CENTER_NAME_CODE$text` / value=`CENTER_NAME_CODE$value` / submit=`CENTER_NAME_CODE`；`FREQUENCY` -> text=`FREQUENCY$text` / value=`FREQUENCY$value` / submit=`FREQUENCY`；`FEETYPE` -> text=`FEETYPE$text` / value=`FEETYPE$value` / submit=`FEETYPE`；`SITE_CODE` -> text=`SITE_CODE$text` / value=`SITE_CODE$value` / submit=`SITE_CODE`；`BL_OPEN` -> text=`BL_OPEN$text` / value=`BL_OPEN$value` / submit=`BL_OPEN`；`DISTRIBUTION_CENTER_CODE` -> text=`DISTRIBUTION_CENTER_CODE$text` / value=`DISTRIBUTION_CENTER_CODE$value` / submit=`DISTRIBUTION_CENTER_CODE`
- 查询按钮：`查询`、`查询`、`查询`
- 保存类按钮：`新增`、`新增`、`新增`
- 导出类按钮：`导出`、`导出`、`导出`、`下载导入模板`、`下载导入模板`、`下载导入模板`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `查询`: `xpath=//*[normalize-space(.)="查询"]`
- `新增`: `#addBtn` / `xpath=//*[@id="addBtn"]` / `xpath=//a[normalize-space(.)="新增"]`
- `新增`: `xpath=//*[normalize-space(.)="新增"]`
- `CENTER_NAME_CODE$text`: `#CENTER_NAME_CODE$text` / `xpath=//*[@id="CENTER_NAME_CODE$text"]`
- `FREQUENCY$text`: `#FREQUENCY$text` / `xpath=//*[@id="FREQUENCY$text"]` / `xpath=//*[@placeholder="全部"]`
- `FEETYPE$text`: `#FEETYPE$text` / `xpath=//*[@id="FEETYPE$text"]` / `xpath=//*[@placeholder="请选择"]`
- `CENTER_NAME_CODE`: text=`CENTER_NAME_CODE$text` / value=`CENTER_NAME_CODE$value` / submit=`CENTER_NAME_CODE`
- `FREQUENCY`: text=`FREQUENCY$text` / value=`FREQUENCY$value` / submit=`FREQUENCY`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `https://tms.ronghuiwl.com/minic/combobox?optionCode=FREQUENCY&_=%3CTS%3E`
  - `FIND_BALANCE_STATICFEE_TYPE_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=%3CTOKEN%3E&_=%3CTS%3E`
  - `FIND_BALANCE_STATICFEE` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_SITE_KICK_CENTER_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_TAB_SITE_COMBOBOX_JJW` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_SITE_COMBOBOX_TYPE_NEW_ONE` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_BALANCE_STATICFEE`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_BALANCE_STATICFEE`
- 请求方法：`POST`
- 主查询参数键：`CENTER_NAME_CODE`、`FREQUENCY`、`FEETYPE`、`SITE_CODE`、`BL_OPEN`、`DISTRIBUTION_CENTER_CODE`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIND_BALANCE_STATICFEE` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_BALANCE_STATICFEE`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_BALANCE_STATICFEE`
- 页面脚本提示接口：
  - `/dataOperation/saveTables`

**Field Mapping**
- 用户输入参数：`CENTER_NAME_CODE`、`FREQUENCY`、`FEETYPE`、`BL_OPEN`、`DISTRIBUTION_CENTER_CODE`
- 登录/站点上下文参数：`SITE_CODE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`CENTER_NAME_CODE` -> `CENTER_NAME_CODE`；`FREQUENCY` -> `FREQUENCY`；`FEETYPE` -> `FEETYPE`；`SITE_CODE` -> `SITE_CODE`；`BL_OPEN` -> `BL_OPEN`；`DISTRIBUTION_CENTER_CODE` -> `DISTRIBUTION_CENTER_CODE`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `searchBtn -> searchMethod();`
  - `addBtn -> addMethod();`
  - `editBtn -> editMethod();`
  - `deleteBtn -> deleteMethod();`
  - `exportBtn -> exportMethod();`
  - `importBtn -> importData();`
  - `importTempBtn -> importTemp();`
- 保存/提交操作键：`TAB_BALANCE_STATICFEE_DEL`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=zadfJDcOkgzUbirE5KjIchzXEsEa8ZImx2GmMs` + 页面标题联合识别。

### 预付款充值申请

**Page Identity**
- 菜单路径：`预付款管理 / 预付款充值申请`
- 页面类型：叶子页面
- `pageId`：`H96jza7SinUWFL4T6vY0ieUwVhX1K82OBxSYt3`
- 页面标题：`预付款充值申请`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`查询条件`、`申请时间`、`申请网点`、`充值状态`、`流水号`、`充值渠道`、`申请总金额:`、`降低充值手续费，请尽快完成网商银行的注册跟授权工作，详情请咨询相应的大区总！`
- 首屏字段：`APPLY_DATE$text`、`APPLY_SITE`、`BL_AUDIT$text`、`SEQ_NO`、`ACCOUNT_TYPE$text`、`MONEY_TOTAL`
- MiniUI text/value 对：`APPLY_DATE` -> text=`APPLY_DATE$text` / value=`APPLY_DATE$value` / submit=`APPLY_DATE`；`APPLY_SITE_CODE` -> text=`APPLY_SITE_CODE$text` / value=`APPLY_SITE_CODE$value` / submit=`APPLY_SITE_CODE`；`BL_AUDIT` -> text=`BL_AUDIT$text` / value=`BL_AUDIT$value` / submit=`BL_AUDIT`；`SEQ_NO` -> text=`SEQ_NO$text`；`ACCOUNT_TYPE` -> text=`ACCOUNT_TYPE$text` / value=`ACCOUNT_TYPE$value` / submit=`ACCOUNT_TYPE`；`MONEY_TOTAL` -> text=`MONEY_TOTAL$text`
- 查询按钮：`查询`、`查询`、`查询`
- 保存类按钮：`新增`、`新增`、`新增`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `查询`: `xpath=//*[normalize-space(.)="查询"]`
- `新增`: `#addBtn` / `xpath=//*[@id="addBtn"]` / `xpath=//a[normalize-space(.)="新增"]`
- `新增`: `xpath=//*[normalize-space(.)="新增"]`
- `APPLY_DATE$text`: `#APPLY_DATE$text` / `xpath=//*[@id="APPLY_DATE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `APPLY_SITE`: `#APPLY_SITE_CODE$text` / `xpath=//*[@id="APPLY_SITE_CODE$text"]` / `xpath=//*[@name="APPLY_SITE"]`
- `BL_AUDIT$text`: `#BL_AUDIT$text` / `xpath=//*[@id="BL_AUDIT$text"]` / `xpath=//*[@placeholder="全部"]`
- `APPLY_DATE`: text=`APPLY_DATE$text` / value=`APPLY_DATE$value` / submit=`APPLY_DATE`
- `APPLY_SITE_CODE`: text=`APPLY_SITE_CODE$text` / value=`APPLY_SITE_CODE$value` / submit=`APPLY_SITE_CODE`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `FIND_TAB_ADVANCE_RECHARGE` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_AGENT_SITE_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_TAB_ADVANCE_RECHARGE_TOTALMONEY`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=%3CTOKEN%3E`
- 请求方法：`POST`
- 主查询参数键：`APPLY_DATE`、`APPLY_SITE_CODE`、`APPLY_SITE`、`BL_AUDIT`、`SEQ_NO`、`ACCOUNT_TYPE`、`MONEY_TOTAL`
- 点击查询后额外请求：
  - `FIND_TAB_ADVANCE_RECHARGE_TOTALMONEY` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=%3CTOKEN%3E`
  - `FIND_TAB_ADVANCE_RECHARGE` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_TAB_ADVANCE_RECHARGE`
- 页面脚本提示接口：
  - `/dataQuery/findAllByCallId?id=FIND_TAB_ADVANCE_RECHARGE_TOTALMONEY`
  - `/dataQuery/findAllByCallId?id=FIND_SITE_BANKACCOUNT_COMBOBOX&USE_SITE_CODE=`
  - `/dataQuery/findAllByCallId?id=FIND_CHECK_BALANCE_ACCOUNT&BL_OPEN=1&SITE_NAME_CODE=`

**Field Mapping**
- 用户输入参数：`APPLY_DATE`、`APPLY_SITE_CODE`、`APPLY_SITE`、`BL_AUDIT`、`SEQ_NO`、`ACCOUNT_TYPE`、`MONEY_TOTAL`
- text/value 到提交字段：`APPLY_DATE` -> `APPLY_DATE`；`APPLY_SITE_CODE` -> `APPLY_SITE_CODE`；`BL_AUDIT` -> `BL_AUDIT`；`ACCOUNT_TYPE` -> `ACCOUNT_TYPE`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `searchBtn -> serachMethod()`
  - `addBtn -> addMethod()`
  - `editBtn -> editMethod()`
  - `transferBtn -> showTransferLog();`
- 前置校验/状态相关 CALL_ID：`FIND_SITE_BANKACCOUNT_COMBOBOX`、`FIND_CHECK_BALANCE_ACCOUNT`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 页面识别建议：优先用 `pageId=H96jza7SinUWFL4T6vY0ieUwVhX1K82OBxSYt3` + 页面标题联合识别。

### 预付款提现申请

**Page Identity**
- 菜单路径：`预付款管理 / 预付款提现申请`
- 页面类型：叶子页面
- `pageId`：`4WU7NSzJrXDnVV2n0KKL4t5WiNZvqSiIwYUR9N`
- 页面标题：`预付款提现申请`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`查询条件`、`申请时间`、`申请网点`、`审核状态`
- 首屏字段：`APPLY_WITHDRAWALS_DATE$text`、`APPLY_WITH_SITE_CODE$text`、`BL_AUDITING$text`
- MiniUI text/value 对：`APPLY_WITHDRAWALS_DATE` -> text=`APPLY_WITHDRAWALS_DATE$text` / value=`APPLY_WITHDRAWALS_DATE$value` / submit=`APPLY_WITHDRAWALS_DATE`；`APPLY_WITH_SITE_CODE` -> text=`APPLY_WITH_SITE_CODE$text` / value=`APPLY_WITH_SITE_CODE$value` / submit=`APPLY_WITH_SITE_CODE`；`BL_AUDITING` -> text=`BL_AUDITING$text` / value=`BL_AUDITING$value` / submit=`BL_AUDITING`
- 查询按钮：`查询`、`查询`、`查询`
- 保存类按钮：`审核状态`
- 导出类按钮：`导出`、`导出`、`导出`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `查询`: `xpath=//*[normalize-space(.)="查询"]`
- `审核状态`: `xpath=//*[normalize-space(.)="审核状态"]`
- `APPLY_WITHDRAWALS_DATE$text`: `#APPLY_WITHDRAWALS_DATE$text` / `xpath=//*[@id="APPLY_WITHDRAWALS_DATE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `APPLY_WITH_SITE_CODE$text`: `#APPLY_WITH_SITE_CODE$text` / `xpath=//*[@id="APPLY_WITH_SITE_CODE$text"]`
- `BL_AUDITING$text`: `#BL_AUDITING$text` / `xpath=//*[@id="BL_AUDITING$text"]` / `xpath=//*[@placeholder="全部"]`
- `APPLY_WITHDRAWALS_DATE`: text=`APPLY_WITHDRAWALS_DATE$text` / value=`APPLY_WITHDRAWALS_DATE$value` / submit=`APPLY_WITHDRAWALS_DATE`
- `APPLY_WITH_SITE_CODE`: text=`APPLY_WITH_SITE_CODE$text` / value=`APPLY_WITH_SITE_CODE$value` / submit=`APPLY_WITH_SITE_CODE`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `FIND_TAB_ADVANCE_WITHDRAWALS` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_AGENT_SITE_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_TAB_ADVANCE_WITHDRAWALS`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- 请求方法：`POST`
- 主查询参数键：`APPLY_WITHDRAWALS_DATE`、`APPLY_WITH_SITE_CODE`、`BL_AUDITING`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIND_TAB_ADVANCE_WITHDRAWALS` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_TAB_ADVANCE_WITHDRAWALS`
- 页面脚本提示接口：
  - `/dataQuery/findAllByCallId?id=FIND_TAB_BALANCE_ACCOUNT_FREEZE`

**Field Mapping**
- 用户输入参数：`APPLY_WITHDRAWALS_DATE`、`APPLY_WITH_SITE_CODE`、`BL_AUDITING`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`APPLY_WITHDRAWALS_DATE` -> `APPLY_WITHDRAWALS_DATE`；`APPLY_WITH_SITE_CODE` -> `APPLY_WITH_SITE_CODE`；`BL_AUDITING` -> `BL_AUDITING`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `searchBtn -> serachMethod();`
  - `applyBtn -> applyMethod()`
  - `exportBtn -> exportMethod()`
- 前置校验/状态相关 CALL_ID：`FIND_TAB_BALANCE_ACCOUNT_FREEZE`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 页面识别建议：优先用 `pageId=4WU7NSzJrXDnVV2n0KKL4t5WiNZvqSiIwYUR9N` + 页面标题联合识别。

### 预付款扫码充值

**Page Identity**
- 菜单路径：`预付款管理 / 预付款扫码充值`
- 页面类型：叶子页面
- `pageId`：`7UpOxFCGgb0awZVChh3qsJ60I346NejCAFpXaE`
- 页面标题：`融辉充值管理页面`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`查询条件`、`充值时间`、`充值网点`、`充值方式`、`充值状态`、`流水号`、`充值金额:`、`降低充值手续费，请尽快完成网商银行的注册跟授权工作，详情请咨询相应的大区总！`
- 首屏字段：`BALANCE_DATE$text`、`SITE_NAME`、`DATA_FORM$text`、`BL_CONFIRM$text`、`GUID`、`MONEY_TOTAL`
- MiniUI text/value 对：`BALANCE_DATE` -> text=`BALANCE_DATE$text` / value=`BALANCE_DATE$value` / submit=`BALANCE_DATE`；`SITE_NAME_CODE` -> text=`SITE_NAME_CODE$text` / value=`SITE_NAME_CODE$value` / submit=`SITE_NAME_CODE`；`DATA_FORM` -> text=`DATA_FORM$text` / value=`DATA_FORM$value` / submit=`DATA_FORM`；`BL_CONFIRM` -> text=`BL_CONFIRM$text` / value=`BL_CONFIRM$value` / submit=`BL_CONFIRM`；`GUID` -> text=`GUID$text`；`MONEY_TOTAL` -> text=`MONEY_TOTAL$text`
- 查询按钮：`查询`、`查询`、`查询`
- 保存类按钮：`新增`、`新增`、`新增`
- 导出类按钮：`导出`、`导出`、`导出`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `查询`: `xpath=//*[normalize-space(.)="查询"]`
- `新增`: `#addBtn` / `xpath=//*[@id="addBtn"]` / `xpath=//a[normalize-space(.)="新增"]`
- `新增`: `xpath=//*[normalize-space(.)="新增"]`
- `BALANCE_DATE$text`: `#BALANCE_DATE$text` / `xpath=//*[@id="BALANCE_DATE$text"]` / `xpath=//*[@placeholder="请选择起止日期"]`
- `SITE_NAME`: `#SITE_NAME_CODE$text` / `xpath=//*[@id="SITE_NAME_CODE$text"]` / `xpath=//*[@name="SITE_NAME"]`
- `DATA_FORM$text`: `#DATA_FORM$text` / `xpath=//*[@id="DATA_FORM$text"]` / `xpath=//*[@placeholder="全部"]`
- `BALANCE_DATE`: text=`BALANCE_DATE$text` / value=`BALANCE_DATE$value` / submit=`BALANCE_DATE`
- `SITE_NAME_CODE`: text=`SITE_NAME_CODE$text` / value=`SITE_NAME_CODE$value` / submit=`SITE_NAME_CODE`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `FIND_TAB_RECHARGE_RECORD` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_AGENT_SITE_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_TAB_RECHARGE_RECORD_SUMMONEY`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=%3CTOKEN%3E`
- 请求方法：`POST`
- 主查询参数键：`BALANCE_DATE`、`SITE_NAME_CODE`、`SITE_NAME`、`DATA_FORM`、`BL_CONFIRM`、`GUID`、`MONEY_TOTAL`
- 点击查询后额外请求：
  - `FIND_TAB_RECHARGE_RECORD_SUMMONEY` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=%3CTOKEN%3E`
  - `FIND_TAB_RECHARGE_RECORD` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_TAB_RECHARGE_RECORD`
- 页面脚本提示接口：
  - `/dataQuery/findAllByCallId?id=FIND_TAB_RECHARGE_RECORD_SUMMONEY`
  - `/dataQuery/findAllByCallId`
  - `/dataQuery/findAllByCallId?id=FIND_NOW`
  - `/dataQuery/findAllByCallId?id=FIND_SITE_BANKACCOUNT_COMBOBOX&USE_SITE_CODE=`
  - `/dataQuery/findAllByCallId?id=FIND_CHECK_BALANCE_ACCOUNT&BL_OPEN=1&SITE_NAME_CODE=`

**Field Mapping**
- 用户输入参数：`BALANCE_DATE`、`SITE_NAME_CODE`、`SITE_NAME`、`DATA_FORM`、`BL_CONFIRM`、`GUID`、`MONEY_TOTAL`
- text/value 到提交字段：`BALANCE_DATE` -> `BALANCE_DATE`；`SITE_NAME_CODE` -> `SITE_NAME_CODE`；`DATA_FORM` -> `DATA_FORM`；`BL_CONFIRM` -> `BL_CONFIRM`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `searchBtn -> serachMethod()`
  - `addBtn -> addMethod()`
  - `exportBtn -> exportMethod()`
  - `transferBtn -> showTransferLog();`
- 前置校验/状态相关 CALL_ID：`FIND_TAB_RECHARGE_RECORD_SUMMONEY`、`FIND_SITE_BANKACCOUNT_COMBOBOX`、`FIND_CHECK_BALANCE_ACCOUNT`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 页面识别建议：优先用 `pageId=7UpOxFCGgb0awZVChh3qsJ60I346NejCAFpXaE` + 页面标题联合识别。

### 网商结算账户维护

**Page Identity**
- 菜单路径：`预付款管理 / 网商结算账户维护`
- 页面类型：叶子页面
- `pageId`：`mrn49OiLVQ7vPU4G8uqUFHllU77hOGgQcJBIEg`
- 页面标题：`网商结算账户维护`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 页签：`查询结果`、`授权记录`
- 查询区/标签：`查询条件`、`网点名称`、`网商银行账户名`、`网商银行卡号`、`授权状态`、`子户卡号`、`启用状态`、`降低充值手续费，请尽快完成网商银行的注册跟授权工作，详情请咨询相应的大区总！ 转账银行名称：浙江网商银行 转账联行号：323331000001`
- 首屏字段：`SITE_NAME`、`ACCOUNT_NAME`、`BANK_ACCOUNT`、`AUTH_STATUS$text`、`SUB_BANK_ACCOUNT`、`ACTIVE_STATUS$text`
- MiniUI text/value 对：`SITE_CODE` -> text=`SITE_CODE$text` / value=`SITE_CODE$value` / submit=`SITE_CODE`；`ACCOUNT_NAME` -> text=`ACCOUNT_NAME$text`；`BANK_ACCOUNT` -> text=`BANK_ACCOUNT$text`；`AUTH_STATUS` -> text=`AUTH_STATUS$text` / value=`AUTH_STATUS$value` / submit=`AUTH_STATUS`；`SUB_BANK_ACCOUNT` -> text=`SUB_BANK_ACCOUNT$text`；`ACTIVE_STATUS` -> text=`ACTIVE_STATUS$text` / value=`ACTIVE_STATUS$value` / submit=`ACTIVE_STATUS`
- 查询按钮：`查询`、`查询`、`查询`、`查询结果`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `查询`: `xpath=//*[normalize-space(.)="查询"]`
- `SITE_NAME`: `#SITE_CODE$text` / `xpath=//*[@id="SITE_CODE$text"]` / `xpath=//*[@name="SITE_NAME"]`
- `ACCOUNT_NAME`: `#ACCOUNT_NAME$text` / `xpath=//*[@id="ACCOUNT_NAME$text"]` / `xpath=//*[@name="ACCOUNT_NAME"]`
- `BANK_ACCOUNT`: `#BANK_ACCOUNT$text` / `xpath=//*[@id="BANK_ACCOUNT$text"]` / `xpath=//*[@name="BANK_ACCOUNT"]`
- `SITE_CODE`: text=`SITE_CODE$text` / value=`SITE_CODE$value` / submit=`SITE_CODE`
- `ACCOUNT_NAME`: text=`ACCOUNT_NAME$text`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `FIND_TAB_MERCHANT_SETTLE_ACCOUNT` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_TAB_MERCHANT_SETTLE_ACCOUNT` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
  - `FIND_TAB_SITE_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_SITE_COMBOBOX&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
  - `FIND_TAB_NOTICE_NOT_SEE` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=FIND_TAB_NOTICE_NOT_SEE`
  - `FIND_TAB_NOTICE_1688ANDTEMU` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=%3CTOKEN%3E`
  - `FIND_TAB_WORK_ORDER_TYPE_REMIND` -> `https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=%3CTOKEN%3E`
- 主查询接口：`FIND_TAB_MERCHANT_SETTLE_ACCOUNT`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- 请求方法：`POST`
- 主查询参数键：`SITE_CODE`、`SITE_NAME`、`ACCOUNT_NAME`、`BANK_ACCOUNT`、`AUTH_STATUS`、`SUB_BANK_ACCOUNT`、`ACTIVE_STATUS`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIND_TAB_MERCHANT_SETTLE_ACCOUNT` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_TAB_MERCHANT_SETTLE_ACCOUNT`
- 页面脚本提示接口：
  - `/dataOperation/saveTables`
  - `/dataQuery/findAllByCallId?id=FIND_WS_SUBACCOUNT_SEQ`
  - `/dataQuery/findPageByCallId?id=FIND_TAB_MERCHANT_SETTLE_ACCOUNT2`

**Field Mapping**
- 用户输入参数：`SITE_NAME`、`ACCOUNT_NAME`、`BANK_ACCOUNT`、`AUTH_STATUS`、`SUB_BANK_ACCOUNT`、`ACTIVE_STATUS`
- 登录/站点上下文参数：`SITE_CODE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`SITE_CODE` -> `SITE_CODE`；`AUTH_STATUS` -> `AUTH_STATUS`；`ACTIVE_STATUS` -> `ACTIVE_STATUS`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `searchBtn -> searchMethod();`
  - `authBtn -> addMethod();`
  - `editBtn -> editMethod();`
  - `findMoneyBtn -> fundMoneyMethod();`
- 保存/提交操作键：`TAB_MERCHANT_SETTLE_ACCOUNT_UPT`
- 前置校验/状态相关 CALL_ID：`FIND_WS_SUBACCOUNT_SEQ`、`FIND_TAB_MERCHANT_SETTLE_ACCOUNT2`、`FIND_TAB_MERCHANT_SETTLE_ACCOUNT`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 存在保存链路，自动化时必须区分“只读查询”和“真正落库”的动作。
- 页面识别建议：优先用 `pageId=mrn49OiLVQ7vPU4G8uqUFHllU77hOGgQcJBIEg` + 页面标题联合识别。

### 代收货款查询结果

**Page Identity**
- 菜单路径：`预付款管理 / 代收货款查询结果`
- 页面类型：叶子页面
- `pageId`：`P0YR2JElDN31ESZNxPxRCElJzaaLUYMlAVV4qq`
- 页面标题：`代收货款结果查询`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 页签：`查询结果`、`支付明细`
- 查询区/标签：`查询条件`、`按运单查询`、`查询时间范围`、`寄件网点`、`派件网点`、`寄件日期`、`签收日期`、`支付状态`、`支付方式`、`返款状态`、`返款时效`、`一次收取5元更改费`
- 首屏字段：`SEND_SITE_CODE$text`、`DISPATCH_SITE_CODE$text`、`BL_PAY$text`、`PAY_TYPE$text`、`searchOrderInput`、`DATE$text`、`REFUND_STATUS$text`、`REBATE_CYCLE$text`
- MiniUI text/value 对：`searchOrderType` -> value=`searchOrderType$value` / submit=`searchOrderType`；`SEND_SITE_CODE` -> text=`SEND_SITE_CODE$text` / value=`SEND_SITE_CODE$value` / submit=`SEND_SITE_CODE`；`DISPATCH_SITE_CODE` -> text=`DISPATCH_SITE_CODE$text` / value=`DISPATCH_SITE_CODE$value` / submit=`DISPATCH_SITE_CODE`；`DATE_TYPE` -> value=`DATE_TYPE$value` / submit=`DATE_TYPE`；`BL_PAY` -> text=`BL_PAY$text` / value=`BL_PAY$value` / submit=`BL_PAY`；`PAY_TYPE` -> text=`PAY_TYPE$text` / value=`PAY_TYPE$value` / submit=`PAY_TYPE`；`searchOrderInput` -> text=`searchOrderInput$text`；`DATE` -> text=`DATE$text` / value=`DATE$value` / submit=`DATE`
- 查询按钮：`查询时间范围`、`查询`、`查询`、`查询`、`查询结果`
- 导出类按钮：`导出`、`导出`、`导出`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询时间范围`: `xpath=//*[normalize-space(.)="查询时间范围"]`
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `SEND_SITE_CODE$text`: `#SEND_SITE_CODE$text` / `xpath=//*[@id="SEND_SITE_CODE$text"]`
- `DISPATCH_SITE_CODE$text`: `#DISPATCH_SITE_CODE$text` / `xpath=//*[@id="DISPATCH_SITE_CODE$text"]`
- `BL_PAY$text`: `#BL_PAY$text` / `xpath=//*[@id="BL_PAY$text"]` / `xpath=//*[@placeholder="请选择"]`
- `searchOrderType`: value=`searchOrderType$value` / submit=`searchOrderType`
- `SEND_SITE_CODE`: text=`SEND_SITE_CODE$text` / value=`SEND_SITE_CODE$value` / submit=`SEND_SITE_CODE`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `FIND_SITE_ALL_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_SITE_ALL_COMBOBOX&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_TAB_GOODS_PAGE_NEW`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_GOODS_PAGE_NEW`
- 请求方法：`POST`
- 主查询参数键：`searchOrderType`、`SEND_SITE_CODE`、`DISPATCH_SITE_CODE`、`DATE_TYPE`、`BL_PAY`、`PAY_TYPE`、`searchOrderInput`、`DATE`、`REFUND_STATUS`、`REBATE_CYCLE`、`SEND_DATE`、`LOGIN_SITE_CODE`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIND_TAB_GOODS_PAGE_NEW` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_TAB_GOODS_PAGE_NEW`
- 页面脚本提示接口：
  - `/dataQuery/findPageByCallId?id=FIND_TAB_RECHARGE_RECORD_DSHK&BILL_CODE=`

**Field Mapping**
- 用户输入参数：`searchOrderType`、`SEND_SITE_CODE`、`DISPATCH_SITE_CODE`、`DATE_TYPE`、`BL_PAY`、`PAY_TYPE`、`searchOrderInput`、`DATE`、`REFUND_STATUS`、`REBATE_CYCLE`
- 页面自动补齐参数：`SEND_DATE`
- 登录/站点上下文参数：`LOGIN_SITE_CODE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`searchOrderType` -> `searchOrderType`；`SEND_SITE_CODE` -> `SEND_SITE_CODE`；`DISPATCH_SITE_CODE` -> `DISPATCH_SITE_CODE`；`DATE_TYPE` -> `DATE_TYPE`；`BL_PAY` -> `BL_PAY`；`PAY_TYPE` -> `PAY_TYPE`；`DATE` -> `DATE`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `COD_del -> mini.get('CODE').setValue('');`
  - `searchBtn -> searchMethod();`
  - `exportBtn -> exportMethod()`
  - `RecPayBut -> Recpayclick();`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 页面识别建议：优先用 `pageId=P0YR2JElDN31ESZNxPxRCElJzaaLUYMlAVV4qq` + 页面标题联合识别。

### 代收额度明细查询

**Page Identity**
- 菜单路径：`预付款管理 / 代收额度明细查询`
- 页面类型：叶子页面
- `pageId`：`3YIwLCXTNdTri3Cf7VetKwUiKixIejvCSHCfuH`
- 页面标题：`代收货款明细`
- iframe URL：`/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`

**DOM Structure**
- 查询区/标签：`按运单查询`、`类型`、`账户编码`、`网点名称`
- 首屏字段：`TRANSACTION_TYPE$text`、`ACCOUNT_CODE`、`SITE_CODE$text`、`BILL_CODE`
- MiniUI text/value 对：`searchOrderType` -> value=`searchOrderType$value` / submit=`searchOrderType`；`TRANSACTION_TYPE` -> text=`TRANSACTION_TYPE$text` / value=`TRANSACTION_TYPE$value` / submit=`TRANSACTION_TYPE`；`ACCOUNT_CODE` -> text=`ACCOUNT_CODE$text`；`SITE_CODE` -> text=`SITE_CODE$text` / value=`SITE_CODE$value` / submit=`SITE_CODE`；`BILL_CODE` -> text=`BILL_CODE$text`
- 查询按钮：`查询`、`查询`、`查询`
- 导出类按钮：`导出`、`导出`、`导出`
- Grid / 子 Grid：首屏未稳定拿到列头，通常需要切页签、先查询或等待二次渲染。
- 稳定选择器候选：
- `查询`: `#searchBtn` / `xpath=//*[@id="searchBtn"]` / `xpath=//a[normalize-space(.)="查询"]`
- `查询`: `xpath=//*[normalize-space(.)="查询"]`
- `TRANSACTION_TYPE$text`: `#TRANSACTION_TYPE$text` / `xpath=//*[@id="TRANSACTION_TYPE$text"]` / `xpath=//*[@placeholder="请选择"]`
- `ACCOUNT_CODE`: `#ACCOUNT_CODE$text` / `xpath=//*[@id="ACCOUNT_CODE$text"]` / `xpath=//*[@name="ACCOUNT_CODE"]`
- `SITE_CODE$text`: `#SITE_CODE$text` / `xpath=//*[@id="SITE_CODE$text"]`
- `searchOrderType`: value=`searchOrderType$value` / submit=`searchOrderType`
- `TRANSACTION_TYPE`: text=`TRANSACTION_TYPE$text` / value=`TRANSACTION_TYPE$value` / submit=`TRANSACTION_TYPE`

**Network Flow**
- 首屏初始化：
  - `https://tms.ronghuiwl.com/widget/home?authenticationKey=%3CAUTH_KEY%3E&pageId=%3CTOKEN%3E&_t=%3CT%3E&_winid=%3CWINID%3E`
  - `FIND_TAB_INVOICING_ACCOUNT_DETAIL_PAGE` -> `https://tms.ronghuiwl.com/userView/getUserViewByUrl`
  - `FIND_SITE_ALL_COMBOBOX` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=FIND_SITE_ALL_COMBOBOX&pageSize=100&pageIndex=0&key=&_=%3CTS%3E`
- 主查询接口：`FIND_TAB_INVOICING_ACCOUNT_DETAIL_PAGE`
- 主查询地址：`https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- 请求方法：`POST`
- 主查询参数键：`searchOrderType`、`TRANSACTION_TYPE`、`ACCOUNT_CODE`、`SITE_CODE`、`BILL_CODE`、`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- 点击查询后额外请求：
  - `FIND_TAB_INVOICING_ACCOUNT_DETAIL_PAGE` -> `https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=%3CTOKEN%3E`
- `userView/getUserViewByUrl`：
  - `/dataQuery/findPageByCallId?id=FIND_TAB_INVOICING_ACCOUNT_DETAIL_PAGE`

**Field Mapping**
- 用户输入参数：`searchOrderType`、`TRANSACTION_TYPE`、`ACCOUNT_CODE`、`BILL_CODE`
- 登录/站点上下文参数：`SITE_CODE`
- 分页技术参数：`pageIndex`、`pageSize`、`sortField`、`sortOrder`、`totalColumns`
- text/value 到提交字段：`searchOrderType` -> `searchOrderType`；`TRANSACTION_TYPE` -> `TRANSACTION_TYPE`；`SITE_CODE` -> `SITE_CODE`

**State / Validation Logic**
- 查询入口：本次 live 点击的是 `searchBtn`。
- 点击绑定（从内联脚本截取）：
  - `COD_del -> mini.get('CODE').setValue('');`
  - `searchBtn -> searchMethod();`
  - `exportBtn -> exportMethod();`
- 前置校验/状态相关 CALL_ID：`FIND_TAB_INVOICING_ACCOUNT_DETAIL_PAGE`

**Automation Notes**
- 适合先封装接口：页面已经暴露稳定查询请求，可以先复用查询链路，再决定是否走 DOM。
- 页面识别建议：优先用 `pageId=3YIwLCXTNdTri3Cf7VetKwUiKixIejvCSHCfuH` + 页面标题联合识别。
