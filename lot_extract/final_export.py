"""流式对齐分片表头并生成最终 CSV。"""

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

from .material_merge import (
    FALLBACK_RULES, ExportRules, MaterialRule, archive_rules, load_material_rules,
    material_column_key, normalize_name,
)
from .tables import LotPolicy, inherited_columns, numeric_order


def column_rank(column: str, position: int, rules: tuple[MaterialRule, ...] | None = None) -> tuple:
    """统一列顺序；材料按产品配置排序，同时兼容旧版“工序0-”表头。"""
    if key := material_column_key(column, rules):
        return (1, *key)
    material = re.match(r"^工序0-(.+?)_(\d+)(?:_|$)", column)
    if material:
        name, number = material.groups()
        configured = (load_material_rules() if rules is None else rules) + FALLBACK_RULES
        normalized = normalize_name(name)
        engineering = next((i for i, rule in enumerate(configured)
                            if normalized == normalize_name(rule.name) or normalized in rule.aliases),
                           len(configured))
        return 1, engineering, name, int(number), position
    if column.startswith("工序0-"):
        configured = load_material_rules() if rules is None else rules
        return 1, len(configured) + len(FALLBACK_RULES), column, 0, position
    if rules is not None:
        process = re.fullmatch(r"工序([^_]+)(?:_执行([0-9]+))?_(.+)", column, re.DOTALL)
        if process:
            field = process[3]
            natural = tuple(int(part) if i % 2 else part
                            for i, part in enumerate(re.split(r"([0-9]+)", field)))
            return 2, numeric_order(process[1]), int(process[2] or "1"), natural, field
        if column.startswith("未匹配"):
            return 3, column
        return 0, position
    process = re.match(r"^工序(\d+)(?:_执行(\d+))?_", column)
    if process:
        return 2, int(process[1]), int(process[2] or "1"), position
    if column.startswith("工序"):
        return 2, float("inf"), 1, position
    return (3 if column.startswith("未匹配") else 0), position


def join_parts(
    directory: Path,
    parts: list[dict],
    check: Callable[[], None],
    progress: Callable[[int, int], None],
    *,
    rules: ExportRules | None = None,
    inheritance: dict[str, str | None] | None = None,
) -> dict:
    """先确定最终表头，再逐行按位置对齐；只在全部校验通过后发布 final.csv。

    第一遍仅读各分片表头，结合全任务证据确定省略列；第二遍逐行读写实际数据。
    内存保留列集合、每片指纹和当前行，不收集整个年度宽表。输入已做公式转义，
    这里保留原文本，不能再次添加单引号。
    """
    fields: dict[str, int] = {}
    fingerprints = []
    for part in parts:
        check()
        with (directory / part["file"]).open(encoding="utf-8-sig", newline="") as stream:
            header = next(csv.reader(stream), [])
        if not header or "lot_no" not in header or len(header) != len(set(header)):
            raise ValueError(f"{part['file']} 的表头为空、缺少 LOT 或存在重复列。")
        fingerprints.append(hashlib.sha256(json.dumps(header, ensure_ascii=False).encode()).digest())
        for field in header:
            fields.setdefault(field, len(fields))
    omitted = inherited_columns(fields, inheritance or {})
    # 旧任务没有 inheritance 时不猜测重复列，仍可按原始列集合完成拼接。
    material_rules = rules.materials if rules is not None else None
    policy = rules.lot if rules is not None else LotPolicy()
    columns = sorted((column for column in fields if column not in omitted),
                     key=lambda column: column_rank(column, fields[column], material_rules))
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
            for part, fingerprint in zip(parts, fingerprints, strict=True):
                check()
                count = 0
                with (directory / part["file"]).open(encoding="utf-8-sig", newline="") as stream:
                    reader = csv.reader(stream)
                    header = next(reader, [])
                    if hashlib.sha256(json.dumps(header, ensure_ascii=False).encode()).digest() != fingerprint:
                        raise ValueError(f"{part['file']} 的表头在拼接期间发生变化。")
                    width = len(header)
                    positions = {name: index for index, name in enumerate(header)}
                    lot_index = positions["lot_no"]
                    # 每片只计算一次“最终列 -> 本片位置”。缺列指向 width，
                    # 也就是后面追加的空字符串；表头完全一致时直接写原列表。
                    indices = None if header == columns else [positions.get(column, width) for column in columns]
                    for row in reader:
                        check()
                        if len(row) != width:
                            raise ValueError(f"{part['file']} 的行宽与表头不一致。")
                        if not policy.matches_csv(row[lot_index]):
                            raise ValueError(f"{part['file']} 包含不符合产品规则的系统 LOT。")
                        if indices is None:
                            writer.writerow(row)
                        else:
                            row.append("")
                            writer.writerow([row[index] for index in indices])
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
            "omitted_columns": omitted,
            "bytes": destination.stat().st_size,
            "schema_sha256": hashlib.sha256(json.dumps(columns, ensure_ascii=False).encode()).hexdigest(),
            "exceeds_excel_columns": len(columns) > 16384,
            "exceeds_excel_rows": rows > 1048575,
        }
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def join_saved_export(directory: Path, *, product: str | None = None) -> Path:
    """离线拼接一个已完成的存档任务；已有完整结果时拒绝覆盖。"""
    root = Path(directory)
    manifest = root / "manifest.json"
    state = json.loads(manifest.read_text(encoding="utf-8"))
    if state.get("status") != "complete" or not state.get("parts"):
        raise ValueError("只支持有分片的已完成任务，不能将未完成任务标记为完整结果。")
    if (root / "final.csv").exists():
        raise ValueError("任务目录已有最终结果，不能覆盖。")
    for part in state["parts"]:
        if Path(part["file"]).name != part["file"]:
            raise ValueError("清单中的分片文件名无效。")
    report = lambda done, total: print(f"{done}/{total}", flush=True)
    rules = archive_rules(state, product)
    state["final_csv"] = join_parts(
        root, state["parts"], lambda: None, report,
        rules=rules if "rules" in state or product is not None or rules.product != "DFEC" else None,
        inheritance=state.get("inheritance"),
    )
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
    return root / "final.csv"
