---
module: project
type: operations
tags: [git, github, version-control, network]
status: active
updated: 2026-08-30
---

# GitHub 项目管理与国内网络说明

## 工具分工

- WSL 内的 Git 负责 `status`、分支、差异检查、暂存和提交，避免 Windows/WSL 文件权限语义混乱。
- Windows GitHub CLI 负责 GitHub 登录、仓库管理和 Draft PR。登录凭据保存在 Windows keyring，关机重启后继续有效。
- Git 远程协议使用 HTTPS。只有 HTTPS Git 在当前网络长期不稳定时，才按 GitHub 官方方案切换到 `ssh.github.com:443`。

## 每项改动

```bash
git status -sb
git switch main
git pull --ff-only
git switch -c agent/<task-name>

# 修改和验证后，只加入本任务文件
git add -- path/to/file path/to/test
python3 agent/scripts/check_documentation.py
git diff --cached --check
git diff --cached
git commit -m "<concise task summary>"
git push -u origin agent/<task-name>
```

随后使用 Windows GitHub CLI 或 GitHub 插件创建以 `main` 为基线的 Draft PR。未成功推送或未成功建立 Draft PR时，任务尚未完成。

## 网络预检

不要永久保存局域网 IP 形式的代理地址。网络异常时，先在新的 PowerShell 窗口执行：

```powershell
curl.exe -sS -o NUL -w "direct=%{http_code}`n" `
  --connect-timeout 10 --max-time 30 https://api.github.com

Test-NetConnection 127.0.0.1 -Port 7890
```

直连可用时无需代理。需要本地代理时，只为当前 PowerShell 进程设置：

```powershell
'HTTP_PROXY','HTTPS_PROXY','ALL_PROXY' |
ForEach-Object { Remove-Item "Env:$_" -ErrorAction SilentlyContinue }

$env:HTTP_PROXY = 'http://127.0.0.1:7890'
$env:HTTPS_PROXY = 'http://127.0.0.1:7890'
```

该设置会随窗口关闭而消失，不会把过期代理带到下次开机。GitHub 登录本身仍保存在 keyring，不需要重新授权。禁止把代理账号、密码、Token 或 GitHub 认证信息写进脚本和仓库。

## 故障顺序

1. 执行 `gh auth status`，区分认证问题与网络问题。
2. 测试 `https://api.github.com` 直连。
3. 若直连失败，测试本地 `127.0.0.1:7890` 并仅在当前进程启用代理。
4. 清除任何指向旧局域网地址的 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`。
5. 只有 HTTPS Git 仍不可用时，评估 SSH over 443；不得关闭 SSH 主机密钥检查。

## 首次基线例外

仓库第一次建立时允许直接创建和推送 `main`，并创建 `pre-architecture-baseline-<日期>` 标签。该例外在首次基线验证完成后永久结束，后续所有阶段必须使用独立 `agent/*` 分支和 Draft PR。
