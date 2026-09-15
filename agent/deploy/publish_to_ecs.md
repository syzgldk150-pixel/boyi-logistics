---
module: deployment
type: operations
status: active
updated: 2026-09-15
---

# 发布到 ECS

## 当前发行方式

当前核心发布只接受 **Service-V2-only 退役索引**，并在服务器核验原业务实例的权威 `COMPLETED` 迁移归属。索引绑定最终 Git 提交，不含 V1 ZIP；不再签名、复用或恢复 V1 动作包。

V2 业务插件通过后台独立 ZIP 安装或升级。仅改文档或 Host 源码时不重建业务插件；修改插件算法时按[插件维护入口](../service_v2_plugins/README.md)测试、打包并升级对应实例。核心部署不等于已安装插件升级。

提交、推送及必要检查通过后，在仓库根用 Python 3.10 构建最终提交对应的索引。输出目录必须不存在，父目录放在本次任务的 `.task_tmp/` 下：

```bash
PYTHONPATH=agent:. PYTHON_DOTENV_DISABLED=1 python agent/scripts/build_first_party_plugin_release.py \
  --service-v2-only \
  --release-sha "$(git rev-parse HEAD)" \
  --output-root ".task_tmp/<task>/retired-release"
```

该模式拒绝 `--private-key`、`--key-id`、`--reuse-artifact-root`、`--trust-root` 与 `--digest-lock`。不用读取或复制私钥。

Windows 标准发布入口：

```powershell
powershell -ExecutionPolicy Bypass `
  -File "\\wsl.localhost\Ubuntu\home\deng\projects\boyi-logistics\agent\deploy\publish_to_ecs.ps1" `
  -Target auto `
  -AutomationPluginArtifactRoot "<本次 retired-release 目录的 Windows 路径>" `
  -AutomationPluginTrustRoot "<既有公钥 trust 目录的 Windows 路径>"
```

发布脚本目前仍要求包含 Agent 的路径传入非空、只含 Ed25519 `.pub` 的公钥目录；这是发布接口保留的校验输入，不表示仍发布 V1 签名包。Console-only 不处理插件输入。服务器通过 `verify_first_party_plugins.py --service-v2-only --require-completed-migrations` 核验索引和数据库归属，条件不满足即停止，不能改回旧签名模式绕过。

发布与验证结束后只清理本次本地工件目录；远端回滚材料按下文保留。

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

Agent 入口依赖的顶层组合模块 `business_composition.py` 与 `harness_composition.py` 必须随包发布；发布边界测试会沿 `main.py` 的本地顶层导入检查白名单，防止源码测试通过但安装后缺少启动模块。

当前 V2 源码还要经过精确过滤：`scripts/first_party_release_scope.py` 从代码 allowlist 读取允许的包，
只发布 `service_v2_plugins/` 下的当前包、`_shared/` 及明确允许的根文件。构包后与远端编译前
再次核验集合；缺包、多包或夹带 `legacy/`、旧 `first_party_automation_plugins/` 均失败。
旧迁移样例与摘要只供离线回归，不进入线上导入路径。

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
10. shared/migration 路径先执行版本化迁移，再安装两个 unit 并按需原子切换共享虚拟环境；Agent-only/Console-only 只安装自己的 unit，且不执行迁移。包含 Agent 的路径写入 `runtime/release_sha`、验证 V2 退役索引与完成归属并创建仅属于本次 SHA 的 release hold；Console-only 不创建或消费 Agent hold。
11. 每条路径只健康检查本次重启的服务：Agent `/health` 必须返回本次 Git SHA，Console 首页必须可访问。shared/migration 额外执行 Agent/Console 签名身份联通和依赖哈希检查；包含 Agent 的路径继续执行控制平面 manifest 门禁，并通过签名激活端点恢复 Direct 新调用及 Scheduler；旧 WorkflowRunner 保持 reserved。
12. 提交点之前失败时，只停止和恢复本次路径负责的服务、源码、unit 与发布清单；shared/migration 才恢复两套运行时和共享虚拟环境。包含 Agent 的路径仍保留受保护写检查、精确插件版本合同、release hold 和两阶段稳定健康回滚；shared/migration 额外保留 migration checksum 与数据库恢复门禁。Console-only 不触碰 Agent、插件或数据库。所有路径的回滚材料仍位于本次 stage，删除安全边界和失败保留语义不变。
13. 健康检查成功后仍保留本次远端暂存树、精确回滚包和上一版虚拟环境，直到事项中心、定时自动化、财务、每日应签与客服影子投影完成业务验收。清理必须是验收后的独立、有界管理动作，不得由发布成功路径自动执行；数据库快照同样保留到验收结束。

