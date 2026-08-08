---
module: 价格获取
type: 模块文档
tags: [TMS, API, OCR登录, 批量报价, 同名消歧, 断点续传, 网点匹配]
related: [[01-amap-address-fetch], [03-quote-sheet-generation], [TMS价格结构分析], [tms-batch-quote-resume]]
status: active
updated: 2026-06-16
---

# 模块二：TMS 系统价格获取

> 登录 TMS 物流系统，批量查询全国 3056 个区/县在 8 个重量区间 × 4 种产品 × 2 种派送方式下的运费报价。默认按区间边界取样；对历史原始价表中已确认依赖非边界最佳重量的 549 个区域，再额外加载覆盖权重点，保持代码与现有数据源一致。

---

## 模块职责

自动化完成 TMS 系统（`https://tms.ronghuiwl.com`）的批量报价提取，包含 OCR 验证码登录、地址解析、网点匹配、价格计算存储过程调用，输出完整价格矩阵。

## 当前状态：持续维护（2026-03-16 增量修复）

- 总地址：3056 条（全部完成）
- 总报价记录：100,478 条（有效 99,896 / 异常 582）
- 有效率：99.4%
- 测试模式：默认每个重量区间仅测两端边界；对 `non_boundary_weight_overrides.json` 中的区域叠加历史权重点
- 覆盖：31 省 × 8 重量区间 × 4 产品 × 2 派送方式
- 非边界权重点覆盖：549 个区域、2561 行原始记录
- **已修复**：133 个区县数据错误（RC1 同名区县错配 16 个 + RC2 地址库错误 17 个 + RC3 处理异常 100 个）
  - RC1：14/16 修复成功（根因：address_database.json 地址指向同名他城），2 个为 TMS 系统盲区
  - RC2：17 个地址已修正
  - RC3：100 个全部修复成功
- Agent `price_tool` 已接入本模块：飞书单地址报价默认通过 Agent `/tms/get_price`，使用独立的 `price` TMS 短信登录态（`/admin/tms/price-session/*`）；批量报价脚本仍保留本目录 `get_price.py` / `login_manager.py` 的离线采集入口。

---

## 文件说明

| 文件 | 类型 | 说明 |
|------|------|------|
| `batch_run.py` | 脚本 | **主入口**：批量报价提取（按地址），断点续传，自动重登 |
| `batch_run_by_site.py` | 脚本 | **指定网点报价**：直接输入网点名称获取价格，跳过地址解析（用于自提部等无派送区域的网点） |
| `build_non_boundary_weight_overrides.py` | 脚本 | **覆盖文件生成**：从原始价表提取非边界最佳重量区域，生成覆盖权重点 JSON |
| `repair_mismatch.py` | 脚本 | **定向修复**：修复同名区县错配/地址库错误/处理异常（仅重跑受影响区县） |
| `repair_selected_regions.py` | 脚本 | **指定区域重跑**：按 `省份|城市|区/县` 只重跑少量区域，并写回原始价表 |
| `weight_scan.py` | 脚本 | **逐公斤扫描**：单地址 1-3000kg 逐公斤测试价格，输出阶梯变化规律 |
| `get_price.py` | 库 | **核心 API 封装**：地址报价先复用真实“运单录入”页完整地址解析，拿到目的网点/派件网点后再计算价格；不再拆分地址做目的网点兜底匹配 |
| `browser_address_resolver.py` | 库 | **录单页地址解析器**：复用 Playwright 登录后的“运单录入”页真实输入链路，通过详细地址控件 blur 获取目的网点、派件网点和省市区字段；无头页面会补齐公开登录上下文和地图空实现，避免页面解析函数被展示依赖打断 |
| `login_manager.py` | 库 | **登录管理器**：ddddocr OCR 识别验证码，requests.Session 无浏览器登录 |
| `shared/price_utils.py` | 共享库 | **Decimal 工具**：费用求和、单价除法、利润加计，统一财务精度 |
| `shared/weight_sampling.py` | 共享库 | **采样工具**：边界点生成、非边界覆盖权重点加载 |
| `shared/tms_weights.py` | 共享库 | **重量区间定义**：8 段区间常量 |
| `config.json` | 配置 | TMS 请求头配置（Origin/User-Agent 等） |
| `batch_progress.json` | 数据 | 断点续传进度：`done_indices`(3056) + `results`(100478) |
| `non_boundary_weight_overrides.json` | 数据 | **非边界权重点覆盖**：549 个区域的额外测试重量 |
| `全国省市县TMS价格表.xlsx` | 数据 | 完整报价数据源（100,478 行），供模块三读取 |
| `batch_run.log` | 日志 | 运行日志（追加写入） |
| `weight_scan_analysis.md` | 文档 | **价格结构分析**：逐公斤扫描结论，8区间划分，两地对比验证 |

