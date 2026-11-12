"""原始表约定与 CSV 处理：表结构、LOT 规则、校验、读写及大小限制。"""

from __future__ import annotations

import codecs
import csv
import os
import re
from collections.abc import Callable, Hashable, Iterable
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from tempfile import NamedTemporaryFile


PRODUCT_FIELDS = ("murata_type", "toko_type")

LOT = "prass_lot"
PROCESS = "prass_lot_process"
DEFECT = "prass_process_defect"
MATERIAL = "prass_process_material"
MACHINE = "prass_process_machine"
UPSTREAM_PREFIX = "upstream_"
# 前工程只读取批次、工序、不良和机台，不继续递归查询材料的材料。
UPSTREAM_TABLES = (LOT, PROCESS, DEFECT, MACHINE)

# 值的结构为（中文明细名，候选键字段）。
# process_key 区分具体工序执行；pass_order 只表示道次，不能单独充当工序身份。
# 此字典的插入顺序也用于导出顺序和宽表中各类明细的排列。
TABLES = {
    LOT: ("批次", ("lot_no",)),
    PROCESS: ("工序", ("lot_no", "process_key")),
    DEFECT: ("不良", ("lot_no", "process_key", "pass_order", "defect_code")),
    MATERIAL: ("材料", ("lot_no", "process_key", "pass_order", "seq")),
    "prass_process_tool": ("工具", ("lot_no", "process_key", "pass_order", "tool_code")),
    "prass_process_worker": ("人员", ("lot_no", "process_key", "pass_order", "worker_code")),
    MACHINE: ("机台设备", ("lot_no", "process_key", "pass_order", "machine_code")),
}

EXTRA_FIELDS = {
    PROCESS: {"pass_order"},
    DEFECT: {"defect_name", "defect_qty"},
    MATERIAL: {"material_lot_no"},
}


class ExportSizeError(ValueError):
    """完整批次超出资源预算，可缩小 LOT 批次重试；不代表原始数据有误。"""


def validate_columns(table: str, fields: list[str]) -> None:
    """检查关联/合并所需的最少字段，并拒绝会造成取值歧义的重复列名。

    允许出现更多业务字段：取数采用 SELECT *，合并时会继续处理这些原始字段。
    本函数只校验表头，记录级的候选键重复由 read_tables 检查。
    """
    needed = set(TABLES[table][1]) | EXTRA_FIELDS.get(table, set())
    missing = needed - set(fields)
    if missing:
        raise ValueError(f"{table} 缺少字段：{', '.join(sorted(missing))}")
    if len(fields) != len(set(fields)):
        raise ValueError(f"{table} 存在重复列名")


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

    stream 可以是手动工具的文本文件，也可以是 BoundedCsv；
    具体编码和文件大小控制由调用方负责，本函数只负责 CSV 格式与公式样式文本处理。
    """
    writer = csv.writer(stream)
    writer.writerow(map(spreadsheet_text, columns))
    for row in rows:
        writer.writerow(spreadsheet_text(row.get(column, "")) for column in columns)


class BoundedCsv:
    """给二进制输出流包一层文本 write 接口，逐次检查 UTF-8 字节预算。

    csv.writer 负责引号、逗号和换行转义；本类负责编码、BOM 和文件大小限制。
    中文字符可能占多个字节，所以按编码后的字节数计量，不能只计算字符数。
    """

    def __init__(self, stream, available: int, label: str):
        """available 是当前文件可用的剩余字节数，包含 UTF-8 BOM 和表头。"""
        self.stream = stream
        self.available = available
        self.label = label
        self.size = 0
        # BOM 便于 Excel 识别 UTF-8 中文，原始记录内容仍由 csv.writer 原样写入。
        self.write_bytes(codecs.BOM_UTF8)

    def write_bytes(self, content: bytes) -> None:
        """在写入前检查上限，超限抛错而不是留下看似完整的截断文件。"""
        if self.size + len(content) > self.available:
            raise ExportSizeError(f"{self.label}超过文件大小上限，请缩小查询范围。")
        self.stream.write(content)
        self.size += len(content)

    def write(self, text: str) -> None:
        """供 csv.writer 调用：把一段 CSV 文本编码后交给受限写入方法。"""
        self.write_bytes(text.encode("utf-8"))


def is_system_lot(lot_no: str) -> bool:
    """仅检查系统 LOT；不对材料 LOT 应用此条件。"""
    return lot_no.startswith("E") and lot_no.count("E") == 1


def filter_final_rows(rows: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    """只检查顶层系统 LOT；该行的材料 LOT 和所有单元格保持不变。"""
    return [row for row in rows if is_system_lot(row["lot_no"])]


def filter_csv(source: str | Path, output: str | Path | None = None) -> Path:
    """筛选已有最终 CSV，保留表头及列顺序，写新文件而不覆盖输入文件。"""
    source = Path(source)
    destination = Path(output) if output is not None else source.with_name(source.stem + "_E_only.csv")
    if destination.resolve() == source.resolve() or (
        destination.exists() and source.exists() and os.path.samefile(source, destination)
    ):
        raise ValueError("输出文件不能覆盖输入 CSV。")

    temporary = None
    kept = removed = 0
    try:
        with source.open(encoding="utf-8-sig", newline="") as input_stream:
            reader = csv.DictReader(input_stream)
            fields = reader.fieldnames or []
            if "lot_no" not in fields or len(fields) != len(set(fields)):
                raise ValueError("输入 CSV 必须有唯一的 lot_no 列，且表头不能重复。")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(
                mode="w", encoding="utf-8-sig", newline="", dir=destination.parent, delete=False
            ) as output_stream:
                temporary = Path(output_stream.name)
                writer = csv.DictWriter(output_stream, fieldnames=fields)
                writer.writeheader()
                for row in reader:
                    if None in row or any(value is None for value in row.values()):
                        raise ValueError("输入 CSV 行与表头不一致。")
                    if is_system_lot(row["lot_no"]):
                        writer.writerow(row)
                        kept += 1
                    else:
                        removed += 1
            os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(f"已筛选：保留 {kept} 行，排除 {removed} 行 -> {destination}")
    return destination
