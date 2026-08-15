# Nginx 生产边界

`boyi-worker-mtls.conf` 必须作为一个整体、且仅一次 include 在 `boyi.homes` 的 HTTPS
`server {}` 内。它使用 server 级 `ssl_verify_client optional` 保持普通 Console/内部 API 客户端
不必提供证书，仅在精确的 `/internal/v1/automation/worker/` location 要求验证结果为 `SUCCESS`。

固定生产路径：

- 受版本控制配置：`/etc/nginx/snippets/boyi-worker-mtls.conf`
- Worker 客户端 CA：`/etc/nginx/mtls/boyi-worker-client-ca.pem`
- 启用站点：`/etc/nginx/sites-enabled/boyi.homes.conf`

站点内必须有且只有一行：

```nginx
include /etc/nginx/snippets/boyi-worker-mtls.conf;
```

系统配置由管理员在应用发布之外单独安装。配置和 CA 必须是 root 拥有的常规文件，完整路径不得经过
符号链接，文件及父目录不得允许 group/other 写；仓库不会生成、复制或打印 CA/私钥。管理员将当前
提交中的 snippet 安装到固定路径、人工提供 CA 后，必须先执行：

```bash
sudo -n /usr/sbin/nginx -t
sudo systemctl reload nginx.service
```

`remote_release.sh` 在备份、依赖环境构建、停服务、迁移或源码同步前重新验证：staged snippet 与安装
文件 SHA-256 完全一致、站点精确引用、路径/owner/mode 安全、Nginx 正在运行且 `nginx -t` 成功。
任一条件不满足时发布失败关闭，不会自动改写 `/etc/nginx` 或证书目录。

Worker location 总是覆盖来自公网的 TLS 身份头，只从 Nginx 的 `$ssl_client_verify` 和
`$ssl_client_escaped_cert` 生成上游值；`X-Worker-Device-ID` 仅用于选择待校验设备。它同时清空普通
internal token、Console principal、execution capability 和 webhook token。其他 `/internal/v1/*`
location 不受该 snippet 接管，继续使用原有 `X-Agent-Internal-Token`/签名 Console 边界。
