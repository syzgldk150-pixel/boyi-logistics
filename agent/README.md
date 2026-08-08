# agent

本目录是 Agent 运行时工程根目录，对齐服务器上的 `/home/boyce/agent`。

## 这里负责什么

- HTTP 运行时入口
- Agent 编排与工具调度
- 飞书消息接入
- 知识库、调度模板、Phase 7 同步链路
- 价格获取、财务对账等业务工具封装

## 不在这里改什么

- 控制台页面
- OCR 工作区模板与前端样式
- `/automations` 图形化配置页

这些统一在并列目录 `../console/`。

## 关键目录

- `agent/`
  Agent 核心编排、调度、工具执行
- `feishu/`
  飞书消息入口与回复格式
- `tools/`
  业务工具实现
- `docs/`
  项目文档、模块说明、代码定位索引
- `price_scripts/`
  价格工具底层业务目录
- `finance_reconciliation/`
  财务 ETL 底层业务目录

## 常见修改入口

- 改 `/health`、`/chat`、`/run-tool`
  看 `main.py`、`agent/core.py`
- 改工具执行或超时并发
  看 `agent/tool_executor.py`
- 改工具注册与参数
  看 `tools/registry.yaml`、`agent/tool_registry.py`
- 改飞书机器人不回话
  看 `feishu/bot.py`、`feishu/message_handler.py`
- 改同步链路或 Phase 7
  看 `tools/*sync_tool.py`、`agent/scheduler.py`

## 本地运行

本目录本身没有独立 Web 控制台。

- Agent 服务入口：`main.py`
- 稳定后台启动脚本：`./start_agent.sh`
- 停止脚本：`./stop_agent.sh`
- 本地虚拟环境必须使用 WSL/Linux 结构：`.venv/bin/python`
- 不要在当前目录保留 Windows 虚拟环境结构（如 `.venv/Scripts`、`.venv/Lib`），否则会与 WSL 本地启动链路冲突
- 控制台本地启动：去 `../console/start_backend.sh`

如果需要单独拉起 Agent：

```bash
cd /home/deng/projects/agent
./start_agent.sh
```

停止：

```bash
cd /home/deng/projects/agent
./stop_agent.sh
```

## TMS 本地验证

- TMS 兼容业务接口统一挂在 `:9000/tms/*`
- TMS 登录态管理接口统一挂在 `:9000/admin/tms/session/*`
- 本地验证优先从并列目录启动控制台：

```bash
cd /home/deng/projects/console
./start_backend.sh
```

- 然后默认打开首页大盘 `http://127.0.0.1:8765/`；需要验证 TMS 登录态时再进入 `/automations`
- 页面顶部支持保存默认账号、密码、手机号；验证码发送固定使用这套已保存配置
- 凭据运行态保存在 `agent/tms_runtime/state/login_profile.json`，不进入版本控制
- 本地未验证通过前，不执行 ECS 发版或旧服务切换

## 飞书机器人接入

- `FEISHU_EVENT_MODE=websocket`：沿用现有飞书长连接模式。
- `FEISHU_EVENT_MODE=webhook`：启用飞书事件订阅模式，飞书请求地址指向 `POST /feishu/webhook/event`。
- Webhook 模式建议配置 `FEISHU_EVENT_VERIFICATION_TOKEN`；当前未实现 Encrypt Key 解密，飞书事件订阅请保持不加密。
- Webhook 模式下支持的文本触发：
  - `地址，重量，体积`
- 价格查询中的体积字段可省略，默认按 `0.1` 处理；也可填写厘米尺寸表达式，如 `30*23*103*1+97*23*31*4`，按 `长*宽*高*件数` 合计后转换为立方米并保留三位小数。申明价值由韵达页面按重量自动调整，飞书不传。

## 发布到 ECS

- 发布脚本：`deploy/publish_to_ecs.ps1`
- 发布说明：`deploy/publish_to_ecs.md`
- 默认推荐：直接跑 `auto`，脚本会自动判断只发 `agent`、只发 `console` 或全量发布

## 先读哪些文档

1. `AGENTS.md`
2. `docs/code_navigation_index.md`
3. 对应子目录下的 `AGENTS.md`

## 与服务器的对应关系

- 本地：`/home/deng/projects/agent`
- 服务器：`/home/boyce/agent`

目录结构应尽量保持一致，避免本地可改、服务器难同步。