业务代码频繁提交时，发布只同步受管源码并重启受影响服务，不会重新创建虚拟环境，也不会重复下载 OCR、OpenCV、Playwright、pandas 等依赖。锁文件变化归入 shared 路径，才承担完整依赖安装成本并协调两个服务。

`/health` 是公开的精简存活接口，只返回状态和 `release_sha`。详细组件状态位于带 `X-Agent-Internal-Token` 的 `/internal/v1/health`。

## 发布范围

默认 `-Target auto` 使用三个独立指纹：Agent、Console、shared/migration。shared 指纹包含
`shared/`、数据库迁移、迁移运行器以及两边依赖清单；只要它变化，`auto` 就优先选择 shared
协调路径。显式 `-Target agent` 或 `-Target console` 若检测到未发布的 shared/migration 变化会
失败关闭，必须改用 `-Target shared`，不能用窄目标绕过迁移或共享依赖发布。

需要显式目标时，使用上面的完整命令并调整 `-Target`：

- `shared`：协调发布 Agent、Console 和共享变更；`all` 为兼容别名。
- `agent`：只发布 Agent，仍须传入退役索引和公钥目录。
- `console`：只发布 Console，无需插件工件和公钥参数。

通常使用 `auto`。共享变更存在时不能用较小目标绕过迁移或依赖更新。

`-SkipRestart` 和 `-SkipHealthCheck` 仅用于用户明确授权的维护场景。常规生产发布不得跳过重启或健康检查。

迁移后启动失败而旧运行器禁止恢复时，保持两个服务停止和原发布 hold；修复版本走同一 shared 发布流程，额外传入 `-RecoverReleaseHoldSha "<失败发布的40位SHA>"`。发布器持有远端发布锁，仅在原 hold 的身份精确匹配、Agent/Console 均为 inactive 且主进程与控制进程均已退出时原子接管 hold；全程不删除 hold 或启动旧运行器。仍执行全部备份、签名、写入静止、迁移和健康检查。再次失败继续保留 hold 与新旧恢复材料，不自动开放原先已暂停的服务。

先核实实际暂停标记，不能仅根据失败提交推断 hold 仍存在。若回滚日志为 `DATABASE_VERSION_NEWER` 且发布器已清除其自身 hold，两服务仍停止时应使用普通 shared 前向发布；此时传入 `-RecoverReleaseHoldSha` 会因缺少可接管标记而失败。

启动和健康检查读取插件历史时，代次及其 coeffect/effect 日志在同一事务批量读取；没有活动租约的代次不重复加载完整执行快照。所有历史代次、未知写和激活日志校验继续保留，发布启动健康检查时限不变。回归入口为 `tests/test_generation_listing_queries_mysql.py`。

插件进程注册恢复与代次健康检查分别使用单次只读事务，复用连接及一致快照，结束后立即释放；不会跨检查缓存业务状态。写操作继续使用独立提交事务，后续读取必须看到已提交的新配置。这样避免为每个历史代次重新连接 MySQL，同时保留全部租约、未知写和代次完整性检查。

已绑定 automation_id 的定时注册直接采用既有插件并发及错过执行策略，不再为每条定时重复查询插件能力目录；实际触发仍经原可信插件入口复核代次、配置和权限。普通工具的能力读取及财务特殊策略不变。

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
