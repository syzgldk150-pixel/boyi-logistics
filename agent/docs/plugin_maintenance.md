---
module: 插件局部维护
type: 操作说明
tags: [插件, 测试, 打包, 维护]
related: [service_v2_developer_tooling.md, automation_plugin_platform.md, code_navigation_index.md]
status: active
updated: 2026-09-15
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

当前应签算法和类型清单位于 `service_v2_plugins/sync_daily_should_sign_v2/payload/business/`；从 2.0.8 起 Host 只保存插件明确提交的布尔判断，不再共同维护规则。当前业务规则见[身份与统一对话维护说明](../../docs/identity_and_unified_chat.md)。以下 2.0.3 记录解释来源读取优化，不是当前版本声明。

`sync_daily_should_sign_v2` 的包内 `daily_sign_io.py` 在本次 Invocation 内按账号角色和来源前缀复用宿主返回的不可变来源身份。此前每条问题记录重复读取两次相同身份，大页数据会耗尽默认单动作调用次数。新调用、嵌套调用结束及异常退出均恢复各自上下文，不保存历史成功值；业务数据读取和写后回读仍实际执行，调用次数上限不变。

2.0.3 另将本次运行的逐票轨迹核验设为单个并发请求，与现有轨迹接口的并发合同一致，避免同一插件内部并发请求互相返回 `BUSINESS_RESOURCE_BUSY`。所有候选仍实际查询，任何真实查询错误仍阻止发布，不降低完整性标准。

每日应签的宿主 `read_tracking` 端口固定使用已有的 `decrypt_masked=False` 查询模式，只投影主单编号和扫描事实。该业务不使用收寄件人详情，不能因为客户字段仍脱敏就丢弃已返回的真实扫描记录；普通详情查询的解密规则不变。此端口修复属于核心更新，不能用升级插件包替代；回归同时覆盖真实轨迹适配器和敏感详情不进入插件输出。

回归 `tests/test_daily_sign_v2_packaged_protocol.py` 使用真实 ZIP、Broker 和隔离 MySQL，包含超过默认次数的大页问题数据、多个历史候选遇到仅允许单请求的接口，以及写后数据损坏场景。仅包内来源复用和并发调整可单独升级 ZIP；Host 端口适配与公共协议变化仍需核心发布。
