---
module: 插件局部维护
type: 操作说明
tags: [插件, 测试, 打包, 维护]
related: [service_v2_developer_tooling.md, automation_plugin_platform.md, code_navigation_index.md]
status: active
updated: 2026-10-08
---

# 插件局部测试和打包

入口为 `scripts/plugin_maintenance.py`，从仓库根目录运行。它选择明确插件对应的真实
payload、生产 adapter、Broker/router 与结果校验测试；不会调用部署器或操作生产服务、
生产数据库，也不会读取 `.env` 或凭据文件。需要协议与 MySQL 的测试只在明确的隔离环境运行。
当前支持的 V2 插件以 `scripts/plugin_maintenance.py` 的 `PLUGIN_TESTS` 和各包 Manifest 为准，覆盖日常业务插件；不在文档另存一份容易过期的版本表。
`describe` 列出实际测试节点，未知插件或缺失测试会失败，不退回全量或无测试打包。

每日应签的接口返回适配归宿主 `plugin_core_adapters/daily_sign_ports.py`：问题件只传业务字段，主单轨迹只传扫描事实，
飞书只传规范记录、单元格和明确的写入确认，不向插件转发账号标识、表格定位信息及重复包装。
缺失数据、分页未读完、接口报错必须失败，不能解释为空表；正式写入仍由插件进行新鲜回读核验。
修改这些共享接口须核心发布。对应验证为 `tests/test_daily_sign_connector_responses.py`、
`tests/test_daily_sign_connector_http_scope.py` 和 `tests/test_daily_sign_v2_packaged_protocol.py`；
最后一项使用真实插件 ZIP、Broker 和隔离 MySQL，并覆盖写后数据不匹配时停止后续发布。

```bash
PYTHONPATH=agent:. PYTHON_DOTENV_DISABLED=1 python -m scripts.plugin_maintenance describe sync_customer_service_problems_v2
PYTHONPATH=agent:. PYTHON_DOTENV_DISABLED=1 python -m scripts.plugin_maintenance test sync_customer_service_problems_v2 --report PATH_TO_NEW_REPORT
```

`package` 必须先通过选定插件的局部测试，再调用现有权威 packager；失败不会生成工件。
输出与报告不能覆盖已有文件。Service v2 的版本由其 Manifest 决定，首方 v2 包仍使用
既有 `service_v2_plugins._shared.build_zip` 注入受管 SDK 与共享 payload。

```bash
PYTHONPATH=agent:. PYTHON_DOTENV_DISABLED=1 python -m scripts.plugin_maintenance package self_pickup_problem_upload_v2 --output PATH_TO_NEW_ZIP --report PATH_TO_NEW_REPORT
```

V2 由超级管理员上传已验证 ZIP，不使用 V1 私钥签名流程。旧 ACTION_V1 的 `--version`、
`--test-signing` 和签名环境参数仅保留给历史离线回归，不能作为当前生产打包或发布方法。
核心部署使用 Service-V2-only 退役索引，详见[ECS 手册](../deploy/publish_to_ecs.md)。

## 核心更新判定

冻结宿主后加 `--base-ref FROZEN_COMMIT`，入口比较实际 Git 差异和未跟踪文件。
目标插件自身目录与其已审核局部测试属于局部候选；测试辅助文件与文档单列，不被误判为核心更新。
共享契约、数据库迁移、运行器或宿主依赖变化返回 `CORE_UPDATE_REQUIRED` 并阻止局部测试/打包。
其他插件或插件共享源码变化返回 `MULTI_PLUGIN_REVIEW_REQUIRED`，要求分别核验受影响插件后处理，
同样阻止误用本插件的单独测试范围，但不把其他插件变化宣称为宿主核心更新。
未指定比较基线则明确报告 `NOT_COMPARED`，不能据此声称兼容性已经证明。

源码归属比较不替代安装期 Host API、能力、数据契约、版本、签名与 generation 校验。
升级和回退必须继续经过原有实例生命周期、精确版本/CAS与在途排空机制；此离线入口
不会直接覆盖正在运行的代码、修改主服务依赖、开启业务定时或触发真实业务写入。

## 规则变更的验证范围

| 变更 | 维护与验证要求 |
|---|---|
| 扫描批量、候选上限、问题件说明等包内规则 | 使用现有 Host 合同，测试并升级对应 ZIP；预览和正式确认须绑定同一包所提交的计划。 |
| 分批分类共享规则 | 唯一源码为 `split_pending_problem_upload_v2/payload/split_rules.py`；构建器分别装入分批与到货统计 ZIP，两个包都需验证和升级。 |
| 财务重扫、目标、日期切块、分页规则 | 由财务包生成计划；Host 按计划和实际采集分页核验，保留来源总量、金额与余额链检查。 |
| Host API、公共字段协议或持久化合同 | 按核心变更处理，并检查所有消费包；不能只升级 ZIP 或让旧包继续使用不兼容的新 Host。 |

`tests/test_plugin_business_boundary.py` 在冻结 Host 上运行真实隔离 ZIP，覆盖包内上限、问题件说明、分批分类、财务默认值和页大小变化。相关用例已进入 `PLUGIN_TESTS`；以 `describe` 输出为准，不用修改 Host 后的通过结果证明插件规则独立。扫描计划绑定另由 `tests/test_sync_scan_codes_v1_v2_parity.py` 覆盖。

2026-10-08 的显式计划协议涉及核心和三个 ZIP 的协同升级，操作顺序见[插件维护入口](../service_v2_plugins/README.md#显式计划协议的协同升级)，实际安装和只读预览结果见[上线记录](../../docs/plugin_business_boundary_20261008.md)。后续安装响应超时应先回读原操作和实例状态，不能直接重复提交。

## 验证证据

V2 测试前后按权威打包器计算实际包、清单、成员文件及所选测试文件摘要；任何变化令测试失败。打包再次核对相同材料和实际输出 ZIP，拒绝测试后源码漂移。

机器报告保留实际测试命令、退出码、源码提交、工作区是否有未提交内容、目标插件、
工件版本与摘要。未通过测试的报告记 `FAIL`；报告中有未提交工作区时不能把提交 SHA
误当成工件全部源码已经提交。第一轮仍执行完整 CI；此入口仅用于后续兼容插件维护。

当前 Service V2 六包 CLI 演练可在 `v32_cli_test` 隔离库运行
`python -m tests.v32_acceptance.plugin_cli_batch`。它逐一真实执行 `describe/test/package`，
生成各自日志、测试报告、当前 V2 ZIP 和独立计算的摘要；不会安装这些 ZIP。
正式冻结后加 `--host-freeze PATH_TO_FREEZE_JSON`，各局部命令使用冻结 SHA 比较，
并在全部操作前后核验宿主文件。每次使用新的目录，历史结果不覆盖。

## 每日应签的维护边界

当前应签算法和类型清单位于 `service_v2_plugins/sync_daily_should_sign_v2/payload/business/`；Host 只保存插件明确提交的布尔判断，不共同维护规则。现行版本只读取 R13 来源，不调用 TMS 问题件、签收或轨迹接口；业务规则见[身份与统一对话维护说明](../../docs/identity_and_unified_chat.md)。

回归 `tests/test_daily_sign_v2_packaged_protocol.py` 使用真实 ZIP、Broker 和隔离 MySQL，覆盖写后数据损坏等场景。只改包内规则或来源处理时可单独升级 ZIP；Host 端口适配与公共协议变化仍需核心发布。
