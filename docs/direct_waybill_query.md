# 寄件数据查询与覆盖范围（架构 V1 / P4）

寄件查询与夜间同步分开：Console 和 Agent 查询走 `send-waybills-query` 普通接口；夜间插件独立采集并写同一数据库。查询不创建 Command、Run 或后台领取任务。

## 寄件字段与金额（2026-10-14）

两个寄件插件 2.1.0 的飞书总成本字段统一为 **录单运费**，Console 统一读取 `recorded_cost`，只显示一项 **录单成本**。它不是客户运费，也不再写入 `transfer_fee`。融辉从原页 `GUEST_FREIGHT`（原页标签“录单金额”）读取；韵达普通寄件逐票读取原页编辑器 `renderer.price.Total`，填仓寄件读取本次列表 `Total_Cost_Money`。普通列表成本可能被掩码，不能拿掩码替代编辑器金额，也不能使用旧成本填补新查询失败。

| 字段 | 融辉原页 | 韵达原页 |
| --- | --- | --- |
| 实际重量 | `BILL_WEIGHT` | `Gross_Weight` |
| 目的网点 | `DISPATCH_UNDERLING_SITE`；`DESTINATION` 是目的地 | `Buyer_Destination_Dot_Name` |
| 计费重量 | `FEE_WEIGHT`，独立保留 | 不用其他重量猜测 |
| 运费 | `FREIGHT` | `Freight` |
| 到付款 | `TOPAYMENT` | `COD` |
| 代收货款 | `GOODS_PAYMENT` | `Collection_Money` |
| 保价金额 | `INSURANCE` | `Insured_Amount` |
| 寄件地址 | `SEND_MAN_ADDRESS` | `Sender_Address` |
| 需要回单 | `BL_INDENTURE` | 编辑器 `Delivers_Return`；`Return_Type` 是退回件 |

金额用 Decimal 校验，保留真实零值；缺失、非数值、非有限数字或身份不唯一必须明确失败/无数据，不能替换为零。迁移 `060_platform_waybill_fields.sql` 新增平台费用、地址与回单字段，刷新跨库视图并撤销旧覆盖证明。旧成本不自动拷入新字段，旧融辉重量在重新读取前显示“历史重量（待核验）”。Console 缺失/非法成本显示无数据并停止对应汇总。

韵达详情按原页“全部历史数据”选项显式请求 `history=extra`；`history=now` 在原页标为“近10月”，不能用它的空结果判定历史运单不存在。普通寄件只按列表 `Sign_State` 原文“已签收”/“未签收”映射 `signed`/`in_transit`，未知值阻断同步。填仓单按精确运单根的 `smi.info.scanFlag` 键集合判断：存在 `signed` 为已签收，否则存在 `send`、`transfer` 或 `deliver` 为运输中；缺失、空集合或仅有问题标记均失败。两种来源不互相回退，历史回填不默认运输中。编辑器返回的 `Logistics_Id` 必须与请求精确一致，再使用 `price.Total`。

飞书历史字段按已有字段 ID 原位改名、按唯一运单编号更新托管字段，保留记录 ID、附件、关联和人工列。普通日期、编号、体积重量同义字段统一；旧名称与新名称同时存在等歧义不能任选其一。源列表未返回的既有飞书记录保留并报告，不能据此删除；缺单号的业务行也不能猜测匹配。韵达普通 Sheet 保持现有当日快照语义，历史全集位于对应 Bitable 和运单数据库。

## 已实现的路径

- 发货计费吨位复用 `waybill_query.collect_ronghui_day` 完整分页，在 `shipment_source.py` 中按宿主唯一用途账号、真实站点与开单日期构建本次快照，不新增账本或复用旧展示重量。每次按需刷新，因此能排除已从原查询消失的作废单；详细来源合同与验收边界见 [发货吨位与飞书知识](shipment_knowledge_queries.md)。

- `agent/agent/send_waybills_business.py`：数据库优先；按明确日期范围补查未覆盖的来源，明确单号补查只做 upsert；失败返回本地已保存数据和 `partial`，不能把失败当作空平台。
- `agent/plugin_core_adapters/waybill_query.py`：组合真实原生分页/详情接口、现有字段转换及数据库；不在 Agent 内层依赖 tools。宿主提供已核实的来源与当前权限观察器，浏览器和插件不能自报“已验证”。
- `shared/waybill_pagination.py`：校验总数、分页停止条件、重复实体和页间总量变化。上游目前使用按日分页，没有伪造增量游标。
- `shared/waybill_source_coverage.py`：实体键为平台 + 稳定业务来源 + 上游实体；账号只表示调用绑定，换账号不会另造实体。覆盖记录另包含权限范围、日期、观察时间和版本。
- `agent/migrations/045_waybill_source_coverage.sql`：新增来源身份和覆盖表。旧行起初保持 NULL；仅在原平台本次精确返回能核实旧原生采集身份时原位补全，规则见下文，不按账号或插件名猜归属。
- 两个同步工具与两个实际生产 projection 均接入来源范围写入器。已证明完整分页的夜间快照才可替换对应平台/来源/权限/日期范围；精确补查不删除其他行；缺 scope 证明不执行 SQL 写入并明确 partial/失败。
- `console/database.py`：同一响应的列表、总数和汇总在同一 MySQL 一致性快照中读取。
- `agent/plugin_core_adapters/arrival_report.py`：提供历史发布与写入证据读取。当前 V2 允许后续成功的到货清单用本次有效结果覆盖原到货表；查询失败或写结果未知必须明确报告，不能作为成功发布或自动重放的依据；不改统计算法。

