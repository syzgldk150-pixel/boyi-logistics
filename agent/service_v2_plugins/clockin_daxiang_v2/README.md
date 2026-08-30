---
module: 自动化插件平台
type: 插件开发说明
tags: [service_v2, 大祥站, 打卡]
related: [../clockin_daxiang_s_v2/README.md]
status: active
updated: 2026-08-31
---

# clockin_daxiang_v2

这是独立的 `service_v2` `1.1.0` 源包。Provider `run` 操作固定声明 `external_write`，三个 Host action 仍在 `capabilities[*].operations` 中以字符串声明，其 effect 由 Host Capability Registry 权威给出。每次运行都校验逐 contribution governance；两次提交必须分别留下宿主写开始回执，并以 `precheck -> submit -> verify -> submit -> verify` 的 Python-only Host 调用观测、唯一 Evidence 和严格 postcondition proof 闭合 ResultVerifier，任一缺失进入未知写而不重放。首次安装只启用 Console 人工入口；Manifest 同时声明与旧项目一致的 `Asia/Shanghai` 每日 18:30 定时入口，但默认关闭，只有完成真实写后核验并执行迁移接管后才能启用。账号、站点参数和运行状态由平台项目绑定，包内不包含凭据、飞书命令、Webhook、事件订阅或自定义前端。

从仓库根目录构建到忽略的任务临时目录中的新路径；验证完成后精确删除生成物：

```bash
python agent/service_v2_plugins/_shared/build_zip.py --source agent/service_v2_plugins/clockin_daxiang_v2 --output .task_tmp/clockin_daxiang_v2.zip
```
