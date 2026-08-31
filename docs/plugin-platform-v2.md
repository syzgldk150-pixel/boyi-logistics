---
module: 自动化插件平台 v2
type: 开发与迁移手册
tags: [ZIP 插件, service_v2, Host API, 能力代理, 双轨迁移]
related:
  - ../agent/docs/automation_plugin_platform.md
  - ../agent/agent/automation_plugins/manifest_v2.py
  - ../agent/agent/automation_plugins/package_v2.py
  - ../agent/agent/automation_plugins/developer_v2.py
  - ../agent/agent/automation_plugins/developer_reports_v2.py
  - ../agent/agent/automation_plugins/developer_simulator_v2.py
  - ../agent/agent/automation_plugins/host_capability_registry.py
  - ../agent/agent/automation_plugins/service_registry.py
  - ../agent/agent/automation_plugins/service_v2_projection.py
  - ../agent/agent/automation_plugins/production.py
  - ../agent/agent/automation_plugins/production_projection_identity.py
  - ../agent/agent/automation_plugins/production_snapshot.py
  - ../agent/agent/scheduler.py
  - ../agent/agent/orchestration/automation_project_service_v2.py
  - ../agent/agent/orchestration/automation_project_policy_plan.py
  - ../agent/agent/orchestration/result_verifier.py
  - ../agent/main.py
  - ../agent/scripts/service_v2_plugin.py
  - ../agent/extension_sdk/schemas/manifest-v2.schema.json
  - ../agent/docs/service_v2_developer_tooling.md
  - ../agent/service_v2_plugins/
  - ../console/services/extensions.py
  - ../console/services/automation_plugin_management.py
  - ../console/services/automation_projects.py
  - ../console/services/automation_project_contributions.py
  - ../console/services/waybill_entry_extensions.py
  - ../agent/agent/orchestration/service_v2_waybill_entry_extension_host.py
  - ../shared/waybill_entry_extensions.py
  - ../shared/automation_plugin_generation_repository.py
  - ../shared/automation_plugin_generation_transition_repository.py
  - ../shared/automation_plugin_generation_runtime_repository.py
  - ../agent/migrations/034_runtime_generation_activation_journal.sql
status: active
updated: 2026-08-31
---

# ZIP 插件平台 v2 开发、安装与迁移手册

## 1. 定位与当前边界

Service v2 的目标是让一个系统事先不知道的 ZIP 在安装后贡献新的服务和入口，而不再为每个插件修改核心工具注册表或 Broker handler。插件拥有业务编排，宿主拥有身份、账号绑定、能力授权、隔离执行、运行租约、Evidence 和生命周期。

v1 与 v2 继续并存，但二者不是同一种包：

| 项目 | v1 | v2 |
|---|---|---|
| 数据分类 | `ACTION_V1` | `SERVICE_V2` |
| Manifest 判别 | `schema_version=1`，`runtime_model` 缺省或为 `action_v1` | `schema_version=2` 且 `runtime_model=service_v2` |
| 上传信任 | Ed25519 签名包 | 已验证 Console `super_admin` 直接上传无签名 ZIP |
| 身份 | 签名动作 | SHA-256 内容寻址的服务包 |
| 扩展方式 | 一个受管动作 | 服务、多个操作和声明式贡献点 |
| 迁移方式 | 保留运行 | 新建独立 v2 项目并行验证；不能把 v1 项目原地升级成 v2 |

安装器只读取 `schema_version + runtime_model` 做一次严格分流。v2 解析失败不会回退 v1，v1 解析失败也不会尝试 v2。历史 v1 包和已安装字节保持原样，v2 不要求、也不生成 Ed25519 签名。

Console 信息架构同样只有一个状态源：`/extensions` 与详情页展示真实 Catalog 中的包、权限摘要、实例健康并承载安装/升级/启停/卸载；`/automations` 维护每个项目的配置、绑定、入口、定时、权限、运行和 v1→v2 并行迁移验证。扩展中心只是现有 Catalog 和生命周期处理器的安全投影，不新增表、包仓、安装框架或运行状态。固定 15 个业务模块不属于扩展，尚未实现的 Connector 也不得预先伪造为可安装类型。

当前实现已经具备 v2 Manifest/ZIP 校验、内容摘要、Linux Python 3.10 环境、独立可查询的 Host Capability Registry、五态 effect 治理、代际注册、单 Provider 服务注册表、Catalog 就绪状态、受管 KV/collection、跨插件 `service.invoke`、数据保留以及迁移 pair/run-key 的持久化原语。以下边界必须如实显示：

| 能力 | offline_contract | offline_runtime | production_runtime |
|---|---|---|---|
| Harness | `COMPLETE` | sandbox canary 成功时 `READY / OFFLINE_RESTRICTED` | `PRODUCTION_GATED` |
| Webhook / Event | `COMPLETE` | 只有当前进程已绑定可信 ingress 时 `READY` | `PRODUCTION_GATED` |
| tracking Connector | `COMPLETE` | 仅显式 `connector-test` fixture 命令为 `READY` | `PRODUCTION_GATED` |

- Provider 的每个操作都必须以闭合 `{name,effect}` 对象声明不可变 effect；effect 只允许 `read/compute/internal_write/external_write/destructive`。同一字段进入 Manifest 摘要、Service Registry、generation、compiled invocation 和逐 contribution Plan，任一缺失或漂移都关闭失败。
- `service.invoke` 已由宿主统一实现，只能调用 Manifest `requires` 中的精确服务和 Provider 已声明的操作；解析到缺失、阻断、歧义、循环或超过八层的调用链都会显式失败。静态 Broker grant 是动态 effect 的保护上限，不代表每次调用都是写：分发前仍按 Registry 中的 Provider 精确 effect 分类，并受当前调用 contribution 的 effect ceiling 约束；只读/计算调用不产生写标记，写调用必须先留下宿主写尝试。被调用 Provider 仍使用自己的 generation lease、能力授权、隔离进程和写后 Evidence。
- `browser.session` 只开放宿主已注册且逐项审核的 action；当前双打卡包可使用 `ronghui.clock.precheck/submit/verify`。`http.request`、`file.read`、`file.write`、`event.publish` 以及任意未注册 browser action 仍固定失败为 `CAPABILITY_UNAVAILABLE`，不能旁路直连。
- Scheduler 可接入现有 `scheduled_tasks`（当前宿主时区为 `Asia/Shanghai`）。默认启用的 Scheduler 只有在 cron 可无损表示为固定 `minute hour * * *` 时才允许安装并自动转成项目 `daily_times`；不兼容默认值在写入任何项目之前失败。飞书命令已接入独立动态 Dispatcher：只允许已启用、committed/READY generation 的 exact command，固定 Action v1 与跨项目命令冲突在整批 prepare 时失败。Webhook 与 non-durable Event 由 composition root 绑定唯一 `ServiceV2ManagedIngress`：只有“持久注册就绪 + 当前进程 ingress 已绑定”才可投影有效 `READY`，未绑定调用返回 `PROJECT_RUNTIME_PROJECTION_STALE`。该 ingress 只接受已验证的闭合 identity，没有新增 HTTP 路由或外部总线；关机先撤销 backend availability，再解绑进程入口。`durable=true` Event 仍固定 `CAPABILITY_UNAVAILABLE`。真实飞书 tenant/Webhook/WS/机器人回复、Webhook 公网入口与验签、真实 event source、payload/version 合同、Outbox fan-out、ACK/retry/dead-letter/replay、多 Agent 进程全局仲裁、数据库迁移、部署及生产等价故障注入均为 `PRODUCTION_GATED`。
- Manifest 虽可声明 `resident`，Catalog 会以 `RESIDENT_RUNTIME_UNAVAILABLE` 阻断，而不是误报为可运行；当前生产插件必须使用 `on_demand`。
- 每次子进程同时受 Bubblewrap 挂载/用户/PID/网络命名空间和固定 `/usr/bin/prlimit` 约束：地址空间 1 GiB、最多 64 个进程、CPU 300 秒、单文件 16 MiB、最多 128 个打开文件。`bubblewrap` 或 `prlimit` 缺失、不是绝对常规文件或启动 canary 失败时，运行时整体 fail closed。
- 两个双打卡 v2 源包已经完成代码和离线合同验证，但在生产项目中完成真实提交与独立新鲜回读前，不得声称真实写入验收完成。

## 2. ZIP 目录合同

ZIP 根目录不能再包一层项目文件夹，只允许一个根 `manifest.json` 和 `payload/` 下的普通文件：

```text
manifest.json
payload/
  main.py
  helpers.py
  requirements.lock          # 可选
  wheelhouse/                # 有 requirements.lock 时使用
    dependency-1.2.3-py3-none-any.whl
```

开发规则如下：

1. `runtime.entrypoint` 必须是 `payload/` 下的 `.py` 文件；所有文本文件使用 UTF-8，包内路径使用 POSIX `/`。
2. 依赖可不带；若声明 `requirements_lock`，必须同时把清单逐个列出的 wheel 放入 `payload/wheelhouse/`。安装只允许离线、哈希锁定、`--no-deps` 的二进制 wheel，不访问软件源。
3. ZIP 不允许绝对路径、`..`、反斜杠、大小写冲突、目录项、符号链接、加密成员、异常压缩比或非普通文件。
4. 不允许 `setup.py`、`pyproject.toml`、安装/卸载 hook，亦不允许 HTML、CSS、JavaScript、Wasm、模板或 `static/frontend/ui/web` 自定义前端。Console 只从 Manifest Schema 渲染系统表单。
5. Console 上传入口限制为 32 MiB；校验器还限制成员数量、单文件大小、解压总量和压缩比。不要用大 ZIP 携带业务数据。
6. 包内不得包含账号、密码、Cookie、Token、私钥、真实账号 ID、数据库连接串或客户业务原始资料。

