---
module: OCR识别
type: 模块文档
tags: [OCR, 单据录入, Qwen-OCR, 模板配置, MySQL, waybills]
related: [ocr-self-learning-plan.md, ../project_overview.md, ../code_navigation_index.md]
status: active
updated: 2026-08-30
---

# OCR 识别模块概述

## 当前结论

OCR 的现行运行入口全部位于 `console/`。当前是 `Qwen-OCR 单引擎 + OpenCV 轻预处理 + 后台任务队列 + 人工复核 + MySQL 入库`，没有启用 PaddleOCR、双引擎路由、自动训练或模型热更新。

`ocr-self-learning-plan.md` 是未完成的历史方案，不是当前实施说明。仓库虽然已经有训练相关表结构和配置占位，但没有训练样本采集器、Paddle provider、训练管线、模型激活流程或准确率看板。

## 已实现能力

- `/ocr` 支持多图/文件夹上传、任务排队和处理状态展示
- 原始图片与中间产物保存到 Console 运行目录，不进入源码仓库
- OpenCV 负责方向纠正、文档区域裁切、轻度增强/降噪和质量检测
- Qwen-OCR 执行整页识别、分块追问与必要字段定向提取
- `/documents/{id}` 提供整张单据与字段表单的人工复核
- 复核页支持置信度提示、填写人和键盘操作
- 确认成功后更新 `documents`，并创建来源为 `ocr` 的 `waybills` 行
- `/waybills/manual` 提供独立的手工录单入口
- 模板可在 `/templates/new` 与 `/templates/{template_name}/edit` 管理

## 数据库存量

迁移 `agent/migrations/003_console_runtime_tables.sql` 建立 OCR 现行表和预留表：

| 表名 | 当前用途 |
|------|----------|
| `documents` | OCR 文档、字段结果与复核状态 |
| `waybills` | OCR 确认或手工录入后的结构化运单 |
| `writers` | 填写人/复核员选项 |
| `training_samples` | 已建表，当前没有自动采集链路 |
| `model_versions` | 已建表，当前没有训练或激活链路 |
| `accuracy_log` | 已建表，当前没有准确率记录/看板链路 |

运行时不得由 `console/database.py` 或请求路径执行 DDL；结构只由 `agent/migrations/` 和部署期迁移器维护。

## 关键文件

- `console/services/documents.py`：OCR 页面和复核请求处理
- `console/app_support.py`：OCR 服务、队列与 `apply_review()` / 手工录单逻辑
- `console/task_queue.py`：后台任务队列
- `console/ocr_providers.py`：当前 Qwen-OCR provider
- `console/database.py`：只读写现有 MySQL 结构
- `console/template_store.py`、`console/config/templates/`：模板管理
- `console/templates/document.html`：复核页
- `console/runtime/originals/`、`console/runtime/artifacts/`：不跟踪的运行时文件

## 未实现能力

- 从人工修正自动生成字段裁剪训练样本
- PaddleOCR provider 与 Qwen/Paddle 双引擎调度
- 本地 GPU 微调、数据集划分、模型版本评估和热加载
- OCR 准确率趋势、字段准确率和引擎对比看板
- 自动以准确率阈值跳过 Qwen

这些能力在真实实现、迁移、测试和验收完成前必须保持未启用；不得仅凭预留表或配置字段宣称已上线。

## 运行方式

从仓库根目录执行 Console 的受管启动脚本：

```bash
cd /home/deng/projects/boyi-logistics/console
./start_backend.sh
```

页面入口：`/ocr`、`/documents/{id}`、`/waybills/manual`。
