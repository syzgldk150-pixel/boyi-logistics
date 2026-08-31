---
module: Service v2 离线开发工具
type: 开发手册
tags: [Service v2, CLI, 确定性打包, 权限投影, 离线场景]
related:
  - ../../docs/plugin-platform-v2.md
  - ../agent/automation_plugins/developer_v2.py
  - ../agent/automation_plugins/developer_reports_v2.py
  - ../agent/automation_plugins/developer_simulator_v2.py
  - ../scripts/service_v2_plugin.py
  - ../extension_sdk/schemas/manifest-v2.schema.json
status: active
updated: 2026-08-31
---

# Service v2 离线开发工具

## 1. 边界

`agent/scripts/service_v2_plugin.py` 是 Service v2 源码、ZIP 和闭合场景的离线开发入口。它只处理调用者明确指定的本地路径，并把源码目录或 ZIP 交给现有 `verify_unsigned_plugin_zip_v2` 与 `ServiceV2ProjectContract.from_manifest` 权威链。七个命令都不会连接 Agent、Console、生产数据库、TMS、飞书或其他网络服务，也不会安装插件、建立项目、创建 grant、改变授权或触发生命周期操作。

在仓库根目录通过 Agent 包根直接运行：

```bash
PYTHONPATH=agent PYTHON_DOTENV_DISABLED=1 python -m scripts.service_v2_plugin --help
```

命令成功时向标准输出写一个稳定 JSON 对象；参数、合同或本地安全检查失败时向标准错误写闭合错误并返回非零状态。工具不加载 `.env`，也不接受凭据、账号 ID 或生产连接参数。

## 2. 七个命令

| 命令 | 调用形式 | 结果与边界 |
|---|---|---|
| `init` | `init DESTINATION --plugin-id ID [--name NAME] [--version VERSION]` | 只在不存在的目标目录创建最小 `compute` + Console 示例；创建前先在内存中走完整包与项目合同校验，不预建 Scheduler、Webhook、飞书、Event 或 Harness。 |
| `validate` | `validate ARTIFACT` | 对源码目录先做确定性内存打包，对 ZIP 直接校验；只返回从真实字节计算的 identity 与合同回执。 |
| `package` | `package SOURCE OUTPUT` | 生成确定性 ZIP；输出已存在时拒绝覆盖，最终路径出现前先完成权威验证。 |
| `inspect` | `inspect ARTIFACT` | 只投影 canonical identity、成员相对路径/大小/摘要、合同摘要与安装向导材料；不输出文件正文、绝对路径或环境。 |
| `permissions` | `permissions ARTIFACT` | 从已验证 Manifest、Provider effect 与 Host Capability Registry 投影声明权限；不创建 grant，不解析项目绑定，不代表当前运行授权。 |
| `diff` | `diff BEFORE AFTER` | 比较两个已验证工件的身份、版本、成员、Manifest、权限、effect、贡献、配置 Schema 与存储声明；只给审阅分类，不声明项目配置或运行兼容性。 |
| `test` | `test ARTIFACT --scenarios FILE [--timeout-seconds SECONDS]` | 在真实本地 Linux sandbox 中运行闭合 fixture；不接触真实 Host、账号、网络或业务数据。timeout 默认 30 秒，只接受 `1..300`，CLI 解析后模拟器会再次执行同一有界校验。 |

常用流程示例：

```bash
PYTHONPATH=agent PYTHON_DOTENV_DISABLED=1 python -m scripts.service_v2_plugin init ./sample_compute --plugin-id sample_compute
PYTHONPATH=agent PYTHON_DOTENV_DISABLED=1 python -m scripts.service_v2_plugin validate ./sample_compute
PYTHONPATH=agent PYTHON_DOTENV_DISABLED=1 python -m scripts.service_v2_plugin package ./sample_compute ./sample_compute.zip
PYTHONPATH=agent PYTHON_DOTENV_DISABLED=1 python -m scripts.service_v2_plugin inspect ./sample_compute.zip
PYTHONPATH=agent PYTHON_DOTENV_DISABLED=1 python -m scripts.service_v2_plugin permissions ./sample_compute.zip
PYTHONPATH=agent PYTHON_DOTENV_DISABLED=1 python -m scripts.service_v2_plugin diff ./sample_compute.zip ./sample_compute-next.zip
PYTHONPATH=agent PYTHON_DOTENV_DISABLED=1 python -m scripts.service_v2_plugin test ./sample_compute.zip --scenarios ./sample-compute-scenarios.json
```

