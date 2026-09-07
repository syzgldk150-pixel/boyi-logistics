# V3.2 隔离验收环境

入口为 `bash tests/v32_acceptance/isolated_environment.sh setup|run|stop`。所有环境和 MySQL 数据仅在项目 `.task_tmp/v32/environment`；测试 MySQL 为独立进程、端口 `127.0.0.1:33326`、无密码的本地测试 root 身份，不读取系统 MySQL 配置，也不启动系统数据库服务。`run` 清空继承环境并禁用 dotenv；数据库名称必须以 `_test` 结尾。迁移显式使用 `/dev/null`。测试身份及签名私钥仅由各 fixture 在内存生成。

本次实测平台是 Ubuntu 24.04 / WSL，系统普通文件 `/usr/bin/python3.10`（官方 CPython 3.10.20）、MySQL 8.0.46、真实 bwrap/prlimit、锁定 Playwright Chromium。Python 3.12 不能代替正式验收。两套生产锁联合安装，pytest/ruff 版本与 CI 相同。

Node DOM 回归使用锁定 Playwright wheel 自带的 Linux Node（本次实际 `v24.13.0`）；两个隔离 runner 只把该任务虚拟环境的 `playwright/driver` 加入 PATH，不依赖系统 Node 或 Windows PATH，不删除缺失 Node 时被跳过的原断言。`setup` 会实际执行该 Node 的版本检查。

全新 Ubuntu 24.04 先安装编译/运行依赖（不得启动已有 MySQL）：`build-essential libssl-dev zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev libffi-dev liblzma-dev libncurses-dev uuid-dev tk-dev bubblewrap util-linux libaio1t64 libnuma1`。从 Python 官方发布目录下载 `Python-3.10.20.tar.xz`，在项目临时目录解压，执行 `./configure --prefix=/usr --with-ensurepip=install`、`make -j8`，检查 ssl/bz2/lzma/sqlite3/ctypes 后由管理员执行 `make altinstall`。保留系统默认 Python；沙箱解释器必须为非符号链接的 `/usr/bin/python3.10`。Chromium 所需系统库可按锁定 Playwright 的 `install-deps chromium` 清单安装。

AF_UNIX 路径长度要求短临时目录。以当前项目绝对路径为准创建一次绑定：

```sh
mkdir -p "$PWD/.task_tmp/tmp"
sudo mkdir -p /tmp/boyi-v32-tmp
sudo mount --bind "$PWD/.task_tmp/tmp" /tmp/boyi-v32-tmp
bash tests/v32_acceptance/isolated_environment.sh setup
bash tests/v32_acceptance/isolated_environment.sh run python --version
bash tests/v32_acceptance/isolated_environment.sh run python agent/scripts/verify_locked_environment.py agent/requirements.lock
```

`setup` 下载明确的 Ubuntu MySQL core 包并解包，绝不安装 mysql-server 服务；下载失败显式失败，不换版本。生产锁不改写。Chromium 下载到该临时环境目录；复用已安装的相同浏览器时，仅在命令前显式设置 `V32_CHROMIUM_EXECUTABLE=/absolute/path/to/chrome`。

数据库分工：CI 使用 `agent_control_plane_test`；浏览器使用 `v32_e2e_test`；简单设置实际生效使用 `v32_m01_test`；财务字段演练使用 `v32_m02_test`；自提决策演练使用 `v32_m03_test`；日常扫描/统计和问题件链路分别使用 `v32_a01_test` / `v32_a01_problem_test`。对应 fixture 的准备入口仅重建自己明确指定的合成测试库，不并行重建同一数据库。完整验收入口见 `agent/scripts/accept_low_maintenance_v32.py --help`；各脚本记录实际结果，缺失或失败不能作为通过。

