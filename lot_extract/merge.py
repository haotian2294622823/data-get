"""离线合并原始 CSV，产出“每个系统 LOT 一行”的全字段宽表。

主流程：制造 LOT 独立合并 → 材料按 process_name 分工程独立合并 →
按原始用料关系拼到同一系统 LOT 行 → write_wide 输出。
本模块不连接数据库。材料回库查询由 service.py 完成，合并只使用本次存档文件。
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path

from .schema import DEFECT, LOT, MACHINE, MATERIAL, PROCESS, TABLES, UPSTREAM_PREFIX, UPSTREAM_TABLES, ExportSizeError, validate_columns

# CSV 中所有值保留为字符串，避免 LOT 前导零、编号格式和原始小数表示被改写。
Row = dict[str, str]
# 仅用于判断 Excel 公式样式文本是否其实是合法数字，不参与数值汇总。
NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
# 这些字段可以在值与父记录完全一致时省略，避免宽表重复展开身份信息。
# 数量不在其中：批次数量、工序数量和不良数量的业务含义不同，不能因相等就删除。
BATCH_FIELDS = {"lot_no", "seiku", "murata_type", "toko_type", "lot_flag"}
PROCESS_FIELDS = {
    "process_key",
    "pass_order",
    "actual_start_datetime",
    "actual_finish_datetime",
}


@lru_cache(maxsize=8192)
def numeric_order(value: str) -> tuple[int, Decimal, str]:
    """生成排序键，让数值 2 排在 10 前面；无法转数值的文本排在数值之后。

    Decimal 避免浮点数比较误差；这里只生成排序依据，不改写原始编号或数量。
    返回三元组中的首位区分数值/文本，后两位分别用于数值比较和文本比较。
    """
    try:
        number = Decimal(value)
        if number.is_finite():
            return 0, number, ""
    except InvalidOperation:
        pass
    return 1, Decimal(0), value


def pass_text(value: str) -> str:
    """统一道次的比较和列名前缀：1、1.0 视为同一道次，空值显示为“未提供”。"""
    invalid, number, text = numeric_order(value)
    return (text or "未提供") if invalid else format(number.normalize(), "f")


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


def read_tables(
    base: Path, sources: tuple[str, ...], prefix: str = ""
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
                pass_text(row[k]) if k == "pass_order" else row[k] for k in keys
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


def merge_records(
    headers: dict[str, list[str]],
    tables: dict[str, list[Row]],
    *,
    max_columns: int | None = None,
    max_cells: int | None = None,
    flat_details: bool = False,
) -> tuple[list[str], list[Row], list[str]]:
    """将一组 LOT 的批次、工序和明细展开，返回（列名，宽表行，差异提示）。

    系统 LOT 和材料 LOT 都调用这个函数，各自按自己的 pass_order 排序。
    flat_details 为真时，明细直接放在材料列组下，不再加道次或工程名前缀；
    工程原始字段放在“工程记录N”下，明细保留关联键以便核对所属工程。
    用料记录保留原始 material_lot_no，系统 LOT 与材料 LOT 因此能在同一行对照。
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
    # 材料明细不再靠工序前缀定位，保留关联字段；系统道次仍体现在列名中。
    if flat_details:
        for source in details:
            skipped[source].difference_update(PROCESS_FIELDS)
    else:
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

        for process_position, process in enumerate(process_list, 1):
            pass_no = pass_text(process["pass_order"])
            head = "工序" + label(pass_no)
            counts[head] += 1
            execution = counts[head]
            prefix = (
                f"工程记录{process_position}"
                if flat_details
                else head + (f"_执行{execution}" if execution > 1 else "")
            )
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
                    if flat_details:
                        prefix = ""
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


def merge_tables(
    base_dir: Path | str, *, max_columns: int | None = None, max_cells: int | None = None
) -> tuple[list[str], list[Row], list[str]]:
    """读取一次查询的存档，合并系统记录，再把直接材料的前工程列挂回系统行。

    兼容没有 upstream 文件的旧存档；一旦出现前工程文件，就要求四张全部齐备。
    材料缺批次主记录时，有真实明细的仍保留这些明细。
    返回的 rows 始终以系统 LOT 为行，材料 LOT 只增加列组，不增加系统批次行数。
    """
    # 材料模块复用本文件的单 LOT 展开函数，延迟导入避免循环初始化。
    from .material_merge import merge_and_attach_materials

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
    # 第二、三阶段由材料模块完成：分工程合并，再按用料关系拼接。
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