这些路径都是说明用本地路径；命令不会把它们转换成安装请求或发送到服务端。

## 3. 源目录、SDK 与确定性 ZIP

源码根目录必须精确只有以下两个入口：

```text
manifest.json
payload/
  main.py
  ...
```

`manifest.json` 必须是普通文件，`payload/` 必须是真实目录，递归成员只能是普通目录或普通文件。符号链接、特殊文件、额外根成员和源码自带的 `payload/boyi_plugin_sdk.py` 都会失败。SDK 不由插件作者复制；打包器从仓库当前 Service v2 SDK 单点注入，防止源码携带不同实现。

目录遍历先检查全部目录项名称，再读取任何成员内容。`.env*`、credential、secret、key、certificate、session、token、Cookie 和密码等敏感候选名称会在打开文件前被拒绝；工具不会为了判断候选内容而读取该文件。显式 JSON 场景文件同样使用敏感名称检查、`lstat`、`O_NOFOLLOW` 和读取前后文件身份/时间元数据核对，只接受严格 UTF-8、无重复键、无非有限数字的根对象。

源码按成员相对路径排序，以固定 ZIP 元数据和仓库 SDK 生成字节确定的包；相同输入产生相同包 identity。`package` 在内存中先完成 `verify_unsigned_plugin_zip_v2` 与 `ServiceV2ProjectContract.from_manifest`，再写输出目录中的临时文件，并以不覆盖方式公布最终文件。目标已存在、并发出现、写入失败或发布失败都会显式报错；失败只清理本次精确临时对象。

Manifest 的编辑器 Schema 位于 `agent/extension_sdk/schemas/manifest-v2.schema.json`。它帮助编辑器补全闭合字段，但运行时权威仍是 `manifest_v2.py`、ZIP verifier 与 `ServiceV2ProjectContract`；通过编辑器 Schema 不能代替 `validate`。

Console/Feishu contribution 可选声明 `selection_preview_operation`；原 `operation` 是 execute，两者必须属于同一 service，且 preview 必须是 `read`、execute 必须是 `external_write`。同一包的 Console/Feishu selection 声明必须共享完全相同的 service/preview/execute 三元组；这三个 Host-owned 参数 `dry_run/selected_bill_codes/preview_fingerprint` 不能与插件配置字段重名，其他 contribution kind 也不能声明 selection 配对。

仅 `service.invoke` capability 可选声明 `action_call_limits`。键集合必须与 `operations` 精确相等，每个值为 `1..1000` 的整数且总和不超过 1000；这些额度进入签名 Broker contract，不能增删 action 或改变 effect/governance。未声明时继续使用旧的每 action 64 次额度和旧 canonical material。

Connector 不是一种 ZIP Provider。源码包的 `provides` 与 contribution target 仍只能使用 `plugin.*`；声明宿主 Connector 依赖时，`requires` 必须使用三种闭合形式之一：`{service,binding_kind:account,account_role}`、`{service,binding_kind:resource,resource_role}` 或 `{service,binding_kind:host_internal}`。账号角色必须在 `account_roles` 中声明为 `required=true`，资源角色可为 `required=true|false`；`validate` 会拒绝额外字段、未声明角色、非必填账号角色，以及 ZIP 尝试提供 `connector.*` 的情况。

每个 Connector operation 的合同固定为 `{name,effect,input_schema,output_schema,max_input_bytes,max_output_bytes}`，effect 允许 `read/internal_write/external_write`。input/output cap 纳入扩展 contract hash；legacy account + read + 默认 cap 的 canonical hash 保持旧 material，不因新增 binding 类型或 cap 字段漂移。宿主先解析 binding、input Schema 和 input cap，再决定是否允许写 marker；handler 返回后再核验 output Schema、output cap 和结果脱敏。`preflight_services` 只闭合解析依赖，不增加 Broker call。账号/资源 ID、绑定字段和宿主引用只能留在 Broker/Host 私有 side channel，插件结果或错误中出现这些值（含嵌套/包装）必须失败。

`connector.fixture.tracking@1/query` 只用于显式离线 Host 集成测试：测试调用方必须主动提供本地 JSON fixture 和 `fixture` 系统的 `tracking_account` 绑定。它不会由开发 CLI、安装器或生产组合自动加载；生产 `ConnectorRegistry` 默认为空。真实 TMS、飞书、数据库和写 Connector 都是 `PRODUCTION_GATED`，开发工具不得用 fixture 结果冒充生产连通性或授权。

