---
module: 首页消息监控
type: 外部页面清点
status: active
updated: 2026-11-27
---

# 原页处理功能清点（博益处理页 1:1 复刻依据）

用户 2026-11-27 确认：不在原页处理，按原页功能在博益做处理页面，经后台业务账号调用原平台接口完成问题件回复、投诉处理、工单处理等；验收要求处理逻辑与原页一致、功能正常、操作闭环。本文是阶段 0 的只读清点结果：2026-11-27 用 ECS 上融辉操作场/大祥账号与韵达默认账号的登录态，只读取原页模板与脚本（不打开记录详情、不提交任何写入），记录每类待办在原页上的处理按钮、调用的写接口、主要字段与前端校验。只记录页面结构与接口形状，不含账号、运单、正文等业务数据。

字段级细节（每个表单控件的选项、必填、联动）在实现对应类型时再按本文的入口逐项取数；原平台服务端的校验只能在真实记录上实测确认。

## 通用机制

附件通道（2026-11-27）：浏览器选文件即上传到 Console `/customer-service/problems/attachments/upload`（带用途 `purpose`），按原页对应页面的数量/大小/文件名规则先检查（`shared/original_page_attachments.py`），再由 Agent 用业务账号传到原平台，原平台返回整理为统一附件描述（融辉 `FILE_PATH=/unauth/download/<fileDir>`，韵达 `new_name/old_name/file_path`），提交处理动作时带回。经公网入口单次最大 30MB（Nginx `client_max_body_size 34m`），原页允许更大的（融辉工单 200MB、申诉 100MB）需在原系统上传或调大 Nginx。原先按服务器文件路径上传的结果确认对两平台的返回格式都不成立，已改为以整理出的附件描述为准。

- **融辉**：所有写入都是 `POST /dataOperation/saveTables`，正文为若干组 `$Z.Parameter("<操作键>")`，同一次提交可以包含多组（例如“新增处理记录 + 更新工单状态 + 新增附件”），由原系统一次事务保存。附件先 `POST /file/upload?sysFileUploadId=ALL` 上传取得路径，再作为附件行随同保存。处理人、处理网点等字段原页取自登录信息 `userInfo`（`loginEmpName/loginEmpCode/loginSiteName/loginSiteCode`），时间先读 `FIND_NOW` 的服务器时间。记录编号有的原页本地生成（`getWorkOrderCodeTwo`），有的读 `FIND_GUID`。
- **韵达**：问题件系统、网点运营系统（调拨件）为表单 `POST`；快运工单系统 CSSP 为单页应用，处理接口在 `/cssp/comp/sysCompDeal/*.do` 与 `/cssp/collaboration/sysCollaborationManage/*`；仲裁系统 `kyzc` 为 `POST /kyzc/arbitrament/...`。

## 融辉

### 收到问题件（“收到问题件查询”）

| 原页按钮 | 函数 | 写入 | 要点 |
| --- | --- | --- | --- |
| 点选一条 | `rowClick` | `TAB_PROBLEM_UPT`：`BILL_CODE + REGISTER_DATE + BL_SEE=1` | “未处理”且“未查看”的问题件，点开即标为已查看 |
| 保存（回复） | `saveReplyData` → `saveReplDateAfterCheck` | `TAB_PROBLEM_UPT`（先用 `FIND_TAB_PROBLEM_BY_BILL_CODE1` 按单号+类型+登记时间读回整行，改字段后整行写回） | 写 `REVERSION`（回复）、`REVERSION_MAN/_CODE/_DATE/_SITE`、`BL_SEE=1`、`DEAL_RESULT`（处理结果；处理中→`BL_CHECKOK=0`，处理完毕→1）、提货方式 `PICK_TYPE`、司机 `DRIVER_DATA/DRIVER_NAME/IDENTITY_CARD_NO/LICENSE_PLATE_NO/MOBILE_PHONE/VEHICLE_TYPE`、`PICKUP_TIME_DAY/HOUR`、`RETURN_PROCESS_OPINION`（退回处理意见） |
| 确认转发 | `turnSendData` | `TAB_PROBLEM_UPT`：改通知网点 `SEND_SITE/SEND_SITE_CODE` | 通知网点不能为空 |

