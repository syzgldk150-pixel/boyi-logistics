---
module: 自动化插件平台
type: 插件开发说明
tags: [service_v2, 到货统计, migration]
related: [../../first_party_automation_plugins/sync_arrival_stats/payload/action.py]
status: candidate
updated: 2026-08-31
---

# sync_arrival_stats_v2

这是 `sync_arrival_stats` 的 Service v2 离线候选包。到货统计的算法、18
字段规范化、分页、去重、累计扫描和提交顺序只来自现有 v1
`payload/action.py`；构建器把该文件以及统一 v1 Result contract 的字节一致副本
嵌入 ZIP，安装后的子进程不会从 v1 路径导入或修改 `sys.path`。

`payload/plugin.py` 只做 reviewed primitive 到精确 `service.invoke` Connector
的适配。账号、五个 Sheet 资源和 Host 内部投影均由宿主绑定；插件进程不会收到
账号 ID、资源 ID、密码、Cookie、Token 或真实文件路径。当前 Registry 组合保持
空，显式 fixture 仅由离线测试注入。

Scheduler contribution 省略 schedule 且 `default_enabled=false`；离线迁移候选始终
保持无 schedule、默认关闭。若 source 存在已启用 Scheduler，迁移会显式返回
`PLUGIN_MIGRATION_SCHEDULER_PRODUCTION_GATED`，不得离线复制或切换。v1 入口和生产
接管保持不变。真实 Ronghui、飞书、数据库 Connector、写后核验、入口切换、安装和
部署均为 `PRODUCTION_GATED`。

`pending_sheet_disabled` 是显式必配项：迁移 source 的关闭值必须原样写入；缺失配置
在项目合同校验阶段失败。值为 `true` 时不调用可选 pending Sheet，值为 `false` 时
必须由 Host 在首个 mutation 前预检该可选 resource binding，缺失只能显式
`BROKER_ROLE_UNBOUND`，不得在 action 后段才失败或静默回退。

测试记录 20,000 条、18 字段 waybill 和 19 字段统计记录的代表性 canonical JSON
测量，并以 16 MiB 作为候选上限；它不是正式 operation cap。v1 文本字段尚无正式
maxLength，且真实 arrival Connector descriptor/handler 仍为 `PRODUCTION_GATED`。正式
schema、per-operation cap 和 contract hash 必须与生产适配一并以代码拥有的 Connector
descriptor 落地，包不得自行宣称或绕过该上限。

从仓库根目录构建到任务临时目录中的新路径；验证完成后精确清理生成物：

```bash
python agent/service_v2_plugins/_shared/build_zip.py --source agent/service_v2_plugins/sync_arrival_stats_v2 --output .task_tmp/sync_arrival_stats_v2.zip
```
