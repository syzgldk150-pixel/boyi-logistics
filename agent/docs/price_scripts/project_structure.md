---
module: 价格获取
type: 项目结构
tags: [目录树, 脚本说明, 数据流, 业务流程, 产品规则]
related: [[01-amap-address-fetch], [02-tms-price-fetch], [03-quote-sheet-generation]]
status: active
updated: 2026-06-03
---

# project_structure

> 本文件由 Claude Code 自动生成，供快速了解各脚本职责与项目架构使用。

---

## 目录树

> 项目按业务流程拆分为三个模块，存放在 `scripts/` 下对应子目录中。

```
price_scripts/
├── .env                                    # 敏感信息（账号/密码/API Key）
├── CLAUDE.md                               # Claude Code 指令文件（规则、敏感信息规范）
├── project_structure.md                          # 本文件：项目架构与脚本说明
├── tms-batch-quote-resume.md                   # 续跑命令与运行参数说明
├── 网页自动化与数据提取 Agent 执行指令.md    # 初始创建逻辑（项目背景）
├── 财务与数据相关任务基准逻辑.md            # 财务数据分析基准规范（Decimal精度、自验证等）
│
├── 分拨信息地址最新版7.1.xlsx                # 分拨中心信息（59条，补充自提部报价输入）
│
├── 输出结果/                                # ★ 最终输出目录
│   ├── 全国省市县TMS价格表.xlsx            # 多Sheet单价分析Excel（7Sheet + 6图表）
│   ├── 全国明细单价表.xlsx                 # 最低单价宽表（派送/自提各2415行）
│   ├── 客户报价表.xlsx                    # 物流频次报价表（含自提部行；当前成品价格已含 +0.15 元/kg，且可被 liveTMS 审计结果按同口径回写）
│   ├── 物流高频目的地库.json              # 各省高频目的地地址库
│   ├── 指定网点价格表.xlsx                # 指定网点报价结果（由batch_run_by_site生成）
│   ├── 全国报价表(省城版).xlsx            # 省城版报价表（仅省会城市区级，自提/派送）
│   └── 价格分析图表/                        # 8张PNG分析图表
│       ├── 01_各省中位单价排名.png
│       ├── 02_各省单价箱线图.png
│       ├── 03_重量区间单价趋势.png
│       ├── 04_产品派送方式单价对比.png
│       ├── 05_省重量单价热力图.png
│       ├── 06_派送vs自提溢价率.png
│       ├── 省重量最低单价热力图_自提.png
│       └── 省重量最低单价热力图_派送.png
│
└── scripts/
    ├── 01_高德地址获取/                      # 模块一：高德地图API地址获取
    │   ├── update_addresses_amap.py        # ★ 高德POI搜索：为地址库补充真实地址（已完成）
    │   ├── address_database.json           # 全国3056个区/县地址库（核心数据）
    │   ├── amap_progress.json              # 高德地址获取断点续传进度
    │   └── amap_fetch.log                  # 高德地址获取运行日志
    │
    ├── 02_tms_price_fetch/                       # 模块二：TMS系统价格获取
    │   ├── batch_run.py                    # ★ 主脚本：TMS批量报价提取（已完成 2026-03-02，97475行）
    │   ├── batch_run_by_site.py            # ★ 指定网点报价：直接输入网点名称获取价格
    │   ├── repair_mismatch.py              # ★ 定向修复：同名区县错配/地址库错误/处理异常
    │   ├── weight_scan.py                  # 逐公斤扫描工具（价格阶梯分析）
    │   ├── get_price.py                    # ★ 核心API封装：地址解析、价格计算
    │   ├── login_manager.py                # ★ 登录管理器：OCR识别验证码
    │   ├── config.json                     # TMS请求头配置
    │   ├── batch_progress.json             # TMS批量报价断点续传进度
    │   ├── 全国省市县TMS价格表.xlsx         # ★ 完整报价数据源（97475行）
    │   └── batch_run.log                   # 批量报价运行日志（追加写入）
    │
    └── 03_财务汇总图表/                      # 模块三：财务汇总与图表
        ├── 生成价格表.py                    # ★ 单价分析脚本：生成多Sheet Excel + 分析图表
        ├── 生成明细单价表.py                # ★ 最低单价宽表：每区县每重量段取各产品最低单价
        ├── 生成物流频次报价表.py            # ★ 物流频次报价表：高频目的地TOP10/省，7→3合并，+0.15利润
        ├── 补充自提部报价.py               # ★ 补充自提部：省会自提部TMS报价→插入客户报价表
        ├── 生成重量单价热力图.py            # ★ 省×重量段最低单价热力图（派送/自提各一张）
        ├── 生成客户报价表.py               # ★ 客户报价表：省级P65+利润，偏远区县附加报价
        └── 生成省城报价表.py               # ★ 省城版报价表：仅省会城市区级，P65+利润
```