保存前校验：选“收件人自行提货/安排司机提货”时司机信息须填完整，司机件数合计不得大于开单件数；已回复过的不能再选“处理中”（“问题件只能回复一次”）；回复内容与司机信息至少一项；按 `FIND_BILL_STATUS_ON_PROBLEM` 查运单：来源为协商再投接口/APP/K13 的问题件，若该运单已有“退回/弃货”的问题件则不允许再处理，运单已签收只允许“再派”，已退件/已弃货不允许处理。已回复后再改处理结果时原页会清空回复、提货、司机、退回意见字段再写。

**已按本节重做（2026-11-27）**：原先客服页和消息中心的融辉回复只写 `GUID/BILL_CODE/REVERSION`；现按原页读回整行写回，规则在 `shared/problem_reply_rules.py`。原页“处理结果”控件已注释，保存实际写 `BL_CHECKOK=1` 且不改 `DEAL_RESULT`；`FIND_BILL_STATUS_ON_PROBLEM` 只返回 `BILL_STATUS/DEAL_RESULT`，原页“已有退回/弃货问题件”的判断实际不生效，只有本问题件来源为协商再投接口/APP/K13 时按运单已签收/已退件/已弃货拦截。打开回复框即按原页标“已查看”（仅未处理且未查看时，不提示结果）、左侧显示“登记网点联系人和电话”（`FIND_SITE_INFO_BY_PROBLEM_SITE`）、“转发通知网点 + 确认转发”（网点查 `FIND_SITE_ALL_COMBOBOX`，参数 `key`；转发后按整行读回通知网点）均已做。

### 收到工单（“工单接收方查询-工单裁定申诉”）

| 原页按钮 | 函数 → 子页 | 写入 | 要点 |
| --- | --- | --- | --- |
| 响应 | `answer` → “工单管理-跟进-完结”（`_FOLLOW`） | `TAB_WORK_ORDER_DETAILS_ADD`（`HANDLE_STATE=2`，处理内容必填）+ `TAB_WORK_ORDER_UPT`（`WORK_STATE=3` 待处理、`RESPONSE_DATE`）+ `TAB_WORK_ORDER_DETAILS_PIC_ADD` | 已完结不可；单个工单最多响应 2 次（`FIND_TAB_WORK_ORDER_DETAILS` 中 `HANDLE_STATE=2` 的条数） |
| 完结 | `clickend` → 同上（`_END`） | 处理记录 `HANDLE_STATE=3` + `WORK_STATE=4` 已完结、`OVER_TIME` + 附件 | 只有“待责任方处理”可完结 |
| 催工单 | `urgeWork` → 同上（`_URGE`） | 处理记录 `HANDLE_STATE=4` + `URGE_COUNT+1` | 已完结不可催 |
| 申诉 | `appeal` → “工单管理-申诉” | 处理记录 `HANDLE_STATE=7` + `TAB_WORK_ORDER_UPT`：`APPEAL_STATUS=1`、`APPEAL_DATE/MAN/SITE`、`APPEAL_CLAIM_AMOUNT` + 附件 | 只有待申诉（`APPEAL_STATUS=0`）可申诉；按工单类别 `FIND_TAB_WORK_ORDER_TYPE` 判断是否开启申诉、申诉时效（自裁定时间起小时数）、是否必须附件 |
| 提交裁定 / 申诉审核 | `saveProcessMethod` / `appealCheck` | — | 总部操作，博益账号不涉及 |

附件：最多 9 个，单个不超过 200MB，同名不可重复。

