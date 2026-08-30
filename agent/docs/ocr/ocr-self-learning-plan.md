---
module: OCR识别
type: 历史实施方案
tags: [OCR, 自学习, PaddleOCR, 训练管线, 未实施]
related: [module_overview.md]
status: historical
implementation_status: not_implemented
superseded_by: module_overview.md
updated: 2026-08-30
---

# OCR 自学习反馈闭环 + 录单系统演进 — 历史方案

> 历史说明：本文是未完成的设计草案，不是当前运行手册。当前只上线 Qwen-OCR、人工复核和 MySQL/waybills 入库；训练相关表与配置仅为预留，自学习采样、PaddleOCR、双引擎、微调、模型激活和准确率看板均未实现。本文中的拟建文件、命令、配置和 DDL 不得直接执行。现行事实以 `module_overview.md`、`agent/migrations/` 和当前代码为准；数据库结构只能通过顺序迁移维护，失败必须显式报告，不得采用本文草案中的运行时建表或静默 `except`。

## Context

当前物流 Agent 的 OCR 模块使用 Qwen-VL-OCR（云端 API）识别手写托运单，识别率较低（尤其是潦草手写）。系统已有完整的「OCR 识别 → 预填表单 → 人工校对 → 确认入库」流程，但人工校对产生的纠错数据被直接丢弃，没有反哺模型。

**业务背景**：
- OCR 纸质单据识别是**过渡方案**，后期将转为纯电脑录单开单
- 但纸质单据识别仍需保留（部分场景不可避免）
- 当前后台页面 UI 交互体验需要优化

**目标**：
1. 切换到 MySQL 存储（.env 中连接信息已配好）
2. 加入 Human-in-the-Loop 自学习闭环，持续提升 OCR 识别率
3. 优化后台页面 UI 交互体验
4. 架构上为后期「纯电脑录单」预留扩展空间

**当前开发环境**：
- 全部在本地开发测试（有 NVIDIA GPU），项目尚未部署到服务器
- MySQL 数据库在服务器上，本地通过远程连接
- 部署到服务器后再做训练/推理分离（服务器无 GPU，训练在本地跑）

---

## .env 变量清单

`console/.env` 中需要的变量（用户已填写 MySQL 连接信息）：

```bash
# MySQL 连接（已填写）
DOCFLOW_MYSQL_HOST=...
DOCFLOW_MYSQL_PORT=3306
DOCFLOW_MYSQL_USER=...
DOCFLOW_MYSQL_PASSWORD=...
DOCFLOW_MYSQL_DATABASE=...

# PaddleOCR（阶段三开启）
DOCFLOW_PADDLE_ENABLED=false
DOCFLOW_PADDLE_CONFIDENCE=0.8

# 训练参数（阶段四）
DOCFLOW_TRAINING_THRESHOLD=50
```

---

## 实施阶段

### 阶段一：MySQL 切换 + 训练数据表 + UI 基础优化

**改动文件**：
- `console/database.py` — 新增 5 张表（含 waybills）、补 MySQL 索引、增加 schema 迁移方法
- `console/config.py` — 新增训练相关配置项
- `console/templates/document.html` — UI 交互优化
- `console/static/style.css` — 视觉优化

#### 1.0 自动创建 MySQL 数据库

在 `database.py` 的 `initialize()` 方法中，先连接 MySQL（不指定数据库），执行 `CREATE DATABASE IF NOT EXISTS {database_name}`，然后再切换到该库建表。这样用户不需要手动建库。

#### 1.1 documents 表补字段

```sql
ALTER TABLE documents ADD COLUMN writer_id VARCHAR(64) NOT NULL DEFAULT '';
```

#### 1.2 新增 training_samples 表

```sql
CREATE TABLE IF NOT EXISTS training_samples (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    document_id BIGINT NOT NULL,
    field_name VARCHAR(128) NOT NULL,
    template_name VARCHAR(128) NOT NULL,
    writer_id VARCHAR(64) NOT NULL DEFAULT '',
    ocr_value TEXT NOT NULL,
    correct_value TEXT NOT NULL,
    is_correction TINYINT NOT NULL,        -- 1=人工改了, 0=OCR本身正确
    confidence_original FLOAT NOT NULL,
    crop_image_path VARCHAR(1024) NOT NULL,
    source_image_path VARCHAR(1024) NOT NULL,
    bbox_json TEXT NOT NULL,
    created_at DATETIME NOT NULL,
    INDEX idx_ts_field (field_name),
    INDEX idx_ts_writer (writer_id),
    INDEX idx_ts_correction (is_correction),
    INDEX idx_ts_document (document_id)
);
```

