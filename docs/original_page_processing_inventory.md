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

**已按本节重做（2026-11-27）**：原先客服页和消息中心的融辉回复只写 `GUID/BILL_CODE/REVERSION`；现按原页读回整行写回，规则在 `shared/problem_reply_rules.py`。原页“处理结果”控件已注释，保存实际写 `BL_CHECKOK=1` 且不改 `DEAL_RESULT`；`FIND_BILL_STATUS_ON_PROBLEM` 只返回 `BILL_STATUS/DEAL_RESULT`，原页“已有退回/弃货问题件”的判断实际不生效，只有本问题件来源为协商再投接口/APP/K13 时按运单已签收/已退件/已弃货拦截。打开时标“已查看”与“确认转发”尚未做。

### 收到工单（“工单接收方查询-工单裁定申诉”）

| 原页按钮 | 函数 → 子页 | 写入 | 要点 |
| --- | --- | --- | --- |
| 响应 | `answer` → “工单管理-跟进-完结”（`_FOLLOW`） | `TAB_WORK_ORDER_DETAILS_ADD`（`HANDLE_STATE=2`，处理内容必填）+ `TAB_WORK_ORDER_UPT`（`WORK_STATE=3` 待处理、`RESPONSE_DATE`）+ `TAB_WORK_ORDER_DETAILS_PIC_ADD` | 已完结不可；单个工单最多响应 2 次（`FIND_TAB_WORK_ORDER_DETAILS` 中 `HANDLE_STATE=2` 的条数） |
| 完结 | `clickend` → 同上（`_END`） | 处理记录 `HANDLE_STATE=3` + `WORK_STATE=4` 已完结、`OVER_TIME` + 附件 | 只有“待责任方处理”可完结 |
| 催工单 | `urgeWork` → 同上（`_URGE`） | 处理记录 `HANDLE_STATE=4` + `URGE_COUNT+1` | 已完结不可催 |
| 申诉 | `appeal` → “工单管理-申诉” | 处理记录 `HANDLE_STATE=7` + `TAB_WORK_ORDER_UPT`：`APPEAL_STATUS=1`、`APPEAL_DATE/MAN/SITE`、`APPEAL_CLAIM_AMOUNT` + 附件 | 只有待申诉（`APPEAL_STATUS=0`）可申诉；按工单类别 `FIND_TAB_WORK_ORDER_TYPE` 判断是否开启申诉、申诉时效（自裁定时间起小时数）、是否必须附件 |
| 提交裁定 / 申诉审核 | `saveProcessMethod` / `appealCheck` | — | 总部操作，博益账号不涉及 |

附件：最多 9 个，单个不超过 200MB，同名不可重复。

### 被投诉方（“被投诉方查询”）

| 原页按钮 | 函数 → 子页 | 写入 | 要点 |
| --- | --- | --- | --- |
| 回复 | `addReplyMethod` → “投诉回复-新增” | `TAB_PROCESS_RECORD_ADD`（`PROCESS_TYPE=投诉回复`、`PAGE_REPLY_TYPE=被投诉方回复`、回复内容必填，编号取 `FIND_GUID`）+ `TAB_PROCESS_RECORD_PATH_ADD` | 附件最多 10 个，单个不超过 20MB |
| 新增申诉 | `addMethod` → “申诉登记_测试_工单” | 见“申诉” | 带入运单号、处理类型、索赔金额 |
| 仲裁反馈（投诉信息页签） | `saveComplainMethod` | `TAB_EXCEPTION_UPT`：`ARBITRATION_BACK_CONTENT`、`BL_ARBITRATION_REPLY=1`、`ARBITRATION_REPLY_DATE/SITE/MAN` | 只提交与原值不同的字段 |

### 投诉方（“投诉方登记”）

| 原页按钮 | 函数 | 写入 | 要点 |
| --- | --- | --- | --- |
| 取消投诉 | `deleteMethod` | `TAB_EXCEPTION_DEL` | 仅投诉人可取消；已审核不支持取消；违禁品类型不可改 |
| 提交申诉（页签） | `saveAppealMethod` | `TAB_EXCEPTION_UPT`：`APPEAL_CONTENT/TYPE/DATE/MAN/SITE`、`APPEAL_STATUS`、`ARBITRATE_STATUS`、`STATUS` + 附件 `TAB_EXCEPTION_PATH_ADD` | 超过投诉申诉期不允许 |
| 提交材料（页签） | `saveClaimMethod` | `TAB_EXCEPTION_UPT`：`CLAIM_DATE/MAN/SITE` + 附件 | 理赔材料 |
| 仲裁反馈 | `saveComplainMethod` | 同被投诉方 | |
| 修改 / 新增 | `editMethodCs` / `addMethodCs5` | 投诉登记表单（异形件、查重查方等多个变体） | 属于新登记投诉，不是待办处理，建议不纳入 |

