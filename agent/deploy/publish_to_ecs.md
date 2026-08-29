# 发布到 ECS

Agent 或 shared/migration 发布必须显式传入与待发布 Git SHA 完全一致的签名首方插件目录，
以及只含受信 Ed25519 `.pub` 公钥的信任根。Console-only 发布不处理插件输入。需要协调两个
服务的 shared/migration 发布使用 `-Target shared`（`-Target all` 是它的兼容别名）：

```powershell
powershell -ExecutionPolicy Bypass `
  -File "\\wsl.localhost\Ubuntu\home\deng\projects\boyi-logistics\agent\deploy\publish_to_ecs.ps1" `
  -Target shared `
  -AutomationPluginArtifactRoot "<signed-artifact-directory>" `
  -AutomationPluginTrustRoot "<public-trust-root-directory>"
```

签名包必须在提交、推送及 CI 通过后，使用最终的 40 位提交 SHA 构建；私钥路径和 key ID
只传给只读源树的本地构建器，不写入仓库、发布目录或命令输出：

```bash
PYTHONPATH=agent:. python agent/scripts/build_first_party_plugin_release.py \
  --private-key "<protected-private-key-path>" \
  --key-id "<key-id>" \
  --release-sha "$(git rev-parse HEAD)" \
  --output-root "<temporary-artifact-directory>"
```

如果本次提交没有修改任何受签名动作包 payload，优先复用上一版已经验签的不可变 ZIP；构建器会
使用公共信任根重新验签上一版 release index 和每个包，并逐文件比较 ZIP 内容与当前受审源码，
只有完全一致时才为新的 Git SHA 生成 release index。此路径不会读取私钥，也不会重新签名：

```bash
PYTHONPATH=agent:. python agent/scripts/build_first_party_plugin_release.py \
  --reuse-artifact-root "<previous-signed-artifact-directory>" \
  --trust-root "<public-trust-root-directory>" \
  --release-sha "$(git rev-parse HEAD)" \
  --output-root "<temporary-artifact-directory>"
```

签名模式与复用模式必须二选一。只要 payload 有任何字节变化，复用就失败关闭；此时必须提升插件
版本并走受保护私钥的签名模式，禁止把不同源码重新绑定到旧签名 ZIP。

构建目录应在发布与验收完成后精确清理；公共信任根可以长期保留。发布器会再次检查
release index、包集合、签名、digest lock 与 Git SHA，任何漂移都在远端 mutation 前失败关闭。

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

首方动作源码还要经过第二层精确过滤：`scripts/first_party_release_scope.py` 用 AST 读取代码 allowlist，
只允许共享 `_runtime` 与当前 `RUNNABLE` 包进入暂存树；不会导入或解析 `BLOCKED` payload。PowerShell
构包结束后与远端 `compileall` 之前都会重验 staged 包集合与 allowlist 完全相等，缺包或夹带包均
失败关闭。`BLOCKED` 源码只由 CI 的独立非阻断审计读取，不能影响不包含它的生产包。

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
4. 在项目内 `.task_tmp/` 构建白名单暂存包，上传到 `/home/boyce/.boyi-deploy/release-*`；Agent 与 Console 独立发布时各自使用唯一 stage，避免前一服务保留的回滚包阻断后一服务。
5. 在本次 `/home/boyce/.boyi-deploy/release-*/_rollback/` 内建立当前受管源码、发布清单、unit 与旧虚拟环境引用的精确回滚包。
6. 先确认首方 staged 源码只含代码 allowlist 包，再对远端暂存包执行 `compileall`；存在 SQL 迁移时必须找到受支持的 `--check` 迁移预检入口，并在任何 DDL 前验证官方 MySQL 8.x。
7. 只有 shared、数据库 migration、任一依赖清单或迁移运行器发生变化时，才分别计算 Agent、Console `requirements.lock` 的 SHA-256 并生成联合哈希。两个服务共用唯一的 `runtime-deps-<联合哈希>` 环境；哈希和两份锁校验均一致时直接复用，否则创建新共享环境并一次性安装两份锁文件的并集。Agent-only 与 Console-only 不构建、不切换共享虚拟环境。
8. Console-only 只停止 Console；Agent-only 只停止 Agent；shared/migration 才同时停止两个服务。每条路径都先确认自己负责的 unit 已退出，未选中的服务不停止、不重启。
9. 按 `.deploy-source-manifest` 同步源码，只删除上一版清单中存在而本版已移除的文件；不递归删除未受管业务数据。
10. shared/migration 路径先执行版本化迁移，再安装两个 unit 并按需原子切换共享虚拟环境；Agent-only/Console-only 只安装自己的 unit，且不执行迁移。包含 Agent 的路径写入 `runtime/release_sha`、安装已验签插件并创建仅属于本次 SHA 的 release hold；Console-only 不创建或消费 Agent hold。
11. 每条路径只健康检查本次重启的服务：Agent `/health` 必须返回本次 Git SHA，Console 首页必须可访问。shared/migration 额外执行 Agent/Console 签名身份联通和依赖哈希检查；包含 Agent 的路径继续执行控制平面 manifest 门禁，并通过签名激活端点释放 Scheduler/WorkflowRunner hold。
12. 提交点之前失败时，只停止和恢复本次路径负责的服务、源码、unit 与发布清单；shared/migration 才恢复两套运行时和共享虚拟环境。包含 Agent 的路径仍保留受保护写检查、精确插件版本合同、release hold 和两阶段稳定健康回滚；shared/migration 额外保留 migration checksum 与数据库恢复门禁。Console-only 不触碰 Agent、插件或数据库。所有路径的回滚材料仍位于本次 stage，删除安全边界和失败保留语义不变。
13. 健康检查成功后仍保留本次远端暂存树、精确回滚包和上一版虚拟环境，直到事项中心、定时自动化、财务、每日应签与客服影子投影完成业务验收。清理必须是验收后的独立、有界管理动作，不得由发布成功路径自动执行；数据库快照同样保留到验收结束。

