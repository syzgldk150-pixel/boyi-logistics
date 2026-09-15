---
module: project
type: operations
tags: [git, github, version-control, network]
status: active
updated: 2026-09-15
---

# GitHub 项目管理与国内网络说明

## 工具分工

- WSL 内的 Git 负责 `status`、分支、差异检查、暂存和提交，避免 Windows/WSL 文件权限语义混乱。
- Windows GitHub CLI 负责 GitHub 登录、仓库管理和 Draft PR。登录凭据保存在 Windows keyring，关机重启后继续有效。
- Git 远程协议使用 HTTPS。只有 HTTPS Git 在当前网络长期不稳定时，才按 GitHub 官方方案切换到 `ssh.github.com:443`。

## 每项改动

用户已确认本项目只维护一个最新 `main`。保留现有工作区改动，不自动创建分支、Draft PR 或恢复引用；如用户另行要求独立分支，再按该次要求执行。禁止强推覆盖远端历史。

```bash
git status -sb
git fetch origin
# 工作区和分支归属核对后，仅快进更新
git switch main
git pull --ff-only origin main

# 修改和验证后，只加入本任务文件
git add -- path/to/file path/to/test
python3 agent/scripts/check_documentation.py
git diff --cached --check
git diff --cached
git commit -m "<concise task summary>"
git push origin main
git rev-parse HEAD
git ls-remote --heads origin
```

推送前若远端已有新提交，应先复核并整合，不得强推。推送后核对远端 `main`；需要同步 ECS 时再按[发布手册](../agent/deploy/publish_to_ecs.md)执行并核对服务版本。

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

## 历史工作流

早期分支、Draft PR 和首次基线标签规则仅用于解释旧历史，已由上述用户确认的单一 main 策略替代。