## Console 查询与后台补查

`GET /waybills` 只读取当前数据库快照，页面不等待原平台响应。用户主动点击“查询”后，浏览器先显示列表，再用同源 `POST /waybills/source-refresh` 提交来源与日期区间。请求需要真实 MySQL 管理员会话和查询权限，Console 用现有签名身份调用 Agent；应急 Basic Auth 不可发起补查。缺少日期、范围超过 31 天或来源为博益开单/OCR 时不调用原平台。

同一 Console 进程内，相同 `(source, date_from, date_to)` 的并发请求共享一次补查；完整成功结果复用 120 秒，失败或不完整结果不缓存。浏览器完成后重取当前列表并显示更新时间或不完整原因。翻页、排序和改变每页条数仅查询数据库，离开页面中止浏览器等待，不取消服务端已经开始的补查。

融辉与韵达最多使用两个线程并行补查，各平台复用一条数据库连接，并一次读取日期区间的覆盖记录。按日期查询不再先读取无用缓存；发布使用显式事务、批量锁定候选与批量插入，内容未变化的行不更新。遗留实体的唯一身份判定、取消状态保留和失败回滚保持不变。韵达逐单详情仍按顺序读取，避免扩大原平台请求并发；因此首屏提速不代表补查本身没有等待。

## 博益运单修改

作废（`POST /waybills/{id}/status`）只对博益开单和 OCR 开放；融辉、韵达寄件以原站为准，详情不显示作废按钮，服务端也拒绝其作废请求（2026-10-14）。原站已取消的平台单由同步与人工核实后剔除，不在后台作废。

查询详情可修改未作废的博益开单（`source=manual`），OCR 和平台同步单只读。`GET /waybills/{id}/edit-data?source=manual` 返回当前字段与原值指纹；`POST /waybills/{id}/manual-update` 提交稳定请求 UUID、指纹与 `field_*` 字段，使用与新建相同的规范化和当前生效的扩展校验。修改不能改变运单号。

仓储在同一事务中锁定精确博益记录，核对原值指纹、保存字段差异并写 `boyi_waybill_edit_logs`。旧版本、已作废或来源错误明确拒绝，重复请求幂等返回原结果。`GET /waybills/{id}/edit-history?source=manual` 在用户展开记录时读取修改人、时间和前后值；保存后刷新列表和详情，打印继续读取最新数据。迁移 057 的实体库及权限要求见 [分库说明](database_domains.md)。

## 日期及身份规则

当天已有部分数据不代表当天完整。23:55 的采集不标记为日终完成，翌日补取当天尾段后才产生完整历史日覆盖；当前日查询仍刷新来源。中断、缺页、总数变化不发布覆盖。单号更新会使相应日覆盖失效，后续完整查询重新核实。

只在相同平台、已验证业务来源与实体键下更新；不同平台同号不合并，同范围重复身份明确报错。取消状态保留。完整替换不删除另一权限范围或身份不明的旧行。列表每次响应一致，不提供跨多次 HTTP 请求冻结同一分页版本的承诺。

旧原生采集器 `WaybillRepository.sync_records` 将平台写入 `source`，将平台返回的 `BILL_CODE`／`Logistics_Id` 写入 `waybill_no`，且不绑定录入文档或人工开单人。现在只有已审核的 `ronghui:native-waybill`／`yunda:native-waybill` 来源、本次返回的 `source_record_id` 与 `waybill_no` 精确相同，且数据库中存在唯一、平台和单号二进制精确匹配的上述旧采集行时，才在同一事务中原位补全来源身份。保留原主键、创建时间和已取消状态，其他业务字段采用本次真实值，不要求金额、日期或收件信息与旧值相同。这样首次查询及再次刷新都不会另插一份旧运单，也不会重复汇总金额。

多个旧候选、旧行与其他已确认身份同时存在、残缺来源字段、录入文档／人工开单人标记，以及自定义来源与旧行重叠，均在业务行更新前明确失败并回滚本次发布，不选第一条、不删除历史行或在页面隐藏重复。未在本次真实结果出现的未知旧行继续保留。夜间写后核验与提交响应丢失时的精确回读共用同一身份判定；原位补全计为更新，不能伪报新增。