超级管理员免的是“发布审批和签名”，不是技术校验。下列情况安装必须硬阻断：ZIP 损坏或摘要不一致、路径/成员不安全、Manifest 非法、Host API 不兼容、Linux Python 3.10 环境不可用、离线依赖不可安装，以及相同 `plugin_id + version` 已对应不同字节。新版本增加或减少能力应显示差异并写审计，但不另设发布审批。

### 2.1 离线开发 CLI

仓库根目录以 `PYTHONPATH=agent PYTHON_DOTENV_DISABLED=1 python -m scripts.service_v2_plugin ...` 运行统一开发入口。它提供七个纯本地命令：`init` 创建最小无写 compute + Console 源码，`validate` 走 ZIP verifier 与项目合同权威链，`package` 生成确定性且不可覆盖的 ZIP，`inspect` 只显示 identity/成员摘要/合同向导，`permissions` 只投影声明权限而不授权，`diff` 比较两个已验证工件但不声称项目兼容，`test` 使用闭合 scenarios 在真实本地 sandbox 中运行。

源码目录精确只能有根 `manifest.json` 与 `payload/`，其中只能包含普通目录和普通文件；源码不得携带 `payload/boyi_plugin_sdk.py`，打包时由仓库当前 SDK 单点注入。遍历先按目录项名称拒绝 `.env*`、credential、secret、key/cert、session/token/Cookie/密码等敏感候选，再读取任何成员内容；符号链接、特殊文件、额外根成员均失败。相同成员字节按固定顺序和 ZIP 元数据产生相同包 identity，输出已存在或并发出现时拒绝覆盖，最终文件发布前必须完成权威验证。

`validate/inspect/permissions/diff` 都只消费显式本地路径和已验证工件，不读取项目仓储、活动 generation、账号池、环境配置或生产状态。Manifest 编辑器 Schema 在 `agent/extension_sdk/schemas/manifest-v2.schema.json`，但不能代替运行时 parser/contract。完整命令语法、scenario Schema 和输出边界见 `agent/docs/service_v2_developer_tooling.md`。

`test ARTIFACT --scenarios FILE` 的 scenario `entrypoint` 填 contribution ID；模拟器按 Manifest 解析后，插件请求中的 `entrypoint` 才是 contribution kind。它只接受闭合 `arguments/host_calls/expect`，使用一次性本地 capability、规范化 UUID 后拒绝重放、执行总量与逐 action 配额、按序精确 fixture 和不含正文的摘要报告。没有离线 Provider 合同的 `service.invoke` 固定拒绝；模拟器也不具备真实独立 Evidence/Postcondition 闭环，因此任何已到达本地 Host 的成功写均保守归类为 `WRITE_OUTCOME_UNKNOWN`。执行必须经过真实 `/usr/bin/bwrap`、`/usr/bin/prlimit`、无网络命名空间、只读包与系统 Python/stdlib、最小环境和 Unix Broker。Manifest 固定 Python 3.10，模拟器只接受受信系统 Python 3.10；主机只有 Python 3.12、工具/canary 不可用或运行时不可信时均以 `SIMULATOR_SANDBOX_UNAVAILABLE` 关闭失败，不替代执行或回退普通子进程。当前模拟器不构建离线依赖环境，包声明 requirements lock 或 wheelhouse 时以独立的 `SIMULATOR_DEPENDENCIES_UNSUPPORTED` 失败。七个命令都不会连接生产、安装插件、创建 grant 或改变授权。

## 3. Manifest v2 合同

所有顶层字段都必须出现，未知字段会失败。当前 Host API 为 `2.0.0`，所以区间必须满足 `minimum <= 2.0.0 < maximum_exclusive`。

### 3.1 字段规则

| 字段 | 规则 |
|---|---|
| `plugin_id` | 稳定小写 snake_case；版本发布后不重命名 |
| `version` | 严格 `MAJOR.MINOR.PATCH`；同版本内容不可变 |
| `runtime` | `kind=python_subprocess`、`python=3.10`、`mode=on_demand|resident`，入口和离线依赖路径必须位于 `payload/` |
| `provides` | 至少一个服务；名称固定为 `plugin.<plugin_id>.<service>@<major>`；ZIP 不能提供或覆盖 `connector.*`；每个操作是字段精确为 `name/effect` 的对象，名称稳定且唯一，effect 只能是五态闭集 |
| `requires` | 插件服务依赖精确为 `{service}`；宿主 Connector 依赖只能是闭合的 `{service,binding_kind,account_role}`、`{service,binding_kind,resource_role}` 或 `{service,binding_kind}`（分别对应 `account`、`resource`、`host_internal`）；账号角色必须在 `account_roles` 中声明为 `required=true`，资源角色可声明 `required=true|false`；不能依赖自己提供的服务 |
| `capabilities` | 只声明实际使用的能力、精确 action、账号角色或资源角色；仅 `service.invoke` 可选 `action_call_limits`，其键必须精确覆盖 operations 且每项为 `1..1000` 整数；相关 action 的声明上限合计可超过 1000，但运行时全局上限仍固定为 1000，未声明时保留旧的每 action 64 次默认 |
| `account_roles` | 声明角色、允许系统和是否必填；账号由项目绑定，不进入配置 JSON |
| `resource_roles` | 声明角色、允许资源种类和是否必填 |
| `contributes` | 必须同时含 `console/scheduler/webhook/feishu/events` 五个数组，可选 `harness/module_slots`；所有 contribution `id` 在包内全局唯一 |
| `config_schema` | 顶层必须是 `type=object`、`additionalProperties=false`，只含 `properties/required`；禁止账号 ID 和凭据类字段名 |
| `storage` | 明确 `kv` 与 `collections`；不需要存储时也要写 `false` 和空数组 |

贡献点只声明“宿主可以挂什么”：

- Console：`id/title/service/operation/default_enabled`。
- Scheduler：在上述字段外可声明五段 cron 和时区；仅当 `default_enabled=false` 时允许省略 `schedule`，省略只表示默认关闭，不能填充或推断 cron。启用的 contribution 必须使用项目配置中的真实 schedule；MIG001 若源项目 Scheduler 已启用，明确返回 `PLUGIN_MIGRATION_SCHEDULER_PRODUCTION_GATED`，不在离线层复制或切换；本任务的 arrival 源无 Scheduler，目标保持 disabled/no schedule。若声明默认启用，当前 Host 仍只接受 `Asia/Shanghai` 与固定数字 `minute hour * * *`，且一个包最多一个默认 Scheduler。项目迁移测试期必须关闭物理任务。
- Webhook：只接受 `POST` 和稳定 route 段。
- 飞书：声明命令文本，但发布时仍需宿主侧受审路由能力；不能借此把任意文本变成特权入口。
- Selection：仅 Console/Feishu 可选增加 `selection_preview_operation`；原 `operation` 是 execute。两者必须属于同一 service，preview effect 必须是 `read`、execute effect 必须是 `external_write` 且名称不同；同一包的 Console/Feishu selection 声明还必须共享完全相同的 service/preview/execute 三元组，其他 contribution kind 禁止该字段。
- Event：声明全局 exact 事件名、是否 durable 和目标服务操作；当前只有 `durable=false` 可进入离线 best-effort Dispatcher，`durable=true` 明确不可用且不降级。
- Module Slot：可选 `module_slots` 中每项字段精确为 `id/slot/title/service/operation/default_enabled`；当前 slot 只允许 `waybill_entry.actions` 与 `waybill_entry.validators`，目标 Provider operation 只允许 `read/compute`，插件不能携带或返回前端代码。

### 3.2 可复制的最小示例

```json
{
  "schema_version": 2,
  "runtime_model": "service_v2",
  "plugin_id": "demo_snapshot_v2",
  "name": "示例快照服务",
  "version": "1.0.0",
  "description": "展示 Service v2 的最小声明式合同。",
  "host_api": {
    "minimum": "2.0.0",
    "maximum_exclusive": "3.0.0"
  },
  "runtime": {
    "kind": "python_subprocess",
    "python": "3.10",
    "mode": "on_demand",
    "entrypoint": "payload/main.py",
    "requirements_lock": null,
    "wheelhouse": []
  },
  "provides": [
    {
      "service": "plugin.demo_snapshot_v2.snapshot@1",
      "operations": [
        {"name": "run", "effect": "internal_write"}
      ]
    }
  ],
  "requires": [],
  "capabilities": [
    {
      "name": "storage.collection",
      "operations": ["get", "query", "upsert"],
      "account_role": null,
      "resource_role": null
    }
  ],
  "account_roles": [],
  "resource_roles": [],
  "contributes": {
    "console": [
      {
        "id": "manual_run",
        "title": "人工运行",
        "service": "plugin.demo_snapshot_v2.snapshot@1",
        "operation": "run",
        "default_enabled": true
      }
    ],
    "scheduler": [
      {
        "id": "daily_run",
        "title": "每日运行",
        "service": "plugin.demo_snapshot_v2.snapshot@1",
        "operation": "run",
        "default_enabled": false,
        "schedule": {
          "kind": "cron",
          "expression": "0 2 * * *",
          "timezone": "Asia/Shanghai"
        }
      }
    ],
    "webhook": [],
    "feishu": [],
    "events": []
  },
  "config_schema": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "dry_run": {
        "type": "boolean",
        "default": true
      }
    },
    "required": ["dry_run"]
  },
  "storage": {
    "kv": false,
    "collections": [
      {
        "name": "run_state",
        "fields": [
          {"name": "business_date", "type": "string", "required": true},
          {"name": "result", "type": "json", "required": true}
        ],
        "indexes": [
          {"name": "by_business_date", "fields": ["business_date"]}
        ],
        "unique_constraints": [
          {"name": "one_per_business_date", "fields": ["business_date"]}
        ]
      }
    ]
  }
}
```

