"""将六张 prass CSV 合并成一批次一行的普通宽表。

批次身份只保留一次，工序公共字段只在工序保留一次，明细独立展开。
去重依据是字段层级和父记录，不是碰巧相同的单元格值。
用法：python merge_csv.py [CSV目录] [--output 输出文件.csv]
不连接数据库，不依赖第三方库，不修改六张输入文件。
"""

from __future__ import annotations

import argparse
import csv
import os
import tempfile
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

Row = dict[str, str]
TABLES = {
    "prass_lot": ("批次", ("lot_no",)),
    "prass_lot_process": ("工序", ("lot_no", "process_key")),
    "prass_process_defect": (
        "不良",
        ("lot_no", "process_key", "pass_order", "defect_code"),
    ),
    "prass_process_material": ("材料", ("lot_no", "process_key", "pass_order", "seq")),
    "prass_process_tool": (
        "工具",
        ("lot_no", "process_key", "pass_order", "tool_code"),
    ),
    "prass_process_worker": (
        "人员",
        ("lot_no", "process_key", "pass_order", "worker_code"),
    ),
}
BATCH_FIELDS = {"lot_no", "seiku", "murata_type", "toko_type", "lot_flag"}
PROCESS_FIELDS = {
    "process_key",
    "pass_order",
    "actual_start_datetime",
    "actual_finish_datetime",
}


def numeric_order(value: str) -> tuple[int, Decimal, str]:
    """道次按数值排序；编号和数量的原始文本不变。"""
    try:
        number = Decimal(value)
        if number.is_finite():
            return 0, number, ""
    except InvalidOperation:
        pass
    return 1, Decimal(0), value


def pass_text(value: str) -> str:
    invalid, number, text = numeric_order(value)
    return (text or "未提供") if invalid else format(number.normalize(), "f")


def label(value: str) -> str:
    # 转义列名分隔符，源单元格不作替换。
    return (
        value.replace("%", "%25")
        .replace("_", "%5F")
        .replace("\n", "%0A")
        .replace("\r", "%0D")
    )


def read_tables(base: Path) -> tuple[dict[str, list[str]], dict[str, list[Row]]]:
    headers, tables = {}, {}
    for source, (_, keys) in TABLES.items():
        with (base / f"{source}.csv").open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fields = reader.fieldnames or []
            rows = list(reader)
        if not set(keys) <= set(fields) or len(fields) != len(set(fields)):
            raise ValueError(f"{source} 缺少必需字段或存在重复列名")
        seen = set()
        for row in rows:
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"{source} CSV 行与表头不一致")
            if not row["lot_no"]:
                raise ValueError(f"{source} LOT 为空，无法确定归属")
            key = tuple(
                pass_text(row[k]) if k == "pass_order" else row[k] for k in keys
            )
            if key in seen:
                raise ValueError(
                    f"{source} 候选键重复，请核实事件键；不能取第一条或合计"
                )
            seen.add(key)
        headers[source], tables[source] = fields, rows
    for source, required in {
        "prass_lot_process": {"pass_order"},
        "prass_process_defect": {"defect_name", "defect_qty"},
    }.items():
        if not required <= set(headers[source]):
            raise ValueError(f"{source} 缺少展开需要的字段")
    return headers, tables


