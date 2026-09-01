---
status: active
updated: 2026-08-31
source_of_truth: console/AGENTS.md
---

# Console

Console 是 `boyi-logistics` 单仓中的 Web 控制台。它与 `agent/`、`shared/` 并列，组合入口位于 `console/app.py`；本地路径不是旧的独立仓 `/home/deng/projects/console`。

## 仓库位置

- 单仓根目录：`/home/deng/projects/boyi-logistics`
- Console 源码：`/home/deng/projects/boyi-logistics/console`
- Agent 源码：`/home/deng/projects/boyi-logistics/agent`
- 共享契约与仓储：`/home/deng/projects/boyi-logistics/shared`
- ECS 分拆部署目录：Console 为 `/home/boyce/console`，Agent 为 `/home/boyce/agent`

开发、测试和 Git 操作都从单仓根目录理解文件关系；不要再使用旧的并列仓路径。

## 职责边界

Console 负责：

- 后台登录、管理员会话、统一导航和页面壳层
- OCR、博益手工录单、寄件运单、物流跟踪和回单工作台
- 客服、财务、货拉拉地图调度与比价、专线分流页面
- 扩展包与生命周期、自动化项目、业务账号、系统状态和 LLM 设置界面；事项详情作为内部控制平面深链保留
- MySQL 中 Console 业务数据的校验、查询与受控写入
- 以签名管理员身份代理 Agent 的 `/internal/v1/*` 接口

Console 不负责：

- Agent 的业务编排、工具执行、审批状态机、Scheduler 或飞书长连接
- 直接调用第三方写接口来绕过 Agent Command/Run
- 在运行时创建或修改数据库结构
- 在主站同源上下文中运行韵达或融辉活动原页

## 代码分层

- `app.py`：组合根、HTTP 生命周期、认证门禁和最终请求分发；业务逻辑不应继续堆入这里。
- `routes/`：按业务域识别 GET/POST 路径，再把请求交给对应服务。
- `services/`：认证、Agent API、自动化、控制平面、业务模块、客服、财务、TMS 代理、回单/运单和 OCR 文档等领域服务。
- `navigation.py`：15 个固定模块菜单和“扩展中心 / 系统状态”控制平面菜单的唯一静态注册处。
- `database.py`：MySQL 文档仓储；只验证结构及读写数据。
- `config.py`：无副作用配置解析；`runtime_config.py` 只由服务入口执行一次运行时 bootstrap。
- `finance_service.py`：财务查询、分页、金额字符串和受控命令参数的 Console 适配层。
- `templates/`、`static/`：页面模板、样式和浏览器交互。
- `tests/`：Console 路由、服务、模板和契约回归测试。

数据库迁移统一维护在 `../agent/migrations/`。Console 保留 `ThreadingHTTPServer`，但路径识别应进入 `routes/`，领域处理应进入 `services/`。

## Agent 接口边界

所有 Console 到 Agent 的调用统一经过 `services/agent_api.py` 的 `_agent_request()`，使用 `/internal/v1/*` 与统一的 `ok/data/error` 响应契约。

- 普通服务连接使用内部服务 Token。
- 管理员命令、审批、账号、系统状态和旧模块审计读取还必须绑定真实 MySQL 管理员会话，并由服务端签名。
- 浏览器不能声明或覆盖管理员 principal、角色或来源。
- 物流跟踪由 Console `/tracking` 代理 Agent `/internal/v1/tms/tracking_query`。
- 一般执行型写请求提交 `/internal/v1/commands`；自动化项目手工执行使用专用项目 invoke 接口。
- Basic Auth 仅为应急兼容入口，不具备控制平面写权限。

## 数据库

Console 运行时唯一业务数据库是与 Agent 共用的 MySQL；没有 SQLite 运行时回退。连接不可达或结构不满足要求时应显式失败，不得静默切换后端。

数据库 DDL 只能由 Agent 的顺序迁移和部署迁移器执行。原图、处理产物、临时上传、日志和其他运行态文件位于 `console/runtime/`，不属于源码或数据库迁移。

## 第三方原页隔离