---

## 脚本说明

### 生成价格表.py（`scripts/03_finance_summary_charts/`）— 单价分析与图表生成

**职责**：读取 `scripts/02_tms_price_fetch/全国省市县TMS价格表.xlsx` 报价数据，计算每公斤单价（总费用 ÷ 计算重量），生成包含7个Sheet的分析Excel和6张可视化图表。

**遵循规范**：`财务与数据相关任务基准逻辑.md`（Decimal精度、向上进位、自验证校验）。

**输出Excel Sheet**：
1. 使用说明 — 数据来源、重量区间对照、阅读指南
2. 省级单价汇总 — 31省的单价统计概览（按中位单价升序）
3. 区县单价汇总 — 全国各区/县的单价统计
4. 省×重量中位单价矩阵 — 融速达·自提模式下各省×重量区间中位单价（带颜色渐变）
5. 产品维度单价表 — 按产品类型×派送方式×重量区间的全国单价统计
6. 全国明细单价表 — 每个区/县每种组合的完整单价
7. 地址解析失败清单

**输出图表**（`输出结果/价格分析图表/`目录）：
- 01_各省中位单价排名.png — 横向柱状图
- 02_各省单价箱线图.png — 单价分布箱线图
- 03_重量区间单价趋势.png — 四产品×自提/派送趋势线
- 04_产品派送方式单价对比.png — 0-100kg基准对比
- 05_省重量单价热力图.png — 融速达·自提热力图
- 06_派送vs自提溢价率.png — 各省派送溢价率

**运行命令**：
```bash
cd "C:/Users/DENG/Desktop/price_scripts/scripts/03_finance_summary_charts" && python 生成价格表.py
```

---

### 生成客户报价表.py（`scripts/03_finance_summary_charts/`）— 客户报价表生成

**职责**：读取 `输出结果/全国明细单价表.xlsx`，将 7 个重量段合并为 4 段，通过两轮筛选（省中位数×1.10 识别偏远、剔除后 P65 定省价）生成简化版客户报价表。

**核心参数**：
| 参数 | 值 | 说明 |
|------|------|------|
| `OUTLIER_RATIO` | 1.10 | 区县成本 > 省中位数 × 1.10 视为该段超标 |
| `min_exceed_count` | 2（≥2/4段） | 半数以上重量段超标才判定偏远 |
| `PROVINCE_PERCENTILE` | 65 | 剔除偏远后省级成本取 P65 |
| `PROFIT_MARGIN` | 0.15 元/kg | 成本上加利润 |

**偏远判定逻辑（已修复 2026-03-03）**：
- 逐区县检查合并后的 4 个重量段是否 > 省中位数 × 1.10
- **≥2/4 段超标**才判定偏远（旧逻辑为任意 1 段即判定，会产生误报）
- 修复后异常记录从 1609 → 1395（减少 214 条误报）

**输出 Excel**（4 个 Sheet）：
1. 自提报价 — 31 省级报价（P65 成本 + 0.15 利润）
2. 派送报价 — 同上
3. 偏远区县附加报价_自提 — 偏远区县自提单独报价（成本 + 利润）
4. 偏远区县附加报价_派送 — 偏远区县派送单独报价（成本 + 利润）

