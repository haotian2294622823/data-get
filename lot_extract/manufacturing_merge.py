"""将制造工程、设备、不良及其他明细展开为每个制造 LOT 一行。"""

from __future__ import annotations

from collections import defaultdict

from .csv_io import Row, check_shape
from .schema import DEFECT, LOT, PROCESS, TABLES, numeric_order, pass_text


# 这些字段可以在值与父记录完全一致时省略，避免宽表重复展开身份信息。
# 数量不在其中：批次数量、工序数量和不良数量的业务含义不同，不能因相等就删除。
BATCH_FIELDS = {"lot_no", "seiku", "murata_type", "toko_type", "lot_flag"}
PROCESS_FIELDS = {
    "process_key",
    "pass_order",
    "actual_start_datetime",
    "actual_finish_datetime",
}


def label(value: str) -> str:
    """转义拼接进列名的动态片段，避免原文的下划线被误认作层级分隔符。"""
    # 先转义百分号，确保原文中的 %5F 与由下划线转义得到的 %5F 不会混淆。
    # 只处理列名片段，源单元格内容不作替换。
    return (
        value.replace("%", "%25")
        .replace("_", "%5F")
        .replace("\n", "%0A")
        .replace("\r", "%0D")
    )


def merge_records(
    headers: dict[str, list[str]],
    tables: dict[str, list[Row]],
    *,
    max_columns: int | None = None,
    max_cells: int | None = None,
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
    skipped: dict[str, set[str]] = defaultdict(set)
    # 只省略明确可继承的身份、关联和工序时间字段。
    # 批次/工序/不良的数量有不同含义，哪怕数值相同也必须分别保留。
    # 按整张来源表判断：某字段所有记录都与父值一致才省略，有一条差异就整体保留。
    for source in by_lot:
        is_process = source == PROCESS
        candidates = BATCH_FIELDS if is_process else BATCH_FIELDS | PROCESS_FIELDS
        fields = sorted(candidates & set(headers[source]))
        mismatches = dict.fromkeys(fields, 0)
        # 每条记录只查一次父记录，不再为每个待检查字段重扫整张表。
        for row in tables[source]:
            lot = lots[row["lot_no"]]
            parent = lot if is_process else processes.get((row["lot_no"], row["process_key"]), {})
            for field in fields:
                expected = (
                    lot.get(field)
                    if field in BATCH_FIELDS and field not in parent
                    else parent.get(field)
                )
                actual = row[field]
                same = expected is not None and (
                    pass_text(actual) == pass_text(expected)
                    if field == "pass_order"
                    else actual == expected
                )
                if not same:
                    mismatches[field] += 1
        for field in fields:
            if mismatches[field]:
                notices.append(
                    f"{source}.{field} 有 {mismatches[field]} 条与父记录不同或缺少父值，已保留该字段"
                )
            else:
                skipped[source].add(field)
    # 制造道次已经体现在列名中。
    skipped[PROCESS].add("pass_order")

    # 不良名称适合做列名，但可能同名不同码或同码不同名，需要双向检查歧义。
    name_codes: dict[str, set[str]] = defaultdict(set)
    code_names: dict[str, set[str]] = defaultdict(set)
    for row in tables[DEFECT]:
        name_codes[row["defect_name"]].add(row["defect_code"])
        code_names[row["defect_code"]].add(row["defect_name"])
    keep_defect_name = any(
        not name or len(code_names[code]) > 1
        for name, codes in name_codes.items()
        for code in codes
    )

    # 每列记录一个可比较的排序键：批次字段在前，已匹配工序其次，未匹配明细最后。
    # 工序内部再按道次、重复执行序号、明细类型、事件和原始字段顺序排列。
    column_order: dict[str, tuple] = {
        field: (0, i) for i, field in enumerate(headers[LOT])
    }
    check_shape(len(column_order), len(lots), max_columns, max_cells)
    result: list[Row] = []
    for lot_no, lot in sorted(lots.items()):
        output = dict(lot)
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
                if field not in skipped[PROCESS]:
                    put(prefix + "_" + field, process[field], rank + (0, field_index))

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
                    prefix, rank = (
                        "未匹配工序",
                        (2, numeric_order(row["pass_order"]), 0),
                    )
                    notices.append(f"{source} 有未匹配工序明细，已保留在未匹配分组")
                else:
                    prefix, rank = prefixes[row["process_key"]]
                if source == DEFECT:
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
                        (prefix + "_" if prefix else "")
                        + "不良_"
                        + title
                        + (f"_记录{position}" if position > 1 else "")
                    )
                    event_order = (numeric_order(code), position)
                    # 直接把数量写在“不良_裂纹”这类列下，不合成“裂纹:2”的文本。
                    # 同类明细若需多列，使用“记录2”等后缀，始终不累计不良数量。
                    put(
                        detail,
                        row["defect_qty"],
                        rank + (source_index, event_order, -1),
                    )
                else:
                    # 材料、工具、人员和设备逐条展开为“材料1”“材料2”等列组。
                    # 不把多张一对多明细表直接 JOIN，避免组合后行数膨胀、数量重复。
                    group_counts[prefix, source] += 1
                    position = group_counts[prefix, source]
                    detail = (prefix + "_" if prefix else "") + f"{TABLES[source][0]}{position}"
                    event_order = (position,)
                for field_index, field in enumerate(headers[source]):
                    # 未匹配组不能靠工序前缀还原关联信息，须保留原始键与时间。
                    if field in skipped[source] and not (
                        prefix == "未匹配工序" and field in PROCESS_FIELDS
                    ):
                        continue
                    if source == DEFECT:
                        # 数量已经写进不良项目列；名称无歧义时已体现在列名中。
                        # 不良代码继续保留，名称有歧义时也额外保留原始名称字段。
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

    columns = sorted(column_order, key=column_order.__getitem__)
    return columns, result, notices
