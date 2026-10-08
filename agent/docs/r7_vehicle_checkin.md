---
module: R7车线每日打卡
type: 维护手册
status: active
updated: 2026-10-08
---

# R7车线每日打卡

插件 `r7_vehicle_checkin_v2`，版本 `1.1.0`。每天按北京时间查询今天及前两天的计划发车任务，完整读取后，只对运输状态“车辆到达”（55）的任务逐条执行“到达待卸”。例如 2026-10-08 执行时范围为 2026-10-06 00:00:00 至 2026-10-08 23:59:59。

## 账号、手动执行与定时

在现有自动化页面安装 ZIP，进入实例设置，为 `r7_operator` 选择一个 R7 账号。账号登录态来自项目“自动化账号管理”保存的独立 SSO 会话；插件不带账号密码，不取默认账号，不导入开发浏览器的会话。登录失效时结束本次执行，由账号管理重新登录后发起新调用。

实例提供“执行R7车线每日打卡”手动入口，以及 `daily_checkin` 定时入口。定时默认关闭，用户在项目已有定时设置中选择每日时间并启用。每次执行重新计算三天范围，不补跑历史调用。飞书固定指令“R7车线每日打卡”也默认关闭，按实例权限启用。网页与飞书自然对话沿用现有插件会话服务，以已启用的对应渠道、账号配置和项目权限决定可调用性；本包未额外声明只读 Harness 能力。

1.1.0 已将 R7 查询、提交和核验逻辑全部移回 ZIP，使用通用 `http.request`。宿主只提供已选账号的认证请求及真实响应核验，不再注册 R7 专用业务接口。首次安装前仍需发布这一次通用宿主能力；以后同系统新增业务接口或改规则只升级插件。旧 1.0.0 专用 Connector 已移除，新核心需配套 1.1.0 ZIP。核心发布、插件安装启用及真实执行分别核验；定时只有在设置执行时间并启用后才运行。

## 原站协议与写后核验

2026-10-08 用 DrissionPageMCP 在原站 `/operateManage/vehicleSchedule/vehicleRegular` 验证了查询、勾选、“到达待卸”弹窗和确认请求：

| 接口（均为 POST） | 用途 |
| --- | --- |
| `/gateway/tms/public/lineTask/pageGet` | `queryType=1`、`publishStatus_CondList=["20"]`，按 `headPlanGoTime_CondStart/CondEnd` 查询；200 条分页已实测支持 |
| `/gateway/public/aurora/auth` | 获取当前登录账号的操作站点及站点类型，插件按这些业务字段选择操作站点 |
| `/gateway/tms/public/lineTask/getById` | 按任务 ID 读取新鲜详情，写前复核任务身份、状态及计划发车时间 |
| `/gateway/tms/public/lineTask/saveCenterPunch` | 使用页面详情字段及 `clockType=2`、`restockTag=false`、当前站点和操作时间，提交到达待卸 |

页面一次只能选择一条执行此动作，插件也逐条提交。查询阶段先读完所有页并核对总数和任务唯一性，再开始写入。请求参数及业务详情由插件处理；认证头、Cookie、Token 留在 Host 内，返回插件前移除凭据字段。写回执及回读均有 Host 独立观测，插件提供业务字段比较，Host 按实际响应逐项核对，见 [通用账号请求](plugin_account_http.md)。

操作站点沿用原页登录站点规则（类型 110/401）；往返同站路线按页面规则选最后一个途经站。司机到达记录缺失、已有人工到达记录、站点不明、关键字段缺失或任务状态变化时，明确失败，不自动通过原页的额外确认，也不覆盖旧打卡。

提交成功后以服务器回执中的人工到达时间为准，再独立读取同一任务，核对同一个途经站 ID 和人工到达时间。服务器可能调整提交时间，不能用弹窗时间代替服务器时间。超时、回执异常或回读不一致返回未知写结果并停止，不自动重试。无符合任务时返回“无需打卡”，不会提交。

每页 200 条，最多 100 页；本次候选超过 200 条则在任何写入前失败。正常执行免除浏览器启动、页面渲染和逐行点击；未进行可比较的执行耗时基准测试。

## 源码及验证

- 业务编排：`agent/service_v2_plugins/r7_vehicle_checkin_v2/payload/action.py`。
- R7 请求及业务核验：`agent/service_v2_plugins/r7_vehicle_checkin_v2/payload/r7_client.py`。
- 接口路径及读写声明：该插件的 `manifest.json` 中 `http_requests`。
- 通用账号传输：`agent/agent/automation_plugins/account_http.py`；通用响应字段核验：`http_write_verification.py`。
- 发布清单：`agent/scripts/first_party_release_scope.py` 单独纳入新包；历史 `r7_arrival_checkin`、`r7_departure_checkin` 继续停用。
- 回归：`tests/test_r7_vehicle_checkin_v2.py` 包含原页结构 HTTP fixtures、真实 Linux 隔离 ZIP/Broker、账号选择、手动/定时/飞书入口、无候选和未知写停止。

在仓库根目录、项目 Python 3.10 环境中执行：

```bash
PYTHONPATH=agent:. PYTHON_DOTENV_DISABLED=1 PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/test_r7_vehicle_checkin_v2.py tests/test_first_party_release_source_scope.py
PYTHONPATH=agent:. PYTHON_DOTENV_DISABLED=1 python -m scripts.service_v2_plugin package agent/service_v2_plugins/r7_vehicle_checkin_v2 .task_tmp/r7-vehicle-checkin/r7_vehicle_checkin_v2-1.1.0.zip
PYTHONPATH=agent:. PYTHON_DOTENV_DISABLED=1 python -m scripts.service_v2_plugin inspect .task_tmp/r7-vehicle-checkin/r7_vehicle_checkin_v2-1.1.0.zip
PYTHONPATH=agent:. PYTHON_DOTENV_DISABLED=1 python -m scripts.service_v2_plugin permissions .task_tmp/r7-vehicle-checkin/r7_vehicle_checkin_v2-1.1.0.zip
```

此前原站协议验收（1.0.0 制作期间，1.1.0 本次未重复提交生产打卡）：当日三天范围查到 6 条，只有 1 条“车辆到达”；页面确认后该条变为“到达待卸”（58），独立详情回读与服务器回执一致。再次查询剩余“车辆到达”为 0，未重复提交。该结果证明原站协议与此次页面操作；隔离 ZIP 测试使用模拟业务端，不代替生产实例安装后的实际运行验收。