Scheduler contribution 在 `default_enabled=false` 时可以省略 `schedule`，且不得由 CLI 或安装器填充伪造时间；enabled contribution 只能使用项目真实 schedule。MIG001 的 arrival source 没有 Scheduler，目标保持 disabled/no schedule；若源项目已有 enabled Scheduler，迁移必须显式返回 `PLUGIN_MIGRATION_SCHEDULER_PRODUCTION_GATED`，不能在离线层复制或切换。

### MIG001 到货统计包

`agent/service_v2_plugins/sync_arrival_stats_v2/` 是独立 Service v2 包，包内算法和结果契约使用 v1 payload/action 与共享结果模块的逐字节嵌入副本。离线 fixture 只验证代表性 payload 的 parity、稳定投影和 primitive 顺序，不是正式容量上限或生产吞吐证明；真实 TMS/Feishu/资源读写、独立写后核验、descriptors/handlers、安装、入口接管和部署均保持 `PRODUCTION_GATED`。

### MIG002 自提问题件候选包

`agent/service_v2_plugins/self_pickup_problem_upload_v2/` 是独立的
Service v2 离线候选包。它的唯一业务算法源仍是 v1
`agent/first_party_automation_plugins/self_pickup_problem_upload/payload/action.py`；
专用 `agent/service_v2_plugins/_shared/build_zip.py` 在确定性 ZIP 中逐字节嵌入
该 action 和 first-party result helper（分别为
`payload/action.py`、`payload/boyi_plugin_result.py`），并注入受管的
`main.py` 与 SDK。相同输入产生相同 ZIP，构建器只写调用方指定且此前不存在的
输出路径。候选 payload 不导入 legacy 路径、不修改 `sys.path`，也不使用 whole-tool
fallback；因此它不是 generic `service_v2_plugin package` 示例的生产安装结果。

该包提供
`plugin.self_pickup_problem_upload_v2.self_pickup_problem_upload@1` 的两个不可变
operation：`preview/read` 与 `execute/external_write`。Console 和 Feishu
contribution 都声明 `operation=execute`、`selection_preview_operation=preview`，
且 `default_enabled=false`；不存在 Scheduler、Webhook、Event 或 Harness。
Preview 请求必须是 `dry_run=true` 且不带选择；正式执行必须是 `dry_run=false`，并带
规范的非空 `selected_bill_codes` 与 `preview_fingerprint`。Feishu 的生产入口切换
还必须支持“preview → 用户多轮选择 → 带原 preview fingerprint 的 execute”，不能
把命令直接变成一次性写入入口。

包外 Connector 依赖严格分为三个独立 service：

| Connector service | binding | 本地 v1 role 检查 | primitive → operation/effect |
|---|---|---|---|
| `connector.boyi.self_pickup_source_sheet@1` | resource `self_pickup_source_sheet` | `self_pickup_source_sheet` | `read_rows` → `read_rows/read` |
| `connector.boyi.self_pickup_primary_ronghui@1` | account `account_id` | `account_id` | `query` → `query/read`；`create` → `create/external_write`；`verify` → `verify/read` |
| `connector.boyi.self_pickup_daxiang_s_ronghui@1` | account `daxiang_s_account_id` | `daxiang_s_account_id` | `query` → `query/read`；`create` → `create/external_write`；`verify` → `verify/read` |

这些 v1 role 仅用于包内 adapter 的本地一致性检查，账号/资源标识只存在于 Host
私有 side channel，绝不进入插件 JSON。持久配置仅有
`include_daxiang_s_self_pickup` 与 `limit`。`service.invoke` action budget
必须精确为 `read_rows=1`、`query=250`、`create=250`、`verify=250`，总量为 751。

正式执行的第一次真实 Connector 调用必须一次提交三个唯一且完整的
`preflight_services`，之后不得重复 preflight 或额外调用 Broker；preview 只预检
source Sheet。写边界从 `create` 开始：写前失败为 `NOT_APPLIED`，`create` 之后的
异常为 `WRITE_OUTCOME_UNKNOWN`。正式成功必须保留全部 Host Evidence，并逐票证明
`verify`；preview 成功只能产生 read-only Evidence。离线 fixture 覆盖双来源、重复、
重排稳定性、来源漂移、全目标 preflight 失败以及 create/verify 不确定结果，并比较
v1/v2 稳定业务投影和 primitive 顺序；fixture Host Broker 不代表真实 Connector。

三个真实 Connector 的 descriptors/handlers/注册、安装、项目配置、账号与资源绑定、
Console/Feishu 入口所有权切换（尤其 Feishu 多轮选择）、真实 Sheet/Ronghui 读写与
独立验证、Evidence 验收、部署和生产运行均为 `PRODUCTION_GATED`。离线测试可以显式
注入本地 Host Broker，但不得伪造生产 Connector 注册或真实业务数据。