上例的 Scheduler 默认关闭，因此管理员可在项目设置中明确选择宿主支持的每日时刻。若把它改成 `default_enabled=true`，表达式必须仍是固定的 `0 2 * * *` 形式；周、月、范围、步长或其他时区不能作为当前 Host 的自动默认值。

`indexes` 和 `unique_constraints` 是运行时合同，不是插件 DDL。`upsert` 会在同一个事务中完成文档 CAS、普通索引摘要刷新和唯一约束检查；冲突固定返回 `CAPABILITY_COLLECTION_UNIQUE_CONFLICT`。`get` 使用精确 `document_key`；`query` 只接受 Manifest 已声明的普通索引、该索引全部字段的精确等值 `values`，以及 `1..100` 的 `limit`。不支持按 unique constraint 查询、范围条件、前缀搜索或任意扫描。

### 3.3 Host Capability Registry 与 effect

Host capability 不是一组散落的字符串常量。`HostCapabilityRegistry` 以精确 `(api_version, capability, action)` 为键，描述 effect、input/output Schema、handler key、账号/资源角色要求、Scheduler 资格、单次调用上限、超时和启用状态。Manifest 安装检查与每次 Broker 调用都重新解析这个权威描述；未注册、停用、重复或动态描述漂移都会关闭失败，Broker 统一返回 `CAPABILITY_UNAVAILABLE`。

`capabilities[*].operations` **仍是 action 字符串数组**，例如 `storage.collection` 的 `"get"`、`"query"`、`"upsert"`。插件只声明自己要调用什么，不能在 capability 声明中附加 effect、risk、lock 或 Harness 标志。`service.invoke` 是动态服务能力：它的静态 grant 只开放受保护的动态 effect 分发，实际 effect 必须在调用前从目标 Provider 的不可变操作合同取得，effect ceiling 则来自调用 contribution 的精确治理。

`service.invoke.action_call_limits` 只调整签名 Broker contract 中每个已声明 action 的调用预算，不允许增删 action 或改变 effect/governance。相关 action 的声明 maxima 可以因路径相关而合计超过 1000；这不是另一份可兑现的总预算，运行时始终把 `max_broker_calls` 截为 1000，Broker 同时强制该全局计数和逐 action 计数。旧包未声明该字段时继续生成原 canonical material 与每 action 64 次额度，不能因新字段支持而漂移历史 hash。

五态 effect 是唯一治理输入，宿主按下表机械派生，不按 `get/list/query/run` 等名称猜测，也不复用 generation lifecycle 的 `effect_kind`：

| effect | operation type / risk | lock | Evidence 与重试 | Harness | Broker 投影 / 全自动 |
|---|---|---|---|---|---|
| `read` | `read / low` | 无 | 不要求；最多 3 次安全尝试 | 允许 | `read` / 允许 |
| `compute` | `compute / low` | 无 | 不要求；最多 3 次安全尝试 | 允许 | `read` / 允许 |
| `internal_write` | `internal_projection_write / medium` | 项目 | 要求 `outcome` 与 postcondition；不自动重试 | 允许 | `write` / 允许 |
| `external_write` | `external_write / high` | 外部目标 | 要求 `service/operation/outcome` 与 postcondition；不自动重试 | 不允许 | `write` / 允许 |
| `destructive` | `destructive / extreme` | 破坏性目标 | 要求 `service/operation/outcome` 与 postcondition；不自动重试 | 不允许 | `write` / 不允许 |

Provider effect 来自 `provides[*].operations[*].effect`，Host capability effect 来自 Registry；两者都会生成完整 governance，但插件都不能自行降级风险。每个 contribution 指向的 Provider 操作决定自己的 effect，生成时把精确 target 与 governance 同时写入 invocation contract 和 compiled invocation；Planner、锁、ResultVerifier 与恢复路径只使用这一份逐 contribution 合同，不能用插件级最严格汇总替代具体调用。

## 4. 插件进程与服务规则

一个 v2 包可以提供多个服务和操作，但每个服务名同时只能有一个活动 Provider。Registry 同时持久化每个操作的精确 effect，恢复时会逐项核对，不能用同名操作的新 effect 覆盖旧合同。相同不可变包被多个项目实例引用时按包 SHA-256 共享 Provider 身份和引用计数；不同字节争用同一服务名会产生 Provider 冲突，不能按加载顺序覆盖。

插件之间不得 `import` 对方源码、读取对方目录或直接访问对方进程。跨插件依赖只能写入 `requires`，调用只能经 `service.invoke` 和服务注册表；调用方不能指定未声明服务、任意 Provider、其他项目路径、新的账号绑定或自选 effect。宿主先解析目标 Provider 的精确 effect，再确认它没有超过调用 contribution 的 effect ceiling；随后用该 effect 建立目标 generation lease。`read/compute` 不调用写标记，`internal_write/external_write/destructive` 在分发前调用一次宿主写标记，读 contribution 因而不能借 `service.invoke` 升级成写。Provider 被停用、卸载或变得不可用时，宿主先持久化关闭 consumer 的物理 Scheduler，再撤销进程内 contribution/service 路由；恢复时只有项目启用、精确 committed generation 稳定、desired schedule 有效且 migration pair 的入口所有权仍属于该项目，才重新打开任务。

### 4.1 宿主 Connector Registry

`ConnectorRegistry` 是宿主代码拥有的适配器注册表，与 ZIP Provider 使用的 `ServiceRegistry` 严格分离。ZIP 的 `provides` 和全部 contribution target 仍只能指向 `plugin.*`，不能注册、替换或控制 `connector.*` 的生命周期。Connector 依赖只能在 `requires` 使用三种显式闭合绑定：`binding_kind=account` 搭配 `account_role`（对应清单角色必须 `required=true`）、`binding_kind=resource` 搭配 `resource_role`（角色可 `required=true|false`），或 `binding_kind=host_internal`（不带账号/资源角色）。宿主在安装、generation 准备和每次调用核对服务、绑定类型、角色和允许系统/资源种类。

每个 Connector operation 都是不可变 `{name,effect,input_schema,output_schema,max_input_bytes,max_output_bytes}` 合同；effect 允许 `read/internal_write/external_write`。输入/输出 cap 属于扩展 contract hash；但 legacy account + `read` + 默认 cap 的 canonical material 保持旧形态，不能因新增绑定类型或 cap 字段漂移历史 hash。调用经 `service.invoke` 时，宿主只向适配器传递私有 account/resource/host-internal binding ref；插件不能提交或看到绑定标识、连接位置或认证材料。`preflight_services` 只做闭合依赖/绑定解析，不增加 Broker call；写 marker 前只完成 binding、input cap 和 input Schema 校验，handler 返回后再做 output cap、output Schema 及敏感结果脱敏。插件结果及错误不得包含账号/资源 ID 或其字段值（包括嵌套、包装或资源标识字段）；缺少注册、缺少唯一绑定、角色/系统/资源种类漂移、输入输出不闭合、超 cap 或敏感结果均显式失败。

首个 `connector.fixture.tracking@1/query` 只有 `read` effect。它的离线合同已闭合，且可由开发 CLI 显式运行：`service_v2_plugin.py connector-test --fixture-root <绝对可信目录> --fixture <相对 JSON 路径> --tracking-number <单号>`。该命令通过真实 `ConnectorRegistry.invoke` 和现有 input/output Schema 校验返回闭合结果，不输出 fixture 路径、合成账号或 Registry 私有身份，并固定声明 `write_attempted=false`。fixture 只能由该命令显式加载；生产 Agent 组合、安装器和其他开发命令仍构造空 `ConnectorRegistry`。Catalog 的顶层 `connectors` 与 `plugins/instances` 分开，只投影服务名、标题、绑定类型、角色、允许系统/资源种类及操作/effect，不提供生命周期动作。因此当前状态为 `offline_contract=COMPLETE`、`offline_runtime=READY`、`production_runtime=PRODUCTION_GATED`；真实 TMS、飞书、数据库或任何写 Connector 及其生产账号/资源接入、部署和验收均未开放。

插件入口从标准输入读取宿主生成的闭合 JSON，只使用其中的项目配置、入口类型、已解析 target 和逐 contribution `governance`；target 与 governance 由 committed contract 生成，插件不得覆盖。标准输出只能返回一个 JSON 对象，读/计算成功至少包含：

```json
{
  "status": "SUCCESS",
  "data": {},
  "meta": {
    "source_system": "demo_snapshot_v2",
    "observed_at": "2026-08-31T00:00:00Z",
    "record_count": 0,
    "pagination_complete": true,
    "evidence_refs": [],
    "write_outcome": "NOT_APPLIED"
  },
  "warnings": [],
  "error": null
}
```

插件不能在输出中提供 `account_id`；宿主从 generation 绑定的 Python-only side channel 注入并验证账号证明。`internal_write` 成功必须在 `data.evidence` 给出 `outcome=WRITE_VERIFIED`；`external_write/destructive` 还必须给出与 compiled target 完全一致的 `service/operation`。三类写都要返回唯一 Evidence 引用、`write_outcome=WRITE_VERIFIED`、按索引报告的 `postconditions`，以及字段精确闭合的 `postcondition_evidence`；唯一 proof 必须绑定最后一条 Evidence，并带有可与规范化结果反算一致的 summary。