### 申诉（“申诉登记”）

| 原页按钮 | 函数 → 子页 | 写入 | 要点 |
| --- | --- | --- | --- |
| 新增 | `addMethod33` → “申诉登记_测试_工单”（被投诉方“新增申诉”同此页） | `TAB_EXCEPTION_APPEAL_ADD`（`COMPLAINT_STATUS`、`APPEAL_MAN/SITE`、`CREATE_*`、`REGISTER_DATE`）+ 附件 `TAB_EXCEPTION_PATH_ADD` | 运单号、投诉编号、索赔金额、电话必填；超过投诉申诉期不允许；同一运单+同一部门+同一类型只能登记一次；回单禁止申诉；须上传附件；核对申诉网点与运单轨迹网点（`FIND_TRAJECTORY_SITECODE1`）；单文件不超过 100MB |
| 删除 | `deleteMethod` | `TAB_EXCEPTION_APPEAL_DEL` | 仅“待审核”可删除 |

### 代取件（“网点代取件业务”，仅大祥、大祥S账号）

| 原页按钮 | 函数 | 写入 | 要点 |
| --- | --- | --- | --- |
| 撤销 | `revokeFun` | `TAB_UNITE_ORDERS_UPT` | 已揽收不支持撤销；已取消不能重复撤销；撤销待审核不支持撤销 |
| 编辑 | `updateOrder` → 代取件开单页（约 37 万字符的开单表单） | 开单表单 | 已揽收、已取消不可编辑；属于改单，建议首期不做 |

## 韵达

### 问题件（问题件系统 `query/reply.html` 弹窗）

保存：`POST /ky_problem/public/index.php/query/replyInfo.html`，字段 `prob_status`（**数值** 1 处理中 / 2 已处理，原页没有“已完结”）、`reply_text`（必填，最多 600 字）、`prob_main_id`、`old_prob_status`（原页固定传 2）、`review_entry_status`（预约进仓类问题件需选 1 同意 / 2 不同意）、`file_arr`（附件最多 3 个，先 `issue/uploadImg.html` 上传，文件名不能含符号）。另有常用回复语（`getReplyOften`/`saveReplyOften`）；双击一行会调用 `query/Read.html` 标已读。

**已按本节重做（2026-11-27）**：原先按文字（处理中/已处理/已完结）提交、`old_prob_status` 传当前状态文字；现按原页提交数值状态、固定 `old_prob_status=2`、支持预约进仓确认，回复前读详情（`reply_auth` 为 2 或状态 2/4 时原页不显示回复按钮），回复后读回详情核对。附件（最多 3 个）与常用回复语尚未做。

### 收到工单与催单（快运工单系统 CSSP）

处理接口（`/cssp/comp/sysCompDeal/` 下，`.do`）：接单 `receive`、指定人接单 `receiveAppointUser`、加急接单 `urgentReceive`、首次回复 `firstReply`、及时响应 `promptResp`、回复处理 `replyHandling`（JSON 正文）、结案说明 `statement`、转部门 `turnOrg`、延时 `extendLaging`、上级协助 `upOrgHelp`（先 `upOrgHelpValidate`）、首次责任申诉 `fristDutyAppeal`、理赔申请 `claimApply`、补充说明 `bcsmExplain`、违规申报 `wgDeclare`；催单回复 `/cssp/collaboration/sysCollaborationManage/cuiReplay`，协同 `saveCol`；附件 `comp/uploadFile/uploadCommon`。各动作的字段由页面组件组装，实现时按动作逐个从 `static/js/app.*.js` 取数并在真实工单上核对。

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

### 仲裁（仲裁系统“仲裁查询”）

本页只有：留言 `arbitramentSearch/saveMessage`、继续申报 `goOnDeclare`（进入申报页）、查看详情（申报、留言、图片、处理记录）。仲裁系统首页菜单未能只读取得，是否还有“仲裁处理/举证”页需用业务账号在原系统菜单中确认后再清点。

## 后续

1. 用户按上表确认要做的动作（标“建议不纳入”的除外）。
2. 按“问题件（含重做现有回复）→ 融辉工单 → 融辉投诉/申诉 → 韵达工单/催单 → 韵达调拨件/仲裁 → 融辉代取件”逐类实现：字段取数、博益处理面板、后台接口（提交后按原页同一查询读回核对）。
3. 每个动作在一条真实记录上实测一次，核对原平台状态后验收。