**已做（2026-11-27）**：响应/完结/催工单/申诉四个按钮与对话框按原页实现（`shared/original_page_actions.py` 的 `work_order_*`）。保存须用对话框子页自己的 `authenticationKey/pageId`（从工单页面 HTML 里的 `/widget/home?authenticationKey=…&pageId=…` 链接按子页标题找，用外层页面的标识读子页接口会被拒为“非法的请求”）；提交前按单号+类型+类别重新读这张工单，提交后以处理记录里出现本次 GUID 且工单状态/催单次数/申诉状态变化为成功。工单类别（`FIND_TAB_WORK_ORDER_TYPE`）查不到时原页不拦，博益同样不拦。

### 被投诉方（“被投诉方查询”）

| 原页按钮 | 函数 → 子页 | 写入 | 要点 |
| --- | --- | --- | --- |
| 回复 | `addReplyMethod` → “投诉回复-新增” | `TAB_PROCESS_RECORD_ADD`（`PROCESS_TYPE=投诉回复`、`PAGE_REPLY_TYPE=被投诉方回复`、回复内容必填，编号取 `FIND_GUID`）+ `TAB_PROCESS_RECORD_PATH_ADD` | 附件最多 10 个，单个不超过 20MB |
| 新增申诉 | `addMethod` → “申诉登记_测试_工单” | 见“申诉” | 带入运单号、处理类型、索赔金额 |
| 仲裁反馈（投诉信息页签） | `saveComplainMethod` | `TAB_EXCEPTION_UPT`：`ARBITRATION_BACK_CONTENT`、`BL_ARBITRATION_REPLY=1`、`ARBITRATION_REPLY_DATE/SITE/MAN` | 页面里没有 `ARBITRATION_BACK_CONTENT` 输入框和对应按钮（只剩脚本），实际不可用，博益不做 |

**已做（2026-11-27）**：“回复”（子页“投诉回复-新增”，表单字段为处理类型、运单号、投诉编号、回复网点/人及编码、回复内容；`PAGE_REPLY_TYPE` 只用于附件类型“被投诉方回复附件”，不写入记录；以 `FIND_TAB_PROCESS_RECORD` 读到本次 GUID 为成功）与“新增申诉”（见“申诉”）。

### 投诉方（“投诉方登记”）

| 原页按钮 | 函数 | 写入 | 要点 |
| --- | --- | --- | --- |
| 取消投诉 | `deleteMethod` | `TAB_EXCEPTION_DEL` | 仅投诉人可取消；已审核不支持取消；违禁品类型不可改 |
| 提交申诉（页签） | `saveAppealMethod` | `TAB_EXCEPTION_UPT`：`APPEAL_CONTENT/TYPE/DATE/MAN/SITE`、`APPEAL_STATUS`、`ARBITRATE_STATUS`、`STATUS` + 附件 `TAB_EXCEPTION_PATH_ADD` | 超过投诉申诉期不允许 |
| 提交材料（页签） | `saveClaimMethod` | `TAB_EXCEPTION_UPT`：`CLAIM_DATE/MAN/SITE` + 附件 | 理赔材料 |
| 仲裁反馈 | `saveComplainMethod` | 同被投诉方 | |
| 修改 / 新增 | `editMethodCs` / `addMethodCs5` | 投诉登记表单（异形件、查重查方等多个变体） | 属于新登记投诉，不是待办处理，建议不纳入 |

