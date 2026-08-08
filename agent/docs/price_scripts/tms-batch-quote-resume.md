---
module: 价格获取
type: 操作手册
tags: [TMS, 批量报价, 断点续传, 运行命令]
related: [[02-tms-price-fetch]]
status: active
updated: 2026-03-06
---

# TMS 批量报价提取 - 续跑指令

## 脚本位置
```
C:\Users\DENG\Desktop\agent\price_scripts\scripts\02_tms_price_fetch\
```

## 续跑命令
直接在 Claude Code 对话中说「运行续跑命令」，或手动执行：

```bash
cd "C:/Users/DENG/Desktop/price_scripts/scripts/02_tms_price_fetch" && python -u batch_run.py >> batch_run.log 2>&1
```

## 断点续传说明
- 脚本**自动支持断点续传**，无需任何额外参数
- 进度保存在：`batch_progress.json`（记录已完成的地址索引 + 结果）
- 每处理 10 条地址自动保存一次进度
- 重启后脚本读取 `batch_progress.json`，自动跳过已完成项，从断点继续

## 关键文件
| 文件 | 位置 | 说明 |
|------|------|------|
| `batch_run.py` | `scripts/02_tms_price_fetch/` | 主脚本 |
| `address_database.json` | `scripts/01_amap_address_fetch/` | 待处理的地址列表（共 3056 条）|
| `batch_progress.json` | `scripts/02_tms_price_fetch/` | 断点续传进度文件（自动生成）|
| `TMS_Price_Matrix.xlsx` | `scripts/02_tms_price_fetch/` | 输出结果文件（每10条自动更新）|
| `batch_run.log` | `scripts/02_tms_price_fetch/` | 运行日志（追加写入）|

## 检查进度
运行后可查看日志中的 `[Save]` 行：

```bash
cat "C:/Users/DENG/Desktop/price_scripts/scripts/02_tms_price_fetch/batch_run.log" | grep -a "Save" | tail -5
```

格式示例：
```
[Save] 1480/3056  速度: 98条/h  预计剩余: 937min
```

## 运行参数（脚本内配置）
| 参数 | 值 | 说明 |
|------|----|------|
| `SLEEP_ADDR` | 3 秒 | 地址间隔，避免限流 |
| `SLEEP_WEIGHT` | 0.8 秒 | 同地址不同重量区间间隔 |
| `SLEEP_RATELIMIT` | 60 秒 | 遇到限流后等待时间 |
| `RELOGIN_INTERVAL` | 80 条 | 每处理 N 条主动重登一次 |
| `SAVE_INTERVAL` | 10 条 | 每 N 条保存一次进度 |
| `MAX_RETRY` | 3 次 | 单地址最大重试次数 |

## 注意事项
- 运行前确认 `batch_progress.json` 存在（若缺失则从头开始）
- 若 `TMS_Price_Matrix.xlsx` 被 Excel 打开，脚本会自动另存为带时间戳的文件
- 日志为**追加模式**，历史运行记录会保留在 `batch_run.log` 中
