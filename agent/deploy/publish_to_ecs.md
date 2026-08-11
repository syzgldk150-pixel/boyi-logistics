# 发布到 ECS

标准发布入口：

```powershell
powershell -ExecutionPolicy Bypass -File "\\wsl.localhost\Ubuntu\home\deng\projects\boyi-logistics\agent\deploy\publish_to_ecs.ps1"
```

生产目标固定为：

- SSH：`boyce@123.57.106.70`
- Agent：`/home/boyce/agent`，`agent.service`
- Console：`/home/boyce/console`，`console.service`
- Shared：`/home/boyce/shared`

## 发布前提

脚本采用失败关闭策略，以下任一条件不满足都会停止：

1. Git 工作区必须干净，当前分支必须配置 upstream。
2. 脚本会先 `git fetch`，本地 `HEAD` 必须与远程 upstream 完全一致。
3. 本地 `127.0.0.1:9000` 不得有 Agent 监听，避免与 ECS 同时消费飞书任务。
4. Windows `known_hosts` 必须已有经过人工核验的 ECS 主机密钥。
5. SSH 只允许固定私钥、公钥认证、`BatchMode=yes`、`IdentitiesOnly=yes` 和 `StrictHostKeyChecking=yes`；不允许 root、密码回退或跳过主机校验。
6. 远端执行用户必须是 `boyce`，systemd `WorkingDirectory` 必须与上述固定目录一致。

## 源码白名单

发布包只从 `git ls-files` 取得已提交文件，再按 Agent、Console、Shared 明确白名单构建。未跟踪文件即使位于项目目录中也不会上传。

以下内容始终排除：

- `.env`、凭据、Cookie、Token、登录态文件；
- 虚拟环境、日志、缓存、运行态和临时文件；
- 财务 `metadata`、业务表格/PDF、数据库文件、OCR 原图和输出报表；
- 压缩包和其他生成物。

Console `static/` 下已纳入 Git 的面单 PNG 属于明确静态资产例外。

## 事务式发布流程

固定顺序如下：

1. 检查 Git 工作区和远程提交。
2. 检查本地 Agent 已停止。
3. 校验 SSH 主机密钥、远端用户和 systemd 工作目录。
4. 在项目内 `.task_tmp/` 构建白名单暂存包，上传到 `/home/boyce/.boyi-deploy/release-*`。
5. 在本次 `/home/boyce/.boyi-deploy/release-*/_rollback/` 内临时备份当前受管源码和发布清单，只供本次失败回滚使用。
6. 对远端暂存包执行 `compileall`；存在 SQL 迁移时必须找到受支持的 `--check` 迁移预检入口。
7. 分别计算 Agent、Console `requirements.lock` 的 SHA-256，再生成联合哈希。两个服务共用唯一的 `runtime-deps-<联合哈希>` 环境；若当前共享环境的哈希一致且分别通过两份锁文件校验，直接复用。只有任一锁变化或校验失败时，才创建新的共享环境并一次性安装两份锁文件的并集。
8. 按 `.deploy-source-manifest` 同步源码，只删除上一版清单中存在而本版已移除的文件；不递归删除未受管业务数据。
9. 将当前同名 systemd unit 写入当次临时回滚目录，再安装新 unit、按需原子切换虚拟环境、执行 `daemon-reload`，写入 `runtime/release_sha` 后重启原服务。
10. Agent `/health` 必须返回本次 Git SHA；Console 必须可访问；成功后把锁文件哈希写入当前环境供后续发布判定。
11. 任一步失败，删除本次新建环境与新增受管文件、恢复旧虚拟环境、源码备份和发布清单，并重启旧版本。
12. 健康检查成功后，删除本次临时回滚目录、历史 `/home/boyce/.boyi-backups/` 与 `/home/boyce/agent_backups/`，清理 `/home/boyce/.boyi-venvs/` 中所有非当前环境，并删除仅用于安装依赖的 `/home/boyce/.cache/pip/`；ECS 最终只保留一个 Agent/Console 共用运行环境，不持久保留发布备份、旧虚拟环境或安装缓存。

业务代码频繁提交但锁文件未变时，发布仍会同步受管源码并重启受影响服务，但不会重新创建虚拟环境，也不会重复下载 OCR、OpenCV、Playwright、pandas 等依赖。锁文件变化时才承担完整依赖安装成本。

`/health` 是公开的精简存活接口，只返回状态和 `release_sha`。详细组件状态位于带 `X-Agent-Internal-Token` 的 `/internal/v1/health`。

## 发布范围

默认 `-Target auto` 根据本地发布状态哈希判断范围。Shared 变化会同时影响 Agent 与 Console 的范围指纹。

```powershell
# 全部发布
powershell -ExecutionPolicy Bypass -File "\\wsl.localhost\Ubuntu\home\deng\projects\boyi-logistics\agent\deploy\publish_to_ecs.ps1" -Target all

# 只发布并重启 Agent
powershell -ExecutionPolicy Bypass -File "\\wsl.localhost\Ubuntu\home\deng\projects\boyi-logistics\agent\deploy\publish_to_ecs.ps1" -Target agent

# 只发布并重启 Console
powershell -ExecutionPolicy Bypass -File "\\wsl.localhost\Ubuntu\home\deng\projects\boyi-logistics\agent\deploy\publish_to_ecs.ps1" -Target console
```

`-SkipRestart` 和 `-SkipHealthCheck` 仅用于用户明确授权的维护场景。常规生产发布不得跳过重启或健康检查。

本地范围状态保存在忽略目录 `agent/deploy/state/publish_state.json`。本地和远端临时暂存目录在流程结束后自动清理；失败回滚材料仅存在于当次远端暂存目录中，不作为长期备份保留。

## Nginx 边界

- 正式入口：`https://boyi.homes`
- Console 和 Agent 均只监听回环地址，公网不得直接开放 `8765` 或 `9000`。
- Nginx 配置位于 `deploy/nginx/`；发布脚本只同步源码，不自动修改 `/etc/nginx`、证书或安全组。
- 系统配置切换必须单独备份并通过 `nginx -t` 后执行。