**已做（2026-11-27）**：取消投诉（仅投诉人，APP 投诉允许投诉网点；违禁品不可；以 `FIND_TAB_EXCEPTION_REGISTER` 读回已审核则拦、删除后读不到为成功）、提交申诉、提交材料。原页细节照做：提交申诉的页签只在“投诉状态和仲裁状态都有值且都不是审核驳回”时拦下（仲裁状态为空即放行）；复审驳回或已传过申诉附件时按钮置灰；申诉期判断对日期字符串计算为 NaN，实际不拦；提交材料在处理类型不为空且不是 2 时拦（处理类型是文字，等于实际只有为空时可提交）；两个页签的图片格都走 `/file/upload?sysFileUploadId=IMG`、每格一张，主记录保存后再逐张 `TAB_EXCEPTION_PATH_ADD`。2026-11-28 已做“新增”“修改”：两者都打开子页“投诉登记-异形件”（原页另有“新增测试-仲裁改造”测试按钮，不做）。博益用专用渲染器 `console/static/complaint_form.js` 按原页字段顺序与联动：运单编号带出日期与产品类型（运单查 `FIND_BILL_INFO_ON_EXCEPTION_REG`，查不到查代取件订单 `…REG1`）；处理类型 `GET_EXCEPTION_TYPE_DISTINCT` → 投诉类别 `FIND_EXCEPTION_TYPE_COMBOBOX2` → 投诉类型 `FIND_EXCEPTION_TYPE_COMBOBOX`（都带登录网点 `LEVELS`）；投诉类型决定被投诉网点范围（`ACCUSED_SITE_TYPE` → `SITE_TYPE`，封签号不一致用 `…COMBOX5`，其余 `…COMBOX6`）、附件科目（`FIND_TAB_EXCEPTION_FILE_NEW`，必填科目须有附件）、处理部门、类型描述、仲裁类与投诉内容必填；对象切换被投诉网点/部门（`FIND_TAB_DEPT_COMBOBOX`）/人（`FIND_TAB_EMPLOYEE_COMBOBOX`）；代登记时选投诉网点；操作场（`OPERATE_ATTR=2`）按 `GET_CZC_SITE_CODE` 询问改绑网点；围板箱编号 `FIND_TAB_COLLAR_CURRENT_SITE`；破损类的是否修复/未修复原因/其它原因。Agent 重新读运单、类型配置（`FIND_TAB_EXCEPTION_TYPE`）与网点，补齐隐藏字段，按原页顺序跑约 15 项检查后 `TAB_EXCEPTION_ADD/UPT` + `TAB_EXCEPTION_PATH_ADD`，以 `FIND_TAB_EXCEPTION_REGISTER` 读回。新增挂在“投诉 · 未完结”分类上方并先选业务账号，修改挂在本人投诉上（投诉或仲裁待审核才可改，违禁品不可改，运单号不可改，已传附件可删除）。原页细节：修改时不重新读运单，签收判断按原页用记录本身的签收日期、签收标记按 0 处理；类型配置查不到时原页按“未签收”拦下。

### 申诉（“申诉登记”）

| 原页按钮 | 函数 → 子页 | 写入 | 要点 |
| --- | --- | --- | --- |
| 新增 | `addMethod33` → “申诉登记_测试_工单”（被投诉方“新增申诉”同此页；`addMethod` 的速通子页未绑定按钮） | `TAB_EXCEPTION_APPEAL_ADD`（`COMPLAINT_STATUS`、`APPEAL_MAN/SITE`、`CREATE_*`、`REGISTER_DATE`）+ 附件 `TAB_EXCEPTION_PATH_ADD` | 运单号、投诉编号、索赔金额、电话必填；超过投诉申诉期不允许；同一运单+同一部门+同一类型只能登记一次；回单禁止申诉；须上传附件；核对申诉网点与运单轨迹网点（`FIND_TRAJECTORY_SITECODE1`）；单文件不超过 100MB |
| 删除 | `deleteMethod` | `TAB_EXCEPTION_APPEAL_DEL` | 仅“待审核”可删除 |

**已做（2026-11-27）**：删除；被投诉方“新增申诉”（子页“申诉登记_测试_工单”）：处理类型 `GET_EXCEPTION_TYPE_DISTINCT1` → 申诉类型 `FIND_APPEAL_TYPE_COMBOBOX`（按登录网点类型带 `APPEAL_SIDE` 中心/网点），申诉类型决定申诉内容是否必填（`IS_COMPLAINT_CONTENT`）、提示文字（`TIP`）、是否必须附件（`BL_EVIDENCE`，看前 3 格）、偷重偷方显示实际重量；流水号为本地 yyyyMMddHHmmss + 5 位随机数、投诉日期为当天（因此申诉期检查只在申诉期为负时拦）；申诉保存成功后一次性保存全部附件（原页没有附件时只提示“请至少选择一个附件”，申诉照样已保存）；以申诉登记列表读到新流水号为成功。“申诉登记”页的“新增”按钮实际绑定 `addMethod33`，打开的也是“申诉登记_测试_工单”（“申诉登记_新增-速通”子页没有按钮绑定）；2026-11-28 已做：挂在消息中心“申诉 · 未完结”分类上方，因四个融辉账号合并显示，对话框先选业务账号，运单号、处理类型、索赔金额由用户填写，其余检查与写入同“新增申诉”。