## 已核验的原页范围与剩余边界

`waybill_query_scope.py` 现从原平台读取当前登录业务上下文、查询权限模式和实际查询过滤器，生成 `current_query_profile` 覆盖范围。融辉读取原页唯一 `loginSiteCode`，韵达读取 `/client/user/info` 的组织/网点类型与原页分享、用户和建单人限制模式；请求前后再次观察，不允许采集中范围漂移。实际账号由后台明确的默认绑定选择，缺失或有多个默认绑定明确失败；账号使用同时登记到发布/凭据修改互斥范围。相同来源换账号保持实体身份，覆盖另行核验。

这证明的是当前原页查询权限与过滤器覆盖，**不声明全组织完整**，响应固定给出 `all_organization_complete=false`。上游没有返回权威全网点可见集合，不能把管理员字样或站点码单独当作全组织权限。缺上下文、登录失效或解析矛盾仍返回本地快照和明确 partial，不能伪造平台空结果。

2026-09-09 在用户新开并登录的两平台标签页完成只读验证，原工作中的融辉标签未操作。融辉原生 `FIND_BILL_SEND` 查询返回 4 行，分别精确读取 `getBillByCode`，每次唯一实体与请求单号一致；原页“寄件日期”及列表/详情实际字段均为 `REGISTER_DATE`。已同步修正夜间插件字段映射，数据库插入时间 `INSERT_DATE` 不再充当寄件日期。韵达原生寄件列表返回 1 行；其详情接口返回包含子单的 15 个节点，所有节点共享父单 `Logistics_Id`，因此必须匹配唯一的外层单号键，不能取第一个 logistics 节点。寄件列表与精确父单详情的 `Mail_Date` 实际相同。测试数据只模拟外部 HTTP 边界，未对生产业务做写入。

韵达页面同时嵌入编辑器，分享模式声明实际重复两次但内容一致；观察器允许同值重复，冲突仍报错。此前浏览器目标串页问题通过新标签精确定位解决；该次停止核验记录不再代表当前阻塞。当前证据不包括 ECS 保存账号配置的实际运行或所有组织/权限模式的遍历。

## 验证与复现

先按 [验收环境与命令入口](architecture_refactor_acceptance_mapping.md) 定义 `isolated`，只连接独立的 loopback MySQL 8 测试实例并禁止加载 dotenv；测试会创建并删除自己随机命名的测试库。临时目录均在项目内。

```bash
isolated "$TASK_PYTHON" -m pytest -q tests/test_waybill_source_coverage.py tests/test_waybill_source_coverage_mysql.py tests/test_daily_send_production_adapter.py tests/test_daily_send_core_handlers.py tests/test_daily_send_plugin_router.py tests/test_yunda_source_contracts.py tests/test_first_party_core_handlers.py tests/test_first_party_action_payloads.py agent/tests/test_phase7_sync_tools.py --junitxml=.task_tmp/v1-report/p4-scoped-nightly.xml -o junit_family=legacy
```

本次实际通过结果和每个用例名在 `p4-scoped-nightly.xml`，原始输出在同目录 `p4-scoped-nightly.log`。新增 `tests/test_waybill_query_profiles.py` 验证上述真实页面结构、实际账号使用登记和取消时等待线程真正结束。组合回归见 `p4-final.xml` / `p4-final.log`；仅以文件内结果为准，迁移环境错误不能计为通过。涵盖真实分页协议→现有转换→MySQL→查询，生产夜间 projection→MySQL，原生融辉单号详情→局部 upsert，源/账号/权限隔离、尾段补全和失败保留；外部 HTTP 与身份观察边界使用明确合成数据，业务函数与数据库使用实际实现。

`p4-arrival-ownership-mysql.log` 保存新 Invocation/lease/receipt 关联和原统计/到货清单真实签名包业务链的隔离结果；`p4-console-query.log` 保存 Console 查询回归。它们是本轮切片证据，不能替代完整 A/B/C 与维护演练。

历史 P4 组合重新运行结果为 211 passed、5 subtests passed，见 `p4-final.xml`。客服业务发布补接后，当前业务 refs→原插件详情复核→事务发布、来源仓储和到货所有权组合为 51 passed，见 `p4-customer.xml` / `p4-customer.log`；覆盖已回复关闭、未证实/登录错误保留、人工备注保留和无旧执行实体新增。完整 C07 签名包 Direct 入口验收由总报告单独记录。

## 更新与回退

本次包含共享表迁移和宿主接口更新，属于核心更新。先备份、隔离迁移和验收，再按项目发布流程更新；本文件不授权 ECS 发布。迁移只新增列/表，不删除旧业务字段。回退代码不能撤销外部业务写入；保留新增列及新来源数据，回退版本不得继续旧的按平台整日删除路径。发布前必须完成生产 scope 核验及全量适用验收，未完成不得标记为可发布。
