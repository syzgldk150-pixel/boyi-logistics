# 发布到 ECS

本目录提供本地开发仓发布到 ECS 的标准化入口。

## 适用范围

- 本地 Agent 根目录：`/home/deng/projects/agent`
- 本地控制台目录：`/home/deng/projects/console`
- 本地共享模块目录：`/home/deng/projects/shared`
- 远端 Agent 目录：`/home/boyce/agent`
- 远端控制台目录：`/home/boyce/console`
- 远端共享模块目录：`/home/boyce/shared`

## 发布脚本

- PowerShell：`deploy/publish_to_ecs.ps1`

## 同步范围

### Agent

- 根文件：
  - `AGENTS.md`
  - `CLAUDE.md`
  - `README.md`
  - `main.py`
  - `requirements.txt`
  - `agent.service`
  - `project_overview.md`
  - `deploy/publish_to_ecs.ps1`
  - `deploy/publish_to_ecs.md`
- 目录：
  - `deploy/nginx/`
  - `agent/`
  - `docs/`
  - `feishu/`
  - `knowledge/`
  - `prompts/`
  - `tms_docs/`
  - `tools/`
  - `price_scripts/`
  - `finance_reconciliation/`
  - `../shared/` → `/home/boyce/shared/`

### Console

- 根文件：
  - `AGENTS.md`
  - `CLAUDE.md`
  - `README.md`
  - `app.py`
  - `check_syntax.py`
  - `config.py`
  - `console.service`
  - `database.py`
  - `finance_service.py`
  - `line_haul_contacts.py`
  - `ocr_providers.py`
  - `preprocessing.py`
  - `requirements.txt`
  - `start_backend.sh`
  - `stop_backend.sh`
  - `task_queue.py`
  - `template_store.py`
  - `known_issues.md`
- 目录：
  - `config/`
  - `static/`
  - `templates/`
  - `../shared/` → `/home/boyce/shared/`

## 不同步的内容

- `.env`
- `config.json`
- `credentials*`
- `secrets*`
- `.venv`
- `runtime/`
- `logs/`
- `tmp/`
- `temp/`
- `__pycache__/`
- `deploy/state/`

这些都属于环境态或运行态内容，不纳入发布覆盖。

## TMS 运行边界

- 常规发布脚本只覆盖 `/home/boyce/agent` 与 `/home/boyce/console`
- TMS Runtime 已并入 `/home/boyce/agent/agent/tms_runtime`
- `console /automations` 页面顶部提供 TMS 登录态中心，实际代理到 Agent `/admin/tms/session/*`
- 历史 `/root/http_service` 与 N8N 不再是运行时来源，也不属于本脚本默认动作范围

## 一次性切换脚本

- 单次切换入口：`deploy/cutover_legacy_tms.ps1`
- 固定流程：
  - 先执行 `publish_to_ecs.ps1 -Target all`
  - 再做 `:9000/health`、`:8765/`、`/admin/tms/session/status` 健康检查
  - 在 `/automations` 顶部模块完成短信验证码登录冒烟
  - 再跑价格 / POST / 浏览器三类 TMS 冒烟
  - 全部通过后才停用并删除旧 `/root/http_service` 与 N8N 资产
- 危险删除动作不会并入 `publish_to_ecs.ps1` 默认发布流程

## auto 模式

脚本默认 `-Target auto`，会比较本地同步范围的哈希，只发布真正变化的部分：

- 只改 `agent` 侧代码：只发 `agent`
- 只改 `console` 页面：只发 `console`
- 两边都改：全发
- 没有变更：直接退出，不重启服务

状态缓存保存在本地：

- `deploy/state/publish_state.json`

这个文件只用于本地判断发布范围，不会上服务器。

## 使用前提

1. 本机已经配置好 SSH 公钥，可直接登录 ECS。
2. ECS 上已存在：
   - `/home/boyce/agent`
   - `/home/boyce/console`
3. ECS 上 `.env`、虚拟环境、systemd 服务都已经配置完成。

## 生产域名与 HTTPS

- 正式入口：`https://boyi.homes`
- `http://boyi.homes`、`http://www.boyi.homes`、`https://www.boyi.homes` 均跳转到根域名 HTTPS。
- 最终 Nginx 配置：`deploy/nginx/boyi.homes.conf`
- 首次签发证书前的 HTTP/ACME 配置：`deploy/nginx/boyi.homes.bootstrap.conf`
- Certbot 续期成功后 reload 钩子：`deploy/nginx/reload-nginx.sh`
- Console 只监听 `127.0.0.1:8765`；外网只开放 `80/443`，不得直接开放 `8765`。
- `publish_to_ecs.ps1` 会同步 `deploy/nginx/`，但不会自动覆盖 `/etc/nginx`、签发证书或修改安全组；这类系统变更必须先备份、执行 `nginx -t` 和健康检查后再切换。

## 常用命令

### 自动判断发布范围

```powershell
powershell -ExecutionPolicy Bypass -File "\\wsl.localhost\Ubuntu\home\deng\projects\agent\deploy\publish_to_ecs.ps1"
```

### 强制全量发布

```powershell
powershell -ExecutionPolicy Bypass -File "\\wsl.localhost\Ubuntu\home\deng\projects\agent\deploy\publish_to_ecs.ps1" -Target all
```

### 只发 Agent

```powershell
powershell -ExecutionPolicy Bypass -File "\\wsl.localhost\Ubuntu\home\deng\projects\agent\deploy\publish_to_ecs.ps1" -Target agent
```

### 只发 Console

```powershell
powershell -ExecutionPolicy Bypass -File "\\wsl.localhost\Ubuntu\home\deng\projects\agent\deploy\publish_to_ecs.ps1" -Target console
```

### 只同步，不重启

```powershell
powershell -ExecutionPolicy Bypass -File "\\wsl.localhost\Ubuntu\home\deng\projects\agent\deploy\publish_to_ecs.ps1" -SkipRestart
```

### 跳过健康检查

```powershell
powershell -ExecutionPolicy Bypass -File "\\wsl.localhost\Ubuntu\home\deng\projects\agent\deploy\publish_to_ecs.ps1" -SkipHealthCheck
```

### 跳过飞书长连等待

```powershell
powershell -ExecutionPolicy Bypass -File "\\wsl.localhost\Ubuntu\home\deng\projects\agent\deploy\publish_to_ecs.ps1" -SkipFeishuCheck
```

## 默认动作

脚本默认会：

1. 判断应该发布 `agent`、`console` 还是两者都发
2. 用 `scp` 上传代码和文档
3. 重启对应的 systemd 服务
4. 检查：
   - `http://127.0.0.1:9000/health`
   - `http://127.0.0.1:8765/`
5. 如果本次发布包含 `agent`，默认额外等待：
   - `/health` 中 `components.feishu_ws = connected`

这样可以避免刚重启完时飞书 WebSocket 还没连上，脚本过早把状态看成最终结果。

## 建议使用方式

- 改页面：默认直接跑 `auto` 就够了
- 改 Agent 能力、工具、飞书：默认直接跑 `auto` 就够了
- 明确知道要全量刷新：手工指定 `-Target all`

## 注意

- 如果修改涉及数据库结构、系统包、Python 依赖升级，不要只依赖这个脚本，还要额外做环境变更。
- 如果只是本地试 UI，没有必要每次都发版。
