---
module: 自动化插件平台
type: 维护入口
status: active
authority: canonical
owner: repository
updated: 2026-10-08
---

# 当前插件维护入口

现行功能使用 Service V2 独立 ZIP 和 Direct Invocation。Console、定时及飞书固定指令直接调用插件；自然对话由共用会话服务选择已授权能力。每次调用结束返回结果，不产生待领取任务。

## 业务规则与 Host 的边界

插件通过 Host 复用账号登录态、权限、定时、外部接口和结果存储，因此会依赖主系统提供的稳定能力。业务规则独立指筛选、分类、批次、文案和计算只由 ZIP 决定；Host 不再保存一份相同规则。已有能力和合同不变时，业务调整只需升级对应 ZIP；公共协议变化仍需核心更新。

扫描由插件生成 `batch_count/batch_plan_sha256`。Host 将其绑定到同一次预览和确认，不按自己的默认批量重建计划；插件在首个写入前重读来源并复核计划，Host 核验实际提交和独立账本回读。详细合同见[平台手册](../../docs/plugin-platform-v2.md#只读预览与未启用定时)。

自提和分批预览显式返回 `selection_limit`（当前分别 250、90）。Host、飞书和后台消费同一值；缺失时拒绝确认并要求更新插件、重新读取。正式问题件查询显式提交单号、类型、责任方、说明摘要和顺延标志，后续写入必须与已绑定计划一致；业务文案及类型规则只在包内维护。

分批分类唯一源码为 `split_pending_problem_upload_v2/payload/split_rules.py`。打包器将它装入分批和到货统计各自的独立 ZIP；修改规则须构建、测试并升级两个包。统计提交已分类快照和完整表格行，Host 原样保存并做独立新鲜回读，不从统计行再次生成分批判断。分类结果为零时仍提交表头并清理旧投影。

财务重扫天数、目标集合、日期切块和页大小属于插件计划。Host 保留账号权限、结构/范围约束及实际采集证据，分页按本次实际页大小核验；来源总数、交易金额和余额链的核对不变。通用传输容量上限与业务默认值分别维护。

`tests/test_plugin_business_boundary.py` 在冻结 Host 上执行真实隔离 ZIP，覆盖改变限额、问题件说明、分批判断和分页后的行为；UI 回归覆盖超量阻止及重新选择。测试变体不作为生产安装包。

### 显式计划协议的协同升级

自提、分批和到货统计三个包提升到 2.1.0；财务只修改 Host，现有财务 ZIP 不变。账号、资源、权限及定时配置均不变。本次显式计划协议需要核心与三个 ZIP 一起升级：先通过真实管理员入口暂停这三个实例并等待调用排空，再按标准流程发布核心，逐实例升级 ZIP，回读版本/代次后恢复原启用状态与原定时。不能把旧包留在新 Host 上继续运行，也不能只发布源码代替安装。保留旧包及核心回滚材料；失败时保持相关实例暂停，在恢复匹配版本后才启用。

2026-10-08 已按上述顺序完成核心 `17ad00c` 与三个 2.1.0 ZIP 的安装，恢复原启用状态，原配置保持不变。具体版本、核验结果和未覆盖的正式业务范围见[上线记录](../../docs/plugin_business_boundary_20261008.md)；该记录是当时状态，后续以真实实例为准。

## 源码归属

每个 `<plugin_id>_v2/payload/action.py` 是对应业务编排的唯一源码。每日应签的采集、计算、顺延类型、截止时间和表格渲染位于 `sync_daily_should_sign_v2/payload/business/`。融辉网点打卡的共同实现位于 `_shared/clock_runtime.py`；公共包内结果协议位于 `_shared/result.py`。

构建器 `_shared/build_zip.py` 只从当前插件及明确的共享协议文件取源码，不读取旧 V1 目录。共享的原页字段协议位于仓库根 `shared/ronghui_finance_fields.py`、`shared/ronghui_customer_problem_fields.py`，由 Host 和 ZIP 同源引用；修改这些公共协议仍需要核心更新。`_shared/` 的改动也须检查所有受影响包。

每日应签向 Host 明确提交 `upload_complete/before_cutoff/postpones_sign` 布尔判断。Host 只验证类型、身份和持久化回读，不维护问题件类型清单或重新计算截止时间，缺少判断时明确拒绝，绝不静默回退。新增顺延类型只修改插件规则并升级该 ZIP。

每日应签只读取 R13 截至今天的全部未签收单，再按本次单号批量读取累计到货快照；扫描默认每批 200 单。各插件现行规则见本目录 `CLAUDE.md`，每日应签协议详见[业务说明](../../docs/identity_and_unified_chat.md)。

各插件当前版本以其 `manifest.json` 为准；线上安装版本和执行结果以后台真实实例记录为准，不从目录名推断。

## 测试与升级

仓库根目录使用 Python 3.10：

```bash
PYTHONPATH=agent:. PYTHON_DOTENV_DISABLED=1 python -m scripts.plugin_maintenance test sync_scan_codes_v2
PYTHONPATH=agent:. PYTHON_DOTENV_DISABLED=1 python -m scripts.plugin_maintenance package sync_scan_codes_v2 --output .task_tmp/scan.zip --report .task_tmp/scan-report.json
```

迁移而来的现有插件替换插件 ID 即可测试和打包。新建通用包 `r7_vehicle_checkin_v2` 使用 `scripts.service_v2_plugin` 打包，页面接口、参数及核验规则应全部在包内，通过通用 `http.request` 复用账号登录态，详见 [通用账号请求](../docs/plugin_account_http.md)。账号、定时、原站协议与局部测试命令见 [R7车线每日打卡](../docs/r7_vehicle_checkin.md)。V2 由超级管理员安装，保留原配置和入口；不使用旧 V1 签名包格式。现有实例后续维护使用升级入口，不重新执行历史迁移。真实隔离 ZIP、MySQL 和两类飞书表回读测试见 `tests/test_daily_sign_v2_packaged_protocol.py`，其中包含只改包内顺延类型、Host 不变的场景。

## 历史边界

生产发布要求 Service-V2-only 退役索引及全部权威 COMPLETED 迁移记录。`agent/legacy/first_party_automation_plugins/` 只保留离线迁移回归所需的 V1 传输、两种旧适配器、摘要、分批1.0.26的固定规则字节及扫描 V1 的固定 action 字节，不进入 ECS 发布清单。该固定快照只重建已有历史摘要，不维护或执行当前算法；V2仍只从本目录读取现行规则。旧版本身份和已执行 SQL 仅用于解释历史事实。发布器将服务器旧源码树移入当次回滚材料，当前运行目录不再保留旧导入树。
