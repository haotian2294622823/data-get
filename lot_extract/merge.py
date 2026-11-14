"""制造宽表：展开制造工程、挂接工序0，并提供离线 CSV 合并入口。"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path
from tempfile import NamedTemporaryFile

from .final_export import column_rank
from .material_merge import ExportRules, archive_rules, load_export_rules, merge_and_attach_materials
from .tables import (
    DEFECT, LOT, MACHINE, MATERIAL, PROCESS, TABLES, UPSTREAM_PREFIX, UPSTREAM_TABLES,
    BATCH_FIELDS, PROCESS_FIELDS, QUANTITY_FIELDS,
    Row, check_shape, clean_qty, filter_final_rows, inherited_columns, label, note_inheritance,
    numeric_order, pass_text, read_tables,
    write_wide as write_wide,
)


def merge_tables(
    base_dir: Path | str, *, max_columns: int | None = None, max_cells: int | None = None,
    rules: ExportRules | None = None, inheritance: dict[str, str | None] | None = None,
) -> tuple[list[str], list[Row], list[str]]:
    """读取一次查询的存档，合并系统记录，再把直接材料的前工程列挂回系统行。

    兼容没有 upstream 文件的旧存档；一旦出现前工程文件，就要求四张全部齐备。
    材料缺批次主记录时，有真实明细的仍保留这些明细。
    返回的 rows 始终以系统 LOT 为行，材料 LOT 只增加列组，不增加系统批次行数。

    inheritance 省略时，在本次输入范围内决定省列；批量导出会传入空字典收集证据，
    此时返回完整候选列，等 final_export 汇总所有分片后再省列，避免跨批次丢值。
    """
    base = Path(base_dir)
    rules = rules if rules is not None else load_export_rules()
    evidence = {} if inheritance is None else inheritance
    upstream_files = [(base / f"{UPSTREAM_PREFIX}{table}.csv").exists() for table in UPSTREAM_TABLES]
    has_upstream = any(upstream_files)
    # 旧手动存档允许只有六表；新存档必须有设备表和四张前工程表，空表也应有表头。
    sources = tuple(
        table for table in TABLES
        if table != MACHINE or has_upstream or (base / f"{MACHINE}.csv").exists()
    )
    headers, tables = read_tables(base, sources)
    limits = {"max_columns": max_columns, "max_cells": max_cells}
    if has_upstream and not all(upstream_files):
        raise ValueError("前工程 CSV 不完整，请检查本次查询目录。")
    columns, rows, notices = merge_records(headers, tables, inheritance=evidence, **limits)
    if has_upstream:
        columns, upstream_notices = merge_and_attach_materials(
            base, columns, rows, tables[MATERIAL], rules=rules.materials, inheritance=evidence, **limits,
        )
        notices.extend(upstream_notices)
    if inheritance is None:
        omitted = inherited_columns(columns, evidence)
        columns = [column for column in columns if column not in omitted]
        for row in rows:
            for column in row.keys() & omitted.keys():
                del row[column]
    positions = {column: i for i, column in enumerate(columns)}
    columns.sort(key=lambda column: column_rank(column, positions[column], rules.materials))
    return columns, rows, notices


def merge_records(
    headers: dict[str, list[str]],
    tables: dict[str, list[Row]],
    *,
    max_columns: int | None = None,
    max_cells: int | None = None,
    inheritance: dict[str, str | None],
) -> tuple[list[str], list[Row], list[str]]:
    """将一组 LOT 的批次、工序和明细展开，返回（列名，宽表行，差异提示）。

    制造工序按 pass_order 排序；同一道次的不同执行分别保留。
    本函数假定输入已通过 read_tables 的格式和唯一性校验。
    """
    lots = {r["lot_no"]: r for r in tables[LOT]}
    processes = {
        (r["lot_no"], r["process_key"]): r for r in tables[PROCESS]
    }
    details = tuple(table for table in tables if table not in (LOT, PROCESS))
    # 先建立“表名 → LOT → 该 LOT 的记录”索引，避免每处理一个 LOT 都重扫整张表。
    # 工序身份使用 (lot_no, process_key)，后续挂接明细时还会核对 pass_order。
    by_lot: dict[str, dict[str, list[Row]]] = {}
    for source, records in tables.items():
        if source == LOT:
            continue
        by_lot[source] = defaultdict(list)
        for row in records:
            if row["lot_no"] not in lots:
                raise ValueError(f"{source} 包含批次表不存在的 LOT，不能静默丢弃")
            by_lot[source][row["lot_no"]].append(row)
    if any(not key for _, key in processes):
        raise ValueError("工序键为空，无法可靠区分执行记录")

    notices: list[str] = []
    differences: dict[tuple[str, str], int] = defaultdict(int)

    def compare(source: str, field: str, column: str, parent_column: str | None,
                expected: str | None, actual: str) -> None:
        # None 表示缺少父字段；空字符串是有效原值，两者不能混为一谈。
        if expected is None or expected != actual:
            parent_column = None
            differences[source, field] += 1
        note_inheritance(inheritance, column, parent_column)

    # 每列记录一个可比较的排序键：批次字段在前，已匹配工序其次，未匹配明细最后。
    # 工序内部再按道次、重复执行序号、明细类型、事件和原始字段顺序排列。
    column_order: dict[str, tuple] = {
        field: (0, i) for i, field in enumerate(headers[LOT])
    }
    check_shape(len(column_order), len(lots), max_columns, max_cells)
    result: list[Row] = []
    for lot_no, lot in sorted(lots.items()):
        output = {field: clean_qty(value) if field in QUANTITY_FIELDS else value
                  for field, value in lot.items()}
        process_list = by_lot[PROCESS].get(lot_no, [])
        process_list.sort(
            key=lambda r: (numeric_order(r["pass_order"]), r["process_key"])
        )
        counts: dict[str, int] = defaultdict(int)
        prefixes: dict[str, tuple[str, tuple]] = {}

        def put(column: str, value: str, rank: tuple, target: Row = output) -> None:
            """写入当前 LOT 的一个单元格，同时登记全表列顺序并检查规模。"""
            # target 的默认值绑定本轮 output；每个 LOT 都重新定义此函数。
            # 同一行出现同名列必须报错，直接赋值覆盖会丢失另一条原始记录。
            if column in target:
                raise ValueError(f"列名冲突：{column}，禁止覆盖")
            target[column] = value
            if column not in column_order:
                check_shape(len(column_order) + 1, len(lots), max_columns, max_cells)
                column_order[column] = rank

        for process in process_list:
            pass_no = pass_text(process["pass_order"])
            head = "工序" + label(pass_no)
            counts[head] += 1
            execution = counts[head]
            prefix = head + (f"_执行{execution}" if execution > 1 else "")
            rank = (1, numeric_order(process["pass_order"]), execution)
            prefixes[process["process_key"]] = prefix, rank
            for field_index, field in enumerate(headers[PROCESS]):
                column = prefix + "_" + field
                value = process[field]
                put(column, clean_qty(value) if field in QUANTITY_FIELDS else value, rank + (0, field_index))
                if field in BATCH_FIELDS:
                    compare(PROCESS, field, column, field, lot.get(field), value)

        for source_index, source in enumerate(details, 1):
            group_counts: dict[tuple[str, str], int] = defaultdict(int)
            records = by_lot[source].get(lot_no, [])
            records.sort(
                key=lambda r: tuple(numeric_order(r[k]) for k in TABLES[source][1])
            )
            for row in records:
                parent = processes.get((lot_no, row["process_key"]))
                # 工序键相同还不够，道次也必须一致；异常明细保留在“未匹配工序”。
                if parent is None or pass_text(parent["pass_order"]) != pass_text(
                    row["pass_order"]
                ):
                    parent = None
                    prefix, rank = (
                        "未匹配工序",
                        (2, numeric_order(row["pass_order"]), 0),
                    )
                    notices.append(f"{source} 有未匹配工序明细，已保留在未匹配分组")
                else:
                    prefix, rank = prefixes[row["process_key"]]
                if source == DEFECT:
                    code = row["defect_code"]
                    if not code:
                        raise ValueError("不良代码为空，无法确定项目身份")
                    title = "代码" + label(code)
                    group_counts[prefix, title] += 1
                    position = group_counts[prefix, title]
                    detail = (
                        (prefix + "_" if prefix else "")
                        + "不良_"
                        + title
                        + (f"_记录{position}" if position > 1 else "")
                    )
                    event_order = (numeric_order(code), position)
                    # 数量写入“不良_代码01”，原名称写入后面的“_名称”列。
                    # 代码确定列身份，改名不会另开数量列；多条记录始终不累计数量。
                    put(
                        detail,
                        clean_qty(row["defect_qty"]),
                        rank + (source_index, event_order, -1),
                    )
                    if not (row["defect_qty"] or row["defect_name"]):
                        put(detail + "_代码", code, rank + (source_index, event_order, -1))
                else:
                    # 材料、工具、人员和设备逐条展开为“材料1”“材料2”等列组。
                    # 不把多张一对多明细表直接 JOIN，避免组合后行数膨胀、数量重复。
                    group_counts[prefix, source] += 1
                    position = group_counts[prefix, source]
                    detail = (prefix + "_" if prefix else "") + f"{TABLES[source][0]}{position}"
                    event_order = (position,)
                for field_index, field in enumerate(headers[source]):
                    if source == DEFECT and field in ("defect_qty", "defect_code"):
                        continue
                    suffix = "名称" if source == DEFECT and field == "defect_name" else field
                    column = detail + "_" + suffix
                    value = row[field]
                    put(column, clean_qty(value) if field in QUANTITY_FIELDS else value,
                        rank + (source_index, event_order, field_index))
                    if field in BATCH_FIELDS or field in PROCESS_FIELDS:
                        parent_column, expected = None, None
                        if parent is not None and (field != "lot_no" or source != MACHINE or row["machine_code"]):
                            if field in parent and prefix + "_" + field in output:
                                parent_column, expected = prefix + "_" + field, parent[field]
                            elif field in BATCH_FIELDS and field in lot:
                                parent_column, expected = field, lot[field]
                        compare(source, field, column, parent_column, expected, value)
        result.append(output)

    notices.extend(f"{source}.{field} 有 {count} 条与父记录不同或缺少父值，已保留原值"
                   for (source, field), count in sorted(differences.items()))
    columns = sorted(column_order, key=column_order.__getitem__)
    return columns, result, notices


def merge_csv(
    base_dir: str | Path | None = None, output_path: str | Path | None = None, *, product: str | None = None,
) -> Path:
    """合并并保存宽表，返回输出路径；未指定目录时读取项目目录。

    手动入口未传入页面的列数/单元格上限，大目录仍需考虑本机内存。
    notices 是保留数据后产生的提示，不等同于会中止导出的异常。
    """
    base = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parents[1]
    destination = Path(output_path) if output_path is not None else base / "批次全字段宽表.csv"
    sources = [f"{table}.csv" for table in TABLES]
    sources.extend(f"{UPSTREAM_PREFIX}{table}.csv" for table in UPSTREAM_TABLES)
    # resolve 统一相对路径和绝对路径写法，防止用户把输出文件指定成某张原始表。
    if destination.resolve() in {(base / source).resolve() for source in sources}:
        raise ValueError("输出文件不能覆盖原始表。")
    manifest = base / "manifest.json"
    state = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else {}
    rules = archive_rules(state, product)
    evidence: dict[str, str | None] = {}
    columns, rows, notices = merge_tables(base, rules=rules, inheritance=evidence)
    total = len(rows)
    rows = filter_final_rows(rows, rules.lot)
    omitted = inherited_columns(columns, evidence)
    columns = [column for column in columns if column not in omitted]
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        # 临时文件与目标文件放在同一目录，完整写入并关闭后再原子替换目标。
        # Windows 不允许重命名仍打开的临时文件，所以 os.replace 必须放在 with 外。
        with NamedTemporaryFile(
            mode="w", encoding="utf-8-sig", newline="", dir=destination.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            write_wide(stream, columns, rows)
        os.replace(temporary, destination)
    finally:
        # 写入/替换失败时清理残留；成功替换后临时路径已不存在，missing_ok 允许它缺失。
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(f"已合并：{len(rows)} 个系统 LOT，{len(columns)} 列 -> {destination}")
    if total != len(rows):
        print(f"按产品的系统 LOT 规则筛选，已排除 {total - len(rows)} 行。")
    for notice in notices:
        print("提示：" + notice)
    return destination
