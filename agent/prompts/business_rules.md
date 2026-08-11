# 业务规则

## TMS 系统

- TMS 全称"融辉运输管理系统"，是公司核心业务系统
- 运单号格式通常以 YS 开头
- Agent 在 `127.0.0.1:9000` 暴露版本化的 `/internal/v1/tms/*` 自动化接口

## Agent TMS 可用端点

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

- 报价通过 Agent `/internal/v1/tms/get_price` 查询；如果只有发站/到站信息，可使用该接口的兼容参数
- 优先输入完整收货地址 + 重量 + 体积，体积缺省按 0.1m³ 处理
- 涉及金额必须保留两位小数
- 报价结果需说明是否含提货费

## 财务同步规则

- 财务账单通过 `sync_finance_bills` 在线同步到共享账本，不使用已移除的 legacy ETL 管道
- 缺少真实来源、日期或关键字段时必须显式失败，不得用历史值或本地旧数据兜底
- 金额统一使用共享 `Decimal` 规则，并在写入前完成总额、行数和异常值校验