### MIG003 分批问题件候选包

`agent/service_v2_plugins/split_pending_problem_upload_v2/` 是默认关闭的独立
Service v2 离线候选包。确定性构建器逐字节嵌入 v1
`first_party_automation_plugins/split_pending_problem_upload/payload/action.py` 与共享
result helper；插件 payload 不导入 `agent`、`tools` 或 legacy whole-tool，也不修改
`sys.path`。包提供同一 service 的 `preview/read` 和 `execute/external_write`；Console
与 exact 飞书命令“分批”都声明 execute + selection preview，且无
Scheduler/Webhook/Event/Harness。

v1 action 继续唯一拥有 A:S 19 列校验、分批/有发未到分类、逐票
`expected_quantity = arrived_quantity + pending_quantity`、汇总数量守恒、候选指纹、
最多 90 票有序选择、全部目标 query 先于写、全量 MySQL snapshot 与 Sheet 投影，
以及逐票 create → fresh verify → daily-sign event → result 的顺序。正式选择即使只含
候选子集，snapshot 与 Sheet 仍投影全部当前未齐来源；不得把投影缩成 selected 子集。

五个包外 Connector 使用最小权限分离：

| Connector service | binding | 本地 v1 role 检查 | primitive → operation/effect |
|---|---|---|---|
| `connector.boyi.split_pending_source_sheet@1` | resource `split_pending_source_sheet` | `split_pending_source_sheet` | `feishu.sheet.read_rows` → `read_rows/read` |
| `connector.boyi.split_pending_target_sheet@1` | resource `split_pending_target_sheet` | `split_pending_target_sheet` | `feishu.sheet.replace_rows` → `replace_rows/external_write` |
| `connector.boyi.split_pending_projection@1` | host_internal | target role | snapshot read/replace/result upsert → `snapshot_read/read`、`snapshot_replace/internal_write`、`result_upsert/internal_write` |
| `connector.boyi.split_pending_ronghui@1` | account `account_id` | `account_id` | problem query/create/verify → `problem_query/read`、`problem_create/external_write`、`problem_verify/read` |
| `connector.boyi.split_pending_problem_ledger@1` | account `account_id` | `account_id` | daily-sign event upsert → `event_upsert/internal_write` |

两个账号 Connector 必须共享项目当前精确 `account_id` 绑定；ledger 不能降为
host-internal，因为事件写合同依赖同一账号 descriptor。Preview 首次调用只提交
source + projection preflight；execute 首次调用一次提交全部五项，之后不重复。
九个 `service.invoke.action_call_limits` 的最坏路径为固定调用各 1 次、五个逐票动作各
90 次，总计 454。Host 通用选择最多 250 票不放宽该 action 的签名上限；第 91 票必须
在任何 Broker 调用前失败。

写边界从全量 `snapshot_replace` 调用前开始，之后 Sheet、Ronghui、event 或 result 的
任何异常、响应丢失或读回不闭合都必须是 `WRITE_OUTCOME_UNKNOWN`，不得重放。离线
fixture 只证明代表性 19 列/数量守恒、字节嵌入、v1-v2 projection/primitive parity、
每票独立 Host Evidence 和错误边界；真实 5,000×19 容量仍需在真实 descriptor 的
input/output cap 下实测，禁止截断。五个真实 Connector、账号/资源绑定、安装、入口
切换、真实 Sheet/MySQL/TMS 读写、生产 Evidence、数据库故障演练和部署均为
`PRODUCTION_GATED`。

## 4. 安全投影的含义

### `validate` 与 `inspect`

两者都消费同一已验证工件。`validate` 给出可追踪的包、Manifest、文件集、runtime、service/contribution/capability/storage 与 governance 摘要；`inspect` 在此基础上列出成员的相对路径、大小和 SHA-256，并复用安装向导的权限、角色、配置与贡献投影。它们不读取项目仓储、活动 generation、账号池或生产健康状态。

### `permissions`

权限报告的 authority 固定为 declaration-only。Provider 操作使用 Manifest 中的显式五态 effect；Host action 使用代码拥有的 Host Capability Registry；`service.invoke` 只显示运行时解析 Provider effect 的动态上限。报告中的 `grant=false`、未评估项目绑定和角色声明不能解释为插件已获授权，也不能代替管理员安装时的权限确认。

### `diff`

