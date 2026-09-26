---
module: repository-documentation
type: index
tags: [documentation, navigation, authority, lifecycle]
status: active
authority: canonical
owner: repository
updated: 2026-09-26
---

# 仓库文档索引

本页是 `boyi-logistics` 的仓库级文档入口。实现检索以 Git 跟踪文件为范围，并按下方索引补充查阅用户指定的本地项目档案；`.task_tmp/`、`tmp/`、缓存、运行态和其他未归档生成物不作为现行项目文档。

## 推荐读取顺序

1. 先读对应工具的根级指令：[AGENTS.md](../AGENTS.md) 或 [CLAUDE.md](../CLAUDE.md)，并查看下方本地项目档案清单，按任务范围选择相关资料。
2. 按需求查 [代码定位索引](../agent/docs/code_navigation_index.md)。
3. 进入目标模块后，只读该模块的指令和命中的少量说明文件。
4. 代码、迁移、测试与发布脚本始终优先于历史说明；发现冲突时修正文档，不以旧说明覆盖当前实现。

## 本地项目档案

用户已将桌面项目资料统一归档到 `C:\Users\DENG\Desktop\博益项目档案`，WSL 路径为 `/mnt/c/Users/DENG/Desktop/博益项目档案`。每次新任务先查看该目录，再按当前任务查阅相关任务书、验收证据或发布记录，不必全量读取。

以下名称均相对于该档案目录：

| 文件或目录 | 用途与查阅入口 |
|---|---|
| `BOYI_CODEX_MASTER_DEVELOPMENT_AND_STAGE_AUDIT_V1_0.md` | 项目总任务书、开发阶段和阶段核查基准。 |
| `BOYI_CODEX_PHASE1_FINAL_CLOSEOUT_R1.md` | 第一阶段收尾任务、执行范围和验收要求。 |
| `BOYI_PHASE1_CLOSEOUT_20260916/` | 第一阶段验收与历史发布资料；先看 `README.md`、`phase1_final_closeout.md`，再查相关证据。 |
| `BOYI_STAGE_AUDIT_20260924/` | 阶段核查结果、证据索引和当时的后续任务；先看 `stage-audit.md`、`next-tasks.md`。 |
| `BOYI_QUERY_KNOWLEDGE_20260924/` | 发货吨位和飞书知识的授权、核对与验收资料；先看 `部署前验收.md`，按需查 `predeployment-evidence/`。 |
| `BOYI_PRODUCTION_RELEASE_20260926/` | 生产发布过程与上线验证；先看 `生产发布记录.md`、`发布状态.json`，按需查 `evidence/`。 |
| `SERVER_B_CODEX_DEPLOYMENT_V2.md` | 马来西亚服务器 B 的独立部署任务书，按服务器 B 相关任务查阅。 |

档案是用户指定的背景与历史证据来源，不随源码发布。引用时核对文档时间、适用范围和实际完成状态；结合本次用户要求、现行代码及必要的线上核实判断，不能把旧任务书中的待办或授权自动沿用到新任务。目录不可达时如实说明，不猜测内容。新增或移动档案时同步维护本索引。

## 现行权威文档

- [业务接口与独立插件调用架构](architecture_direct_invocation.md)：双服务、普通业务直接调用、独立插件 Invocation 和维护归属。
- [当前插件源码、测试与升级入口](../agent/service_v2_plugins/README.md)、[Service V2 平台合同](plugin-platform-v2.md)：现行业务源码、Host 边界、独立 ZIP 与退役发布。
- [身份权限与统一对话](identity_and_unified_chat.md)：后台账号和飞书身份继承、共用 AI 服务、授权插件执行及当前应签规则。
- [V3.2 维护边界](low_maintenance_v32.md)、[复现与发布回退](low_maintenance_v32_release.md)：所属模块入口、可选 AI、简单设置、局部维护与来源历史。
- [寄件查询与范围覆盖](direct_waybill_query.md)：数据库优先、平台补查及完整来源边界。
- [发货吨位与飞书知识读取](shipment_knowledge_queries.md)：类型化查询、追问、受限 Wiki CLI 与真实来源核实边界。
- [代码定位索引](../agent/docs/code_navigation_index.md)、[项目总览](../agent/docs/project_overview.md)：代码、测试和模块入口。
- [数据库迁移](../agent/docs/database_migrations.md)：部署期顺序迁移；运行时只做校验和读写。
- [ECS 发布手册](../agent/deploy/publish_to_ecs.md)、[Nginx 边界](../agent/deploy/nginx/README.md)、[Git 工作流](git_workflow.md)：当前 main 维护、V2-only 发布、验证与回滚。
- [旧执行链退役边界](legacy_execution_retirement.md)、[历史未知写核验](historical_write_recovery.md)、[扫描未知写恢复](scan_recovery_v32.md)：保留事实和人工核验，不恢复旧待领取业务。
- [产品原则](../PRODUCT.md)、[设计系统](../DESIGN.md)：产品与界面约束。

## 模块说明

- [Agent 自动化](../agent/docs/agent_automation/module_overview.md)
- [客服系统](../agent/docs/customer_service/module_overview.md)
- [财务模块](../agent/docs/finance_module.md)
- [运单录入内的 OCR](../agent/docs/ocr/module_overview.md)
- [车辆调度](../agent/docs/dispatch/module_overview.md)
- [AI 客服规划](../agent/docs/ai_service/module_overview.md)

## 历史、规划与快照

- [扩展平台原始方案](extension-platform-baseline.md)、[原迁移执行账本](extension-platform-progress.md)：保留阶段性 TASK 与隔离证据，不是当前待办或生产门禁。
- [旧控制平面](../agent/docs/control_plane_v1.md)、[旧 Action V1 合同](../agent/docs/automation_plugin_platform.md)：解释历史记录；不用于新增插件或日常执行。
- [架构改造验收映射](architecture_refactor_acceptance_mapping.md)、[V3.2 逐组验收要求](low_maintenance_v32_acceptance.json)：阶段验收与复现入口，不能据此推断当前线上每次执行都成功。

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

提交前运行 `python3 agent/scripts/check_documentation.py`，校验本地链接、生命周期元数据和已配置的指令镜像；修改其他层级镜像时也须同步核对。
