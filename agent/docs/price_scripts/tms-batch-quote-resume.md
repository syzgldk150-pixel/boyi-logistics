---
module: 价格获取
type: 操作手册
tags: [TMS, 批量报价, 断点续传, 运行命令]
related: [02-tms-price-fetch.md]
status: superseded
captured_at: 2026-03-06
updated: 2026-08-30
---

# TMS 批量报价提取 - 已退役续跑记录

> 本操作手册已失效。`batch_run.py`、进度文件、输出表和原业务数据均不在当前仓库，不得按历史环境续跑。以下内容只保留当时断点机制的可追溯说明。

## 原脚本状态

旧脚本位置和续跑命令已移除，当前没有可执行入口。需要恢复时必须从受审历史提交重建独立离线环境，并重新验证页面、账号、限流、进度文件和输出数据。

## 历史断点续传机制
- 旧脚本曾自动支持断点续传
- 进度曾保存在 `batch_progress.json`（记录已完成的地址索引和结果）
- 当时每处理一批地址保存一次，重启后跳过已完成索引

## 关键文件
| 文件 | 位置 | 说明 |
|------|------|------|
| `batch_run.py` | `scripts/02_tms_price_fetch/` | 主脚本 |
| `address_database.json` | `scripts/01_amap_address_fetch/` | 待处理的地址列表（共 3056 条）|
| `batch_progress.json` | `scripts/02_tms_price_fetch/` | 断点续传进度文件（自动生成）|
| `TMS_Price_Matrix.xlsx` | `scripts/02_tms_price_fetch/` | 输出结果文件（每10条自动更新）|
| `batch_run.log` | `scripts/02_tms_price_fetch/` | 运行日志（追加写入）|

## 历史日志格式

旧日志中的保存进度格式示例：
```
[Save] 1480/3056  速度: 98条/h  预计剩余: 937min
```

## 历史参数快照
| 参数 | 值 | 说明 |
|------|----|------|
| `SLEEP_ADDR` | 3 秒 | 地址间隔，避免限流 |
| `SLEEP_WEIGHT` | 0.8 秒 | 同地址不同重量区间间隔 |
| `SLEEP_RATELIMIT` | 60 秒 | 遇到限流后等待时间 |
| `RELOGIN_INTERVAL` | 80 条 | 每处理 N 条主动重登一次 |
| `SAVE_INTERVAL` | 10 条 | 每 N 条保存一次进度 |
| `MAX_RETRY` | 3 次 | 单地址最大重试次数 |

## 历史行为说明
- 旧实现缺少进度文件时会从头开始
- 输出表被 Excel 占用时曾改写到带时间戳的新文件
- 旧日志使用追加模式
