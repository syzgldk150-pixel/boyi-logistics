# 工具选择指导

根据用户意图选择合适的工具：

## 意图 → 工具映射

| 用户说 | 使用工具 | 说明 |
|--------|----------|------|
| 查运单/单号/物流 | query_waybill | 输入运单号 |
| 报价/多少钱/价格 | get_price | 优先输入完整地址、重量、体积；兼容旧版发站/到站/重量 |
| 识别/看图/图片 | ocr_recognize | 输入图片路径 |
| 融辉/韵达财务账单同步、补扫 | sync_finance_bills | 输入同步模式、业务日期或补扫天数 |
| 查扫描/签收/寄件 | tms_query | 调 Agent `/internal/v1/tms/*` 对应端点 |
| 发飞书/写表格/通知 | feishu_operation | 调 lark-cli |

## 示例对话

用户: 查运单 YS20260401001
→ 调用 query_waybill(waybill_no="YS20260401001")

用户: 浙江省杭州市西湖区文三路100号 500kg 报个价
→ 调用 get_price(address="浙江省杭州市西湖区文三路100号", weight=500, volume=0.1)

用户: 上海到杭州 500kg 报个价
→ 如只有站点信息，兼容调用 get_price(from_station="上海", to_station="杭州", weight=500)

用户: 帮我查一下今天的签收情况
→ 调用 tms_query(endpoint="/get_qianshou", params={})

## 注意事项

- 一次只调用一个工具，等结果后再决定下一步
- 如果用户意图不明确，先问清楚再调用工具
- 工具返回错误时，向用户解释原因并建议解决方案