外部写不能只返回第三方 ACK。每次写前必须经过宿主写尝试标记，写后必须做独立、新鲜、唯一的回读并返回 Evidence；ResultVerifier 还会把插件报告的 Evidence 顺序、写调用数和结果逐项对照 Broker 保存的 Host 调用观测。每次 Service v2 Broker 成功调用都会在业务 `data` 之外生成独立 `host_evidence_ref` 响应信封，并在 Python-only observation 的独立字段保存同一引用；SDK 仅把它作为不参与字典键、JSON 序列化或 Registry output Schema 的结果属性暴露。插件写结果必须按调用顺序回显这些 Host 引用，不能用业务输出中的同名字段、嵌套 Provider 引用或自行构造的引用替代。双打卡额外核对 `precheck -> submit -> verify -> submit -> verify` 顺序、两次唯一 operation ID、站点、打卡类型和回读结果。缺少 committed generation、写开始回执、Host 观测、唯一引用、严格 postcondition proof 或独立回读时，成功声明不会被接受；无法证明结果时进入 `WRITE_OUTCOME_UNKNOWN`，不得重试原写。插件输出和错误不得回显 Cookie、Token、账号 ID、原始请求或宿主路径。

## 5. 能力代理与凭据隔离

插件进程运行在 fail-closed Linux sandbox 中，不得直接访问系统数据库、任意文件系统或网络。插件只通过短期、本次运行绑定的 Broker capability 调用 Manifest 已声明的精确 `(capability, action, role)`；Broker 同时核对 Registry 的 input/output Schema、handler 和 effect governance。Registry output Schema 只约束 Host handler 返回的业务 `data`，Broker 不得在校验后向其中注入传输元数据；Host 调用引用固定属于独立响应信封和私有 observation。资源上限由宿主固定，ZIP 无权修改；超出内存、进程、CPU、文件或文件描述符限制只终止该插件进程，不能把限制参数作为 Manifest 配置绕过。

平台在每次调用时解析项目当前账号/资源绑定：

- 插件只知道逻辑角色，例如 `operator`，不会收到密码、Cookie、Token 或真实账号 ID。
- 配置 Schema、调用参数、输出和托管文档都会拒绝账号 ID 或凭据类字段。
- 账号缺失或会话失效必须显式 `BLOCKED_LOGIN`，不能选默认账号、第一条账号或历史账号。
- 资源缺失、类型不符或配置缺字段必须显式失败，不能猜测。
- 没有 Host backend 的能力必须返回 `CAPABILITY_UNAVAILABLE`，不能退回插件直接联网、直接读文件或调用旧 whole-tool。

当前受支持的 Manifest 能力名为：`browser.session`、`http.request`、`file.read`、`file.write`、`event.publish`、`service.invoke`、`storage.kv`、`storage.collection`。通用后端已开放托管存储和声明式 `service.invoke`；`browser.session` 只开放逐项注册的受审 action，其余名称是 fail-closed 扩展槽，不代表默认授权。

## 6. 托管 KV 与 collection

插件没有 SQL/DDL 权限。结构化状态写入 `automation_plugin_documents`，作用域固定在自己的 `automation_id`：

- KV 支持 `get` 和带 CAS 的 `put`，内部 collection 为 `_kv`。
- collection 支持精确 `document_key` 的 `get`、带 CAS 的 `put/upsert`，以及按 Manifest 普通索引全部字段做精确等值 `query`。
- `expected_version=0` 表示仅创建；更新必须提交当前版本，冲突显式失败。
- 单文档规范 JSON 上限 1 MiB；NaN、非 JSON 值和凭据/会话类字段会被拒绝。
- collection 写入会校验声明字段、必填字段和基础类型；不会把未声明字段静默保存。普通索引和 unique constraint 在同一个工作单元中以规范 JSON 的 SHA-256 维护，不保存可反查的索引原值。
- unique constraint 在 `upsert` 时强制执行；缺少可选索引字段时不生成该索引键。当前没有任意过滤、范围扫描、按 unique 查询或跨项目读取。

卸载项目时先撤销服务、入口、订阅和运行进程，托管文档从 `ACTIVE` 显式转成 `RETAINED`，内容不随项目行级联删除。只有项目已经卸载后，超级管理员才能通过独立“永久清除数据”请求，携带新的 UUID 请求 ID 和明确原因，把文档转成 `CLEARED` 并永久清空内容；该操作不可恢复，保留操作者、请求、原因、数量和时间作为独立审计。

## 7. 安装、配置与状态机

管理员操作流程：

1. 使用已验证的 Console `super_admin` 会话把 ZIP 拖入安装区；Console 计算收到字节的传输 SHA，Agent 复用最终安装的同一验证器做无副作用技术检查，只返回闭合权限、角色、配置 Schema、贡献点和调度投影。
2. 管理员在同一个连续向导中确认权限，选择账号、资源、配置、入口和定时。浏览器不提交 Manifest、摘要、项目 ID、设备、服务或操作，只提交同一 ZIP、稳定根请求 UUID 和闭合安装意图。
3. 最终安装重新验证 ZIP，不信任前一次检查投影；服务器规范化 `instance_name/config/account_bindings/resource_bindings/enabled_entrypoints/schedule/permissions_confirmed`，把包 SHA、完整意图和操作者绑定为根幂等身份，再建立新的 disabled 项目和不可变版本目录。
4. 项目用确定性子 UUID 保存配置并 reconcile desired generation；只有目标 generation 已精确 `COMMITTED/STABLE` 后，仓储事务才会校验初始配置审计、已提交 generation 和此前零状态变更，并把当时真实的项目 `record_version` 持久化为启用基线。配置、依赖、运行环境或协调失败均保留 disabled `PREPARING/BLOCKED_DEPENDENCY`，不要求重启服务。
5. 响应丢失时，同一根 UUID 只读取原安装并续做尚未完成的配置、reconcile 或启用阶段；ZIP、操作者或任一规范意图字段漂移必须返回幂等冲突，不得生成第二个项目或用最新状态冒充旧请求结果。启用和同步失败后的停用补偿使用连续、确定性的审计 witness；缺少任一 witness 或出现人工状态变更即停止旧请求重放。启用事务提交后进程崩溃、或补偿写不可用的恢复演练尚未在线完成，明确标记为 `PRODUCTION_GATED`，在演练通过前不把安装链路描述为零半启用窗口。
6. Catalog 展示目标版本、活动版本、运行模型、Host API、服务、贡献点、依赖状态和阻断原因。“包已安装”与“v2 generation 已稳定运行”必须分开显示。迁移项目例外，初始只开放 Console 人工入口。

`default_enabled` 不是“无条件可运行”。宿主无法表示的 Scheduler 和 `durable=true` Event 会在技术检查或 generation prepare 阶段显式阻断；Catalog 必须展示 `CAPABILITY_UNAVAILABLE`，不能留下“已启用但没有入口/任务”的假状态。Feishu contribution 只有在其 exact commands 与固定 Action v1、同代 contribution 和其他项目均无冲突时才可进入 READY 投影；Webhook contribution 也必须先取得全局 exact `POST + route` reservation，且 `READY` 只表示无网络 Dispatcher backend，不表示公网已开放。只有取得全局 exact event-name reservation 的 `durable=false` Event 才能以 `managed_event_dispatcher READY` 进入离线 best-effort 投影。

### 7.1 Console / Scheduler / Harness / Feishu / Webhook / Event / Module Slot 无重启热投影

当前受管热投影覆盖 Service v2 的 Console、Scheduler、Harness、Feishu、Webhook、Event 与 Module Slot contribution。generation 协调器先完整准备新代 effect，并由 generation CAS 持久化 committed generation 与对应 `scheduled_tasks`；进程内 `ManagedContributionRegistry` 再批量保存该代 `PREPARED` 材料，不能逐条开放新路由、命令、事件、工具或模块槽。Webhook 的全局 exact `POST + route`、non-durable Event 的全局 exact event name 与 Module Slot 的 generation-bound opaque handle 都必须随整代原子占用，冲突不能留下部分 reservation。投影转换携带由 generation snapshot 权威派生的完整 registration ID 集合；该集合可以为空。`COMMITTED/PREPARED/PENDING_PROJECTION` 的 observed effect keys 必须与 snapshot 计划精确相等；`TARGET/PREPARING/WAITING` 崩溃恢复只允许逐项验证已持久化的合法子集，随后仍从权威 snapshot 原子恢复整代 PREPARED reservation 续做，不能把子集误当完整 committed 代。

generation CAS 在同一数据库事务内写入 `PENDING_PROJECTION` transition token，并保存旧项目字段、旧 project policy 版本、旧 `scheduled_tasks` 及其完整 approval policy 前镜像。运行中的 APScheduler 已绑定时，Provider reference、`ManagedContributionRegistry` 和 `reload_scheduler(strict=True)` 共用同一把投影锁。strict 模式要求整份计划无非法行且每个 Job 均注册成功；失败会按切换前快照恢复全部 Job、触发器选项和 `next_run_time`。只有刷新证据闭合为 `initialized=true` 且 `invalid_tasks=[]` 后，Registry 才一次性切 exact active Provider/Console generation，并用相同 token 把 durable phase ACK 为 `ACTIVE`。

