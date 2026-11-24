"""Daily send-data workbooks kept on the NAS: build, read back and merge.

One workbook per platform and send date. The plugin supplies the columns and
rows of its own snapshot; when a workbook is shared by several accounts of the
same platform (Ronghui 大祥站 and 大祥S站), each account owns the rows whose
``来源网点`` cell is its own site and replaces only those. Pure functions: no
environment, no network.
"""
from __future__ import annotations

import io
import math
from typing import Sequence

from openpyxl import Workbook, load_workbook

SITE_COLUMN = "来源网点"
SHEET_TITLE = "寄件数据"
MAX_COLUMNS = 64
MAX_ROWS = 20_000
MAX_CELL_TEXT = 2_000

Cell = str | int | float | None


class WorkbookError(ValueError):
    """The workbook content or the submitted table is not acceptable."""


def cell_text(value: object) -> str:
    """The comparable text of one cell; numbers compare by value, empty by ''."""

    if value is None:
        return ""
    if isinstance(value, bool):
        raise WorkbookError("表格单元格不能是布尔值。")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise WorkbookError("表格数字无效。")
        return str(int(value)) if value.is_integer() else repr(value)
    return str(value)


def check_table(columns: Sequence[object], rows: Sequence[object]) -> tuple[list[str], list[list[Cell]]]:
    if not isinstance(columns, (list, tuple)) or not columns or len(columns) > MAX_COLUMNS:
        raise WorkbookError("表头无效。")
    names: list[str] = []
    for name in columns:
        if not isinstance(name, str) or not name.strip() or name != name.strip() or len(name) > 64:
            raise WorkbookError("表头无效。")
        names.append(name)
    if len(set(names)) != len(names):
        raise WorkbookError("表头有重复列。")
    if not isinstance(rows, (list, tuple)) or len(rows) > MAX_ROWS:
        raise WorkbookError("表格行数超出上限。")
    checked: list[list[Cell]] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != len(names):
            raise WorkbookError("表格行与表头列数不一致。")
        cells: list[Cell] = []
        for value in row:
            if isinstance(value, bool) or not (value is None or isinstance(value, (str, int, float))):
                raise WorkbookError("表格单元格类型无效。")
            if isinstance(value, float) and not math.isfinite(value):
                raise WorkbookError("表格数字无效。")
            if isinstance(value, str) and len(value) > MAX_CELL_TEXT:
                raise WorkbookError("表格单元格过长。")
            cells.append(None if value == "" else value)
        checked.append(cells)
    return names, checked


def merge_site_rows(
    existing: tuple[list[str], list[list[Cell]]] | None,
    *,
    columns: list[str],
    rows: list[list[Cell]],
    site: str | None,
) -> tuple[list[str], list[list[Cell]]]:
    """The workbook after replacing this submission's rows.

    Without ``site`` the submission is the whole workbook. With ``site`` the
    first column is ``来源网点``; rows of other sites are kept (re-ordered into
    the submitted columns by header name, missing cells empty) and placed
    before or after this site's rows in site-name order.
    """

    if site is None:
        return list(columns), [list(row) for row in rows]
    if not isinstance(site, str) or not site.strip() or len(site) > 64:
        raise WorkbookError("来源网点无效。")
    if SITE_COLUMN in columns:
        raise WorkbookError("来源网点列由系统填写。")
    final_columns = [SITE_COLUMN, *columns]
    by_site: dict[str, list[list[Cell]]] = {site: [[site, *row] for row in rows]}
    if existing is not None:
        old_columns, old_rows = existing
        if SITE_COLUMN not in old_columns:
            raise WorkbookError("NAS 上的表格缺少“来源网点”列，不能按网点合并；请先移走这个文件。")
        site_index = old_columns.index(SITE_COLUMN)
        positions = [old_columns.index(name) if name in old_columns else None for name in columns]
        for old in old_rows:
            owner = cell_text(old[site_index]).strip()
            if not owner:
                raise WorkbookError("NAS 上的表格有一行没有来源网点，不能按网点合并。")
            if owner == site:
                continue
            by_site.setdefault(owner, []).append(
                [owner, *(None if index is None else old[index] for index in positions)])
    merged = [row for name in sorted(by_site) for row in by_site[name]]
    if len(merged) > MAX_ROWS:
        raise WorkbookError("合并后的表格行数超出上限。")
    return final_columns, merged


def build_workbook(columns: list[str], rows: list[list[Cell]]) -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = SHEET_TITLE
    sheet.append(columns)
    for row in rows:
        sheet.append([None] * len(columns))
        line = sheet.max_row
        for index, value in enumerate(row, start=1):
            cell = sheet.cell(row=line, column=index)
            cell.value = value
            if isinstance(value, str):
                cell.data_type = "s"  # text starting with "=" stays text, never a formula
    sheet.freeze_panes = "A2"
    for index, name in enumerate(columns, start=1):
        widest = max([len(name), *(len(cell_text(row[index - 1])) for row in rows[:500])])
        sheet.column_dimensions[sheet.cell(row=1, column=index).column_letter].width = min(max(widest * 1.6, 8), 60)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def read_workbook(content: bytes) -> tuple[list[str], list[list[Cell]]]:
    try:
        book = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:
        raise WorkbookError("NAS 上的表格无法打开。") from exc
    try:
        sheet = book[SHEET_TITLE] if SHEET_TITLE in book.sheetnames else book.worksheets[0]
        lines = [list(line) for line in sheet.iter_rows(values_only=True)]
    finally:
        book.close()
    while lines and all(value in (None, "") for value in lines[-1]):
        lines.pop()
    if not lines:
        raise WorkbookError("NAS 上的表格是空的。")
    header = lines[0]
    while header and header[-1] in (None, ""):
        header.pop()
    columns = [cell_text(name).strip() for name in header]
    if not columns or any(not name for name in columns) or len(set(columns)) != len(columns):
        raise WorkbookError("NAS 上的表格表头无效。")
    rows: list[list[Cell]] = []
    for line in lines[1:]:
        cells = (line + [None] * len(columns))[: len(columns)]
        if any(value not in (None, "") for value in line[len(columns):]):
            raise WorkbookError("NAS 上的表格有超出表头的数据。")
        rows.append([value if isinstance(value, (str, int, float)) and not isinstance(value, bool) and value != ""
                     else (None if value in (None, "") else cell_text(value)) for value in cells])
    return columns, rows


def same_table(left: tuple[list[str], list[list[Cell]]], right: tuple[list[str], list[list[Cell]]]) -> bool:
    if left[0] != right[0] or len(left[1]) != len(right[1]):
        return False
    return all([cell_text(value) for value in a] == [cell_text(value) for value in b]
               for a, b in zip(left[1], right[1]))


__all__ = [
    "SITE_COLUMN", "WorkbookError", "build_workbook", "cell_text", "check_table",
    "merge_site_rows", "read_workbook", "same_table",
]
