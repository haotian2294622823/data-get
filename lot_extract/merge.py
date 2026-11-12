"""离线宽表合并入口，按制造批次信息、工序0、制造工序排列。"""

from __future__ import annotations

from pathlib import Path

from .csv_io import Row, read_tables, write_wide as write_wide
from .manufacturing_merge import merge_records
from .material_merge import merge_and_attach_materials
from .schema import LOT, MACHINE, MATERIAL, TABLES, UPSTREAM_PREFIX, UPSTREAM_TABLES


def merge_tables(
    base_dir: Path | str, *, max_columns: int | None = None, max_cells: int | None = None
) -> tuple[list[str], list[Row], list[str]]:
    """读取一次查询的存档，合并系统记录，再把直接材料的前工程列挂回系统行。

    兼容没有 upstream 文件的旧存档；一旦出现前工程文件，就要求四张全部齐备。
    材料缺批次主记录时，有真实明细的仍保留这些明细。
    返回的 rows 始终以系统 LOT 为行，材料 LOT 只增加列组，不增加系统批次行数。
    """
    base = Path(base_dir)
    upstream_files = [(base / f"{UPSTREAM_PREFIX}{table}.csv").exists() for table in UPSTREAM_TABLES]
    has_upstream = any(upstream_files)
    # 旧手动存档允许只有六表；新存档必须有设备表和四张前工程表，空表也应有表头。
    sources = tuple(
        table for table in TABLES
        if table != MACHINE or has_upstream or (base / f"{MACHINE}.csv").exists()
    )
    headers, tables = read_tables(base, sources)
    limits = {"max_columns": max_columns, "max_cells": max_cells}
    if not has_upstream:
        return merge_records(headers, tables, **limits)
    if not all(upstream_files):
        raise ValueError("前工程 CSV 不完整，请检查本次查询目录。")

    # 第一阶段：只合并制造 LOT；用料原始字段仍保留在制造结果中。
    columns, rows, notices = merge_records(headers, tables, **limits)
    system_columns = columns
    # 材料模块按用料关系找到实际工程，并生成工序0列组。
    columns, upstream_notices = merge_and_attach_materials(
        base, columns, rows, tables[MATERIAL], **limits
    )
    # 批次身份信息在前，工序0紧随其后，再接原有制造工序；只调整列顺序。
    system_fields = set(system_columns)
    material_columns = [field for field in columns if field not in system_fields]
    columns = [
        *headers[LOT],
        *material_columns,
        *system_columns[len(headers[LOT]):],
    ]
    return columns, rows, [*notices, *upstream_notices]
