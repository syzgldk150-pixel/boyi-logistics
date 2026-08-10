# console

本目录是控制台工作区，对齐服务器上的 `/home/boyce/console`，与 `../agent/` 并列。

## 这里负责什么

- 控制台首页和模块页
- OCR 工作区
- 自动化图形化配置页
- 货拉拉调度工作区
- 控制台对 MySQL 的读写

## 不在这里改什么

- Agent API
- 工具执行与调度编排
- 飞书消息长连接
- Phase 7 同步工具本体

这些统一在 `../agent/`。

## 关键文件

- `app.py`
  控制台路由、登录会话、页面数据组装、保存入口
- `config.py`
  控制台路径、MySQL、OCR、模板、运行时配置
- `database.py`
  控制台与 MySQL 的数据存取
- `ocr_providers.py`
  OCR 提供方封装
- `task_queue.py`
  后台 OCR 队列
- `template_store.py`
  OCR 模板管理
- `templates/`
  页面模板，含 `/login` 和 `/settings/accounts`
- `static/style.css`
  控制台样式

## 常见修改入口

- 改首页、导航、模块卡片
  看 `app.py`、`templates/base.html`、`templates/portal.html`
- 改 `/automations`
  看 `templates/automation.html`、`app.py`、`database.py`
- 改 OCR 上传、复核、模板
  看 `templates/document.html`、`ocr_providers.py`、`task_queue.py`
- 改调度页
  看 `templates/dispatch.html`、`static/style.css`

## 本地运行

WSL / Linux：

```bash
cd /home/deng/projects/boyi-logistics/console
./start_backend.sh
```

这条命令现在默认会做两件事：

- 在 WSL 内用 `tmux` 稳定拉起并列目录里的 `agent`
- 在 WSL 内用 `tmux` 稳定拉起当前 `console`

启动前脚本会自动探测常见的本地 MySQL 隧道地址：

- `WSL 网关 IP:23306`
- `WSL 网关 IP:13306`
- `127.0.0.1:23306`
- `127.0.0.1:13306`

也就是说，页面和页面依赖的本地 Agent 会一起起来，不再依赖 Windows `Start-Process -> WSL` 这条容易失稳的后台链路。

如果只想以前台方式跑当前控制台：

```bash
cd /home/deng/projects/boyi-logistics/console
./start_backend.sh --foreground
```

如果只想启动控制台，不自动拉起 Agent：

```bash
cd /home/deng/projects/boyi-logistics/console
./start_backend.sh --no-agent
```

停止：

```bash
cd /home/deng/projects/boyi-logistics/console
./stop_backend.sh
```

Windows PowerShell：

```powershell
wsl bash -lc 'cd /home/deng/projects/boyi-logistics/console && ./start_backend.sh'
```

稳定启动默认依赖 `tmux`。当前脚本会自动复用固定会话名：

- `codex-agent`
- `codex-console`

## 页面入口

- 默认首页大盘：`http://127.0.0.1:8765/`
- `http://127.0.0.1:8765/login`
- `http://127.0.0.1:8765/settings/accounts`
- `http://127.0.0.1:8765/ocr`
- `http://127.0.0.1:8765/dispatch`
- `http://127.0.0.1:8765/automations`

## 后台账号管理

- 控制台默认使用 `/login` 登录页，账号和会话存储在 MySQL 的 `admin_users`、`admin_sessions` 表。
- 首个管理员由 `DOCFLOW_ADMIN_USERNAME`、`DOCFLOW_ADMIN_PASSWORD` 引导创建；不要把真实账号密码写进代码或文档。
- `DOCFLOW_SESSION_SECRET` 用于签名会话 Cookie，绑定域名或长期运行时必须配置固定随机值。
- 账号管理入口为 `/settings/accounts`，支持新增管理员、启用/停用账号、重置密码。
- 现有 `DOCFLOW_BASIC_AUTH_USER` / `DOCFLOW_BASIC_AUTH_PASS` 可作为应急兼容入口。

## 生产域名

- 正式入口为 `https://boyi.homes`；HTTP 和 `www.boyi.homes` 统一跳转到该地址。
- Nginx 反向代理到 `127.0.0.1:8765`，配置来源为 `../agent/deploy/nginx/`。
- 生产环境设置 `DOCFLOW_COOKIE_SECURE=1`，让后台 Cookie 只在 HTTPS 下发送。
- Python 控制台服务只监听 `127.0.0.1`，阿里云安全组不得直接开放 `8765`。

## TMS 本地验证

- `/automations` 顶部第一块就是 TMS 登录态中心
- 先保存默认账号、密码，再点击登录获取图片验证码；旧短信验证码页仍可使用手机号
- 页面刷新后会默认回填已保存的账号和手机号，密码不通过 GET 响应回显
- 状态轮询只刷新登录状态，不会重复回拉密码
- 本地验证通过前，不执行 ECS 发版和 cutover

配套 Agent 健康检查：

- `http://127.0.0.1:9000/health`

## 运行目录

- `runtime/originals/`
  原图归档
- `runtime/artifacts/`
  处理产物
- `runtime/state/`
  控制台状态文件
- `temp/`
  临时文件

## 数据库说明

- 当前唯一后端：`MySQL`
- `console/.env` 优先，其次读取并列的 `../agent/.env`
- 不再保留 SQLite 作为运行时主后端

## 与服务器的对应关系

- 本地：`/home/deng/projects/boyi-logistics/console`
- 服务器：`/home/boyce/console`

目录结构尽量保持一致，避免本地改完后服务器路径错位。

## 发布入口

- 统一发布脚本在并列目录：`../agent/deploy/publish_to_ecs.ps1`
- 发布说明：`../agent/deploy/publish_to_ecs.md`
- 默认推荐：直接跑 `auto`，脚本会自动判断是否只发 `console`