### 代取件（“网点代取件业务”，仅大祥、大祥S账号）

| 原页按钮 | 函数 | 写入 | 要点 |
| --- | --- | --- | --- |
| 撤销 | `revokeFun` | `TAB_UNITE_ORDERS_UPT` | 已揽收不支持撤销；已取消不能重复撤销；撤销待审核不支持撤销 |
| 编辑 | `updateOrder` → 代取件开单页（约 37 万字符的开单表单） | 开单表单 | 已揽收、已取消不可编辑；属于改单，建议首期不做 |

**已做（2026-11-27）**：撤销（确认后提交 `TX_LOGISTIC_ID/STATUS=3/REVOKE_DATE/REVOKE_MAN/_CODE`，读回订单状态为已取消）。编辑未做（待用户确认范围）。

## 韵达

### 问题件（问题件系统 `query/reply.html` 弹窗）

保存：`POST /ky_problem/public/index.php/query/replyInfo.html`，字段 `prob_status`（**数值** 1 处理中 / 2 已处理，原页没有“已完结”）、`reply_text`（必填，最多 600 字）、`prob_main_id`、`old_prob_status`（原页固定传 2）、`review_entry_status`（预约进仓类问题件需选 1 同意 / 2 不同意）、`file_arr`（附件最多 3 个，先 `issue/uploadImg.html` 上传，文件名不能含符号）。另有常用回复语（`getReplyOften`/`saveReplyOften`）；双击一行会调用 `query/Read.html` 标已读。

**已按本节重做（2026-11-27）**：原先按文字（处理中/已处理/已完结）提交、`old_prob_status` 传当前状态文字；现按原页提交数值状态、固定 `old_prob_status=2`、支持预约进仓确认，回复前读详情（`reply_auth` 为 2 或状态 2/4 时原页不显示回复按钮），回复后读回详情核对。附件已接入（最多 3 个、5MB、文件名只允许字母数字汉字与 `-._`、超过 20KB 的图片按原页先压缩，经共用附件通道上传后以 `file_arr` 提交）；常用回复语已做（回复正文上方列出 `query/getReplyOften.html` 的内容，勾一条填入正文、“无”清空；原页没有保存常用语的入口）。

### 收到工单与催单（快运工单系统 CSSP）

处理接口（`/cssp/comp/sysCompDeal/` 下，`.do`）：接单 `receive`、指定人接单 `receiveAppointUser`、加急接单 `urgentReceive`、首次回复 `firstReply`、及时响应 `promptResp`、回复处理 `replyHandling`（JSON 正文）、结案说明 `statement`、转部门 `turnOrg`、延时 `extendLaging`、上级协助 `upOrgHelp`（先 `upOrgHelpValidate`）、首次责任申诉 `fristDutyAppeal`、理赔申请 `claimApply`、补充说明 `bcsmExplain`、违规申报 `wgDeclare`；催单回复 `/cssp/collaboration/sysCollaborationManage/cuiReplay`，协同 `saveCol`；附件 `comp/uploadFile/uploadCommon`。各动作的字段由页面组件组装，实现时按动作逐个从 `static/js/app.*.js` 取数并在真实工单上核对。

