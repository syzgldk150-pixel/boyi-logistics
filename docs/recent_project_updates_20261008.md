---
module: repository-documentation
type: conversation-review
tags: [documentation, release, verification, pending]
status: historical
updated: 2026-10-08
---

# 2026-10-08 近期项目对话与文档核对

本次核对覆盖下列五个近期博益项目对话，结合各对话最近完成结果、当前 main 源码和现行文档整理。它不是全部历史对话的审计，也不把旧运行数量或定时配置视为现在仍不变的状态。该记录补充了上一轮文档更新遗漏的旧表述和待办。

| 对话 | 已有结果与文档入口 | 尚未完成或限制 |
|---|---|---|
| 排查 ECS 扫描码 | 核心与三个 2.1.0 ZIP 协同升级、原配置保持、两个只读预览通过；见[插件修复上线记录](plugin_business_boundary_20261008.md)和[维护入口](../agent/service_v2_plugins/README.md)。 | 新版上线后未提交正式扫描、问题件、统计写表或财务采集，回滚材料保留。 |
| 检查 ECS 运行状态 | 同日 17:47:14 实跑记录：686 件中 679 件成功、7 件签收跳过；见[同日扫描记录](plugin_business_boundary_20261008.md#同日其他对话的扫描实跑记录)。 | 前次提交失败的底层原因未确定；不能用发布前实跑代替新版正式验收。 |
| 制作R7车线每日打卡插件 | 1.1.0 的通用账号请求、包内业务规则和验收范围见[R7 维护手册](../agent/docs/r7_vehicle_checkin.md)、[账号 HTTP 合同](../agent/docs/plugin_account_http.md)。 | 定时已保存但页面超时的问题仍待修复；最后修复请求中断，无已完成的修复提交或发布证据。 |
| 新增百世运单原页 | 最终采用独立窗口，扫码登录后目标为运单录入；对话记录已发布 `6360bf0`。见[Console 使用说明](../console/README.md#百世原页与账号登录)和 Console 指令镜像。 | 已验证线上入口及跳转目标；实际扫码登录、实单保存与打印仍由用户完成。 |
| 修复详细地址网点自动匹配 | 原页助手 0.4.3 修复 MiniUI 详细地址事件值，相关修改与百世扫码账号能力已发布 `77da948`；后续百世账号条目与按钮恢复显示。见[扩展说明](../console/static/browser_extensions/ronghui/README.md)、[自动化账号说明](../agent/docs/agent_automation/module_overview.md)和[项目总览](../agent/docs/project_overview.md)。 | ECS 发布不会自动更新本机扩展；实际开单、保存和出纸不在此前已验证范围内。 |

## 本次补齐

- Agent 及飞书指令镜像取消“宿主固定 90/250 票”和“确认必然执行全部”的误导表述，统一使用已持久化预览的 `selection_limit`。
- 自动化说明及项目总览移除现行统计依赖 Host `split_pending_snapshot.py` 重新分类的旧说明，改为包内共享规则生成、Host 保存和核验；合法空结果与来源读取失败分别说明。
- Console 使用说明补上百世独立窗口与业务账号扫码的区别，项目总览补齐百世账号系统。
- R7 文档保留“保存超时仍待修复”；扫描实跑按其真实时间记录，避免和后续插件发布的验收混为一谈。

`boyi-plugin-builder` 的现有技能说明已明确：页面参数、解析、筛选、计算、提交流程及成功判断属于插件；主系统提供账号登录态、通用请求、权限、定时、存储与执行证据。本次只核对该边界，未修改本机技能文件；项目侧规则以[插件维护说明](../agent/service_v2_plugins/README.md)为入口。

## 对话追溯

上述对话在本机 Codex 的标识依次为 `01a11a8e-a82f-70b2-a24d-5b0b06255917`、`01a11ac9-6638-7e10-a4f6-2f70c79d23eb`、`01a1196d-6c9b-7180-b576-ce9f91c122c1`、`01a11972-f5c3-7002-a8f6-95b78d72c64b`、`01a1165c-c2c2-7e71-b729-3b919f166de3`。仅记录必要结论，不复制会话凭据、业务明细或原始日志。