def merge_tables(
    base_dir: Path | str, *, max_columns: int | None = None, max_cells: int | None = None
) -> tuple[list[str], list[Row], list[str]]:
    headers, tables = read_tables(Path(base_dir))
    lots = {r["lot_no"]: r for r in tables["prass_lot"]}
    processes = {
        (r["lot_no"], r["process_key"]): r for r in tables["prass_lot_process"]
    }
    for source, records in tables.items():
        if any(r["lot_no"] not in lots for r in records):
            raise ValueError(f"{source} 包含批次表不存在的 LOT，不能静默丢弃")
    if any(not key for _, key in processes):
        raise ValueError("工序键为空，无法可靠区分执行记录")

    notices: list[str] = []
    skipped: dict[str, set[str]] = defaultdict(set)
    # 只省略明确可继承的身份、关联和工序时间字段。
    # 批次/工序/不良的数量有不同含义，哪怕数值相同也必须分别保留。
    for source in list(TABLES)[1:]:
        is_process = source == "prass_lot_process"
        candidates = BATCH_FIELDS if is_process else BATCH_FIELDS | PROCESS_FIELDS
        for field in sorted(candidates & set(headers[source])):
            mismatches = 0
            for row in tables[source]:
                lot = lots[row["lot_no"]]
                parent = (
                    lot
                    if is_process
                    else processes.get((row["lot_no"], row["process_key"]), {})
                )
                if field in BATCH_FIELDS and field not in parent:
                    parent = lot
                actual, expected = row[field], parent.get(field)
                same = expected is not None and (
                    pass_text(actual) == pass_text(expected)
                    if field == "pass_order"
                    else actual == expected
                )
                if not same:
                    mismatches += 1
            if mismatches:
                notices.append(
                    f"{source}.{field} 有 {mismatches} 条与父记录不同或缺少父值，已保留该字段"
                )
            else:
                skipped[source].add(field)
    # 道次已经写进工序列名前缀，不再单列。
    skipped["prass_lot_process"].add("pass_order")

    name_codes: dict[str, set[str]] = defaultdict(set)
    code_names: dict[str, set[str]] = defaultdict(set)
    for row in tables["prass_process_defect"]:
        name_codes[row["defect_name"]].add(row["defect_code"])
        code_names[row["defect_code"]].add(row["defect_name"])
    keep_defect_name = any(
        not name or len(code_names[code]) > 1
        for name, codes in name_codes.items()
        for code in codes
    )

    column_order: dict[str, tuple] = {
        field: (0, i) for i, field in enumerate(headers["prass_lot"])
    }
    result: list[Row] = []
    for lot_no, lot in sorted(lots.items()):
        output = dict(lot)
        process_list = [r for r in tables["prass_lot_process"] if r["lot_no"] == lot_no]
        process_list.sort(
            key=lambda r: (numeric_order(r["pass_order"]), r["process_key"])
        )
        counts: dict[str, int] = defaultdict(int)
        prefixes: dict[str, tuple[str, tuple]] = {}

        def put(column: str, value: str, rank: tuple, target: Row = output) -> None:
            if column in target:
                raise ValueError(f"列名冲突：{column}，禁止覆盖")
            target[column] = value
            column_order.setdefault(column, rank)

        for process in process_list:
            pass_no = pass_text(process["pass_order"])
            counts[pass_no] += 1
            execution = counts[pass_no]
            prefix = (
                "工序" + label(pass_no) + (f"_执行{execution}" if execution > 1 else "")
            )
            rank = (1, numeric_order(process["pass_order"]), execution)
            prefixes[process["process_key"]] = prefix, rank
            for field_index, field in enumerate(headers["prass_lot_process"]):
                if field not in skipped["prass_lot_process"]:
                    put(prefix + "_" + field, process[field], rank + (0, field_index))

        for source_index, source in enumerate(list(TABLES)[2:], 1):
            group_counts: dict[tuple[str, str], int] = defaultdict(int)
            records = [r for r in tables[source] if r["lot_no"] == lot_no]
            records.sort(
                key=lambda r: tuple(numeric_order(r[k]) for k in TABLES[source][1])
            )
            for row in records:
                parent = processes.get((lot_no, row["process_key"]))
                if parent is None or pass_text(parent["pass_order"]) != pass_text(
                    row["pass_order"]
                ):
                    prefix, rank = (
                        "未匹配工序",
                        (2, numeric_order(row["pass_order"]), 0),
                    )
                    notices.append(f"{source} 有未匹配工序明细，已保留在未匹配分组")
                else:
                    prefix, rank = prefixes[row["process_key"]]
                if source == "prass_process_defect":
                    code, name = row["defect_code"], row["defect_name"]
                    if not code:
                        raise ValueError("不良代码为空，无法确定项目身份")
                    title = (
                        label(name)
                        if name and len(code_names[code]) == 1
                        else "代码" + label(code)
                    )
                    if (
                        name
                        and len(name_codes[name]) > 1
                        and len(code_names[code]) == 1
                    ):
                        title += "_代码" + label(code)
                    group_counts[prefix, title] += 1
                    position = group_counts[prefix, title]
                    detail = (
                        prefix
                        + "_不良_"
                        + title
                        + (f"_记录{position}" if position > 1 else "")
                    )
                    event_order = (numeric_order(code), position)
                    put(
                        detail,
                        row["defect_qty"],
                        rank + (source_index, event_order, -1),
                    )
                else:
                    group_counts[prefix, source] += 1
                    position = group_counts[prefix, source]
                    detail = f"{prefix}_{TABLES[source][0]}{position}"
                    event_order = (position,)
                for field_index, field in enumerate(headers[source]):
                    # 未匹配组不能靠工序前缀还原关联信息，须保留原始键与时间。
                    if field in skipped[source] and not (
                        prefix == "未匹配工序" and field in PROCESS_FIELDS
                    ):
                        continue
                    if source == "prass_process_defect":
                        if field == "defect_qty" or (
                            field == "defect_name" and not keep_defect_name
                        ):
                            continue
                        suffix = "代码" if field == "defect_code" else field
                    else:
                        suffix = field
                    put(
                        detail + "_" + suffix,
                        row[field],
                        rank + (source_index, event_order, field_index),
                    )
        result.append(output)

    if (max_columns is not None and len(column_order) > max_columns) or (
        max_cells is not None and len(column_order) * len(result) > max_cells
    ):
        raise ValueError("合并宽表超出大小上限，请缩小查询范围。")
    columns = sorted(column_order, key=column_order.__getitem__)
    return columns, [{c: row.get(c, "") for c in columns} for row in result], notices


def main(
    base_dir: str | Path | None = None, output_path: str | Path | None = None
) -> Path:
    base = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parent
    destination = (
        Path(output_path) if output_path is not None else base / "批次全字段宽表.csv"
    )
    if destination.resolve() in {
        (base / f"{source}.csv").resolve() for source in TABLES
    }:
        raise ValueError("输出文件不能覆盖六张输入表")
    columns, rows, notices = merge_tables(base)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # 写完临时文件再替换结果，避免失败时留下半个 CSV。
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8-sig",
            newline="",
            dir=destination.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    print(f"已合并：{len(rows)} 个 LOT，{len(columns)} 列 -> {destination}")
    for notice in notices:
        print("提示：" + notice)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "directory", nargs="?", type=Path, default=Path(__file__).resolve().parent
    )
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    try:
        main(arguments.directory, arguments.output)
    except (OSError, ValueError) as error:
        parser.exit(1, f"合并失败：{error}\n")