真实四业务并发组合 `python -m tests.v32_acceptance.daily_concurrency` 使用 `v32_a02_test`；客服安装/查询/历史和页面故障组合使用 `v32_customer_flow_test`；调度超时及重启复测使用 `v32_scheduler_test`。四业务并发保持扫描与统计的共享快照依赖、账号互斥和统计/分批的真实物理子表互斥，不提高 Runner 容量。第五个实际子进程故意失败，用来核对其余任务及副作用未受牵连。

浏览器库的持久准备入口是 `python -m tests.v32_acceptance.prepare_database`，默认只迁移，显式加 `--reset-owned-fixture` 才重建。随后依次执行 `tests.v32_acceptance.management_fixture`、`tests.v32_acceptance.data_fixture`、`tests.v32_acceptance.browser_performance`、`tests.v32_acceptance.detail_browser`、`tests.v32_acceptance.navigation_probe`、`tests.v32_acceptance.run_acceptance`（均使用 `python -m`）。简单设置实际生效入口为 `python -m tests.v32_acceptance.settings_effective`，要求明确 `--database v32_m01_test`；通过真实页面保存参数和账号，执行下一次插件，再从页面恢复原历史设置并再次执行。

E2E 显式 reset 必须在所有参与者退出并转存旧证据后执行：它在独占文件锁内重建 `v32_e2e_test`，同时清理该环境内精确的 `plugin-installed`、`synthetic-plugins`、`console-runtime`，避免新数据库与旧不可变插件目录冲突。清理直接复用插件存储的只读树删除实现：数据库变更前先验证全部目标及内部文件，拒绝软/硬链接，再只为这些已验证目录恢复删除所需权限；宿主运行时的不可变保护保持原样。普通准备不清理运行目录；E2E ManagementFixture 持共享锁，使用中或路径被符号链接重定向时 reset 明确拒绝。解释器、MySQL daemon/data、其他测试库和报告目录均不删除。安全重置命令为 `bash tests/v32_acceptance/isolated_environment.sh run --database v32_e2e_test python -m tests.v32_acceptance.prepare_database --reset-owned-fixture`，完成后再顺序 seed 与 all 验收；禁止在旧版本未持锁的验收进程尚未退出时使用该命令。

已有固定页面的真实登录与导航冒烟为 `python -m tests.v32_acceptance.legacy_page_smoke`（`v32_e2e_test`）；该检查不运行本地打印程序、OCR业务或外部系统写操作。已有专属插件设置页的账号、配置保存及并发版本验证为 `python -m tests.v32_acceptance.custom_settings`，只使用自己的 `v32_c04_test`。六包 CLI 入口使用 `v32_cli_test`，见 `python -m tests.v32_acceptance.plugin_cli_batch --help`；`--report` 可向严格验收目录写入完整的新报告，仍保留每包原日志与 ZIP。

`custom_settings` 使用现有 `clockin_daxiang_v2` 的真实业务 payload、设置 HTML/JS/CSS 与账号 schema，只在隔离副本中把版本改为 `98.4.0`、移除可选 `contributes.harness`，经原 packager、检查及正常安装。原 `1.2.0` 的 Harness 声明被现有权限保护拒绝，关闭实例入口仍不能提交该组合；这项限制没有修复，也不作为本轮 AI 验收。报告包含 TEST_ONLY ZIP、逐成员不变比较和精确 Manifest patch；真实默认停用实例只进行设置/账号保存、并发版本冲突、历史恢复与重开，不启用定时或调用打卡。

该入口还会从已登录 Console 实际 `window.open` 设置 HTML 资源，验证响应 CSP `sandbox allow-scripts`、顶层 `window.origin=null`，以及访问 `opener.document`、`document.cookie` 均被浏览器以 `SecurityError` 拒绝；不把 iframe 属性单独视为顶层资源隔离证明。

全部参与者结束后执行 `bash tests/v32_acceptance/isolated_environment.sh stop`，确认没有本任务子进程，再卸载 `/tmp/boyi-v32-tmp` 绑定并移除本任务临时目录。保留验收报告和交付工件后清理临时运行数据。此文档及入口不在临时目录，清理后仍可重建。
