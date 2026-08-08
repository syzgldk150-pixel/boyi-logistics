# 财务对账

## 目录职责

`finance_reconciliation/` 负责财务 ETL、抽取器、对账逻辑、报表生成，是 `finance_etl` 的底层业务目录。

## 修改入口

- 改 Agent 财务工具入口：
  - `../tools/finance_tool.py`
- 改 ETL 主流程：
  - `etl/main.py`
  - `etl/reconcile.py`
  - `etl/report.py`
- 改共享配置与财务精度：
  - `etl/shared/config.py`
  - `etl/shared/decimal_utils.py`
  - `etl/shared/validators.py`

## 说明

- 只改 Agent 调用参数时，先查 `../tools/finance_tool.py`
- 改具体财务口径时，再进入 `etl/`

## 相关文档

- `../docs/code_navigation_index.md`
- `CLAUDE.md`