#### 1.3 新增 model_versions 表

```sql
CREATE TABLE IF NOT EXISTS model_versions (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    version_tag VARCHAR(128) NOT NULL UNIQUE,
    base_model VARCHAR(256) NOT NULL,
    training_sample_count INT NOT NULL,
    field_scope TEXT NOT NULL,             -- JSON: ["waybill_no", ...]
    metrics_json TEXT NOT NULL,
    model_path VARCHAR(1024) NOT NULL,
    status VARCHAR(32) NOT NULL,           -- training/ready/active/retired
    created_at DATETIME NOT NULL,
    activated_at DATETIME NULL,
    INDEX idx_mv_status (status)
);
```

#### 1.4 新增 accuracy_log 表

```sql
CREATE TABLE IF NOT EXISTS accuracy_log (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    document_id BIGINT NOT NULL,
    field_name VARCHAR(128) NOT NULL,
    writer_id VARCHAR(64) NOT NULL DEFAULT '',
    ocr_provider VARCHAR(64) NOT NULL,
    model_version VARCHAR(128) NOT NULL DEFAULT '',
    ocr_value TEXT NOT NULL,
    final_value TEXT NOT NULL,
    is_correct TINYINT NOT NULL,
    confidence FLOAT NOT NULL,
    created_at DATETIME NOT NULL,
    INDEX idx_al_field (field_name),
    INDEX idx_al_provider (ocr_provider)
);
```

#### 1.5 新增 writers 表

```sql
CREATE TABLE IF NOT EXISTS writers (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    writer_id VARCHAR(64) NOT NULL UNIQUE,
    display_name VARCHAR(128) NOT NULL DEFAULT '',
    sample_count INT NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL
);
```

#### 1.6 新增 waybills 运单表（最终业务数据）

documents 表是 OCR 处理过程表，waybills 表存放确认入库后的最终运单业务数据，字段为模板中 19 个字段的独立列。

```sql
CREATE TABLE IF NOT EXISTS waybills (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    document_id BIGINT NULL,               -- 关联 documents 表（手动录入时为 NULL）
    waybill_no VARCHAR(128) NOT NULL DEFAULT '',
    destination_site VARCHAR(256) NOT NULL DEFAULT '',
    open_date VARCHAR(64) NOT NULL DEFAULT '',
    receiver_address TEXT NOT NULL,
    receiver_name VARCHAR(128) NOT NULL DEFAULT '',
    receiver_phone VARCHAR(64) NOT NULL DEFAULT '',
    sender_name VARCHAR(128) NOT NULL DEFAULT '',
    sender_phone VARCHAR(64) NOT NULL DEFAULT '',
    goods_name_lines TEXT NOT NULL,
    package_type_lines TEXT NOT NULL,
    quantity_lines TEXT NOT NULL,
    weight_volume VARCHAR(128) NOT NULL DEFAULT '',
    delivery_method VARCHAR(32) NOT NULL DEFAULT '',
    freight_fee VARCHAR(64) NOT NULL DEFAULT '',
    pickup_fee VARCHAR(64) NOT NULL DEFAULT '',
    delivery_fee VARCHAR(64) NOT NULL DEFAULT '',
    transfer_fee VARCHAR(64) NOT NULL DEFAULT '',
    payment_method VARCHAR(64) NOT NULL DEFAULT '',
    remark TEXT NOT NULL,
    writer_id VARCHAR(64) NOT NULL DEFAULT '',
    source VARCHAR(32) NOT NULL DEFAULT 'ocr',  -- 'ocr' 或 'manual_entry'
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    INDEX idx_wb_waybill_no (waybill_no),
    INDEX idx_wb_source (source),
    INDEX idx_wb_created_at (created_at)
);
```

