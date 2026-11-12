"""材料前工程的独立合并模块，不连接数据库。

读取材料四表，按 process_name 分工程合并，再按原始用料关系挂接制造行。
共享 CSV 校验和单 LOT 展开函数沿用 merge.py；材料分组、排序和关联在本模块。
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from .merge import Row, check_shape, label, merge_records, numeric_order, pass_text, read_tables
from .schema import LOT, PROCESS, UPSTREAM_PREFIX, UPSTREAM_TABLES


def material_usages(records: list[Row]) -> dict[str, list[str]]:
    """按首次使用顺序列出系统 LOT 的直接材料，不用材料类别决定工程分组。"""
    usages: dict[str, list[str]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    ordered = sorted(records, key=lambda r: (
        r["lot_no"], numeric_order(r["pass_order"]), r["process_key"],
        numeric_order(r["seq"]), r["material_lot_no"],
    ))
    for row in ordered:
        lot, material = row["lot_no"], row["material_lot_no"]
        if not material.strip() or (lot, material) in seen:
            continue
        seen.add((lot, material))
        usages[lot].append(material)
    return usages


def engineering_order(name: str) -> tuple[int, str]:
    """只控制展示顺序：E-Core、I-Core、卷线、其他；分组仍用完整工程名。"""
    normalized = name.casefold()
    for position, keyword in enumerate(("e-core", "i-core", "卷线")):
        if keyword in normalized:
            return position, name
    return 3, name


def merge_material_engineering(
    headers: dict[str, list[str]],
    tables: dict[str, list[Row]],
    *,
    max_columns: int | None = None,
) -> tuple[dict[str, tuple[list[str], dict[str, Row]]], list[str]]:
    """先按真实 process_name 分工程，再在各工程内合并每个材料 LOT。

    同一材料 LOT 可以有多个工程，各工程只接收自己的工序和明细。
    缺父工序或道次不符的明细单独保留，不能根据材料类别猜测归属。
    输入须已通过 read_tables 校验并补齐缺失的材料批次占位行。
    """
    grouped: dict[str, dict[str, list[Row]]] = {}
    members: dict[str, set[str]] = defaultdict(set)
    parents = {(row["lot_no"], row["process_key"]): row for row in tables[PROCESS]}

    def add(name: str, source: str, row: Row) -> None:
        if name not in grouped:
            grouped[name] = {table: [] for table in tables}
        grouped[name][source].append(row)
        members[name].add(row["lot_no"])

    for row in tables[PROCESS]:
        add(row.get("process_name", "").strip() or "未提供工程", PROCESS, row)
    for source in tables:
        if source in (LOT, PROCESS):
            continue
        for row in tables[source]:
            parent = parents.get((row["lot_no"], row["process_key"]))
            matched = parent is not None and pass_text(parent["pass_order"]) == pass_text(row["pass_order"])
            name = (parent.get("process_name", "").strip() or "未提供工程") if matched else "未匹配工程"
            add(name, source, row)

    lot_names: dict[str, list[str]] = defaultdict(list)
    for name, lots in members.items():
        for lot in lots:
            lot_names[lot].append(name)
    for row in tables[LOT]:
        names = lot_names.get(row["lot_no"], []) or ["未提供工程"]
        for name in names:
            add(name, LOT, row)

    result = {}
    notices = []
    for name in sorted(grouped, key=engineering_order):
        columns, rows, messages = merge_records(
            headers, grouped[name], max_columns=max_columns, flat_details=True
        )
        result[name] = columns, {row["lot_no"]: row for row in rows}
        notices.extend(f"材料工程 {name}：{message}" for message in messages)
    return result, notices


def attach_material_engineering(
    columns: list[str],
    rows: list[Row],
    usages: dict[str, list[str]],
    engineering: dict[str, tuple[list[str], dict[str, Row]]],
    *,
    max_columns: int | None = None,
    max_cells: int | None = None,
) -> list[str]:
    """按原始材料 LOT 关联制造行；工程排序不改变 LOT 归属或合计数量。"""
    primary = set(columns)
    added: dict[str, tuple[int, int, int]] = {}
    for engineering_index, name in enumerate(sorted(engineering, key=engineering_order)):
        fields, materials = engineering[name]
        for row in rows:
            position = 0
            for material in usages.get(row["lot_no"], []):
                source = materials.get(material)
                if source is None:
                    continue
                position += 1
                prefix = f"工序0-{label(name)}_{position}_"
                for field_index, field in enumerate(fields):
                    if field not in source:
                        continue
                    # 材料组自身也带真实 LOT，便于核对同工程的多个材料和共享材料。
                    column = prefix + ("material_lot_no" if field == "lot_no" else field)
                    if column in primary or column in row:
                        raise ValueError(f"前工程列名冲突：{column}")
                    if column not in added:
                        check_shape(len(columns) + len(added) + 1, len(rows), max_columns, max_cells)
                        added[column] = (engineering_index, position, field_index)
                    row[column] = source[field]
    return [*columns, *sorted(added, key=added.__getitem__)]


def merge_and_attach_materials(
    base: Path,
    columns: list[str],
    rows: list[Row],
    records: list[Row],
    *,
    max_columns: int | None = None,
    max_cells: int | None = None,
) -> tuple[list[str], list[str]]:
    """材料模块入口：读材料存档、核对用料范围、分工程合并并挂接制造结果。"""
    usages = material_usages(records)
    headers, tables = read_tables(base, UPSTREAM_TABLES, UPSTREAM_PREFIX)
    requested = {material for materials in usages.values() for material in materials}
    present = {row["lot_no"] for row in tables[LOT]}
    details = {row["lot_no"] for table in UPSTREAM_TABLES[1:] for row in tables[table]}
    if (present | details) - requested:
        raise ValueError("前工程 CSV 包含用料关系中不存在的 LOT，请核对查询目录。")
    for lot in sorted(details - present):
        tables[LOT].append({field: lot if field == "lot_no" else "" for field in headers[LOT]})
    engineering, notices = merge_material_engineering(headers, tables, max_columns=max_columns)
    columns = attach_material_engineering(
        columns, rows, usages, engineering, max_columns=max_columns, max_cells=max_cells
    )
    return columns, notices