---

## 运行命令

```bash
# 单独运行采集
cd "C:/Users/DENG/Desktop/price_scripts/scripts/02_tms_price_fetch"
python -u batch_run.py >> batch_run.log 2>&1

# 或使用流水线（采集 + 生成单价表 + 数据验证）
cd "C:/Users/DENG/Desktop/price_scripts/脚本"
python -u run_pipeline.py          # 从 batch_run 开始
python -u run_pipeline.py --skip   # 跳过采集，直接生成单价表
```

### 单地址测试

```bash
python get_price.py "浙江省杭州市西湖区文三路100号" 100 0.1
```

### 逐公斤价格扫描

```bash
cd "C:/Users/DENG/Desktop/price_scripts/scripts/02_tms_price_fetch"
python weight_scan.py                                   # 默认成都锦江区 1-3000kg
python weight_scan.py --address "浙江省杭州市西湖区文三路100号"  # 指定地址
python weight_scan.py --start 1 --end 500 --step 1      # 只扫描 1-500kg
```

输出：`临时文件/weight_scan_result.xlsx`（完整明细）+ 终端打印价格阶梯变化汇总

### 指定网点报价（batch_run_by_site.py）

```bash
cd "C:/Users/DENG/Desktop/price_scripts/scripts/02_tms_price_fetch"

# 1. 准备输入文件：在本目录创建 指定网点查询.xlsx，包含"网点名称"列
# 2. 运行
python batch_run_by_site.py

# 输出: 输出结果/指定网点价格表.xlsx
```

原理：跳过地址解析（`/map/inputtipsDTH`），直接用网点名称调 `FIND_DESTINATION_BY_NAME`（等同于 TMS 网页"目的地输入框"的搜索）。适用于自提部等无派送区域、地址匹配不到的网点。

三级回退搜索策略：
1. 直接用网点名称搜索（如"长沙自提部"）
2. 提取城市前缀搜索（如"长沙"）→ 在候选中匹配 SITE_NAME
3. 逐个候选深度检查 dispatch_site_name

### 定向修复（repair_mismatch.py）

```bash
cd "C:/Users/DENG/Desktop/price_scripts/scripts/02_tms_price_fetch"

# 先预览受影响区县（不执行修复）
python repair_mismatch.py --dry-run

# 执行完整修复（RC1同名错配 + RC2地址库错误 + RC3处理异常）
python repair_mismatch.py

# 仅修复某一类
python repair_mismatch.py --rc1-only   # 仅同名区县错配
python repair_mismatch.py --rc2-only   # 仅地址库错误
python repair_mismatch.py --rc3-only   # 仅处理异常

# 仅重跑指定区域
python repair_selected_regions.py --region "上海市|市辖区|宝山区"

# 从原始价表重建非边界权重点覆盖
python build_non_boundary_weight_overrides.py
```

---

## 配置参数（batch_run.py）

| 参数 | 值 | 说明 |
|------|----|------|
| `WEIGHT_INTERVALS` | 8 档(min/max) | 每个区间定义范围，如 0-100kg → min=1, max=100 |
| `TEST_POINTS` | 2 | 默认测试两端边界 |
| `VOLUME` | 0.1 m³ | 固定体积 |
| `SLEEP_ADDR` | 3s | 地址间隔，避免限流 |
| `SLEEP_WEIGHT` | 0.5s | 同区间内不同测试重量间隔 |
| `SLEEP_RATELIMIT` | 60s | 限流后等待时间 |
| `RELOGIN_INTERVAL` | 80 条 | 每 N 条主动重登（防 Session 超时） |
| `SAVE_INTERVAL` | 10 条 | 每 N 条保存进度 |
| `MAX_RETRY` | 3 次 | 单地址最大重试次数 |

### 重量区间测试点

每个区间默认测两端边界值；若当前区域存在 `non_boundary_weight_overrides.json` 覆盖，则把覆盖文件里的历史权重点一并加入测试集合，计算每个重量的 `fee / weight`，保留每公斤单价最高的：

| 重量区间 | 测试重量点 |
|----------|-----------|
| 0-100kg | [1.0, 100.0] |
| 101-150kg | [101.0, 150.0] |
| 151-300kg | [151.0, 300.0] |
| 301-350kg | [301.0, 350.0] |
| 351-800kg | [351.0, 800.0] |
| 801-1500kg | [801.0, 1500.0] |
| 1501-3000kg | [1501.0, 3000.0] |
| 3001+kg | [3001.0, 5000.0] |