strict refresh 或 activation ACK 失败时，协调器执行 token 和 base generation 条件化的 reverse CAS：目标新代从未产生任何 lease，且项目、策略、任务 hash 均未并发变化时，精确恢复旧 committed generation、项目版本、任务名称/参数/运行字段和审批策略，再以进程投影的单调 revision 与完整身份做 compare-and-swap，在同一投影锁内恢复旧 Provider、Console/Feishu/Webhook/Event 路由与 Scheduler Job。target 回到 `PREPARED/ROLLED_BACK`，因此同一 immutable target 可以直接重试，不要求重启 Agent 或 Console。

若 reverse CAS 因目标新代存在任何 lease 历史、未知写、并发变化或 token 漂移被拒绝，系统不得继续保留混合代际：transition 标记 `BLOCKED`，持久 Scheduler gate 关闭，全部进程 Provider/Console/Harness/Feishu/Webhook/Event 路由撤销，并通过只存在于动态 Job `kwargs` 的私有 owner marker 删除该项目任务。进程 tombstone 会让后续普通或 strict reload 都跳过该项目；固定 Job 没有 marker，绝不按 id 或闭包猜测归属。启动恢复只依据 durable generation/activation phase 决定继续投影、继续旧代或保持阻断：`DRAINING/DISPOSING/BLOCKED` contribution 仅恢复成不占 route/active map 的诊断记录，`ROLLED_BACK` 完整校验 journal 后不恢复 contribution；通用 lease 入口在 transition 存在时只接受 `ACTIVE`，因此 `PENDING_PROJECTION/BLOCKED` 不会开放新执行。

Agent 启动时先完成审批策略与项目调用器装配，再构造但不启动 APScheduler，并绑定 strict reload 与 emergency withdrawer。随后 reconcile 按 durable phase 完成未确认投影、activation ACK 或阻断撤销；只有恢复闭合后才启动 Scheduler，避免 ACK 先于真实 Job 投影。

停用和卸载使用同一顺序：先严格刷新物理 Scheduler 计划，成功后再整代 withdraw Console/Scheduler/Harness/Feishu/Webhook/Event/Module Slot 注册；失败保留切换前对象并保持 pending，调用仍由 durable 项目状态 fail closed。只提供 service、没有已启用受管 contribution 的 v2 generation 不创建伪 marker。关闭最后一个受管 contribution 的权威空 generation 仍执行原子刷新：旧代即使仍有 lease 也立即从 active map 清除并进入 DRAINING；DRAINING 记录不再接收流量或占用全局飞书命令、Webhook route、event name 或 module-slot handle，但继续保留作租约排空与诊断。

Catalog 只输出白名单 `active_contributions` 与 `contribution_projection_state`；状态仅为 `ACTIVE/STALE/INACTIVE`，每条公开 active 记录只含 `contribution_id/contribution_kind/generation/phase/backend_status`。Console 手工执行入口仍只从与当前 committed generation 精确一致、`phase=COMMITTED`、`backend_status=READY` 且状态为 `ACTIVE` 的 Console 记录派生；Feishu/Webhook/Event/Module Slot 记录可以进入已启用种类展示，但绝不进入通用 Console 手工调用清单。Module Slot 只能经 7.6 的固定录单宿主调用。Harness 使用同一 Registry 的独立私有只读 snapshot，每次调用前再次解析 exact active generation；动态飞书、动态 Webhook、non-durable Event Dispatcher 与 Module Slot Host 都只解析 exact active generation，Policy 还会在创建 Command 前和同一接受 UOW 内再次核对 Registry identity。缺失、歧义、跨代、stale 或 inactive 均关闭失败。这不改变 `ACTION_V1`、既有 Webhook/Feishu、`shared/runtime_events.py`、事务 Outbox 或 Host `event.publish` 合同，也不开放自定义插件前端；`durable=true` Event 仍为 `CAPABILITY_UNAVAILABLE`。

Selection contribution 的首次预览对调用方是零业务参数：`dry_run=true`、空选集和空指纹由 Host 注入，签名 preview operation 以 read/low 治理执行；持久 preview 有效期 15 分钟。确认调用面只接受 `preview_run_id + selected_bill_codes` 以及既有已验证 transport/Actor，项目、代际、配置、contract、contribution、service、operation、effect、账号、资源、完整 fingerprint 和 `dry_run=false` 均由 Host 恢复。Command 接受 UOW 内重新锁定 Run，要求选集为原候选子集并复核全部身份与 phase 后原子 consume；同一 request 按 Command 幂等恢复，另一个 request 重用 preview 返回 `SELECTION_PREVIEW_ALREADY_CONSUMED`。过期、结果篡改、身份或 operation/effect 漂移全部关闭失败。

### 7.2 Harness 首期只读边界

`/harness` 是代码拥有、不可停用的固定 Console 模块。浏览器只能提交规范请求 UUID、Agent 签发的 Session UUID 和最多 4,000 字符的消息；Console 只接受真实 MySQL `admin/super_admin` 会话与同源写请求，再通过既有签名 principal 调用 Agent `/internal/v1/harness/sessions` 和 `/internal/v1/harness/messages`。浏览器或模型均不能提交 `automation_id/service/operation/account_id/resource_id` 等运行身份，动态工具只公开不可逆的 opaque tool id、标题和说明。

六类固定业务工具当前没有真实 handler，因此不进入有效 Tool Catalog。产品 Catalog 和消息执行由同一进程级 `HarnessRuntime` 生成；动态工具只来自当前 exact active 的 `contributes.harness`，其 Provider effect 必须是 `read` 或 `compute`，且机械治理必须同时满足 `harness_allowed=true`、`broker_effect=read`。注册材料必须绑定 generation snapshot 中真实签名的闭合 `runtime_permissions`；网络、浏览器、Office、文件角色、Broker operation 或调用额度任一开放、字段缺失或漂移都会拒绝注册和目录读取，不能生成空权限默认值。进程 backend availability 只影响当前有效投影，不改写持久 generation hash 或数据库合同。

Session 仅保存在进程内有界仓储，状态为 `MEMORY_ONLY_NON_PRODUCTION`，并精确绑定已签名管理员身份。启动时先在 Bubblewrap + `prlimit` 短命子进程执行无业务数据 canary；子进程环境清空、无网络、无仓库/Home/插件挂载、无 shell，只读挂载可信 Python 运行时、标准库及必要动态库。canary 成功后 Session 显示 `status=READY`、`availability=OFFLINE_RESTRICTED`；缺少 sandbox 或 canary 失败时显示 `CAPABILITY_UNAVAILABLE`，消息入口保持 503，不回退 Legacy Agent 或进程内假模型。

离线交互只接受精确文本 `调用只读工具：<完整标题>`。标题必须在当前 active Catalog 中唯一且完全匹配，并且该 `read/compute` 工具的输入 Schema 必须是闭合空对象；零匹配、多匹配、需要参数或 generation 漂移都显式失败。子进程保持 5 秒超时，sidecar 保持 8 次工具调用和 8 KiB 规范 JSON 结果上限。真实 LLM、六类业务读网关、持久 Session 和真实数据验证均为 `PRODUCTION_GATED`；可选 `harness_live_smoke.py` 只供手工合成数据验收，仅从对应环境变量取 Key，不进入 CI 或产品运行链。

### 7.3 动态飞书 Dispatcher 边界

`contributes.feishu[*].commands` 的每条文本以大小写敏感的精确 UTF-8 字节生成全局 `feishu:command:<sha256>` 路由键，键不包含项目 ID；哈希命中后仍必须回查 committed declaration 中存在完全相同的 command，不能用哈希命中替代文本核对。不同项目或同一 generation 的两个 contribution 争用同一 command 时，整批 prepare 以 `CONTRIBUTION_ROUTE_CONFLICT` 失败且不产生部分注册；同一项目相邻 generation 可同时保留材料，但 active map 始终只允许一个 committed generation 接收调用。

消息顺序固定为审批、pending、登录、确认等状态流程 → 固定 Action v1 deterministic route → Service v2 动态 Dispatcher → 既有 Agent/LLM。宿主无条件拥有的登录、任务取消、扫描确认、审批绑定和固定 Action v1 文本均复用实际运行 parser，并通过注入 Registry 的同一个只读判定器在动态整代 prepare 时阻止不可达冲突；条件式审批数字选择不被错误地全局保留。动态匹配不做大小写折叠、模糊、关键词、首项或 LLM 回退。未知命令才继续既有 Agent/LLM；已经匹配但稳定 event/sender/chat 任一缺失时必须公共拒绝并停止。

Dispatcher 从 Registry 只取得 `automation_id/generation/contribution_id`，飞书适配器只提交已验证 `event_id/sender_id/chat_id`。Actor 只能由 `FeishuApprovalService.resolve_actor(sender_id)` 生成；幂等键固定为 `feishu:{event_id}`，trusted context 只含 event/chat。service、operation、参数、账号、资源和原始 Webhook body 均不能跨越入口；Policy 在创建 Command 前和同一接受事务内再次核对 exact `COMMITTED/READY` Registry identity。公共回复只投影业务状态，不展示控制面身份或 Run UUID。

以上只证明离线 fixture、Webhook/WS 共享稳定事件身份的代码链和进程内单 Registry 冲突语义。真实飞书 tenant、Webhook/WS 消费、机器人回复、事件重放以及多 Agent 进程的全局 command 仲裁均为 `PRODUCTION_GATED`；未完成权威单 reconciler 或数据库级 claim 方案前，不得声称生产多进程冲突已闭合。

### 7.4 动态 Webhook Dispatcher 离线边界

