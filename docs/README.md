---
module: repository-documentation
type: index
tags: [documentation, navigation, authority, lifecycle]
status: active
authority: canonical
owner: repository
updated: 2026-08-30
---

# 仓库文档索引

本页是 `boyi-logistics` 的仓库级文档入口。检索只以 Git 跟踪文件为范围；`.task_tmp/`、`tmp/`、缓存、运行态和生成物不属于项目知识。

## 推荐读取顺序

1. 先读对应工具的根级指令：[AGENTS.md](../AGENTS.md) 或 [CLAUDE.md](../CLAUDE.md)。
2. 按需求查 [代码定位索引](../agent/docs/code_navigation_index.md)。
3. 进入目标模块后，只读该模块的指令和命中的少量说明文件。
4. 代码、迁移、测试与发布脚本始终优先于历史说明；发现冲突时修正文档，不以旧说明覆盖当前实现。

## 现行权威文档

- [代码定位索引](../agent/docs/code_navigation_index.md)：需求到代码、测试和模块说明的入口。
- [项目总览](../agent/docs/project_overview.md)：当前服务边界和模块目录。
- [扩展化平台架构基准](extension-platform-baseline.md)：固定核心模块、Service v2、自动化中心、Harness、Connector 与迁移路线图的现行基准。
- [扩展化平台执行账本](extension-platform-progress.md)：无人值守改造的逐 TASK 状态、验证、提交和生产门禁记录。
- [控制平面](../agent/docs/control_plane_v1.md)：Command、Run、审批、Evidence、Outbox 与恢复。
- [数据库迁移](../agent/docs/database_migrations.md)：顺序迁移与部署期结构管理。
- [Action v1 插件兼容轨道](../agent/docs/automation_plugin_platform.md)：现存 v1 自动化插件合同。
- [Service v2 插件平台](plugin-platform-v2.md)：v2 服务插件、能力代理和迁移合同。
- [ECS 发布手册](../agent/deploy/publish_to_ecs.md) 与 [Nginx 生产边界](../agent/deploy/nginx/README.md)：当前发布、回滚材料和独立 origin 配置。
- [Git 工作流](git_workflow.md)：分支、提交、推送和 Draft PR 流程。
- [产品原则](../PRODUCT.md) 与 [设计系统](../DESIGN.md)：产品和界面约束。

## 模块说明

- [Agent 自动化](../agent/docs/agent_automation/module_overview.md)
- [客服系统](../agent/docs/customer_service/module_overview.md)
- [财务模块](../agent/docs/finance_module.md)
- [OCR](../agent/docs/ocr/module_overview.md)
- [车辆调度](../agent/docs/dispatch/module_overview.md)
- [AI 客服规划](../agent/docs/ai_service/module_overview.md)

## 历史、规划与快照

- `docs/ai-development/` 保存阶段性架构目标和迁移快照，不作为当前代码事实。
- `docs/superpowers/` 与 `console/docs/superpowers/` 保存已实施、被取代或历史计划，不作为当前执行清单。
- `agent/docs/price_scripts/` 保存已退出现行入口的离线价格项目资料，除明确标记的当前入口外只作历史参考。
- `agent/tms_docs/` 保存原系统页面抓取快照。页面或接口相关改动必须重新从真实来源验证，不能仅凭快照实现。

## 生命周期字段

- `authority: canonical`：当前事实的权威说明。
- `status: active`：随当前实现维护，可进入默认检索。
- `status: planned` 或 `aspirational`：未来目标，不能解释为已实现。
- `status: implemented`：已完成的设计或计划记录，当前行为仍以代码和现行说明为准。
- `status: superseded`：已被新方案取代，必须提供替代入口。
- `status: historical`：历史记录，不进入默认现行检索。
- `status: snapshot`：外部页面或系统在某次抓取时的证据，必须结合 `captured_at` 与 `verified_at` 判断时效。

提交前运行 `python3 agent/scripts/check_documentation.py`，校验本地链接、生命周期元数据和三组指令镜像。