示例：
- 普通区域：`301-350kg -> [301.0, 350.0]`
- 被覆盖区域：`301-350kg -> [301.0, 313.2, 350.0]`

---

## 敏感信息

| 变量名 | 用途 |
|--------|------|
| `TMS_DAXIANGUSERNAME` | TMS 物流系统账号（从项目根目录 `.env` 读取） |
| `TMS_DAXIANGPASSWORD` | TMS 物流系统密码（从项目根目录 `.env` 读取） |

---

## 脚本依赖关系

```
batch_run.py
  ├── import get_price.py    （TMS API 封装）
  │     ├── browser_address_resolver.py（真实“运单录入”页完整地址解析，不做拆词兜底）
  │     └── import login_manager.py  （OCR 登录）
  └── import login_manager.py（直接导入 TMSAuth）
```

---

## 核心业务流程

### TMS API 调用链（每条地址）

```
1. browser_address_resolver.py   → 真实“运单录入” iframe 对 `ACCEPT_MAN_ADDRESS` 执行完整地址 blur 解析，读取 `DESTINATION_CODE` / `DISPATCH_UNDERLING_SITE_CODE`
2. FIND_CREATE_BILL_DESTINATION  → 获取目的地详情
3. FIND_SITE_AND_CENTER          → 查站点中心
4. FIND_CREATE_BILL_SEND_CENTER  → 发货中心
5. FIND_CREATE_BILL_DISP_CENTER  → 目的地中心（NEW→OLD 降级）
6. FIND_PLAN_GOODS_ROUTE         → 运输路线
7. FIND_TAB_WEIGHT_RATIO         → 体积重量系数
8. FIND_SITE_DISP_INFO           → 网点派送信息
9. P_CALC_CLIENT_PRICE_BILL_SHOW4 → 存储过程计算价格

> 地址解析不再使用 `/map/inputtipsDTH` 的 `areaResults` 拆词结果去调用 `FIND_DESTINATION_BY_NAME` 兜底。录单页没有解析出目的网点时，该地址直接按解析失败处理，避免把路名中的地名误当作目的网点。
> 无头浏览器里的录单页若缺少 `$Z.user.getUserInfo()`、`SITE_LEVELS` 当前站点等级选中行或地图对象，地址 blur 回调会被页面脚本打断；解析器只合并补齐页面公开登录字段、按 `loginSiteType` 选中真实 `SITE_LEVELS` 行；如果页面未创建 `SITE_LEVELS` MiniUI 控件，则用已登录用户的真实 `loginSiteType` 注入一个只供页面脚本读取的兼容控件。`SITE_LEVELS` 下拉接口可能返回 `label/value` 而不是 `LEVELS/TEXT`，解析器会先归一化字段再选中，并补地图空实现，不改变目的网点匹配算法，无法明确匹配站点等级时显式返回解析失败。
> 浏览器地址解析器内部使用专用单线程 worker 执行同步 Playwright 操作；即使报价入口从 FastAPI/飞书的 `asyncio` 调用链触发，也不得在事件循环线程里直接启动或复用同步 Playwright 对象。Agent 运行时必须通过包内路径加载 `agent.tms_runtime.scripts.browser_address_resolver` / `login_manager` / `address_utils` 等运行时模块，避免离线报价脚本目录里的同名模块或 `sys.modules` 缓存串线；`tools.price_tool` 的旧本地报价兜底只能在隔离的导入上下文中临时加载 `price_scripts`，结束后必须恢复 `sys.path` 和相关模块缓存。
> Agent 单地址报价会区分真实不可达和解析器异常：明确的目的地停用/不可用才返回 `网点不可达`；录单页解析超时、目的地字段缺失等不确定失败返回 `error_code=RONGHUI_ADDRESS_RESOLVE_FAILED` 和 `address_resolution_error`。`price_tool.py` 会把融辉单侧失败保留在 `ronghui` 段，不再覆盖韵达报价结果。
> 计费 payload 必须保留运单录入页的默认保价字段：`INSURANCE=3000`、`INSURANCE_FEE=3`。如果留空或传 0，融辉后端过程会按 2000 保价返回 2 元保费，导致报价总额比录单页“总金额”少 1 元。
```

### 产品规则