在 `apply_review()` 确认入库时，自动将 fields 同步写入 waybills 表。后期电脑手动录单也直接写入此表（source='manual_entry'）。

#### 1.7 config.py 新增配置

```python
training_crops_dir: Path       # runtime/training_crops/
paddle_model_dir: Path         # runtime/models/paddle/
training_sample_threshold: int # 50 (env: DOCFLOW_TRAINING_THRESHOLD)
paddle_enabled: bool           # False (env: DOCFLOW_PADDLE_ENABLED)
paddle_confidence_threshold: float  # 0.8 (env: DOCFLOW_PADDLE_CONFIDENCE)
```

#### 1.7 UI 交互优化（document.html + style.css）

当前复核页面的体验改进：
- 置信度着色：绿色(>=0.85) / 黄色(0.5-0.85) / 红色(<0.5)，一眼看出哪些字段需要关注
- 低置信字段自动展开对应裁剪图片，方便人工比对
- Tab 键快速跳到下一个低置信字段
- 修改后的字段高亮显示差异
- 添加「填写人」下拉框（为阶段二做准备）
- 键盘快捷键提示优化

---

### 阶段二：训练数据自动采集

**新建文件**：`console/training_data.py`
**改动文件**：
- `console/app.py` — 在 `apply_review()` 确认时触发采集
- `console/ocr_providers.py` — 提取裁剪工具函数

#### 2.1 training_data.py — TrainingDataCollector 类

```python
class TrainingDataCollector:
    def harvest_from_confirmation(self, document_id, document, template_spec,
                                   fields_before, fields_after, writer_id):
        """确认入库时调用：对比每个字段，裁剪图片，写入 training_samples"""

    def _crop_field_region(self, image_path, field_spec, template_spec):
        """复用 ocr_providers.py 的 bbox 裁剪逻辑，保存为 JPEG"""

    def get_sample_counts(self):
        """按 field_name 分组统计样本数"""
```

**裁剪逻辑**：从 `ocr_providers.py:743` 的 `_build_field_crop_data_url()` 提取纯裁剪函数 `crop_field_from_image()` 供两处共用。

**存储路径**：`runtime/training_crops/{template_name}/{field_name}/{doc_id}_{field_name}.jpg`

**采集规则**：
- 每个字段都采集（不论是否被修改），`is_correction` 标识是否为纠错
- 空值字段跳过不采集
- 采集失败不阻塞确认流程（try/except 包裹）

#### 2.2 app.py apply_review() 改动（第305-346行）

```python
# 在第310行后，深拷贝 fields 作为 before 快照
fields_before = copy.deepcopy(fields)

# 在第328行确认入库分支内，追加：
if status == "confirmed":
    try:
        self.training_collector.harvest_from_confirmation(
            document_id, document, template_spec,
            fields_before, fields, form_values.get("writer_id", ""))
    except Exception:
        pass  # 不阻塞主流程
```

#### 2.3 历史数据回填

新建 `console/backfill_training_data.py`：遍历所有 `status="confirmed"` 的历史文档，利用 `fields_json` 中的 `source` 字段判断是否为人工修正，裁剪字段图片，写入 training_samples。阶段二完成后立即运行，快速积累初始训练集。

---

### 阶段三：PaddleOCR 引擎集成

**新建文件**：
- `console/paddle_provider.py` — PaddleOCR 封装
- `console/dual_engine.py` — 双引擎调度

**改动文件**：
- `console/ocr_providers.py` — 新增 `build_ocr_provider()`
- `console/app.py` — 切换到 `build_ocr_provider()`
- `console/requirements.txt` — 添加 paddlepaddle-gpu, paddleocr

#### 3.1 paddle_provider.py

```python
class PaddleOCRProvider:
    name = "paddle_ocr_local"

    def extract_document(self, image_paths, template_spec, field_state):
        """逐字段裁剪 → PaddleOCR 识别 → 返回 results + debug"""

    def recognize_field_crop(self, crop_image):
        """单张裁剪图 → (text, confidence)"""

    def reload_model(self, model_path):
        """热加载新模型"""
```

**工作方式**：基于模板 bbox 逐字段裁剪后识别，和 Qwen 的整页理解互补。

#### 3.2 dual_engine.py

