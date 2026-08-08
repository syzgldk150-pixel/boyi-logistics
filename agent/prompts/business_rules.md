# 业务规则

## TMS 系统

- TMS 全称"融辉运输管理系统"，是公司核心业务系统
- 运单号格式通常以 YS 开头
- http-service 运行在 localhost:8080，提供 TMS 自动化接口

## http-service 可用端点

| 端点 | 功能 |
|------|------|
| /send_order | 查询当日寄件运单列表 |
| /get_scan | 查询扫描记录 |
| /scan_next | 查询下一站扫描 |
| /get_qianshou | 查询签收状态 |
| /delivery_status | 查询投递状态 |
| /clock_in_dual | 网点打卡签到 |
| /get_wangdiansendlist | 查询网点出港清单 |
| /fetch_dispatch | 查询到货调度列表 |
| /child_count | 统计子件数量 |
| /waybill_tracking | 运单轨迹查询 |
| /get_price | TMS 报价查询 |

## 报价规则

- 报价优先通过本地 TMS 价格脚本查询；如果只有发站/到站信息，可兼容走 http-service 的 /get_price
- 优先输入完整收货地址 + 重量 + 体积，体积缺省按 0.1m³ 处理
- 涉及金额必须保留两位小数
- 报价结果需说明是否含提货费

## 对账规则

- 财务 ETL 管道处理大量数据（通常 2 万行以上）
- 运行时间约 30-40 秒
- 属于重任务，同一时间只能运行一个
