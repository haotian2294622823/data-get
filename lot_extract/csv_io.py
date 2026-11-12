"""原始 CSV 校验、宽表规模检查和 CSV 写出。"""

from __future__ import annotations

import csv
import re
from collections.abc import Callable, Hashable
from pathlib import Path

from .schema import TABLES, ExportSizeError, pass_text, validate_columns


# CSV 中所有值保留为字符串，避免 LOT 前导零、编号格式和原始小数表示被改写。
Row = dict[str, str]
# 仅用于判断 Excel 公式样式文本是否其实是合法数字，不参与数值汇总。
NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


def read_tables(
    base: Path, sources: tuple[str, ...], prefix: str = "", *,
    pass_order_key: Callable[[str], Hashable] = pass_text,
) -> tuple[dict[str, list[str]], dict[str, list[Row]]]:
    """读取指定原始表，返回按表名索引的（表头，记录列表）。

    检查必要字段、行宽、LOT 非空和候选键唯一性。键冲突时直接报错，
    不通过“取第一条”或“相加”猜测含义，避免真实记录在合并前丢失。
    prefix 只区分系统/前工程文件名，返回字典仍使用原始表名作键。
    明细能否关联到批次和工序，由后面的 merge_records 进一步检查。
    """
    headers, tables = {}, {}
    for source in sources:
        keys = TABLES[source][1]
        with (base / f"{prefix}{source}.csv").open(encoding="utf-8-sig", newline="") as stream:
            # utf-8-sig 同时兼容有/无 BOM 的 UTF-8 文件；newline="" 由 csv 管理换行。
            reader = csv.DictReader(stream)
            fields = reader.fieldnames or []
            validate_columns(source, fields)
            rows = list(reader)
        seen = set()
        for row in rows:
            # DictReader 遇到多余列会产生 None 键，缺列会产生 None 值，均不能继续合并。
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"{source} CSV 行与表头不一致")
            if not row["lot_no"]:
                raise ValueError(f"{source} LOT 为空，无法确定归属")
            key = tuple(
                # 道次先归一化，避免把 1 与 1.0 错认成两个独立事件。
                pass_order_key(row[k]) if k == "pass_order" else row[k] for k in keys
            )
            if key in seen:
                raise ValueError(
                    f"{source} 候选键重复，请核实事件键；不能取第一条或合计"
                )
            seen.add(key)
        headers[source], tables[source] = fields, rows
    return headers, tables


def check_shape(width: int, height: int, max_columns: int | None, max_cells: int | None) -> None:
    """检查宽表列数和总单元格数；None 表示调用方未启用对应限制。

    即使当前用稀疏字典保存行，CSV 仍会输出完整矩形，空单元格也计入规模。
    """
    if (max_columns is not None and width > max_columns) or (
        max_cells is not None and width * height > max_cells
    ):
        raise ExportSizeError("合并宽表超出大小上限，请缩小查询范围。")


def spreadsheet_text(value: str) -> str:
    """把疑似 Excel 公式的文本按普通文字输出，合法正负数字保持原样。

    只在输出宽表时处理；原始 CSV 保留数据库值。例如 =ABC 前加单引号，-2 不变。
    """
    candidate = value.lstrip()
    if candidate.startswith(("=", "+", "-", "@")) and not NUMBER.fullmatch(candidate):
        return "'" + value
    return value


def write_wide(stream, columns: list[str], rows: list[Row]) -> None:
    """按统一表头顺序写出宽表，某 LOT 没有的字段填空。

    stream 可以是手动工具的文本文件，也可以是 service.BoundedCsv；
    具体编码和文件大小控制由调用方负责，本函数只负责 CSV 格式与公式样式文本处理。
    """
    writer = csv.writer(stream)
    writer.writerow(map(spreadsheet_text, columns))
    for row in rows:
        writer.writerow(spreadsheet_text(row.get(column, "")) for column in columns)