**已做（2026-11-27）**：“工单处理”详情页的按钮（组件 chunk-43ccf3dd、混入 a835/4bf6、接口表 53c1）：立即响应（处理状态 10）、首次回复（2，预计解决时间不早于今天）、回复处理（3，`replyHandling.do` 实际以表单提交，最多 3 张 jpg/png、超 1MB 先压缩）、转至理赔（状态不在 0/4/5/6/7/8）、结单（3 或 4）、延长时效（1/2/3）、转站点（1/10/2/3）；后三个及转至理赔原页只允许 8:00–18:59 提交。外呼人身份：立即响应/首次回复是“投诉人”开关，回复处理/结单是五选一并带出投诉人/发件人/收件人的姓名电话；姓名 ≤20 字、电话为数字、内容 ≤200 字。其余接口均为 `POST` 查询串、返回 `code` 0/200 为成功；以处理记录条数增加或处理状态变化为成功。催单待回复的“回复”（姓名、外呼电话、沟通记录，带 `colId`）以该催单离开待回复列表为成功。按钮权限（`v-control`）由原系统按账号判断，博益不预判，被拒时显示原系统原因。按钮按账号权限显示（原页 `v-control`，权限取 `/cssp/sys/menu/newTree` 中 type 为 2/3 的节点 `perms`，读不到时不预判）。列表工具栏的“加急工单接单”“接单”挂在消息中心“收到的待处理工单”分类上方（打开时读 `receiveNumQuery`/`urgentReceiveNumQuery` 的本站待接单量；本次接单量为不小于 1 的整数；以待接单量减少为成功）；“转人员”挂在每条工单上（原页列表只允许勾选已分配/已接单/已响应/已首回的工单；转入人员按姓名或工号查 `comp/loadCommon/getDeptEmp`，`gs` 为本机构；提交 `receiveAppointUser.do?userId&complaintIds`，以列表里工单归属人变为所选人员为成功）。2026-11-28 实测韵达默认账号有接单、转人员权限，没有加急接单权限（接口回“无操作权限”），所以博益只显示“接单”。

### 调拨件（网点运营系统“调拨件确认”）

| 原页按钮 | 接口（`/ky_inms/public/index.php/business/replaceLogistics/ReplaceLogisticsConfirm/`） | 字段与要点 |
| --- | --- | --- |
| 接单 | `accept.html` | `Replace_Id`；目的地址为特殊区域/镇上自提/仓库时原页先提示可能加收 |
| 异常登记 | `save.html` | `Replace_Id[]`、`Abnormal_Type`（必选）、`Abnormal_Remark`（最多 50 字） |
| 回复 | `reply.html` | `Abnormal_Type`、`Abnormal_Remark`、`Replace_Id`、`Reply_Content`（最多 200 字） |
| 转单 | `transfer.html` | `Replace_Id`、`Replace_Site_Code`（必填） |
| 延时 | `delay.html` | 选中记录与延时参数 |
| 罚款登记 | `fineRegistration.html` | `Penalty_Type`、`Remark`、`Penalty_Amount`、`Reward_Amount`、`Penalty_Site_Code`、`Reward_Site_Code` |
| 异常图片 | `uploadAbnormalImage.html` / `deleteAbnormalImage.html` | |

**已做（2026-11-27）**：接单（确认后提交 `Replace_Id[]`，读回不再是待接单）、转单（网点查 `like/getWebsite.html`，参数 `keyword`，取 `org_id/org_name`；读回不在本网点待接单）、异常登记（已做过异常登记或状态不是待接单/已接单时不可；三格图片每格一张 jpg/png/jpeg ≤2MB，原页是选中即带 `Replace_Id/Image_Sort` 上传挂到调拨件，博益在提交时先逐张上传再保存）。2026-11-28 补做：延迟发货（揽发/开单超时、已撤销、已延迟过一次不可；延迟时间为今天起 1–29 天，显示“月-日”；延迟原因必填 ≤50 字；三格证明图每格一张，以 `Image_Type=2` 挂到调拨件；拼多多跨境订单先确认撤单提示；`delay.html` 返回 `info=1` 为成功，以延迟发货审核状态或延迟日期变化读回）、转业务员（原业务员只读；新业务员查 `like/getRy.html?type=2`，参数 `keyword`，取 `org_id/org_name`；提交 `updateJobCode.html` 的 `Replace_Id[]` 与 `Job_Code`，以业务员变化读回）、回复（详情页按钮，仅待接单/已接单可用；异常类型可改选，详细描述取详情原值，异常说明去首尾空白后最多 200 字、原页不要求必填；以详情 `ReplyInfo` 条数增加读回）。罚款登记按钮（`edit_cost_but`）在本账号的原页里不下发（按权限隐藏），博益不做。这两类随动作提交的图片先在 Agent 临时目录暂存（2 小时过期），处理后删除。