业务代码频繁提交时，发布只同步受管源码并重启受影响服务，不会重新创建虚拟环境，也不会重复下载 OCR、OpenCV、Playwright、pandas 等依赖。锁文件变化归入 shared 路径，才承担完整依赖安装成本并协调两个服务。

`/health` 是公开的精简存活接口，只返回状态和 `release_sha`。详细组件状态位于带 `X-Agent-Internal-Token` 的 `/internal/v1/health`。

## 发布范围

默认 `-Target auto` 使用三个独立指纹：Agent、Console、shared/migration。shared 指纹包含
`shared/`、数据库迁移、迁移运行器以及两边依赖清单；只要它变化，`auto` 就优先选择 shared
协调路径。显式 `-Target agent` 或 `-Target console` 若检测到未发布的 shared/migration 变化会
失败关闭，必须改用 `-Target shared`，不能用窄目标绕过迁移或共享依赖发布。

```powershell
# Shared / migration 协调发布（all 为兼容别名）
powershell -ExecutionPolicy Bypass -File "\\wsl.localhost\Ubuntu\home\deng\projects\boyi-logistics\agent\deploy\publish_to_ecs.ps1" -Target shared

# 只发布并重启 Agent
powershell -ExecutionPolicy Bypass -File "\\wsl.localhost\Ubuntu\home\deng\projects\boyi-logistics\agent\deploy\publish_to_ecs.ps1" -Target agent

# 只发布并重启 Console
powershell -ExecutionPolicy Bypass -File "\\wsl.localhost\Ubuntu\home\deng\projects\boyi-logistics\agent\deploy\publish_to_ecs.ps1" -Target console
```

`-SkipRestart` 和 `-SkipHealthCheck` 仅用于用户明确授权的维护场景。常规生产发布不得跳过重启或健康检查。

本地范围状态保存在忽略目录 `agent/deploy/state/publish_state.json`。本地上传临时目录在完成后清理；远端当次暂存目录及其 `_rollback` 精确恢复材料在成功发布后保留到业务验收结束。删除 stage 之前发生的回滚失败必须保留该目录并输出 `rollback_incomplete ... recovery_material_preserved=1`；若最终 stage 删除已经开始后失败，则输出 `rollback_cleanup_incomplete ... recovery_material_state=unknown verify_required=1`，不得未经核验声称唯一恢复材料仍完整。

## Nginx 边界

- 正式入口：`https://boyi.homes`
- Console 和 Agent 均只监听回环地址，公网不得直接开放 `8765` 或 `9000`。
- Nginx 配置位于 `deploy/nginx/`；发布脚本只同步源码，不自动修改 `/etc/nginx`、证书或安全组。
- 当前发行代码常量关闭整个 Windows Worker/Tray 运行面。Agent 不装载 Worker 签名密钥或 transport、
  不挂载 `/internal/v1/automation/worker/` 路由；远端发布也不要求 Worker snippet、客户端 CA、服务端
  身份或 dispatcher readiness，缺少这些未来组件不会阻断其余 Linux/ECS 插件。
- `deploy/nginx/boyi-worker-mtls.conf` 和 `deploy/nginx/README.md` 仅保留为未来重新启用时的受审合同。
  届时必须由管理员原样安装 snippet，并在启用的 `boyi.homes` HTTPS server 内精确 include 一次；
  Worker 客户端 CA 固定为 `/etc/nginx/mtls/boyi-worker-client-ca.pem`，仓库与发布器都不生成、复制、
  读取或打印 CA/私钥内容。
- 未来恢复的 Worker snippet 只能接管 `/internal/v1/automation/worker/`，必须由 Nginx 验证 mTLS 并
  覆盖 TLS 身份头；普通 `/internal/v1/*` 仍走内部 Token/签名 Console 边界。恢复提交还必须同时
  重新启用 mutation 前的 staged/installed 哈希、站点引用、路径权限、Nginx active 和 `nginx -t`
  预检，不允许只用环境变量打开运行面。
- 系统配置切换必须单独备份并通过 `nginx -t` 后执行。
