# V3.2 复现、发布与回退

本轮只交付代码、隔离验收及工件，不运行本页的生产操作。首次引入来源数据库、执行恢复及通用设置桥属于核心更新；之后兼容范围内的字段适配和局部业务判断更新对应插件。具体源码归属见 [维护归属表](low_maintenance_v32.md)，原验收逐组要求见 [验收矩阵](low_maintenance_v32_acceptance.json)。

## 从干净环境复现

在本仓库根目录按 [隔离环境说明](../tests/v32_acceptance/environment.md) 准备指定 Python、MySQL 和 Chromium。禁止用生产数据库替换测试库，禁止载入生产 `.env`。以下命令只运行该说明中的隔离环境。

```sh
bash tests/v32_acceptance/isolated_environment.sh setup
bash tests/v32_acceptance/isolated_environment.sh run python agent/scripts/accept_low_maintenance_v32.py --phase ci
```

`--phase ci` 是诊断模式，即使全部代码检查通过，也不会宣称整轮验收通过。宿主源代码提交后，创建新的冻结文件，再运行完整验收；输出目录必须先存在，冻结文件不能覆盖旧证据。

```sh
mkdir -p .task_tmp/v32/final
bash tests/v32_acceptance/isolated_environment.sh run python -m tests.v32_acceptance.host_freeze freeze .task_tmp/v32/final/host-freeze.json
bash tests/v32_acceptance/isolated_environment.sh run python agent/scripts/accept_low_maintenance_v32.py --host-freeze .task_tmp/v32/final/host-freeze.json
bash tests/v32_acceptance/isolated_environment.sh run python -m tests.v32_acceptance.host_freeze verify .task_tmp/v32/final/host-freeze.json
```

完整入口重新执行原验收组、实际浏览器性能、业务链路和维护演练，报告中保留各子命令及日志。它不接受上次遗留的 PASS 文件；缺失、失败、跳过的必要证据均不能算通过。字段和决策演练核对冻结文件、宿主进程启动标识、实际结果、升级/回退和无关任务；演练包只用隔离信任材料。

兼容插件维护的 `describe/test/package` 命令及生产签名环境参数见 [插件维护入口](../agent/docs/plugin_maintenance.md)。更新前先保留当前实际安装 ZIP 和摘要；先测试再打包，不能把测试 ZIP 当作生产签名包。任一公共契约、共享数据库、运行器或宿主依赖变化必须按核心更新处理。

## 首次核心发布顺序

生产发布需另行明确授权，由既有发布流程执行：

- 通过系统 SSH 客户端以 `boyce` 公钥连接既定 ECS；先确认 `id -un`，并核对 `agent.service` 的 `WorkingDirectory=/home/boyce/agent`。不要读取私钥文件或退回密码认证。
- 保留当前应用版本、插件原包、数据库一致性备份和现有账号/计划引用。排空在途执行；未知外部写入先按真实回读核验，不能靠清空租约或删除记录解除。
- 在备份副本上先运行迁移与检查模式，核对来源迁移诊断、角色、计划、启停、Run/实例引用及财务汇总。无法确定的旧来源保持未分配并输出诊断，不能猜测站点。
- 使用经验证的核心提交和相容插件包，运行既有增量迁移。新增来源发布溯源和扫描恢复数据必须与匹配的核心代码同时更新；禁止单独拷贝这些共享模块冒充插件更新。
- 先检查服务配置和沙箱启动自检，再按既有授权流程切换版本和重启。检查模块列表、来源历史、账号设置及只读健康状态；真实定时及外部业务写入按原有明确授权范围恢复。

本轮没有自动执行上述连接、备份、迁移、配置修改、部署或重启。

## 回退条件

插件回退上传本实例曾实际成功提交的原签名 ZIP，使用当前版本/CAS进入同一升级流程。保持当前账号、资源和计划，经契约检查、在途排空及代际切换后生效。未提交准备包、其他实例历史、摘要不符或不相容配置均拒绝。配置恢复走同版本的已提交设置历史，不直接编辑数据库。

核心回退先停止领取新任务并排空在途工作，保留新产生的业务数据和未知写入核验记录。采用相容应用备份；若旧代码不能读取新结构，在恢复副本上完成数据兼容验证后再决定切换。不要反向删除新表、手工覆盖账本或把旧数据库快照直接覆盖已产生新业务的库。

维护演练的“回退”恢复代码/配置版本，不撤销已经核验的外部业务动作。报告同时列出回退前后业务结果与副作用账本。

交付报告与工件转存后，停止本任务隔离 MySQL、浏览器和运行器，卸载专用临时绑定并清理任务临时数据；不清理原项目工作区。本轮交付后停止，不自动进入生产发布或后续智能功能。