**运行命令**：
```bash
cd "C:/Users/DENG/Desktop/price_scripts/scripts/03_finance_summary_charts" && python 生成客户报价表.py
```

---

### 生成物流频次报价表.py（`scripts/03_finance_summary_charts/`）— 物流频次客户报价表

**职责**：从勇胜物流托运单提取各省高频目的地 TOP10，匹配全国明细单价表（自提），7 段合并为 3 段，加 0.15 元/kg 利润，输出客户报价表。

**核心逻辑**：
1. 从物流托运单提取历史目的地，统计各省高频市县（TOP10）
2. 匹配全国明细单价表（自提），优先区/县精确匹配 → 城市级匹配 → 回退到原始报价（含服务大厅）
3. 7 段合并 3 段（去掉 3001+kg）：101-300kg / 301-800kg / 801-3000kg，取子段 max
4. +0.15 元/kg 利润，Decimal ROUND_CEILING

**输出**：`输出结果/客户报价表.xlsx`（1 Sheet「自提报价(按物流频次)」，6 列：省份/目的地/匹配区域/101-300kg/301-800kg/801-3000kg）

**口径说明**：该文件中的价格列已经是“成本单价 + 0.15 元/kg 利润”后的客户报价，不是裸成本价。

---

### liveTMS 审计与回写（`scripts/03_finance_summary_charts/`）

**职责**：`审计客户报价表_live_tms.py` 按客户报价表行回查 live TMS；`回写客户报价表_live_tms.py` 将审计结果中的 `liveTMS_*` 列按 Excel 行号回写到 `客户报价表.xlsx`。

**口径说明**：
- `liveTMS_*` 列与 `客户报价表.xlsx` 一样，已经加计 `+0.15 元/kg`
- 回写动作是“同口径刷新客户报价”，不是把客户报价替换成未加利润的成本价

---

### 补充自提部报价.py（`scripts/03_finance_summary_charts/`）— 省会自提部报价补充

**职责**：从分拨信息表读取省会自提部名称，调用 TMS API 获取报价，处理后插入到客户报价表每省第一行之前。

**核心逻辑**：
1. 读取 `分拨信息地址最新版7.1.xlsx`（B 列=分拨名称），通过 `PROVINCE_DEPOT_MAP` 映射 27 省→31 个现役自提部
2. 对每个 "{分拨名称}自提部" 调用 TMS API（复用 `batch_run_by_site._login/_fetch_one_by_site`）
3. 价格处理：自提筛选→排除 0-100kg→单价计算→每重量段取产品最低→7→3 合并→+0.15→ROUND_CEILING
4. 打开现有客户报价表，检测已有自提部行防重复，从下往上 `insert_rows()` 插入

**省份→分拨映射**（特殊情况）：
- 北京→[北京通州, 北京房山]（2 行）
- 上海→[浦西]（浦东自提部已撤销，不再纳入任务）
- 广东→[虎门, 中山, 潮汕, 深圳]（佛山自提部已撤销，不再纳入任务）
- 跳过：青海/西藏/新疆/海南

**运行结果**（2026-03-16）：31 个现役自提部中 31 个成功获取价格并插入，价格范围 0.28～0.91 元/kg

**运行命令**：
```bash
cd "C:/Users/DENG/Desktop/price_scripts/scripts/03_finance_summary_charts" && python 补充自提部报价.py
```

---

### batch_run_by_site.py（`scripts/02_tms_price_fetch/`）— 指定网点批量报价

**职责**：直接指定网点名称获取 TMS 价格，跳过地址解析步骤。适用于自提部等无派送区域的网点。

**核心机制**：
- 三级回退搜索：直接搜索网点名 → 城市前缀搜索 → 逐候选深度匹配 dispatch_site_name
- 断点续传：`site_batch_progress.json`
- 被 `补充自提部报价.py` 导入 `_login()` 和 `_fetch_one_by_site()`

**输入**：同目录 `指定网点查询.xlsx`（需含"网点名称"列）
**输出**：`输出结果/指定网点价格表.xlsx`

---

### batch_run.py — 主执行脚本（已完成）

