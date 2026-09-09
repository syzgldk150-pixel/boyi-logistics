# 寄件数据查询与覆盖范围（架构 V1 / P4）

本轮把寄件查询与夜间同步分开：Console 和 Agent 查询走 `send-waybills-query` 普通接口；夜间插件独立采集并写同一数据库。查询不创建 Command、Run 或后台领取任务。

## 已实现的路径

- `agent/agent/send_waybills_business.py`：数据库优先；按明确日期范围补查未覆盖的来源，明确单号补查只做 upsert；失败返回本地已保存数据和 `partial`，不能把失败当作空平台。
- `agent/plugin_core_adapters/waybill_query.py`：组合真实原生分页/详情接口、现有字段转换及数据库；不在 Agent 内层依赖 tools。宿主提供已核实的来源与当前权限观察器，浏览器和插件不能自报“已验证”。
- `shared/waybill_pagination.py`：校验总数、分页停止条件、重复实体和页间总量变化。上游目前使用按日分页，没有伪造增量游标。
- `shared/waybill_source_coverage.py`：实体键为平台 + 稳定业务来源 + 上游实体；账号只表示调用绑定，换账号不会另造实体。覆盖记录另包含权限范围、日期、观察时间和版本。
- `agent/migrations/045_waybill_source_coverage.sql`：新增来源身份和覆盖表。旧身份不明行保持 NULL，不按账号、插件名或同名单号猜归属。
- 两个同步工具与两个实际生产 projection 均接入来源范围写入器。已证明完整分页的夜间快照才可替换对应平台/来源/权限/日期范围；精确补查不删除其他行；缺 scope 证明不执行 SQL 写入并明确 partial/失败。
- `console/database.py`：同一响应的列表、总数和汇总在同一 MySQL 一致性快照中读取。
- `agent/plugin_core_adapters/arrival_report.py`：同时读取新 Invocation 的结果/代次回执和历史 Run 的只读证据。当前统计已写、写结果未知等事实不会被简化的到货清单覆盖；不改统计算法。

## 日期及身份规则

当天已有部分数据不代表当天完整。23:55 的采集不标记为日终完成，翌日补取当天尾段后才产生完整历史日覆盖；当前日查询仍刷新来源。中断、缺页、总数变化不发布覆盖。单号更新会使相应日覆盖失效，后续完整查询重新核实。

只在相同平台、已验证业务来源与实体键下更新；不同平台同号不合并，同范围重复身份明确报错。取消状态保留。完整替换不删除另一权限范围或身份不明的旧行。列表每次响应一致，不提供跨多次 HTTP 请求冻结同一分页版本的承诺。

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

最终 P4 组合重新运行结果为 211 passed、5 subtests passed，见 `p4-final.xml`。客服业务发布补接后，当前业务 refs→原插件详情复核→事务发布、来源仓储和到货所有权组合为 51 passed，见 `p4-customer.xml` / `p4-customer.log`；覆盖已回复关闭、未证实/登录错误保留、人工备注保留和无旧执行实体新增。完整 C07 签名包 Direct 入口验收由总报告单独记录。

## 更新与回退

本次包含共享表迁移和宿主接口更新，属于核心更新。先备份、隔离迁移和验收，再按项目发布流程更新；本文件不授权 ECS 发布。迁移只新增列/表，不删除旧业务字段。回退代码不能撤销外部业务写入；保留新增列及新来源数据，回退版本不得继续旧的按平台整日删除路径。发布前必须完成生产 scope 核验及全量适用验收，未完成不得标记为可发布。