`diff` 会指出身份不一致、相同版本不同字节、降级、无变化或需要审阅，并显示文件、权限、effect escalation、贡献、配置 Schema 与 storage 的具体声明差异。它故意把项目配置标为未离线评估，compatibility claim 固定为空；即使两个包只改 payload 或报告没有权限扩张，也不能据此声称既有项目配置、数据迁移或生产运行兼容。

## 5. 闭合场景与真实 sandbox

场景文件根对象必须精确包含 `schema_version` 和非空 `scenarios`。每个场景字段精确为：

- `name`：场景唯一名称。
- `entrypoint`：Manifest 中的 contribution **ID**，例如 `run`；模拟器解析它后，传给插件进程的请求 `entrypoint` 是 contribution **kind**，例如 `console`，二者不能混用。
- `arguments`：必须通过该 contribution 的真实 input Schema，且不能包含账号或敏感字段。
- `host_calls`：按插件真实调用顺序排列的一次性本地 Broker fixture。
- `expect`：必须显式给出 `status/code/write_outcome`，没有默认期望。

最小无 Host 调用场景：

```json
{
  "schema_version": 1,
  "scenarios": [
    {
      "name": "compute_success",
      "entrypoint": "run",
      "arguments": {},
      "host_calls": [],
      "expect": {
        "status": "SUCCESS",
        "code": "OK",
        "write_outcome": "SUCCEEDED"
      }
    }
  ]
}
```

每个 Host call 必须精确包含 `operation/action/role/arguments/data/fault`。`operation/action/role` 必须来自已验证 Manifest 与 Host Registry，fixture 的 arguments/data 必须分别通过真实 capability input/output Schema；调用总数与逐 action 次数都不能超过验证后合同的配额。`service.invoke` 需要真实 Provider 或宿主 Connector 合同与 effect 解析，当前纯本地 CLI 模拟器不注入这两类权威 Registry，因此固定以 `SIMULATOR_SERVICE_INVOKE_UNSUPPORTED` 拒绝，不把保护性的静态写上限或离线 tracking fixture 冒充实际运行依赖。`fault` 只能显式选择：

- `none`：返回本地 fixture data。
- `fail_before_write`：在写开始前失败。
- `write_outcome_unknown`：仅用于声明为写的 capability，强制未知写结果。
- `response_lost`：模拟 Host 已处理但响应丢失；写 capability 同样优先归类为未知写。

本地 Broker 为每个场景创建一次性 capability，先把 UUID 规范化再拒绝重放，并逐项、按序、精确匹配调用参数；多调、少调、乱序、重放、超配额或参数漂移都会失败。报告只保留分类、诊断、调用 identity 与 arguments 摘要，不回显 arguments、fixture data 或插件 result 正文。模拟器没有真实独立 Evidence/Postcondition 闭环，因此任一已到达本地 Host 的成功写也保守归类为 `WRITE_OUTCOME_UNKNOWN`；插件的 success、错误码或 `meta.write_outcome` 都不能覆盖 Host 观察。

`test` 不是进程内 mock。它要求系统真实可用的 `/usr/bin/bwrap` 与 `/usr/bin/prlimit`，使用 `--unshare-all` 的无网络命名空间、只读已验证包、只读系统 Python/stdlib、Unix Broker、临时 `/tmp` 和 `inherited={}` 的最小环境。Service v2 Manifest 固定声明 Python 3.10，因此模拟器只接受受信系统 Python 3.10；主机即使有 Python 3.12 也不能替代执行。工具、启动 canary 或受信 Python 3.10 不可用时均以 `SIMULATOR_SANDBOX_UNAVAILABLE` 关闭失败，不会退回普通本地子进程。当前离线模拟器不构建插件依赖环境；Manifest 只要声明 `requirements_lock` 或非空 wheelhouse 就以独立的 `SIMULATOR_DEPENDENCIES_UNSUPPORTED` 失败，而不是偷偷使用开发机 site-packages。

## 6. 明确禁止

离线开发工具不提供并且不得扩展为以下快捷路径：

- 连接 ECS、生产 Agent/Console、生产数据库、TMS、飞书或任意真实业务系统。
- 安装、升级、启用、停用或卸载插件。
- 创建项目、账号/资源绑定、grant、审批、Scheduler Job 或管理员授权。
- 从 `.env`、凭据文件、Shell 环境或历史运行态补齐场景数据。
- 因本地 `test` 通过而宣称真实外部写、生产兼容或上线验收完成。

安装、生命周期、真实项目绑定、代际激活和生产验收仍只走 `docs/plugin-platform-v2.md` 定义的受管链路。
