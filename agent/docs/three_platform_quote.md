---
module: 三平台报价
type: 维护手册
status: active
updated: 2026-10-12
---

# 三平台报价

`quote_prices_v2` 1.0.0 是独立 Service V2 插件。融辉、韵达、百世的地址解析、产品选择与金额计算全部在 ZIP 内；Host 仅提供账号绑定、已保存登录态、受声明约束的只读 HTTP、最小业务上下文及执行记录。系统工具目录不再提供 `get_price`，旧报价脚本只保留在 `agent/legacy/price_quote/`，不发布或作为回退。

## 输入与结果

飞书发送 `报价 完整省市区及详细地址，重量，体积`，也支持直接发送 `完整地址，重量，体积`。重量单位公斤，体积单位立方米，均须为有限正数。体积还支持 `长*宽*高*件数+...`，尺寸单位厘米；不补默认体积。韵达要求地址包含完整省、市、区，省略省份可能无法解析。

每次查询分别返回融辉、韵达、百世三个结果块，自提与派送分别列出。某平台或某种送货方式无有效报价时显示本次原因，其他成功结果仍保留；不得用上次金额、其他账号或默认费率填充。Console 手工录单调用同一项目，只比较用户所选送货方式，重量和体积保留原输入精度。查询只测算价格，不保存或上传运单。

| 平台 | 本次计算口径 |
| --- | --- |
| 融辉 | 沿用原页详细地址匹配、线路和中心选择，查询可用产品与计费重量、最低保价；按当前费用明细汇总费用并减折扣，分别计算自提和派送。 |
| 韵达 | 从当前录单页读取账号业务上下文与表单默认值，再查询地址、计费重量、保价区间和费用；按原页规则选择费用合计并加当前消息费、截取两位。 |
| 百世 | 从当前账号读取发站，选择平台唯一默认且有效产品；分别解析派送、站点自提地址与服务权限，按现付、普通保价及平台当次最低保价测算，合计响应中实际有金额的费用项。 |

特殊区域、需要人工指定网点/线路、额外派送加价等未闭合场景明确失败。百世结果会注明当次最低保价，上楼、进仓等附加服务须另行核实。当前输入只有地址、重量、体积，不代表已覆盖全部特殊货物与附加服务。

## 安装、账号和入口

先发布支持通用多账号只读 HTTP、`account.context` 与动态输入的核心，再通过真实超级管理员的“自动化 → 安装扩展”上传 ZIP、检查权限、安装。安装默认停用；进入设置绑定三个账号角色：

| 角色 | 账号系统 |
| --- | --- |
| `ronghui_quote` | 融辉报价账号 |
| `yunda_quote` | 韵达账号 |
| `best_quote` | 百世账号 |

保存 Console `quote` 与飞书 `command` 入口后启用实例，回读版本、账号、committed generation 与生效入口，再执行一次真实查询验收。飞书精确路由为“三平台报价”，需沿用当前有效飞书身份与后台权限绑定。本包不声明定时或 Harness 入口；不得另建定时来处理每次询价。后续维护使用同一实例的升级入口。

账号登录失效时结束本次请求，在账号管理完成登录后重新发起查询。开发浏览器登录与 ECS 保存会话分别验证，不复制浏览器凭据到插件。源码提交、核心发布、ZIP 安装和真实业务验收各自记录，不能互相替代。

## 源码与验证

业务实现位于 `agent/service_v2_plugins/quote_prices_v2/payload/`，24 项只读请求在该目录 `manifest.json` 的 `http_requests` 中声明；账号业务上下文只返回融辉当前操作人和站点的四个必要字段，不返回认证材料。飞书与 Console 将本次输入传入真实 Direct Invocation；同一请求 UUID 重放必须匹配原调用的完整输入。

2026-10-11/12 已通过真实原页接口与未保存录单表单对照：融辉六个产品/方式组合、韵达两种方式、百世两种方式金额均一致。真实地址和经营报价不入仓库。纯包测试使用虚构数据；`tests/test_quote_prices_v2_packaged.py` 使用真实 Linux 隔离进程、SDK 与 Broker，外部平台响应由模拟传输提供，不能代替线上实例验收。

在仓库根目录、项目 Python 3.10 环境中执行：

```bash
PYTHONPATH=agent:. PYTHON_DOTENV_DISABLED=1 PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/test_quote_prices_v2.py tests/test_quote_ronghui_v2.py tests/test_quote_yunda_v2.py tests/test_quote_best_v2.py tests/test_quote_prices_v2_packaged.py tests/test_quote_runtime_inputs.py tests/test_quote_system_retirement.py
PYTHONPATH=agent:. PYTHON_DOTENV_DISABLED=1 python -m scripts.service_v2_plugin package agent/service_v2_plugins/quote_prices_v2 .task_tmp/quote_prices_v2-1.0.0.zip
PYTHONPATH=agent:. PYTHON_DOTENV_DISABLED=1 python -m scripts.service_v2_plugin inspect .task_tmp/quote_prices_v2-1.0.0.zip
PYTHONPATH=agent:. PYTHON_DOTENV_DISABLED=1 python -m scripts.service_v2_plugin permissions .task_tmp/quote_prices_v2-1.0.0.zip
```

打包前确保源码目录只有 manifest、Python 源码与声明的静态文件，不含浏览器试价生成的缓存或业务资料。ZIP 不可覆盖，重复构建使用新的输出路径。