**职责**：读取 `address_database.json`，对每条地址调用 TMS 系统 API，提取 8 个重量区间 × 多产品 × 自提/派送的报价，结果写入 `TMS_Price_Matrix.xlsx`。

**关键机制**：
- 断点续传：进度保存在 `batch_progress.json`，每 10 条保存一次
- 自动重登：每处理 80 条主动刷新 Session，防止超时
- 限流处理：遇到 429/502/503 自动等待 60s 后重试（最多 3 次）
- 连续失败：连续 3 条异常自动触发重登

**运行参数**（脚本内配置）：

| 参数                 | 值    | 说明                                  |
| ------------------ | ---- | ----------------------------------- |
| `WEIGHT_INTERVALS` | 8 档  | 0-100kg / 101-150kg / ... / 3001+kg |
| `SLEEP_ADDR`       | 3s   | 地址间隔，避免限流                           |
| `SLEEP_WEIGHT`     | 0.8s | 同地址不同重量区间间隔                         |
| `RELOGIN_INTERVAL` | 80 条 | 每 N 条主动重登一次                         |
| `SAVE_INTERVAL`    | 10 条 | 每 N 条保存进度                           |

**续跑命令**：
```bash
cd "C:/Users/DENG/Desktop/price_scripts/scripts/02_tms_price_fetch" && python -u batch_run.py >> batch_run.log 2>&1
```

---

### get_price.py — TMS API 核心封装

**职责**：封装所有与 TMS 系统（`https://tms.ronghuiwl.com`）通信的 HTTP 请求逻辑。

**主要函数**：

| 函数                            | 说明                                                                                     |
| ----------------------------- | -------------------------------------------------------------------------------------- |
| `browser_address_resolver.py` | 复用真实“运单录入”页完整地址 blur 解析，读取目的网点、派件网点和省市区字段；无头页面补齐公开登录上下文和地图空实现；地址报价不做拆词兜底匹配 |
| `_post_json_list()`           | POST `/dataQuery/findAllByCallId`，通用 TMS 数据查询                                          |
| `_resolve_destination_code()` | 处理二级网点逻辑，返回有效目的地代码                                                                     |
| `_fetch_send_center()`        | 查询发货中心信息                                                                               |
| `_fetch_destination_center()` | 查询目的地中心（优先 NEW 接口，降级旧接口）                                                               |
| `_calc_price()`               | POST `/dataOperation/batchExecProcedure`，调用存储过程 `P_CALC_CLIENT_PRICE_BILL_SHOW4` 计算总费用 |
| `_build_base_payload()`       | 构造价格计算所需的完整参数体                                                                         |
| `fetch_prices()`              | 对外完整接口：输入地址/重量/体积，返回各产品报价字典                                                            |

**也可命令行单独使用**：
```bash
python get_price.py "浙江省杭州市西湖区文三路100号" 100 0.1
```

---

### login_manager.py — TMS 登录管理器

**职责**：使用 `ddddocr` OCR 库自动识别 TMS 图形验证码，通过 `requests.Session` 完成无浏览器登录，返回携带有效 Cookie 的 Session 对象。

**流程**：
1. 预加载登录页（获取初始 Cookie）
2. 获取验证码图片 `GET /validateCode/code`
3. `ddddocr` 识别验证码文字
4. `POST /system/login` 提交用户名/密码/验证码
5. 验证登录态（访问 `/widget/home` 确认未重定向到登录页）
6. 失败自动重试，最多 6 次

**配置来源**：从 `config.json` 读取 `Origin`、`User-Agent` 等请求头。

---

### update_addresses_amap.py — 高德地址补全（已完成）

**职责**：调用高德地图 POI 搜索 API（`AMAP_API_KEYL`），为 `address_database.json` 中每个区/县查找中国农业银行网点的真实地址，更新 `address` 字段。

**搜索策略**（双重兜底）：
1. 先以区/县名为 city 参数直接搜索
2. 若无结果，改用地级市名搜索后过滤 `adname == district`
3. 均无结果则保留原模板地址，记入 `failed_indices`

