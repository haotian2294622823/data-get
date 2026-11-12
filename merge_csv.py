"""手动将同一查询目录下的原始 CSV 合并成宽表。

运行示例：python merge_csv.py "outputs/queries/DFEC/查询编号" --output "outputs/宽表.csv"
本入口只读本地 CSV，不连接数据库；与页面共用 lot_extract.merge 的关联和展开规则。
"""

from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from lot_extract.merge import merge_tables, write_wide
from lot_extract.presentation import filter_final_rows
from lot_extract.schema import TABLES, UPSTREAM_PREFIX, UPSTREAM_TABLES


def main(base_dir: str | Path | None = None, output_path: str | Path | None = None) -> Path:
    """合并并保存宽表，返回输出路径；未指定目录时读取脚本所在目录。

    手动入口未传入页面的列数/单元格上限，大目录仍需考虑本机内存。
    notices 是保留数据后产生的提示，不等同于会中止导出的异常。
    """
    base = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parent
    destination = Path(output_path) if output_path is not None else base / "批次全字段宽表.csv"
    sources = [f"{table}.csv" for table in TABLES]
    sources.extend(f"{UPSTREAM_PREFIX}{table}.csv" for table in UPSTREAM_TABLES)
    # resolve 统一相对路径和绝对路径写法，防止用户把输出文件指定成某张原始表。
    if destination.resolve() in {(base / source).resolve() for source in sources}:
        raise ValueError("输出文件不能覆盖原始表。")
    columns, rows, notices = merge_tables(base)
    total = len(rows)
    rows = filter_final_rows(rows)
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
        print(f"最终呈现仅保留 E 开头的系统 LOT，已排除 {total - len(rows)} 行。")
    for notice in notices:
        print("提示：" + notice)
    return destination


if __name__ == "__main__":
    # 命令行入口负责参数解析；导入本模块时不会自动运行合并。
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        main(args.directory, args.output)
    except (OSError, ValueError, csv.Error) as error:
        parser.exit(1, f"合并失败：{error}\n")
