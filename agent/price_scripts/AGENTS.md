# price_scripts

## 目录职责

`price_scripts/` 负责地址库维护、TMS 批量报价、报价表生成，是价格能力的底层业务目录。

## 修改入口

- 改 Agent 暴露给外部的价格工具：
  - `../tools/price_tool.py`
- 改 TMS 直连查询逻辑：
  - `../tools/tms_tool.py`
- 改底层批量报价管道：
  - `scripts/run_pipeline.py`
  - `docs/price_scripts/02-tms-price-fetch.md`
  - `docs/price_scripts/03-quote-sheet-generation.md`

## 说明

- 如果只是飞书机器人里的报价接口报错，通常先查 `../tools/price_tool.py`，不要先全扫本目录。
- 本目录的详细业务规则以 `../docs/price_scripts/` 为准。

## 相关文档

- `../docs/code_navigation_index.md`
- `CLAUDE.md`