**直辖市处理**：北京/天津/上海/重庆的 city 字段为"市辖区"，自动改用省份名搜索。

**限流处理**：遇到高德 infocode 10003/10009/10014/10044 自动等待（60/120/180s 递增）。

**现状**：已完成所有 3056 条，成功 3054 条，2 条未找到，耗时约 2 分钟。

---

## 数据文件说明

| 文件 | 格式 | 说明 |
|------|------|------|
| `01_高德地址获取/address_database.json` | JSON Array | 3056 条地址数据，每条含 `province`/`city`/`district`/`address` |
| `01_高德地址获取/amap_progress.json` | JSON Object | `done_indices` + `failed_indices`（未找到真实地址的索引） |
| `02_tms_price_fetch/batch_progress.json` | JSON Object | `done_indices`(3056) + `results`(97475)（报价结果数据） |
| `02_tms_price_fetch/全国省市县TMS价格表.xlsx` | Excel | 完整报价数据源（97,475 行），2026-03-02 重测完成 |
| `02_tms_price_fetch/config.json` | JSON Object | TMS 请求头（Origin/UA 等），**注意**：账号信息应从 .env 读取，勿硬编码 |
| `输出结果/全国省市县TMS价格表.xlsx` | Excel | 多Sheet单价分析（7Sheet，由 `生成价格表.py` 输出，2026-03-02 更新） |
| `输出结果/全国明细单价表.xlsx` | Excel | 最低单价宽表，派送/自提各2415行（由 `生成明细单价表.py` 输出，2026-03-02 更新） |
| `输出结果/客户报价表.xlsx` | Excel | 物流频次报价表1Sheet：自提报价(按物流频次)，含31行自提部+247个高频目的地（由 `生成物流频次报价表.py` + `补充自提部报价.py` 输出，2026-03-04） |
| `输出结果/物流高频目的地库.json` | JSON | 各省高频目的地地址库（由 `生成物流频次报价表.py` 输出） |
| `输出结果/指定网点价格表.xlsx` | Excel | 指定网点报价结果（由 `batch_run_by_site.py` 输出） |
| `输出结果/全国报价表(省城版).xlsx` | Excel | 省城版报价表2Sheet：仅省会城市区级P65+利润，广东含广州深圳（由 `生成省城报价表.py` 输出，2026-03-03） |
| `分拨信息地址最新版7.1.xlsx` | Excel | 59条分拨中心信息（分拨名称/仓库地址），`补充自提部报价.py` 输入 |
| `输出结果/价格分析图表/` | PNG ×8 | 6张分析图表 + 2张省重量最低单价热力图（2026-03-02 全部更新） |

---

## 业务流程

