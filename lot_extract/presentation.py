"""最终呈现规则：系统 lot_no 必须以大写 E 开头，且仅含一个大写 E。

不修改数据库取数、原始存档或材料关联信息。
也可单独执行：python -m lot_extract.presentation "合并表.csv" --output "仅E批次.csv"
"""

from __future__ import annotations

import argparse
import csv
import os
from collections.abc import Iterable
from pathlib import Path
from tempfile import NamedTemporaryFile


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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="已有的最终合并 CSV")
    parser.add_argument("--output", type=Path, help="输出路径；默认生成 *_E_only.csv")
    args = parser.parse_args()
    try:
        filter_csv(args.source, args.output)
    except (OSError, ValueError, csv.Error) as error:
        parser.exit(1, f"筛选失败：{error}\n")
