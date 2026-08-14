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
7. 数据库必须是官方 MySQL 8.x（不接受 MySQL 5.7、MariaDB 或未知版本）；迁移预检会在读取迁移历史或执行任何 DDL 前查询并校验服务端版本。
8. Agent 与 Console 的运行环境必须注入同一份非空 `CONSOLE_AGENT_SIGNING_SECRET`，且与 `AGENT_INTERNAL_API_TOKEN` 分离。发布脚本不会生成、读取或打印这两个值；缺少签名密钥时管理员命令、事项审批和账号管理会显式返回 503/403。

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
5. 在本次 `/home/boyce/.boyi-deploy/release-*/_rollback/` 内建立当前受管源码、发布清单、unit 与旧虚拟环境引用的精确回滚包。
6. 对远端暂存包执行 `compileall`；存在 SQL 迁移时必须找到受支持的 `--check` 迁移预检入口，并在任何 DDL 前验证官方 MySQL 8.x。
7. 分别计算 Agent、Console `requirements.lock` 的 SHA-256，再生成联合哈希。两个服务共用唯一的 `runtime-deps-<联合哈希>` 环境；若当前共享环境的哈希一致且分别通过两份锁文件校验，直接复用。只有任一锁变化或校验失败时，才创建新的共享环境并一次性安装两份锁文件的并集。
8. 在首次源码、虚拟环境或数据库变更前同时停止 Agent 与 Console，并确认两个 unit 均已退出；控制平面发布必须使用 `-Target all`，禁止在运行中的调度器/Worker 上同步混合版本源码。
9. 按 `.deploy-source-manifest` 同步源码，只删除上一版清单中存在而本版已移除的文件；不递归删除未受管业务数据。
10. 先执行全部版本化迁移，再安装新 systemd unit、按需原子切换虚拟环境并执行 `daemon-reload`。写入 `runtime/release_sha` 后，发布器创建仅属于本次 SHA 的固定 release hold，再按 Agent、Console 顺序启动；Agent 只注册任务，Scheduler 保持 paused，WorkflowRunner 保持 held 且不领取既存或新 Run，任何自动任务都不得在发布门禁完成前执行。发现遗留 marker 时新发布必须失败关闭，不得覆盖。
11. Agent `/health` 必须返回本次 Git SHA，Console 必须可访问；签名内部健康探针还必须确认 Scheduler paused、WorkflowRunner held 且 active Run 为零。随后完成 Agent/Console 签名身份联通、首次或后续控制平面 manifest、依赖哈希与数据库状态检查，最后才由签名 Console 管理员请求调用激活端点。端点先恢复并确认 WorkflowRunner 与 Scheduler 均可运行，再删除匹配本次 SHA 的 marker，并把财务启动补偿推迟 15 秒；marker 删除是发布提交点。
12. 激活提交点之前任一步失败，发布器保持 Scheduler 与 WorkflowRunner hold，按 bootstrap、017、016、014 的本次变更范围逆序恢复，再恢复旧虚拟环境、源码、unit 与发布清单并重启旧版本。激活期间异常或进程退出必须保留 marker，使下一次启动继续 hold；响应丢失可用新签名 nonce 幂等重试。激活请求一旦发出便不得自动回滚，因为任务可能已经开始；此时必须保留远端暂存树并报告 `release_activation_incomplete`，由人工核验 Scheduler、WorkflowRunner 和业务状态。数据库 DDL 不随普通源码回滚，发布前必须另行完成可恢复数据库快照并保留到业务验收结束。
13. 健康检查成功后仍保留本次远端暂存树、精确回滚包和上一版虚拟环境，直到事项中心、定时自动化、财务、每日应签与客服影子投影完成业务验收。清理必须是验收后的独立、有界管理动作，不得由发布成功路径自动执行；数据库快照同样保留到验收结束。

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

本地范围状态保存在忽略目录 `agent/deploy/state/publish_state.json`。本地上传临时目录在完成后清理；远端当次暂存目录及其 `_rollback` 精确恢复材料在成功发布后保留到业务验收结束。若回滚不完整，发布器也必须保留该目录并输出 `rollback_incomplete`，不得销毁唯一恢复材料。

## Nginx 边界

- 正式入口：`https://boyi.homes`
- Console 和 Agent 均只监听回环地址，公网不得直接开放 `8765` 或 `9000`。
- Nginx 配置位于 `deploy/nginx/`；发布脚本只同步源码，不自动修改 `/etc/nginx`、证书或安全组。
- 系统配置切换必须单独备份并通过 `nginx -t` 后执行。
