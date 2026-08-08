---
module: OCR识别
type: 模块文档
tags: [OCR, 单据录入, Qwen-OCR, 模板配置, MySQL, waybills, 训练样本]
related: []
status: 开发中
updated: 2026-03-30
---

# OCR识别模块概述

## 当前状态

OCR 模块的后端页面和运行链路已经统一到 `console/`。当前工作区使用：

`Qwen-OCR 单引擎 + 批量上传 + 任务队列 + 整页主识别 + 整页分块二次追问 + 必填字段局部定向提取 + 人工复核（置信度着色/填写人/键盘交互） + MySQL + waybills 入库`

## 当前能力

- OCR 工作区上传区已统一为单按钮入口，可选择上传文件夹或多张图片
- 原始图片归档到本地运行目录
- OpenCV 轻预处理
- 后台并发队列识别
- Qwen-OCR 已接通
- 人工复核页使用”整张单据 + 右侧预填表单”
- 复核页置信度着色（红/黄/绿）、填写人选择、键盘快捷操作（Tab/Enter/Ctrl+Enter）
- 待复核列表支持单条删除
- OCR 运行时统一使用 MySQL（阿里云 ECS MySQL 8.0，通过 SSH 隧道连接），`.env` 设 `DOCFLOW_MYSQL_HOST=wsl-gateway` 可自动检测网关 IP
- 确认入库时同步写入 waybills 表
- 支持模板选择、模板新增和模板编辑

## 数据库结构

6 张表：

| 表名 | 用途 |
|------|------|
| `documents` | OCR 文档元数据、识别结果、状态流转 |
| `waybills` | 确认后提取的运单结构化数据 |
| `training_samples` | 人工修正产生的训练样本 |
| `model_versions` | OCR 模型版本与激活状态 |
| `accuracy_log` | 识别准确率追踪 |
| `writers` | 填写人/复核员管理 |

## 模板配置系统

模板系统当前只做两处页面：

1. `OCR 工作区`
   增加 `选择模板 / 编辑模板 / 新增模板`
2. `模板编辑页`
   单独编辑模板名称、描述和 JSON 参数

模板相关文件：

- `console/template_store.py`
- `console/config/templates/`
- `console/templates/template_editor.html`

模板编辑页入口：

- `http://127.0.0.1:8765/templates/new`
- `http://127.0.0.1:8765/templates/{template_name}/edit`

## 关键目录

- `console/app.py`
  项目级本地控制台入口
- `console/task_queue.py`
  OCR 后台任务队列
- `console/ocr_providers.py`
  Qwen-OCR 接口实现
- `console/config/templates/`
  模板 JSON 目录
- `console/runtime/originals/`
  原始单据图片
- `console/runtime/artifacts/`
  处理图和临时文件
- `console/runtime/artifacts/`
  OCR 中间产物与临时文件目录，不再保存运行时数据库
  本地数据库
- `console/runtime/originals/`
  原始单据样本

## OpenCV 当前职责

OpenCV 只保留这 5 件事：

- 方向纠正
- 文档区域裁出并尽量摆正
- 轻度增强对比度和轻度降噪
- 模糊 / 过暗 / 过曝 / 单据占比检测
- 低质量图片分流

## 运行方式

```powershell
wsl bash -lc 'cd /home/deng/projects/console && ./start_backend.sh'
```

页面入口：

- `/`
  项目总览
- `/ocr`
  OCR 工作区
- `/documents/{id}`
  单据复核页