| 产品代码 | 名称 | 适用重量 |
|----------|------|----------|
| 001 | 融惠达 | > 3000kg |
| 002 | 精准零担 | > 800kg |
| 003 | 融安达 | 所有区间 |
| 004 | 融速达 | ≤ 800kg |

### 容错机制

- **断点续传**：每 10 条保存进度至 `batch_progress.json`，重启自动恢复
- **自动重登**：每 80 条刷新 Session，防止超时
- **限流处理**：429/502/503 → 等待 60s + 重登
- **连续失败**：连续 3 条异常 → 触发重登
- **地址解析口径**：地址报价只使用真实“运单录入”页的完整地址解析结果，不做 `areaName` / 区县 / 城市拆词兜底匹配
- **文件占用**：Excel 被打开时自动另存为带时间戳的文件

### 错误标记

| 标记 | 含义 |
|------|------|
| 地址解析失败 | TMS 无法解析该地址 |
| 网点盲区 | 无匹配目的地网点 |
| 目的地不可用 | 目的地代码无效 |
| 处理异常 | 3 次重试均失败 |

---

## 输出数据格式

每行包含：

| 字段 | 说明 |
|------|------|
| 省份 | 省/自治区/直辖市 |
| 城市 | 地级市 |
| 区/县 | 区/县/旗 |
| 匹配网点名称 | TMS 匹配的目的地网点 |
| 产品类型 | 融惠达/精准零担/融安达/融速达 |
| 派送方式 | 自提/派送 |
| 重量区间 | 如 0-100kg |
| 总费用 | 该区间内每公斤单价最高时的运费（元） |
| 最高单价对应重量(kg) | 产生最高每公斤单价的实际测试重量 |
| 测试地址示例 | 查询时使用的完整地址 |

---

## 2026-03-03 Bug 修复记录

### 问题：133 个区县数据错误

排查发现 3 类系统性错误：

| 类型 | 数量 | 根因 | 修复方式 | 结果 |
|------|------|------|---------|------|
| RC1：同名区县错配 | 16 区县 | `address_database.json` 中高德POI搜索同名区县时返回了其他城市的地址（如海口龙华区存了深圳龙华区地址） | 修正 address_database.json 地址 + `repair_mismatch.py` 重跑 | 14 修复 / 2 TMS盲区 |
| RC2：地址库错误 | 17 区县 | `address_database.json` 中特殊行政区划的地址为"北京市西城区..."（高德POI错误） | `repair_mismatch.py` 调用高德 API 重新获取正确地址 | 17 全部修复 |
| RC3：处理异常 | 100 区县 | 岳阳/深圳/佛山等 15 个城市 TMS API 批量返回异常 | `repair_mismatch.py` 重新请求 TMS API | 100 全部修复 |

### RC1 修复结果

| 省份 | 城市 | 区/县 | 修复前（错误） | 修复后（正确） |
|------|------|-------|--------------|--------------|
| 江西 | 南昌 | 西湖区 | 杭州西湖站 | 南昌象湖站 |
| 辽宁 | 沈阳 | 和平区 | 天津南开站 | 沈阳和平站 |
| 浙江 | 舟山 | 普陀区 | 上海普陀站 | 宁波舟山普陀站 |
| 贵州 | 贵阳 | 白云区 | 广州白云机场站 | 贵阳沙文站 |
| 陕西 | 西安 | 长安区 | 石家庄长安站 | 西安高新站 |
| 海南 | 海口 | 龙华区 | 深圳北站S站 | 地址解析失败（TMS不覆盖海口） |
| 山西 | 晋城 | 城区 | 大同站 | 网点盲区（TMS无晋城派送站） |
| 山西 | 阳泉 | 城区 | 大同站 | 地址解析失败（TMS不覆盖阳泉） |

### 代码修复

- `get_price.py`：新增 `_filter_best_destination(dest_candidates, addr_info)` 函数，城市名匹配→省份匹配→回退 `[0]`（防止未来同名问题）
- `batch_run.py`：`_fetch_one()` 中 `dest_candidates[0]` → `_filter_best_destination(dest_candidates, addr_info)`
- `weight_scan.py`：`_resolve_address()` 中同上修改
- `repair_mismatch.py`：定向修复脚本，识别 + 修复 + 重跑受影响区县
- `address_database.json`：修正 16 个 RC1 + 17 个 RC2 = 33 个错误地址

---

## 上游依赖

- 读取 **模块一（01_高德地址获取）** 的 `address_database.json`

## 下游依赖

- 输出的 `全国省市县TMS价格表.xlsx` 被 **模块三（03_财务汇总图表）** 的 `生成价格表.py` 读取
