"""按原始用料关系把材料工程展开为制造宽表的工序0。"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .csv_io import Row, check_shape, read_tables
from .material_rules import (
    IDENTITY_FIELDS, NO_PROCESS, UNKNOWN, UNMATCHED, MaterialRule,
    load_material_rules, material_column_key, material_rule,
)
from .schema import DEFECT, LOT, MACHINE, PROCESS, TABLES, UPSTREAM_PREFIX, UPSTREAM_TABLES, numeric_order


ExecutionKey = tuple[str, str, tuple[int, Decimal, str]]


@dataclass(frozen=True)
class MaterialExecution:
    rule: MaterialRule
    fields: Row


def material_usages(records: list[Row]) -> dict[str, list[str]]:
    """按首次使用顺序收集材料，同一制造 LOT 的重复用料共用前工程。"""
    usages: dict[str, dict[str, None]] = defaultdict(dict)
    for row in sorted(records, key=lambda r: (
        r["lot_no"], numeric_order(r["pass_order"]), r["process_key"],
        numeric_order(r["seq"]), r["material_lot_no"],
    )):
        if row["material_lot_no"].strip():
            usages[row["lot_no"]][row["material_lot_no"]] = None
    return {lot: list(materials) for lot, materials in usages.items()}


def execution_key(row: Row) -> ExecutionKey:
    # 用精确数值比较道次，1 与 1.0 相等。输出仍保留原始字符串。
    return row["lot_no"], row["process_key"], numeric_order(row["pass_order"])


def execution_fields(process: Row, details: dict[str, list[Row]], rule: MaterialRule) -> Row:
    """展开一次执行。只保留有值的可选字段，标识字段用于追溯。"""
    fields = {field: process.get(field, "") for field in IDENTITY_FIELDS}
    fields["material_lot_no"] = process["lot_no"]

    def add(name: str, value: str) -> None:
        if name in fields:
            raise ValueError(f"材料工程字段名冲突：{name}")
        fields[name] = value

    for field, value in process.items():
        if value == "" or field in ("lot_no", "process_name", "process_key", "pass_order"):
            continue
        # 工艺表若也有同名用料字段，另存原值，材料身份始终由该行 lot_no 确定。
        add("工艺_material_lot_no" if field == "material_lot_no" else field, value)

    for source in rule.details:
        code_field = "machine_code" if source == MACHINE else "defect_code"
        records = sorted(details.get(source, []), key=lambda r: (numeric_order(r[code_field]), r[code_field]))
        for number, record in enumerate(records, 1):
            prefix = f"{TABLES[source][0]}{number}_"
            for field, value in record.items():
                # 关联键在本执行列组中已有对应字段，明细只补充自己的值。
                if field not in ("lot_no", "process_key", "pass_order") and (value != "" or field == code_field):
                    add(prefix + field, value)
    return fields


def material_executions(tables: dict[str, list[Row]]) -> tuple[dict[str, list[MaterialExecution]], list[str]]:
    """为每个材料建立执行索引，供使用同一材料的各个制造 LOT 复用。"""
    rules = load_material_rules()
    parents: dict[ExecutionKey, tuple[Row, MaterialRule]] = {}
    details: dict[ExecutionKey, dict[str, list[Row]]] = defaultdict(lambda: defaultdict(list))
    counts: Counter[str] = Counter()
    for process in tables[PROCESS]:
        if not process["process_key"]:
            raise ValueError("材料工序键为空，无法可靠区分执行记录")
        rule = material_rule(process.get("process_name", ""), rules)
        parents[execution_key(process)] = process, rule
        if rule == UNKNOWN:
            counts["未识别工程记录"] += 1

    for source in (MACHINE, DEFECT):
        for record in tables[source]:
            key = execution_key(record)
            if key not in parents:
                # 道次不符和缺父工序时，将明细留在独立列组，保留原始关联键。
                parents[key] = {field: record[field] for field in ("lot_no", "process_key", "pass_order")}, UNMATCHED
            rule = parents[key][1]
            if rule == UNMATCHED:
                counts["未匹配工程明细"] += 1
            if source in rule.details:
                details[key][source].append(record)

    by_lot: dict[str, list[MaterialExecution]] = defaultdict(list)
    for key, (process, rule) in sorted(parents.items(), key=lambda item: (item[0][0], item[0][2], item[0][1])):
        by_lot[key[0]].append(MaterialExecution(rule, execution_fields(process, details.get(key, {}), rule)))

    for row in tables[LOT]:
        if row["lot_no"] not in by_lot:
            fields = execution_fields({"lot_no": row["lot_no"]}, {}, NO_PROCESS)
            by_lot[row["lot_no"]].append(MaterialExecution(NO_PROCESS, fields))
            counts["没有工程记录的材料 LOT"] += 1

    notices = [f"前工程有 {count} 条{reason}，已保留在对应列组" for reason, count in counts.items()]
    return by_lot, notices


def merge_and_attach_materials(
    base: Path,
    columns: list[str],
    rows: list[Row],
    records: list[Row],
    *,
    max_columns: int | None = None,
    max_cells: int | None = None,
) -> tuple[list[str], list[str]]:
    usages = material_usages(records)
    _, tables = read_tables(base, UPSTREAM_TABLES, UPSTREAM_PREFIX, pass_order_key=numeric_order)
    requested = {lot for materials in usages.values() for lot in materials}
    if any(record["lot_no"] not in requested for table in tables.values() for record in table):
        raise ValueError("前工程 CSV 包含用料关系中不存在的 LOT，请核对查询目录。")
    executions, notices = material_executions(tables)
    missing = requested - executions.keys()
    if missing:
        notices.append(f"有 {len(missing)} 个材料 LOT 未查到前工程数据，制造用料列保留原编号")

    primary = set(columns)
    added: set[str] = set()
    repeated: set[tuple[str, str]] = set()
    for row in rows:
        positions: Counter[str] = Counter()
        for lot in usages.get(row["lot_no"], ()):
            # 卷线按材料计数，额外执行留在该材料组内。执行计数型工程每次开新列组。
            material_positions: dict[str, int] = {}
            execution_numbers: Counter[str] = Counter()
            for event in executions.get(lot, ()):
                name = event.rule.name
                if event.rule.number_by == "material_lot":
                    if name not in material_positions:
                        positions[name] += 1
                        material_positions[name] = positions[name]
                    position = material_positions[name]
                    execution_numbers[name] += 1
                    number = execution_numbers[name]
                else:
                    positions[name] += 1
                    position, number = positions[name], 1

                prefix = f"工序0_{name}{position}_"
                if number > 1:
                    prefix += f"执行{number}_"
                    repeated.add((name, lot))
                for field, value in event.fields.items():
                    column = prefix + field
                    if column in primary or column in row:
                        raise ValueError(f"前工程列名冲突：{column}")
                    if column not in added:
                        check_shape(len(columns) + len(added) + 1, len(rows), max_columns, max_cells)
                        added.add(column)
                    row[column] = value

    for name, count in sorted(Counter(name for name, _ in repeated).items()):
        notices.append(f"{name}有 {count} 个材料 LOT 出现多次执行，已保留在同一材料列组")
    return [*columns, *sorted(added, key=material_column_key)], notices
