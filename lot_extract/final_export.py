"""流式对齐分片表头，生成唯一最终 CSV 和 ZIP，不全量加载年度行数据。"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import monotonic
from zipfile import ZIP_DEFLATED, ZipFile

from .presentation import is_system_lot


def column_rank(column: str, position: int) -> tuple:
    material = re.match(r"^工序0-(.+?)_(\d+)(?:_|$)", column)
    if material:
        name, number = material.groups()
        normalized = name.casefold().replace("-", "").replace(" ", "")
        engineering = 0 if "ecore" in normalized else 1 if "icore" in normalized else 2 if "卷" in name or "winding" in normalized else 3
        return 1, engineering, name, int(number), position
    process = re.match(r"^工序(\d+)(?:_执行(\d+))?_", column)
    if process:
        return 2, int(process[1]), int(process[2] or "1"), position
    if column.startswith("工序0-"):
        return 1, 4, column.split("_", 1)[0], 0, position
    if column.startswith("工序"):
        return 2, float("inf"), 1, position
    return (3 if column.startswith("未匹配") else 0), position


def join_parts(
    directory: Path,
    parts: list[dict],
    check: Callable[[], None],
    progress: Callable[[int, int], None],
) -> dict:
    """收集列名并逐行补空对齐；CSV 内容已转义，不重复做公式转义。"""
    fields: dict[str, int] = {}
    for part in parts:
        check()
        with (directory / part["file"]).open(encoding="utf-8-sig", newline="") as stream:
            header = next(csv.reader(stream), [])
        if not header or "lot_no" not in header or len(header) != len(set(header)):
            raise ValueError(f"{part['file']} 的表头为空、缺少 LOT 或存在重复列。")
        for field in header:
            fields.setdefault(field, len(fields))
    columns = sorted(fields, key=lambda column: column_rank(column, fields[column]))
    expected = sum(part["rows"] for part in parts)
    temporary = None
    rows = 0
    last_update = monotonic()
    destination = directory / "final.csv"
    progress(0, expected)
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8-sig", newline="", dir=directory, delete=False
        ) as output:
            temporary = Path(output.name)
            writer = csv.writer(output)
            writer.writerow(columns)
            for part in parts:
                check()
                count = 0
                with (directory / part["file"]).open(encoding="utf-8-sig", newline="") as stream:
                    reader = csv.DictReader(stream)
                    for row in reader:
                        check()
                        if None in row or any(value is None for value in row.values()):
                            raise ValueError(f"{part['file']} 的行宽与表头不一致。")
                        if not is_system_lot(row["lot_no"]):
                            raise ValueError(f"{part['file']} 包含不满足仅以一个 E 开头规则的系统 LOT。")
                        writer.writerow(row.get(column, "") for column in columns)
                        count += 1
                        rows += 1
                        if monotonic() - last_update >= 1:
                            progress(rows, expected)
                            last_update = monotonic()
                if count != part["rows"]:
                    raise ValueError(f"{part['file']} 行数与任务清单不一致，不能发布完整结果。")
        check()
        os.replace(temporary, destination)
        progress(rows, expected)
        return {
            "file": destination.name, "rows": rows, "columns": len(columns),
            "bytes": destination.stat().st_size,
            "schema_sha256": hashlib.sha256(json.dumps(columns, ensure_ascii=False).encode()).hexdigest(),
            "exceeds_excel_columns": len(columns) > 16384,
            "exceeds_excel_rows": rows > 1048575,
        }
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def package_csv(
    directory: Path,
    final_csv: dict,
    check: Callable[[], None],
    progress: Callable[[int, int], None],
) -> dict:
    """ZIP 中只有一份完整 CSV 和结果说明；按块压缩并支持超过 4 GiB 的文件。"""
    destination = directory / "result.zip"
    temporary = None
    size = final_csv["bytes"]
    written = 0
    last_update = monotonic()
    progress(0, size)
    try:
        with NamedTemporaryFile(dir=directory, delete=False) as stream:
            temporary = Path(stream.name)
        with ZipFile(temporary, "w", compression=ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
            with (directory / final_csv["file"]).open("rb") as source, archive.open(
                "final.csv", "w", force_zip64=True
            ) as output:
                while content := source.read(4 * 1024 * 1024):
                    check()
                    output.write(content)
                    written += len(content)
                    if monotonic() - last_update >= 1:
                        progress(written, size)
                        last_update = monotonic()
            archive.writestr("export_summary.json", json.dumps(final_csv, ensure_ascii=False, indent=2))
        check()
        os.replace(temporary, destination)
        progress(written, size)
        return {"file": destination.name, "bytes": destination.stat().st_size}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="将已完成任务的分片拼接为一个 CSV 并打包，无需重新查询数据库。")
    parser.add_argument("directory", type=Path, help="包含 manifest.json 的已完成任务目录")
    args = parser.parse_args()
    try:
        root = args.directory
        manifest = root / "manifest.json"
        state = json.loads(manifest.read_text(encoding="utf-8"))
        if state.get("status") != "complete" or not state.get("parts"):
            raise ValueError("只支持有分片的已完成任务，不能将未完成任务标记为完整结果。")
        if (root / "final.csv").exists() or (root / "result.zip").exists():
            raise ValueError("任务目录已有最终结果，不能覆盖。")
        for part in state["parts"]:
            if Path(part["file"]).name != part["file"]:
                raise ValueError("清单中的分片文件名无效。")
        report = lambda done, total: print(f"{done}/{total}", flush=True)
        state["final_csv"] = join_parts(root, state["parts"], lambda: None, report)
        state["final_zip"] = package_csv(root, state["final_csv"], lambda: None, report)
        state["progress"], state["phase"] = 1.0, "complete"
        temporary = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=root, delete=False) as output:
                temporary = Path(output.name)
                json.dump(state, output, ensure_ascii=False, indent=2)
            os.replace(temporary, manifest)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        print(f"完整 CSV：{root / 'final.csv'}\n结果包：{root / 'result.zip'}")
    except (Exception, KeyboardInterrupt) as error:
        parser.exit(1, f"拼接未完成：{error or '操作已取消'}\n")
