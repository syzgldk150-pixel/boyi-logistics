---
module: 独立插件账号请求
type: 开发手册
status: active
updated: 2026-10-08
---

# 插件拥有业务，宿主提供账号请求

新网页业务的接口路径、参数、分页、字段解析、提交条件和成功判断放在独立 ZIP 内。宿主提供 `http.request` 通用能力，负责使用实例选择的账号登录态、约束目的站点、权限、超时、写开始回执及真实响应观测。新增同系统的页面业务不再需要注册一个专用 Host Connector。

当前支持 R7、R13 的已保存 SSO 会话及 GET/POST JSON 接口。两种系统的站点和认证协议固定在 `agent/agent/automation_plugins/account_http.py`，凭据不出宿主。R7 请求携带原站确认的 `x-appId=tms` 和同源 `aurora-back`，不继承 SSO 的应用标识或 Bearer。业务响应 `code=-2` 按登录拒绝处理；只读请求报告 `BLOCKED_LOGIN`，已开始的写请求仍报告未知写且不重试。其他认证协议、文件上传或非 JSON 接口尚不支持，不能伪称已接通；新增认证协议时扩展通用账号适配器，业务路径继续留在包内。现有数据库、飞书等 Connector 仍可复用。

## 包声明与调用

Manifest 可选新增 `http_requests`；旧包没有该字段时保持原规范化结果和摘要。每条声明包含 `name/account_role/action/method/path`，例如：

```json
{
  "name": "task_page",
  "account_role": "r7_operator",
  "action": "read_json",
  "method": "POST",
  "path": "/gateway/tms/public/lineTask/pageGet"
}
```

对应 `capabilities` 声明 `name=http.request`，`operations` 列出实际需要的 `read_json/write_json`，绑定同一 `account_role`，`resource_role=null`。账号角色必须必填且只声明一个受支持系统；实例仍通过宿主简单设置选择账号。现有权限模型每个 capability/action 只允许一个绑定角色，多个账号实例分别配置。

`read_json` 是只读权限，`write_json` 是外部写权限；实际读写性质来自原站请求验证，不能依据 GET/POST 或接口名字猜测。安装人员审核包内声明，运行时不能把已声明的写接口改成只读调用。接口声明进入包、Manifest 和权限摘要及离线权限报告，修改接口需要升级 ZIP。此机制以管理员安装的插件为业务代码信任边界，不自动证明一个外部接口确实只读。

插件仅传声明名称和 JSON 数据，不传 URL、认证头或账号 ID：

```python
response = broker_call(
    "http.request", action="read_json", role="r7_operator",
    arguments={"request": "task_page", "body": {"currentPage": 1}},
)
data = response["response"]
evidence_ref = response.host_evidence_ref
```

GET 时 `body` 转为查询参数，POST 时作为 JSON。路径必须是规范相对路径，不接受域名、查询串、百分号编码或目录跳转。运行时从本次已验证版本加载声明，核对动作和账号角色；主程序决定站点及登录态，不跟随重定向，不输出响应头或凭据字段。请求/响应各限制 4 MiB，单次连接/读取超时为 5/20 秒，不自动重试。每次调用 `read_json` 上限 800 次、`write_json` 上限 200 次，总计 1000 次。插件沙箱仍无直接网络及宿主文件访问权限。

## 插件定义核验条件，宿主核对真实响应

写操作前记录宿主写开始回执；插件随后发起独立查询。成功结果的 `meta.evidence_refs` 保留本次所有 Host 引用，并为每次写入提供 `meta.http_write_verifications`：

```json
[{"write_ref":"本次写引用","read_ref":"后续读引用","matches":[
  {"write_path":"/response/data/id","read_path":"/response/data/id","expected":"本次业务ID"},
  {"write_path":"/response/data/completedAt","read_path":"/response/data/completedAt","expected":"服务器回执时间"}
]}]
```

路径使用 JSON Pointer。插件选择业务身份和落库字段，宿主检查每条写入都有同账号角色、发生在写入之后的只读观测，并将指定字段与写回执、回读响应及期望值逐一比对。空比较、缺失字段、伪造值、旧回读或错账号均不能通过。宿主不包含 R7 任务状态、站点或打卡时间规则，也不接受插件单独自报“成功”作为核验依据。

无业务写入时保持 `NOT_APPLIED` 和空核验列表；超时或结果未知时明确终止，不为获得成功自动重试。取消后仍沿用现有 Host 调用排空和账号占用释放机制。

## 维护入口

- 协议：`http_request_contract.py`、`manifest_v2.py`、`host_capability_registry.py` 和编辑器 `manifest-v2.schema.json`。
- 传输：`account_http.py`；本次已验证 Manifest 来自 `capability_proxy_v2.py`。已实现的 `http.request` 不得留在不可用占位能力集合中；生产依赖就绪检查和执行驱动均使用过滤后的能力清单。
- 通用核验：`http_write_verification.py`，由 `orchestration/result_verifier.py` 调用。
- 参考插件：`service_v2_plugins/r7_vehicle_checkin_v2/` 1.1.0；旧 1.0.0 的专用 R7 Connector 已移除，本次发布应配套新版 ZIP。
- 测试：`tests/test_account_http.py`、`tests/test_r7_vehicle_checkin_v2.py`。前者验证只改包声明即可调用新路径，后者执行真实隔离 ZIP/Broker 和同一账号的写后核验；业务服务响应为隔离 fixture，不代替生产实例验收。

首次使用需要一次通用宿主能力升级，并在安装后确认插件的依赖就绪和启用状态。以后在已支持账号协议内新增接口、调整业务规则或字段，发布插件 ZIP 即可；不应为这些变化继续增加主程序业务分支。