`contributes.webhook[*]` 只接受严格大写 `POST` 和稳定 route 段。Registry 用大小写敏感的全局 exact `POST + route` 身份准备整代 reservation；同一 generation 内重复或跨项目争用都以 `CONTRIBUTION_ROUTE_CONFLICT` 原子失败，不留下其他 contribution 的部分 reservation。相邻 generation 的材料可以共存，但只有 exact committed/READY generation 进入 active map；停用、卸载、权威空 generation、DRAINING、BLOCKED、ROLLED_BACK 和重启恢复继续服从 7.1 的整代语义。

无网络 `ServiceV2WebhookDispatcher` 的调用面只接收适配器已验证的 method、route 和稳定 `source_event_id`。调用方不能提交或覆盖项目、service、operation、业务参数、账号、资源、Actor、原始 body、query 或 headers；Dispatcher 从 Registry 得到 exact contribution identity，Policy 只按签名项目合同编译调用。Policy 在创建 Command 前、以及同一 Command 接受 UOW 内再次核对 exact Registry identity，路由在两次检查之间被撤销或换代时关闭失败，不用旧目标继续执行。

composition root 启动时构造并绑定唯一 `ServiceV2ManagedIngress`，运输适配器只能通过 `dispatch_verified_webhook(method, route, source_event_id)` 进入已有 Dispatcher。绑定失败会阻止 Agent 启动；未绑定调用返回 `PROJECT_RUNTIME_PROJECTION_STALE`。`managed_webhook_router READY` 只代表持久注册与当前进程绑定同时有效，不代表已有公网入口。本链路未新增 HTTP 路由；现有 `ACTION_V1` Webhook 路由、验签和动态参数合同保持原样，不能被 Service v2 Dispatcher 复用或隐式接管。Event contribution 的独立离线边界见 7.5。真实公网 namespace、逐 route token/signature 及轮换、Nginx/反向代理、真实流量与 replay、跨进程全局 route 仲裁、部署和生产等价故障注入全部为 `PRODUCTION_GATED`。

### 7.5 Non-durable Event Dispatcher 离线边界

`contributes.events[*]` 使用大小写敏感的全局 exact event name。只有 `durable=false` 可以进入 `managed_event_dispatcher READY`；同一 generation 内重复或跨项目争用必须令整代 prepare 原子失败，不能留下其他 contribution 的部分 reservation。相邻 generation、权威空 generation、DRAINING、BLOCKED、ROLLED_BACK、停用、卸载和重启恢复继续服从 7.1 的 exact committed/READY active-map 语义。`durable=true` 在 prepare 时保持 `CAPABILITY_UNAVAILABLE`，绝不静默降级为 non-durable。

无外部总线的 `ServiceV2EventDispatcher` 由同一 `ServiceV2ManagedIngress` 封装，运输适配器只能调用 `dispatch_verified_event(event_name, source_event_id)`。该入口只接受已验证的 exact event name 与稳定 `source_event_id`，不接收 payload、payload version、项目、service、operation、业务参数、账号、资源或 Actor。目标身份和全部调用参数只由 Registry 与签名项目合同派生；Policy 在创建 Command 前和同一接受 UOW 内再次核对 exact Registry identity。未绑定调用固定返回 `PROJECT_RUNTIME_PROJECTION_STALE`。只有 Command 成功接受后才有持久记录，接受前进程退出或事件源断开可能丢失，因此 `READY` 只表示持久注册与当前进程绑定同时有效的离线 best-effort backend，不表示生产入口所有权、可靠投递或持久订阅。

真实 event source、payload/version 合同、Outbox fan-out、ACK/retry/dead-letter/replay、跨进程全局 event-name 仲裁、数据库迁移、部署和生产等价故障注入全部为 `PRODUCTION_GATED`。既有 `shared/runtime_events.py`、事务 Outbox、Host `event.publish`、`ACTION_V1`、Webhook 和 Feishu 链路保持不变，不得把它们接成隐式 Event 总线或降级兜底。

### 7.6 固定录单 Module Slot 边界

Module Slot 只挂载在 Console 本地博益手工录单 frame `/ocr/boyi/frame`；韵达、融辉等跨域原页不读取投影、不渲染按钮，也不接入该调用链。浏览器看到的唯一扩展描述是 Agent GET `/internal/v1/automation-projects/module-slots/waybill-entry` 返回的闭合 `data.module_slots[*] = {slot,handle,title}`。`handle` 是绑定 active generation 的不透明标识；项目、generation、contribution、service、operation、effect、账号、资源、Actor 与角色均不进入浏览器。Console 只使用仓库内固定 HTML/JavaScript/CSS：模板中的 title 经 Jinja autoescape，动态反馈只经 `textContent` 写入；宿主不解释插件 HTML/JavaScript/CSS，也不加载远程前端代码。

动作按钮调用 Console 同源 POST `/waybill-entry/extensions/{slot}/{handle}/invoke`。请求必须带 canonical `X-Browser-Request-UUID`，且 JSON 字段精确为 `{request_id,waybill}`、header UUID 与 body UUID 一致；`waybill` 的键精确为 `open_date,destination_site,sender_name,sender_phone,receiver_name,receiver_phone,receiver_address,goods_name_lines,package_type_lines,quantity_lines,weight_kg,volume_m3,freight_fee,pickup_fee,delivery_fee,transfer_fee,delivery_method,payment_method,insurance_amount,cod_amount,remark`，全部为有界字符串。`waybill_no/weight_volume/return_to/auto_print/action` 不跨越扩展边界。Console 在真实 MySQL 管理员 Session、同源和 UUID 校验后签发既有 principal，再将同一闭合 body 转发到 Agent `/internal/v1/automation-projects/module-slots/waybill-entry/{slot}/{handle}/invoke`；浏览器不能提交任意参数或覆盖运行身份。

`waybill_entry.actions` 只渲染固定 Host 按钮，Agent 只返回 `data={kind:"action",receipt:{command_id,work_item_id,run_id,status,reused,next_poll_after_ms}}`。`waybill_entry.validators` 不依赖浏览器 submit listener：Console 在原 `/waybills/manual` 的实际 `apply_manual_waybill` 前，从同一表单构造上述 21 字段 draft，生成服务端 UUID，并以签名管理员 principal 调用 Agent `/internal/v1/automation-projects/module-slots/waybill-entry/validators/invoke-active`。Agent 先取得 validators-only active snapshot，逐一运行 exact handle，结束后再次取得 snapshot 并要求完全相等；只接受闭合 `data={kind:"validator_set",validation:{valid,issues}}`。`invalid`、调用失败、超时、畸形响应或激活/停用/切代漂移都显式阻止本次保存。稳定 active 集合为空时继续原生落库；GET 投影失败只影响页面展示，旧 DOM 不参与保存门禁。任何 active validator 失败都不允许隐式跳过。

两个 slot 的 Provider effect 只允许 `read/compute`；本任务不允许插件写 TMS、飞书、生产数据库或其他外部系统。实现复用既有 Manifest、generation、Registry、Policy、Command 和 Run 链，不新增表、字段或数据库迁移。真实外部写、生产数据库连接、部署、真实 TMS/飞书数据验证和生产故障演练全部保持 `PRODUCTION_GATED`。

以上只记录离线实现与故障注入合同；没有执行生产 Scheduler、生产数据库或真实业务入口演练，不构成生产验收。

就绪状态的含义：

| 状态 | 含义 | 恢复方式 |
|---|---|---|
| `READY` | 目标 generation 稳定，依赖、配置和绑定闭合 | 可以按已启用入口执行 |
| `BLOCKED_DEPENDENCY` | `requires` Provider 缺失/未活动/冲突，或代际 coeffect 未闭合 | Provider 恢复后重新 reconcile；不重装插件 |
| `NEEDS_CONFIGURATION` | 项目配置或必需资源绑定缺失 | 保存完整配置/资源后重新 reconcile |
| `BLOCKED_LOGIN` | 必需账号未绑定，或执行时登录态不可用 | 绑定/恢复账号后重试新的运行 |
| `BLOCKED_UNKNOWN_WRITE` | 历史写尝试缺少可证明的最终结果；原 Run 禁止重放 | 仅通过权威回读闭合原 receipt，或以新的 Command 发起后续运行 |
| `PREPARING` | 目标版本已持久化但 generation 尚未稳定 | 等待 reconcile，不能误报已运行 |

安装成功但阻断是合法状态。恢复条件后应重新准备服务和入口；卸载/停用 Provider 时，其消费者重新进入 `BLOCKED_DEPENDENCY`，不能保留孤立入口。

## 8. 审计什么，以及为什么它不是审批

超级管理员安装 v2 ZIP 不需要发布审批或逐次运行审批。审计只回答“谁在何时让哪一组字节、能力和入口产生了什么结果”，不会等待另一个人批准。

Service v2 的保存结果、Catalog/Console 投影和实际 invocation 评估都固定为 `PROJECT_FULL_AUTO`；历史策略行即使漂移为 `REQUIRE_EACH_RUN`，也不得展示逐次审批或把新的 v2 Run 推入 `WAITING_APPROVAL`。固定全自动只能在调用精确匹配当前 committed generation 合同且该合同声明 `can_full_auto` 后生效；项目、配置、账号/资源、登录、依赖、入口、代际和未知写门禁仍逐项 fail closed。Command、Run、generation lease、Evidence、写尝试、写后核验和 `WRITE_OUTCOME_UNKNOWN` 处置链路全部保留。

至少应能关联以下证据：

