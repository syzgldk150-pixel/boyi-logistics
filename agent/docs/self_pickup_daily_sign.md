---
module: 自提部每日应签
type: 维护手册
status: active
updated: 2026-10-12
---

# 自提部每日应签

插件 `sync_self_pickup_should_sign_v2`，初始版本 `1.0.0`。读取实例所选 R13 账号截至北京时间今天的全部未签收清单，按单号匹配已有统计累计到货件数，输出到“每日到货表 / 自提部应签明细”。查询范围、十二列内容、差异写入、清尾、独立回读和黄色条件格式均复用现有每日应签源码。

## 安装与绑定

在自动化页面安装 ZIP，实例名称使用“自提部每日应签”。在插件设置中完成两项明确绑定：

- `daily_sign_r13`：选择 R13 自提部账号，登录态由现有账号管理提供，不从开发浏览器导入，也不选择默认账号。
- `daily_sign_sheet`：选择飞书“每日到货表 / 自提部应签明细”电子表格资源，数据范围为该工作表的 `A2:L末行`，第一行保持下述十二列表头。范围末行应覆盖允许管理的全部数据区，以便清除上次残留行。目标资源必须在资源目录中精确登记，不能把原“应签明细”的资源绑定给新实例。

包不携带真实账号 ID、表格定位或凭据；安装、保存绑定、启用和实际执行分别核验。本次创建不自动安装生产实例，也不修改已有每日应签配置。

手动入口为“自提部每日应签”。项目定时及飞书固定命令“自提部每日应签”默认关闭；获得执行时间或渠道授权后，通过实例现有设置启用。无需新增宿主接口、迁移数据库或部署核心服务。

## 输出与来源

| 列 | 表头 | 来源 |
| --- | --- | --- |
| A | 运单编号 | R13 当前查询单号 |
| B | 规划应签收时间 | R13 页面展示应签时间 |
| C | 问题件类型 | R13 当前问题件类型 |
| D | 登记时间 | R13 当前问题登记时间 |
| E | 内容 | R13 当前问题内容 |
| F | 登记网点 | R13 当前登记网点 |
| G | 货物品名 | R13 当前货物品名 |
| H | 包装类型 | R13 当前包装类型 |
| I | 货物件数 | R13 当前件数 |
| J | 收件人地址 | R13 原页地址 |
| K | 送货方式 | R13 当前送货方式 |
| L | 到货件数 | 该单截至当前业务日最近一次成功有效统计的累计值 |

L 列不跨日相加；最新记录为 0 时保留 0，最新为空或从未匹配时显示“无数据”，不得用更早数字补齐。表头不匹配、关键字段缺失、来源失败或不完整时停止发布。权威零结果才清空已绑定工作表的数据区并回读，保留表头。写后无法核验时报告失败或未知写，不重放写入获取成功。

2026-10-12 已通过 DrissionPageMCP 在用户登录的自提部 R13 原页 `/outlets/cargoReceiptWarn` 验证真实查询：Vue 3 / Element Plus，无 iframe；`POST /gateway/site/waybillSignWarn/pageGet` 返回 HTTP 200、业务码 200，分页数据位于 `data.data`，分页元数据为 `currentPage/pageSize/total/totalPage`。页面列为 `waybillNo/displayPlanSignTime/problemType/problemRegisterDate/problemCause/problemRegisterSite/goodsName/packTypeDesc/pcs/dispAddressDesc/dispatchMode`；现有 Host 将单号、展示时间及原页地址规范化后传入插件。账号、原始业务行和认证材料均不进入源码或 ZIP。该原页只读核验不代表插件已在生产安装或写入飞书。

详细查询和标准化协议见[每日应签说明](../../docs/identity_and_unified_chat.md#每日应签现行实现)。

## 与原每日应签的边界

原每日应签还维护全局应签台账和多维表，其快照不按第二账号实例隔离。新插件因此只通过已有 `daily_sign_store.load_state` 按本次单号读取统计来源，并只使用其中的到货历史与完整性证明；不使用原账号台账、签收或问题件历史，也不调用 `start_run/persist_snapshot/finish_run`。执行记录和真实请求证据仍由通用 Direct Invocation / Broker 保存。

新包没有多维表资源角色或其写入权限，不清除、更新或发布原每日应签台账。两包共享 `sync_daily_should_sign_v2/payload/business/` 的规则、字段和 Sheet 发布逻辑，由 `_shared/daily_sign_package.py` 装入各自 ZIP；共享业务修改后须测试并按需要升级两个包。

## 开发与验证

入口为 `service_v2_plugins/sync_self_pickup_should_sign_v2/payload/action.py`；使用既有每日应签 family 构建器注入共同业务模块和通用运行入口，避免复制规则。源码发布清单单独登记新 V2 身份，不建立旧 V1 迁移关系。

在仓库根目录使用项目 Python 3.10，禁用开发工具自动加载配置：

```bash
PYTHONPATH=agent:. PYTHON_DOTENV_DISABLED=1 PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/test_self_pickup_daily_sign_v2.py tests/test_first_party_release_source_scope.py
PYTHONPATH=agent:. PYTHON_DOTENV_DISABLED=1 PYTHONDONTWRITEBYTECODE=1 python -m service_v2_plugins._shared.build_zip --source agent/service_v2_plugins/sync_self_pickup_should_sign_v2 --output .task_tmp/self-pickup-daily-sign/sync_self_pickup_should_sign_v2-1.0.0.zip
PYTHONPATH=agent:. PYTHON_DOTENV_DISABLED=1 python -m scripts.service_v2_plugin validate .task_tmp/self-pickup-daily-sign/sync_self_pickup_should_sign_v2-1.0.0.zip
PYTHONPATH=agent:. PYTHON_DOTENV_DISABLED=1 python -m scripts.service_v2_plugin inspect .task_tmp/self-pickup-daily-sign/sync_self_pickup_should_sign_v2-1.0.0.zip
PYTHONPATH=agent:. PYTHON_DOTENV_DISABLED=1 python -m scripts.service_v2_plugin permissions .task_tmp/self-pickup-daily-sign/sync_self_pickup_should_sign_v2-1.0.0.zip
```

回归使用真实隔离 ZIP、Bubblewrap、Broker 和生产 Connector，业务来源与飞书为隔离测试数据，验证精确绑定、十二列、累计值、零结果、来源失败和写后核验；不能据此宣称真实飞书已更新。