```
Step 1 ─ 地址库补全（已完成）
  scripts/01_amap_address_fetch/update_addresses_amap.py
    └─ 高德POI搜索各区/县农业银行网点
    └─ 更新 address_database.json 中的 address 字段
    └─ 进度保存：amap_progress.json

Step 2 ─ TMS 批量报价（已完成 2026-03-02，97475行）
  scripts/02_tms_price_fetch/batch_run.py
    ├─ login_manager.py  登录TMS（OCR验证码）→ 获取 Session
    ├─ 读取 01_高德地址获取/address_database.json（3056条）
    └─ 对每条地址调用 get_price.py：
         · browser_address_resolver.py  真实“运单录入”页输入完整地址
         · 读取 DESTINATION_CODE / DISPATCH_UNDERLING_SITE_CODE
         · FIND_CREATE_BILL_DESTINATION  获取网点信息
         · _fetch_send/dest_center()  查询中转中心
         · _fetch_plan_route_name()   查询运输路线
         · 遍历8重量区间 × 产品 × 自提/派送
             └─ P_CALC_CLIENT_PRICE_BILL_SHOW4  计算总费用
    ├─ 每10条保存进度 → batch_progress.json
    └─ 每10条更新输出 → 全国省市县TMS价格表.xlsx

Step 3 ─ 单价分析与可视化（已完成 2026-03-02）
  scripts/03_finance_summary_charts/生成价格表.py
    ├─ 读取 02_tms_price_fetch/全国省市县TMS价格表.xlsx（97475行）
    ├─ 计算单价（总费用 ÷ 计算重量，Decimal向上进位）
    ├─ 自验证校验（区县数、报价总数、反算误差）
    ├─ 输出 输出结果/全国省市县TMS价格表.xlsx（7个Sheet）
    └─ 输出 输出结果/价格分析图表/（6张PNG）

Step 3b ─ 最低单价宽表（已完成 2026-03-02）
  scripts/03_finance_summary_charts/生成明细单价表.py
    ├─ 读取原始报价，排除服务大厅/地址解析失败/0-100kg
    ├─ 每区县每重量段取所有产品中最低单价
    └─ 输出 输出结果/全国明细单价表.xlsx（派送/自提两Sheet）

Step 3c ─ 省×重量最低单价热力图（已完成 2026-03-02）
  scripts/03_finance_summary_charts/生成重量单价热力图.py
    ├─ 读取 输出结果/全国明细单价表.xlsx
    ├─ 按省份取各重量段中位数
    └─ 输出 输出结果/价格分析图表/省重量最低单价热力图_自提.png + _派送.png

Step 3d ─ 物流频次客户报价表（已完成 2026-03-03）
  scripts/03_finance_summary_charts/生成物流频次报价表.py
    ├─ 读取勇胜物流托运单，提取各省高频目的地TOP10
    ├─ 匹配全国明细单价表（自提），含回退到原始报价数据
    ├─ 7段合并3段（去掉3001+kg），取子段max
    ├─ +0.15元/kg利润，Decimal ROUND_CEILING
    └─ 输出 输出结果/客户报价表.xlsx（1Sheet：自提报价(按物流频次)，247个目的地）

Step 3e ─ 补充自提部报价（已完成 2026-03-04）
  scripts/03_finance_summary_charts/补充自提部报价.py
    ├─ 读取分拨信息地址最新版7.1.xlsx → 27省31个现役自提部
    ├─ 调用 TMS API（复用 batch_run_by_site._login/_fetch_one_by_site）
    ├─ 自提筛选→最低单价→7→3合并→+0.15利润→ROUND_CEILING
    ├─ 插入到客户报价表.xlsx每省第一行之前（31行成功，3个TMS未匹配）
    └─ 北京→北京通州+北京房山，上海→浦西，广东→虎门+中山+潮汕+深圳（浦东/佛山已撤销）

Step 3f ─ P65客户报价表（已完成 2026-03-03）
  scripts/03_finance_summary_charts/生成客户报价表.py
    ├─ 读取 输出结果/全国明细单价表.xlsx
    ├─ 7段合并4段（取子段max）
    ├─ 省中位数×1.10识别偏远（≥2/4段超标才判定）
    ├─ 剔除偏远后P65定省级成本 + 0.15元/kg利润
    └─ 输出 输出结果/客户报价表.xlsx（自提报价/派送报价/偏远附加_自提/偏远附加_派送 4Sheet）

Step 3g ─ 省城版报价表（已完成 2026-03-03）
  scripts/03_finance_summary_charts/生成省城报价表.py
    ├─ 读取 输出结果/全国明细单价表.xlsx
    ├─ 仅保留省会城市区级数据（排除县级），广东含广州+深圳
    ├─ 7段合并4段 → 城市中位数×1.10识别偏远区（≥2/4段超标）
    ├─ 剔除偏远后P65定城市价 + 0.15元/kg利润
    └─ 输出 输出结果/全国报价表(省城版).xlsx（自提/派送各1Sheet，31行+广东多1行深圳）
```

---

## 产品与重量区间

**产品列表**（CODE → 名称）：
- `001` 融惠达（仅 >3000kg）
- `002` 精准零担（801kg 以上）
- `003` 融安达（所有区间）
- `004` 融速达（800kg 以下）

**重量区间**：0-100 / 101-150 / 151-300 / 301-350 / 351-800 / 801-1500 / 1501-3000 / 3001+（kg）

**派送方式**：自提 / 派送