| 阶段 | 审计内容 |
|---|---|
| 安装 | package event 与 project lifecycle event：操作者、角色、时间、请求 UUID、包 SHA-256、规范 Manifest SHA-256、版本、运行模型、Host API、技术检查结果与初始策略/入口摘要 |
| 能力变化 | 新旧 Host capability action、Registry descriptor、Provider `{name,effect}`、requires、贡献点、配置 Schema 和托管存储声明的摘要/差异 |
| 配置与绑定 | 项目配置版本、入口集合、schedule、账号/资源角色绑定变更；只存受控内部引用和摘要，不记凭据 |
| 代际与服务 | desired/committed generation、逐 contribution target/governance、Provider operation effect、注册/撤销、依赖阻断、启停、升级、回滚和引用排空 |
| 每次运行 | 项目、入口 contribution、service/operation/effect、运行 ID、generation lease、锁、开始/结束、结果、Host 调用观测、Evidence 引用、写尝试和写后验证 |
| 异常写 | `WRITE_OUTCOME_UNKNOWN`、未知原因、禁止重放状态和人工处置记录 |
| 迁移 | migration pair、配置/入口快照摘要、业务运行键归属、测试状态、接管、回滚和完成迁移 |
| 卸载与数据 | 服务/入口撤销、未完成租约或未知写阻断、保留数据，以及独立永久清除的操作者、原因和清除数量 |

不得把密码、Cookie、Token、Authorization、原始第三方响应、敏感页面正文或业务原始数据写入审计。需要追溯内容时使用包摘要、Manifest 摘要、Evidence 引用和受控运行 ID。

## 9. v1 到 v2 的双轨迁移

v1 继续服务，v2 作为新包、新项目和新 `automation_id` 建立。禁止把 v1 包伪装成 v2，也不建设长期兼容桥。

### 9.1 建立迁移对

1. 为旧 `ACTION_V1` 项目安装真正的 `SERVICE_V2` ZIP，生成独立项目。
2. 创建 migration pair 时先在一个事务中写入 `PREPARING` 持久互斥态并禁用目标物理任务；此时旧 v1 入口继续运行，目标即使已有残留内存 Job 也会在 API 返回前触发 Scheduler reload。
3. 在 `PREPARING` 保护下重新读取源项目，只按 `migration_binding_mapping.py` 中代码审阅的闭合一对一角色映射复制业务配置、账号和资源绑定；未知 pair、歧义、缺角色或 Schema 不兼容必须失败，不能按同名、签名、首项或默认值猜测。项目 ID、运行租约和历史 capability 永不复制。
4. 配置复制与快照冻结成功后才把 pair 推进为 `TESTING`。v2 desired 配置可保留原 Scheduler 意图，但物理 Scheduler、飞书生产适配、公网 Webhook namespace 和 Event 仍没有迁移入口所有权；只有 Console 人工入口用于真跑验证。无网络 Webhook Dispatcher 的 `READY` 不改变该门禁。
5. 若复制或冻结中途失败，`PREPARING` 与目标禁用状态继续持久化，API 返回 `202`、失败阶段和 `retry_with_same_request_id=true`。只能用原 migration pair、原 request UUID 精确续做，不能创建第二个 pair。

迁移状态按 `PREPARING -> TESTING -> READY -> CUTTING_OVER -> CUTOVER -> COMPLETED` 推进；失败可进入 `ERROR`，切换前后按门禁进入 `ROLLING_BACK -> ROLLED_BACK`。状态变化必须使用 CAS 版本和唯一请求 ID，响应丢失只能重放同一个请求。

迁移入口 ownership 由持久 pair 状态闭合决定：

| pair 状态 | Console / Scheduler / 固定飞书 |
|---|---|
| `TESTING`、`READY` | v1 唯一拥有；v2 只能离线验证 |
| `CUTOVER`、`COMPLETED` | v2 唯一拥有；`COMPLETED` 后禁止同源新建 pair，后续 v2 generation 升级继续复用 v2 ownership |
| `ROLLED_BACK` | v1 唯一拥有 |
| `PREPARING`、`CUTTING_OVER`、`ROLLING_BACK`、`ERROR`、损坏、过渡或历史归属歧义 | 不猜测任何一侧，全部 fail closed |

固定飞书保留命令只有 exact migration target 才能占用；消息 handler 的 pending、登录、确认和固定 Action v1 优先级不因迁移改变。启用的 v1 webhook 不在本迁移中静默接管，明确保持 `PRODUCTION_GATED`；route 资源也不得映射为业务资源。Scheduler contribution 在禁用（`default_enabled=false`）时可省略 `schedule`；启用必须使用项目真实 schedule，不得伪造默认时间。MIG001 的 arrival 源没有 Scheduler，目标保持 disabled/no schedule；若发现已启用源 Scheduler，则返回 `PLUGIN_MIGRATION_SCHEDULER_PRODUCTION_GATED`，不得在离线层复制或切换。

### 9.2 业务运行键互斥

测试期的双方在执行业务前必须从同一 migration pair 认领同一个 `business_run_key`。键必须从真实、类型化的业务身份生成，例如“动作 + 业务日期 + 网点 + 批次”，不能使用显示名称、当前时间、默认日期或模糊候选。唯一保留的宿主字段 `__host_business_date` 由宿主按 `Asia/Shanghai` 确定性注入；其他 `__host_*` 字段全部拒绝，插件也不能自行覆盖该日期。

- 旧项目已认领该周期时，v2 人工执行被阻止。
- v2 已认领时，旧项目不得重复执行。
- 运行键无法唯一确定时显式失败，零业务写入。
- 每个键记录 owner、lease、运行 ID、到期时间和终态；未知写必须结算为 `OUTCOME_UNKNOWN`，不能换一个键绕过。
- 到期的 `ACTIVE` 锁不会被盲目释放：只有持久租约和写尝试回执能证明未写时才结算为 `EXPIRED`；完整闭合的写后证据结算为 `WRITE_VERIFIED`；其余情况一律结算为 `OUTCOME_UNKNOWN` 并继续阻断。

只有在 v1 和 v2 的所有触发路径都已接入同一认领原语后，业务运行键才算真正生效。仅创建锁表或只让一侧认领不满足验收。

### 9.3 真实验证和接管门禁

每个 v2 项目分别满足以下全部条件，超级管理员才可点击“接管自动执行”：

1. v2 committed generation 持续稳定，目标版本等于活动版本。
2. 所有 `requires`、账号、资源和配置闭合。
3. 已完成一次与当前 migration pair 绑定、进入 `TESTING` 后、针对当前 committed generation、通过 Console 发起且 `dry_run=false` 的人工真实执行；历史 Scheduler、其他入口或迁移前的写入证据均不计入。
4. 每个外部写都有独立、新鲜、唯一的写后 Evidence。
5. 不存在 `WRITE_OUTCOME_UNKNOWN`、活动 migration run-key lease、活动 generation lease 或未完成运行引用。
6. v2 原 Scheduler/飞书/公网 Webhook/Event 配置已准备但仍未取得生产入口所有权；无网络 Webhook Dispatcher 不计作公网接管证据。

切换必须在一个受控事务/切换协议中：先准备 v2 入口，排空旧租约，再原子转移入口所有权；任何一步失败都保持旧入口唯一有效。成功后旧 v1 变为停用但保留，不能立即卸载。

回滚只把后续入口所有权恢复给旧项目，不撤销已经发生的第三方写。存在未知写时禁止切换和回滚；必须先完成人工 reconcile 并留下证据。只有超级管理员明确“完成迁移”，且无租约、未知写和运行引用后，旧项目才允许卸载。

### 9.4 MIG001 到货统计独立包

`sync_arrival_stats_v2` 位于 `agent/service_v2_plugins/sync_arrival_stats_v2/`，是独立的 Service v2 ZIP 项目，不把 v1 项目原地升级，也不通过旧 whole-tool 入口运行。包内算法与结果契约使用 v1 `payload/action.py` 及共享 `_runtime/result.py` 的逐字节嵌入副本；离线 fixture 只核对代表性 payload、稳定结果投影和 primitive 顺序，不能被解释为正式 20,000 条容量或生产吞吐证明。账号、资源和 host-internal binding 只留在宿主侧，插件 JSON 不包含账号/资源 ID。

MIG001 的真实 TMS 读取、Feishu/资源写、内部投影写、独立新鲜写后核验、真实 descriptors/handlers、生产安装、Scheduler/Feishu 入口接管和部署仍为 `PRODUCTION_GATED`。离线完成不等于生产真跑或迁移完成；任何写响应丢失或 postcondition 不闭合都必须保留 `WRITE_OUTCOME_UNKNOWN`，不得重放原写。

### 9.5 MIG002 自提问题件独立包

`self_pickup_problem_upload_v2` 位于 `agent/service_v2_plugins/self_pickup_problem_upload_v2/`，以逐字节嵌入的 v1 `payload/action.py` 与共享结果契约作为唯一业务算法来源。它提供同一 service 上的 `preview/read` 与 `execute/external_write`，通过一个来源 Sheet Connector 和两个精确区分账号角色的 Ronghui Connector 完成全目标 preflight；Host 预算精确为一次 `read_rows` 加最多 250 次 `query/create/verify`，共 751 次。离线测试证明代表性 preview parity、primitive 顺序、来源重排指纹稳定、来源漂移拒绝、正式逐票 query-create-fresh-verify，以及 create/verify 不确定时保留 `WRITE_OUTCOME_UNKNOWN`；代码审阅的一对一 binding map 禁止按同名、签名或首项猜测。

