---
module: 自动化插件平台
type: 插件开发说明
tags: [service_v2, 大祥站, 打卡]
related: [../clockin_daxiang_s_v2/README.md]
status: active
---

# clockin_daxiang_v2

这是独立的 `service_v2` 源包。首次安装只启用 Console 人工入口；Manifest 同时声明与旧项目一致的 `Asia/Shanghai` 每日 18:30 定时入口，但默认关闭，只有完成真实写后核验并执行迁移接管后才能启用。账号、站点参数和运行状态由平台项目绑定，包内不包含凭据、飞书命令、Webhook、事件订阅或自定义前端。

从仓库根目录构建到调用者指定的新路径：

```bash
python agent/service_v2_plugins/_shared/build_zip.py --source agent/service_v2_plugins/clockin_daxiang_v2 --output /tmp/clockin_daxiang_v2.zip
```
