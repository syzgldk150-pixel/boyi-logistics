---
module: 价格获取
type: 模块文档
tags: [高德API, POI搜索, 地址库, 断点续传]
related: [[02-tms-price-fetch], [project_structure]]
status: active
updated: 2026-03-06
---

# 模块一：高德地图 API 地址获取

> 调用高德地图 POI 搜索 API，为全国 3056 个区/县查找真实地址，构建地址库。

---

## 模块职责

为 TMS 价格查询提供真实地址数据。通过高德 POI 搜索"中国农业银行"，获取每个区/县的真实街道地址，写入 `address_database.json`。

## 当前状态：已完成

- 已处理：3056 条
- 成功：3054 条
- 失败（未找到 POI）：2 条

---

## 文件说明

| 文件 | 类型 | 说明 |
|------|------|------|
| `update_addresses_amap.py` | 脚本 | 主入口，执行高德 POI 搜索并更新地址库 |
| `address_database.json` | 数据 | 全国 3056 条地址，字段：`province`/`city`/`district`/`address` |
| `amap_progress.json` | 数据 | 断点续传进度：`done_indices` + `failed_indices` |
| `amap_fetch.log` | 日志 | 运行日志（追加写入） |

---

## 运行命令

```bash
cd "C:/Users/Administrator/Desktop/price_scripts/scripts/01_amap_address_fetch"
python update_addresses_amap.py >> amap_fetch.log 2>&1
```

---

## 配置参数

| 参数 | 值 | 说明 |
|------|----|------|
| `SLEEP_BETWEEN` | 0.4s | 每次 API 调用间隔（约 2.5 QPS） |
| `SAVE_INTERVAL` | 50 | 每 50 条保存一次进度 |
| `MAX_RETRY` | 3 | 单条最大重试次数 |
| `RATELIMIT_WAITS` | [60, 120, 180]s | 限流等待递增时间 |

---

## 敏感信息

| 变量名 | 用途 |
|--------|------|
| `AMAP_API_KEYL` | 高德地图 API Key（从项目根目录 `.env` 读取） |

---

## 核心逻辑

### 搜索策略（双重兜底）

1. **Step 1**：以区/县名为 `city` 参数搜索高德 POI（中国农业银行）
   - 优先选 `adname` 精确匹配的结果
   - 无精确匹配则从全部结果随机选取
2. **Step 2**：若 Step1 无结果，以地级市名搜索后过滤 `adname == district`
3. **均无结果**：保留原模板地址，记入 `failed_indices`

### 特殊处理

- **直辖市**（北京/天津/上海/重庆）：`city` 字段为"市辖区"，自动改用省份名搜索

### 限流处理

- HTTP 429/5xx 或高德 infocode `10003`/`10009`/`10014`/`10044` 触发等待
- 递增等待：60s → 120s → 180s

### 断点续传

- 进度保存在 `amap_progress.json`
- 重启后自动跳过已完成索引

---

## 输出数据格式

`address_database.json` 单条示例：
```json
{
  "province": "湖南省",
  "city": "邵阳市",
  "district": "双清区",
  "address": "邵阳市双清区昭陵西路183号"
}
```

---

## 下游依赖

此模块输出的 `address_database.json` 被 **模块二（02_tms_price_fetch）** 的 `batch_run.py` 读取。
