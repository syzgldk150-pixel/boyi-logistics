---
module: deployment
type: operations
status: active
updated: 2026-10-29
---

# Nginx 生产边界

当前生产站点使用 `boyi.homes.conf`。主站 `https://boyi.homes` 保留 `SAMEORIGIN`，只有
`https://www.boyi.homes/original/` 代理到 Console，供韵达/融辉原页在独立 origin 内运行；
`www` 的其他路径一律跳回主站。系统配置不由应用发布脚本自动覆盖；管理员必须先备份
`/etc/nginx/sites-enabled/boyi.homes.conf` 解析到的实际站点文件，把当前提交中的同名文件
安装到受管的 `sites-available`/`sites-enabled` 路径，执行 `sudo -n nginx -t`，成功后才 reload；
测试或 reload 失败时恢复备份并再次测试。

其他配置文件：`reload-nginx.sh` 为证书续期后的 reload 钩子。
