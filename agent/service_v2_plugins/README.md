---
module: 自动化插件平台
type: 维护入口
status: active
authority: canonical
owner: repository
updated: 2026-09-11
---

# 当前插件维护入口

这里的每个插件都构建为独立 Service V2 ZIP，在隔离子进程中执行实际业务逻辑，通过宿主已注册接口复用账号、读取平台和保存结果。日常触发使用 Direct Invocation，执行结束返回结果，不创建待领取任务。

源码实现和线上安装状态是两个事实：本轮工作区的生产适配、打包与隔离执行已实现；是否已接管线上实例须以实际发布及迁移记录为准。

## 局部修改

常用参数、平台账号和表格引用在后台设置。字段映射、筛选、计算和提交顺序修改对应业务文件；其中 `agent/first_party_automation_plugins/<业务名>/payload/` 是现有算法的唯一源码，构建时嵌入 V2 ZIP，不是安装后调用旧 V1 插件。V1 迁移打包器仍复用这些源码，以便比较迁移前后结果。不得复制一份算法分别维护。

| 功能 | 插件目录 | 主要业务源码 |
| --- | --- | --- |
| 大祥、大祥 S 站打卡 | `clockin_daxiang_v2`、`clockin_daxiang_s_v2` | `_shared/clock_runtime.py` |
| 到货清单、出港清单 | `sync_arrive_list_v2`、`sync_site_send_list_v2` | 同名去掉 `_v2` 的 `payload/action.py` |
| 寄件同步、签收状态 | `sync_daily_send_orders_v2`、`sync_delivery_status_v2` | 同名去掉 `_v2` 的 `payload/action.py` |
| 韵达预报、寄件同步 | `sync_yunda_dispatch_forecast_v2`、`sync_yunda_send_waybills_v2` | 同名去掉 `_v2` 的 `payload/action.py` |
| 扫描、到货统计 | `sync_scan_codes_v2`、`sync_arrival_stats_v2` | 同名去掉 `_v2` 的 `payload/action.py` |
| 自提、分批问题件 | `self_pickup_problem_upload_v2`、`split_pending_problem_upload_v2` | 同名去掉 `_v2` 的 `payload/action.py` |
| 财务、客服采集 | `sync_finance_bills_v2`、`sync_customer_service_problems_v2` | 对应 `action.py` 和字段定义文件 |
| 每日应签 | `sync_daily_should_sign_v2` | `agent/tools/daily_sign_*`；`_shared/daily_sign_package.py` 只适配 I/O 导入 |

宿主接口或共享数据库变化需要核心更新。`_shared/` 有多个包的共同源码，修改它需要检查受影响的插件，不能当作单插件独享文件。

当前设置页修订版为打卡包 `1.2.1`、其他包 `2.0.1`：对象/列表字段可正常加载，多账号选择保持完整集合。版本号属于各插件自身，均使用 Service V2 协议；不是 V1 运行器。真实 ZIP 设置页浏览器回归见 `tests/test_production_plugin_settings_browser.py`。历史 `2.0.0` 清单仅作为迁移 048 的隔离测试数据保留，不参与运行或打包。

## 测试和打包

仓库根目录使用 Python 3.10 和项目锁定的开发依赖：

```bash
PYTHONPATH=agent:. PYTHON_DOTENV_DISABLED=1 python -m scripts.plugin_maintenance test sync_scan_codes_v2
PYTHONPATH=agent:. PYTHON_DOTENV_DISABLED=1 python -m scripts.plugin_maintenance package sync_scan_codes_v2 --output .task_tmp/scan.zip --report .task_tmp/scan-report.json
```

替换插件名即可用于表中所有插件。打包命令先执行该插件的测试，再生成 ZIP 和版本、摘要、源码状态报告；输出路径必须不存在。真实数据库专项验收需显式指定隔离 MySQL，不使用生产库。

V2 使用超级管理员安装权限与上传包摘要校验，不采用旧 V1 的 Ed25519 签名包格式。升级前检查 Host API、声明能力和设置兼容性；回退使用该实例已有的原版本工件，保持原账号和资源配置。

## 切换验证

迁移复制现有配置和准确的入口归属。验证阶段只允许超级管理员从 Console 手动调用目标，定时、飞书和 Webhook 仍由源实例承接。只有取得本次目标版本的真实调用验证结果后才能切换。切换会等待已接受的调用和正在核验的写入结束，不会排队补跑。原来停用的源实例切换后目标仍停用。

隔离验收入口：`tests/test_v2_migration_lifecycle_mysql.py`、`tests/test_plugin_migration_direct_mysql.py`、`tests/test_v2_maintenance_mysql.py`。维护演练在宿主源码不变时真实安装、升级、执行并回退插件，同时运行无关插件；外部平台只在 HTTP/数据源边界替换为隔离服务。