### 仲裁（仲裁系统“仲裁查询”）

本页只有：留言 `arbitramentSearch/saveMessage`、继续申报 `goOnDeclare`（进入申报页）、查看详情（申报、留言、图片、处理记录）。仲裁系统首页菜单未能只读取得，是否还有“仲裁处理/举证”页需用业务账号在原系统菜单中确认后再清点。

**已做（2026-11-27）**：留言（1–1000 字，`messageType=message`，以 `getMessage` 留言记录读回）。2026-11-28 已做“继续申报”（专用渲染器 `console/static/arbitration_form.js`）：仅“退单,可继续申报”且不是费用申请仲裁；可改仲裁类型（`/kyzc/common/select/declareType` 所属类型，费用申请仲裁按原页只能与自身互换）、仲裁类别（所属类别，自动仲裁类别不是当前类别时不可选）、索赔金额（原页数字规则，上限 9999999999.99）、责任网点（`/kyzc/common/dropList/autocompleteCompany`，不是运单关联网点 `getEncode` 时只提示）、异常件数（≤ 子单数）、问题描述（必填 ≤1000）。换类别时按 `/kyzc/basic/complainType/edit/{类别}` 的 Y/N 显示主面单（新增 ≤1）、价值证明（≤6）、索赔函（≤4）、举证图片（≤8），显示的分组必须有图（中心上传的不计入新增上限）；已有视频时须保留 1–3 段，录音最多 2 条；图片只收 jpg/gif/png 且名称 ≤64 字（超 500KB 先压缩），视频 mp4/flv ≤30M，录音 mp3/m4a/wav ≤10M，先在 Agent 暂存。删除的已有附件与因换类别被隐藏的分组里的原图都以 `marinaUpdate=old<fileId>` 提交。换到转退件费（`2019042911092400492`）且“是否地址解析问题”为是时先查 `getZydzJudgeMsg`：是地址问题记 `isAddressParseProblem=T`，否则按原页先确认“非地址解析问题，是否仍然申报？”再记 F。以 multipart 提交 `/kyzc/arbitrament/arbitramentSearch/save`（原表单全部字段），读回 `loadDeclareDetail` 的问题描述、索赔金额、类别。

## 实现位置（2026-11-27）

规则单点在 `shared/original_page_actions.py`（按钮、置灰原因、对话框字段、保存前检查、写入行），附件规则在 `shared/original_page_attachments.py`；Agent `agent/agent/tms_runtime/scripts/original_page_processing.py` 按 `(平台, kind, operation)` 分派：重新读取记录 → 检查 → 按原页写入 → 读回，结果只有读回一致才带 `verified`，否则 `WRITE_OUTCOME_UNKNOWN`。业务动作为客服 `process`（写，超级管理员）与 `lookup`（读：网点查询、联系人、常用回复语、联动下拉），Console `/customer-service/process|lookup` 只转发平台、业务账号、按钮与对话框内容。消息中心条目带 `actions`（按钮与字段），对话框组件为 `console/static/original_page_actions.js`。

## 后续

1. 用户按上表确认要做的动作（标“建议不纳入”的除外）。
2. 按“问题件（含重做现有回复）→ 融辉工单 → 融辉投诉/申诉 → 韵达工单/催单 → 韵达调拨件/仲裁 → 融辉代取件”逐类实现：字段取数、博益处理面板、后台接口（提交后按原页同一查询读回核对）。
3. 每个动作在一条真实记录上实测一次，核对原平台状态后验收。
