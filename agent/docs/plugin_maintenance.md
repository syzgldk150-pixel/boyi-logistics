---
module: 插件局部维护
type: 操作说明
tags: [插件, 测试, 打包, 维护]
related: [service_v2_developer_tooling.md, automation_plugin_platform.md, code_navigation_index.md]
status: active
updated: 2026-09-07
---

# 插件局部测试和打包

入口为 `scripts/plugin_maintenance.py`，从仓库根目录运行。它选择明确插件对应的真实
payload、生产 adapter、Broker/router 与结果校验测试；不会调用部署器或操作生产服务、
生产数据库，也不会读取 `.env` 或凭据文件。需要协议与 MySQL 的测试只在明确的隔离环境运行。
当前范围是扫描、统计、分批、自提及财务、客服采集器。
`describe` 列出实际测试节点，未知插件或缺失测试会失败，不退回全量或无测试打包。

每日应签的接口返回适配归宿主 `plugin_core_adapters/daily_sign_ports.py`：问题件只传业务字段，
飞书只传规范记录、单元格和明确的写入确认，不向插件转发账号标识、表格定位信息及重复包装。
缺失数据、分页未读完、接口报错必须失败，不能解释为空表；正式写入仍由插件进行新鲜回读核验。
修改这些共享接口须核心发布。对应验证为 `tests/test_daily_sign_connector_responses.py`、
`tests/test_daily_sign_connector_http_scope.py` 和 `tests/test_daily_sign_v2_packaged_protocol.py`；
最后一项使用真实插件 ZIP、Broker 和隔离 MySQL，并覆盖写后数据不匹配时停止后续发布。

```bash
PYTHONPATH=agent:. PYTHON_DOTENV_DISABLED=1 python -m scripts.plugin_maintenance describe sync_customer_service_problems
PYTHONPATH=agent:. PYTHON_DOTENV_DISABLED=1 python -m scripts.plugin_maintenance test sync_customer_service_problems --report PATH_TO_NEW_REPORT
```

`package` 必须先通过选定插件的局部测试，再调用现有权威 packager；失败不会生成工件。
输出与报告不能覆盖已有文件。Service v2 的版本由其 Manifest 决定，首方 v2 包仍使用
既有 `service_v2_plugins._shared.build_zip` 注入受管 SDK 与共享 payload。

```bash
PYTHONPATH=agent:. PYTHON_DOTENV_DISABLED=1 python -m scripts.plugin_maintenance package self_pickup_problem_upload_v2 --output PATH_TO_NEW_ZIP --report PATH_TO_NEW_REPORT
```

ACTION_V1 使用当前插件的真实 payload 与既有 Manifest/签名验证实现，要求明确的新
`--version`。隔离验收可加 `--test-signing`：临时 Ed25519 私钥仅在当前进程内存中，报告
只包含公钥并把工件标为 `TEST_ONLY`。该工件不具备生产信任。

正式 ACTION_V1 签名使用发布环境已注入的 `--signing-key-env ENVIRONMENT_NAME` 与
`--key-id KEY_ID`，命令不发现、读取或输出私钥文件。缺失、无效或非 Ed25519 的注入值
明确失败。本轮隔离任务不得使用生产签名材料。

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

## 验证证据

机器报告保留实际测试命令、退出码、源码提交、工作区是否有未提交内容、目标插件、
工件版本与摘要。未通过测试的报告记 `FAIL`；报告中有未提交工作区时不能把提交 SHA
误当成工件全部源码已经提交。第一轮仍执行完整 CI；此入口仅用于后续兼容插件维护。

六个当前插件的完整 CLI 验证可在 `v32_cli_test` 隔离库运行
`python -m tests.v32_acceptance.plugin_cli_batch`。它逐一真实执行 `describe/test/package`，
生成各自日志、测试报告、测试签名 ZIP 和独立计算的摘要；不会安装这些 ZIP。
正式冻结后加 `--host-freeze PATH_TO_FREEZE_JSON`，各局部命令使用冻结 SHA 比较，
并在全部操作前后核验宿主文件。每次使用新的目录，历史结果不覆盖。