旧的同源代理路径 `/ocr/yunda/*`、`/ocr/ronghui/live/*`、`/receipts/yunda/live/*` 和 `/receipts/ronghui/live/*` 对所有方法固定返回 `410 ACTIVE_ORIGINAL_PAGE_DISABLED`，且不会调用 Agent。

当前入口从主站已登录页面请求 `/original-pages/{provider}/launch`，获取一次性短期 ticket 后跳转到独立来源 `https://www.boyi.homes/original/{yunda|ronghui}/`。主站会话 Cookie 不会发送到该来源；独立来源使用路径限定 capability，写请求还需校验独立 Origin。

## 主要页面

- `/`：概览
- `/login`：后台登录
- `/settings/accounts`：管理员账号
- `/settings/system-status`：真实 `super_admin` 的系统版本与组件状态白名单视图
- `/settings/modules`：退役兼容入口，只重定向到系统状态；其 data/audit 子路径仅保留只读兼容
- `/ocr`：运单录入与 OCR
- `/waybills`：已落库寄件运单
- `/tracking`：单票物流跟踪
- `/receipts`：回单管理
- `/modules/customer-service`：客服问题件
- `/modules/finance`：财务工作台
- `/dispatch`：map-only 路线、距离与运输方案比价，不包含车辆档案或真实派单
- `/line-haul-contacts`：专线分流资料
- `/extensions`：真实 Agent Catalog 中的扩展包、权限摘要、已安装项目健康状态和生命周期
- `/extensions/{plugin_id}`：单个扩展包及其项目；项目设置深链回自动化页
- `/automations`：自动化项目配置、绑定、入口、定时、权限和运行；v1→v2 迁移只保留签名后台接口，不在页面展示
- `/automation-accounts`：业务账号凭据与登录态的唯一 UI
- `/settings/llm`：智能模型设置
- `/work-items`：内部控制平面的跨项目历史、审批、Evidence 和异常恢复深链，不进入导航

`/extensions` 与 `/automations` 复用同一个 Agent Catalog 和实例仓储：前者统一管理包与生命周期，后者只管理项目配置与运行；自动化卡片不重复提供包管理入口，Console 不维护第二套插件目录。Service v2 在扩展中心以同一页连续完成 ZIP 检查、权限确认、账号/资源、配置、入口/定时和最终安装；检查不落库，最终请求重新上传并验证同一 ZIP，发送后冻结根 UUID 与规范意图，响应丢失只原样重试。`/automations` 不保存凭据，也不提供登录快捷入口；项目只绑定 Agent Catalog 投影的业务账号。管理员账号与业务自动化账号是两套独立系统。

## 本地启动

WSL / Linux：

```bash
cd /home/deng/projects/boyi-logistics/console
./start_backend.sh
```

默认模式会先启动同仓 Agent，再使用固定 `tmux` 会话启动 Console。可选参数：

- `--foreground`：以前台方式运行 Console
- `--no-agent`：不自动启动 Agent
- `--daemon`：显式使用默认后台模式

停止本地服务：

```bash
cd /home/deng/projects/boyi-logistics/console
./stop_backend.sh
```

Windows PowerShell 通过 WSL 调用同一脚本：

```powershell
wsl bash -lc 'cd /home/deng/projects/boyi-logistics/console && ./start_backend.sh'
```

本地默认入口为 `http://127.0.0.1:8765/`，Agent 存活检查为 `http://127.0.0.1:9000/health`。

## 生产与发布

生产入口为 `https://boyi.homes`；Console 只监听 `127.0.0.1:8765`，由受控 Nginx 反向代理。统一发布入口为：

```powershell
powershell -ExecutionPolicy Bypass -File "\\wsl.localhost\Ubuntu\home\deng\projects\boyi-logistics\agent\deploy\publish_to_ecs.ps1"
```

默认 `auto` 模式按变更范围选择 Console-only、Agent-only 或共享/迁移发布。发布成功后仍保留当次精确回滚包、上一版共享环境和数据库快照，直到业务验收完成；之后才允许独立清理。

更详细且具约束力的规则见 `console/AGENTS.md` 或 `console/CLAUDE.md`。