两个入口默认关闭，Scheduler/Webhook/Event 为空。上述源码、确定性 ZIP、fixture 与 Host 合同不等于安装或生产迁移：三个真实 Connector descriptor/handler、账号/资源与 grant、生产安装和 committed generation、Console/固定飞书多轮选择 ownership 切换、真实飞书读取、真实问题件创建及独立权威列表回读、生产数据库事务演练和部署全部为 `PRODUCTION_GATED`。当前启用的 v1 飞书选择入口会在创建 migration pair 前返回 `PLUGIN_MIGRATION_FEISHU_SELECTION_PREVIEW_PRODUCTION_GATED`，v1 继续唯一生产 owner。

### 9.6 MIG003 分批问题件独立包

`split_pending_problem_upload_v2` 位于 `agent/service_v2_plugins/split_pending_problem_upload_v2/`，确定性构建器逐字节嵌入 v1 `payload/action.py` 与共享结果 helper，不导入 legacy 工具或整链路运行时。它提供同一 service 上的 `preview/read` 与 `execute/external_write`；Console/Feishu 均绑定 `selection_preview_operation=preview` 且默认关闭，无 Scheduler/Webhook/Event/Harness。v1 action 继续唯一拥有 A:S 19 列校验、`应到=已到+未到`、候选指纹、最多 90 票有序选择、全部 query 先于任何写、全量 snapshot/Sheet 投影，以及逐票 create → fresh verify → event → result 的 Evidence 顺序。

包声明源 Sheet、目标 Sheet、内部 projection、融辉账号和同 `account_id` 事件 ledger 五个 Connector；preview 首次调用只预检源 Sheet与 projection，execute 首次调用预检全部五项且不重复。九个 `service.invoke` action 的精确上限合计 454 次。写 marker 从全量 `snapshot_replace` 前开始，此后任何响应丢失、ACK/读回不闭合或后续票失败都保留 `WRITE_OUTCOME_UNKNOWN`，不得重放或回落 whole-tool。离线 fixture 只证明字节嵌入、代表性 19 列/数量守恒、v1-v2 投影与 primitive 顺序、逐票 Host Evidence 和错误边界；五个真实 Connector descriptor/handler/grant、真实 Sheet/MySQL/TMS 数据与写入、安装、committed generation、Console/固定飞书多轮选择 ownership 切换、生产 Evidence、数据库故障演练和部署全部为 `PRODUCTION_GATED`。

### 9.7 MIG004 扫描同步独立包

`sync_scan_codes_v2` 位于 `agent/service_v2_plugins/sync_scan_codes_v2/`，确定性构建器逐字节嵌入 v1 `sync_scan_codes/payload/action.py` 与共享结果 helper，不导入 legacy whole-tool。包提供 `plugin.sync_scan_codes_v2.scan_codes@1` 上的 `preview/read` 与 `execute/external_write`；Console 和精确飞书命令“扫描”都指向 execute 且默认关闭，不声明通用 `selection_preview_operation`、Scheduler、Webhook、Event 或 Harness。配置 Schema 不保存 Host-owned 的 `dry_run` 与 `_scan_preview_binding`。

嵌入 action 继续唯一拥有稳定分页、等价重复合并、H 单排除、主子单分类、候选排序/截断、PREVIEW/FORMAL、15 分钟到期和正式权威重读。包仅声明账号绑定的 `connector.boyi.scan_ronghui@1` 与 Host 内部 `connector.boyi.scan_projection@1`；`read_page/snapshot_replace/submit/verify` 的逐 action 上限为 `500/1/499/499`，声明 maxima 合计 1499，而运行时与 Broker 的全局上限始终为 1000。Preview 首次调用只预检扫描 Connector，execute 首次调用预检两项且不重复。

正式执行必须先用权威重读结果独立核验一次全量快照替换，再让每个批次严格 submit → fresh `server_ledger_verified`；上一批未闭合就不能开始下一批。结果同时证明 `candidate = scheduled + omitted` 与 `scheduled = scanned + skipped`，空来源和非空零候选都只完成经核验的 projection，不伪造第三方提交。snapshot、submit 或 verify 后的任何不确定性固定为 `WRITE_OUTCOME_UNKNOWN`、不可重试。

v2 离线包没有复制 v1 身份专属的一次性 preview 消费机制；现有 `tests/test_scan_preview_binding.py` 继续覆盖该 Host 合同，v1 生产路由仍为唯一 owner。真实 Connector、账号绑定、安装、scan-preview handoff、Console/飞书验收、真实扫描与权威 readback、cutover、生产数据库故障演练和部署全部为 `PRODUCTION_GATED`，migration pair 以 `PLUGIN_MIGRATION_SCAN_PREVIEW_PRODUCTION_GATED` 显式阻断。

## 10. 迁移波次

波次必须按风险和宿主能力闭合情况推进，不能因源代码已存在就跳过真实验证。

1. 同时建立 `clockin_daxiang_v2` 与 `clockin_daxiang_s_v2` 两个独立项目，分别人工真实提交、分别独立回读、分别切换。任一项目成功不能替代另一项目验收。
2. `sync_customer_service_problems`、`sync_yunda_dispatch_forecast`、`sync_site_send_list`、`sync_arrive_list`。
3. `sync_delivery_status`、`sync_daily_send_orders`、`sync_yunda_send_waybills`。
4. `sync_scan_codes`、`self_pickup_problem_upload`、`split_pending_problem_upload`。
5. `sync_daily_should_sign`、`sync_arrival_stats`、`sync_finance_bills`。
6. `r7_arrival_checkin` 与 `r7_departure_checkin` 继续阻断；只有真实页面合同、字段来源和独立写后 Evidence 完整后才单独迁移。

两个双打卡示例源码位于 `agent/service_v2_plugins/clockin_daxiang_v2/` 和 `agent/service_v2_plugins/clockin_daxiang_s_v2/`。当前显式-effect 合同版本均为 `1.1.0`，Provider `run` 固定为 `external_write`；它们默认只启用 Console 人工入口，同时分别声明默认关闭的 `Asia/Shanghai` 每日 18:30 与 18:33 Scheduler，并使用 Registry 受审的 `ronghui.clock.precheck/submit/verify` Host action。源码测试证明逐 contribution 治理、Host 调用顺序、独立写后 Evidence、ResultVerifier 和未知写失败关闭；只有安装到生产项目、绑定真实账号并取得真实 `WRITE_VERIFIED` Evidence 后，才算完成每个项目各自的真跑验收。

## 11. 开发与发布检查清单

开发者交包前：

- [ ] `plugin_id`、版本、服务名和 major 稳定；同版本重新构建得到相同字节或明确提升版本。
- [ ] Manifest 只有闭合字段，Host API 区间覆盖 `2.0.0`；每个 `provides[*].operations` 项精确包含 `name/effect`，所有 contribution 指向本包提供的真实操作。
- [ ] 包内没有凭据、账号 ID、前端、hook、任意下载脚本、SQL/DDL 或其他插件源码。
- [ ] `capabilities[*].operations` 只列 Registry 已注册的 action 字符串，不复制或自报 Host effect；缺失数据、歧义、空响应和写后无法核验均显式失败。
- [ ] 每个 contribution 的 effect、target 和 governance 在编译、Plan 与运行时一致；跨插件调用不超过调用方 ceiling，读/计算路径保持零写标记。
- [ ] Selection contribution 的同 service preview/read 与 execute/external_write 配对闭合，Host-owned 输入、15 分钟候选子集与一次性消费均通过；`service.invoke.action_call_limits` 精确覆盖 operations 且预算边界闭合。
- [ ] Connector 依赖使用显式 account/resource/host_internal binding；每个 operation 的 effect、闭合 input/output Schema 和 input/output cap 进入 contract 校验，legacy account/read/default-cap hash 保持稳定，插件结果不含账号/资源身份。
- [ ] disabled Scheduler contribution 可省略 `schedule`；enabled contribution 只能使用项目真实 schedule。MIG001 source Scheduler 已启用时标记 `PLUGIN_MIGRATION_SCHEDULER_PRODUCTION_GATED`，arrival source 无 Scheduler 时目标保持 disabled/no schedule。
- [ ] 写操作先预检、再由宿主标记写尝试、最后独立回读；Evidence、postcondition proof 与 Host 调用观测闭合，未知结果不重放。
- [ ] 离线依赖全部为清单列明、哈希锁定的 wheel；在 Linux Python 3.10 上完成离线安装测试。
- [ ] ZIP 根结构、UTF-8、摘要、路径安全、压缩限制和输出 JSON 合同通过测试。
- [ ] 对 KV/collection 使用 CAS；索引查询只提交 Manifest 普通索引的完整等值字段，unique 只依赖 `upsert` 事务约束，不假定范围、扫描或按 unique 查询。

两个示例包使用标准库确定性构建器，不生成签名，也不把构建结果写入源码目录。开发时可在项目临时目录执行：

```bash
python agent/service_v2_plugins/_shared/build_zip.py \
  --source agent/service_v2_plugins/clockin_daxiang_v2 \
  --output .task_tmp/clockin_daxiang_v2.zip
```

同一源码应生成相同 ZIP 字节；修改 Manifest 或 payload 后必须提升版本，不能用同一 `plugin_id + version` 覆盖不同摘要。

管理员安装后：

- [ ] 核对包 SHA-256、Manifest、Host capability descriptor、Provider operation effect、服务和默认入口差异。
- [ ] 精确绑定账号/资源，不选默认或首项；确认 Catalog 的目标版本、活动版本和 dependency state。
- [ ] 新插件在全部条件闭合后才启用默认入口；迁移插件只开 Console。
- [ ] 真跑检查 Run、lease、Evidence、写后结果与审计链；`WRITE_OUTCOME_UNKNOWN` 必须先 reconcile。
- [ ] 卸载前确认入口、订阅、服务引用和运行进程已撤销；保留数据与永久清除分别操作。
