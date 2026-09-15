---
module: 自动化插件平台
type: 维护入口
status: active
authority: canonical
owner: repository
updated: 2026-09-15
---

# 当前插件维护入口

现行功能使用 Service V2 独立 ZIP 和 Direct Invocation。Console、定时及飞书固定指令直接调用插件；自然对话由共用会话服务选择已授权能力。每次调用结束返回结果，不产生待领取任务。

## 源码归属

每个 `<plugin_id>_v2/payload/action.py` 是对应业务编排的唯一源码。每日应签的采集、计算、顺延类型、截止时间和表格渲染位于 `sync_daily_should_sign_v2/payload/business/`。打卡的共同实现位于 `_shared/clock_runtime.py`；公共包内结果协议位于 `_shared/result.py`。

构建器 `_shared/build_zip.py` 只从当前插件及明确的共享协议文件取源码，不读取旧 V1 目录。共享的原页字段协议位于仓库根 `shared/ronghui_finance_fields.py`、`shared/ronghui_customer_problem_fields.py`，由 Host 和 ZIP 同源引用；修改这些公共协议仍需要核心更新。`_shared/` 的改动也须检查所有受影响包。

每日应签从 2.0.8 起向 Host 明确提交 `upload_complete/before_cutoff/postpones_sign` 布尔判断。Host 只验证类型、身份和持久化回读，不维护问题件类型清单或重新计算截止时间。新增顺延类型只修改插件规则并升级该 ZIP。部署本次边界变更时应先升级至 2.0.8，再更新 Host；旧 Host 接受这些字段，新 Host 对缺失判断明确拒绝，绝不静默回退。

各插件当前版本以其 `manifest.json` 为准；线上安装版本和执行结果以后台真实实例记录为准，不从目录名推断。

## 测试与升级

仓库根目录使用 Python 3.10：

```bash
PYTHONPATH=agent:. PYTHON_DOTENV_DISABLED=1 python -m scripts.plugin_maintenance test sync_scan_codes_v2
PYTHONPATH=agent:. PYTHON_DOTENV_DISABLED=1 python -m scripts.plugin_maintenance package sync_scan_codes_v2 --output .task_tmp/scan.zip --report .task_tmp/scan-report.json
```

替换插件 ID 即可测试和打包。V2 由超级管理员安装，保留原配置和入口；不使用旧 V1 签名包格式。现有实例后续维护使用升级入口，不重新执行历史迁移。真实隔离 ZIP、MySQL 和两类飞书表回读测试见 `tests/test_daily_sign_v2_packaged_protocol.py`，其中包含只改包内顺延类型、Host 不变的场景。

## 历史边界

生产发布要求 Service-V2-only 退役索引及全部权威 COMPLETED 迁移记录。`agent/legacy/first_party_automation_plugins/` 只保留离线迁移回归所需的 V1 传输、两种旧适配器和摘要，不进入 ECS 发布清单；旧业务算法不在该目录复制。旧版本身份和已执行 SQL 仅用于解释历史事实。发布器将服务器旧源码树移入当次回滚材料，当前运行目录不再保留旧导入树。