```python
class DualEngineProvider:
    def extract_document(self, image_paths, template_spec, field_state):
        """
        策略：
        1. 先跑 PaddleOCR（快、本地、免费）
        2. PaddleOCR 置信度 >= 阈值的字段直接采用
        3. 低置信字段再调 Qwen（慢、云端、收费）
        4. 记录每个字段最终使用哪个引擎
        """
```

**省钱策略**：PaddleOCR 越准，Qwen API 调用越少。

#### 3.3 ocr_providers.py 改动

- 新增 `build_ocr_provider(settings)` 函数，根据 `paddle_enabled` 返回单引擎或双引擎

---

### 阶段四：微调训练管线

> 当前全在本地运行（有 GPU），部署服务器后再做训练/推理分离。

**新建文件**：
- `console/training_pipeline.py` — 训练编排
- `console/train_paddle.py` — CLI 入口

**改动文件**：
- `console/app.py` — 新增训练状态 API

#### 4.1 training_pipeline.py

```python
class TrainingPipeline:
    def __init__(self, settings, repository):
        """读取 MySQL 中的标注数据"""

    def check_readiness(self):
        """检查距上次训练新增了多少标注"""

    def prepare_dataset(self, field_names=None):
        """
        MySQL → PaddleOCR 训练格式：
        - train.txt / val.txt（格式：image_path\tlabel）
        - 90/10 划分
        """

    def run_fine_tuning(self, dataset_path, base_model="ch_PP-OCRv4_rec"):
        """
        PaddleOCR 微调（GPU 加速）：
        - 冻结 backbone 前 N 层
        - batch=32, epochs=50-100, lr=0.0005
        - 保存到 runtime/models/paddle/{version_tag}/
        """

    def evaluate(self, version_tag):
        """在验证集上跑准确率"""

    def activate_if_better(self, version_tag):
        """新模型更好 → 更新 model_versions → PaddleOCRProvider 热加载"""
```

#### 4.2 train_paddle.py — CLI

```bash
python train_paddle.py                          # 自动检测阈值
python train_paddle.py --force                   # 强制训练
python train_paddle.py --field-names waybill_no  # 只训练指定字段
```

#### 4.3 app.py 新增路由

| 路由 | 方法 | 用途 |
|------|------|------|
| `/api/training/status` | GET | 返回样本数、上次训练时间、当前模型版本 |
| `/api/training/trigger` | POST | 手动触发训练（后台线程执行） |

#### 4.4 部署后扩展（后续再做）

部署到服务器后，训练端和推理端分离：
- 新增 `upload_model.py` 工具，将本地训练好的模型上传到服务器
- 服务器端新增 `/api/model/upload` 接收模型并热加载
- 服务器端 PaddleOCR 切换为 CPU 推理模式

---

### 阶段五：准确率看板

**新建文件**：`console/accuracy_tracker.py`、`console/templates/accuracy.html`
**改动文件**：
- `console/app.py` — 新增看板路由
- `console/templates/base.html` — 导航加入看板链接

#### 5.1 accuracy_tracker.py

```python
class AccuracyTracker:
    def record(self, document_id, fields_before, fields_after, provider, model_version):
        """写 accuracy_log"""

    def field_accuracy(self, days=30):
        """按字段统计准确率"""

    def provider_comparison(self, days=30):
        """Qwen vs PaddleOCR 对比"""

    def improvement_trend(self, window_days=7):
        """周维度准确率趋势"""
```

#### 5.2 accuracy.html 看板

- 总体准确率趋势折线图（Chart.js）
- 按字段准确率柱状图
- 按填写人准确率表格
- 训练状态卡片（样本数 / 阈值 / 上次训练 / 当前模型）
- 「触发训练」按钮

#### 5.3 路由

| 路由 | 用途 |
|------|------|
| `/ocr/accuracy` | 准确率看板页面 |

---

### 阶段六：智能优化 + 电脑录单预留

#### 6.1 智能 Qwen 降级

当某字段 PaddleOCR 准确率 > 95%（近 50 条），DualEngine 对该字段完全跳过 Qwen，节省 API 费用。

#### 6.2 电脑录单入口预留

在现有 document.html 基础上，增加「手动开单」模式入口：
- 直接打开空白表单手工填写（不需要上传图片）
- 和 OCR 识别共用同一个 documents 表和字段结构
- `source` 标记为 `manual_entry`，与 OCR 文档区分
- 为后期纯电脑录单系统做数据结构和 UI 复用的铺垫

---

## 文件变更总览

### 新建文件（8 个）

| 文件 | 职责 | 预估行数 |
|------|------|---------|
| `console/training_data.py` | 标注采集、字段裁剪 | ~200 |
| `console/paddle_provider.py` | PaddleOCR 封装 | ~250 |
| `console/dual_engine.py` | 双引擎调度策略 | ~150 |
| `console/training_pipeline.py` | 微调编排、模型版本管理 | ~350 |
| `console/accuracy_tracker.py` | 准确率统计查询 | ~200 |
| `console/train_paddle.py` | CLI 训练入口 | ~80 |
| `console/backfill_training_data.py` | 历史数据回填 | ~100 |
| `console/templates/accuracy.html` | 准确率看板 | ~200 |

### 修改文件（9 个）

| 文件 | 改动点 |
|------|--------|
| `console/database.py` | +4 张表 DDL、+MySQL 索引、+schema 迁移方法 |
| `console/config.py` | +5 个配置字段 |
| `console/app.py` | apply_review() 挂钩采集、+训练API路由、+看板路由、切换 provider 构建函数 |
| `console/ocr_providers.py` | 提取 `crop_field_from_image()` 共享函数、+`build_ocr_provider()` |
| `console/requirements.txt` | +paddlepaddle-gpu, +paddleocr |
| `console/templates/document.html` | +填写人下拉框、+置信度着色、+低置信字段图片展开、+键盘交互优化 |
| `console/templates/base.html` | +准确率看板导航链接 |
| `console/static/style.css` | +置信度着色样式、+字段高亮样式 |
| `console/.env` | +PaddleOCR 开关、+训练参数（MySQL 已配好） |

### 文档更新（6 个）

| 文件 | 更新内容 |
|------|---------|
| `CLAUDE.md` | 模块清单加「OCR自学习」行，文档结构加新 md |
| `AGENTS.md` | 同步 CLAUDE.md 的变更 |
| `console/CLAUDE.md` | 新增自学习相关文件说明、训练链路、新路由 |
| `console/AGENTS.md`（如存在则同步，不存在则创建） | 同步 console/CLAUDE.md |
| `docs/ocr/module_overview.md` | 补充自学习闭环架构、PaddleOCR 双引擎说明 |
| `docs/ocr/自学习训练流程.md`（新建） | 训练数据格式、微调参数、模型版本管理 |

---

## 验证计划

### 阶段一验收
- [ ] MySQL 连接成功，控制台启动无报错
- [ ] 6 张新表全部自动创建（documents 补 writer_id + 5 张新表含 waybills）
- [ ] 确认入库后数据自动同步到 waybills 表
- [ ] 现有 OCR 上传→识别→复核流程不受影响
- [ ] 复核页面置信度着色生效，低置信字段突出显示

### 阶段二验收
- [ ] 确认一张单据后，training_samples 表新增对应行数（=非空字段数）
- [ ] `runtime/training_crops/` 下生成对应裁剪图片
- [ ] 填写人字段可选择/输入，写入 documents.writer_id
- [ ] 采集模块异常不阻塞确认流程
- [ ] 回填脚本可处理历史 confirmed 文档

### 阶段三验收
- [ ] `DOCFLOW_PADDLE_ENABLED=true` 启动后，PaddleOCR 基线模型可用
- [ ] 双引擎模式下，每个字段结果标注了使用的引擎
- [ ] PaddleOCR 低置信字段回退到 Qwen

### 阶段四验收
- [ ] `python train_paddle.py --force` 可完成一次微调
- [ ] model_versions 表写入新版本记录
- [ ] 新模型热加载后，PaddleOCR 使用新权重

### 阶段五验收
- [ ] `/ocr/accuracy` 页面可访问
- [ ] 趋势图、字段准确率、引擎对比数据正确
- [ ] 「触发训练」按钮可用

### 阶段六验收
- [ ] 智能降级逻辑按阈值跳过 Qwen
- [ ] 手动开单入口可用，数据正确写入 documents 表
