"""材料工程：读取展示规则，并按用料关系展开制造宽表的工序0。"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from .tables import (
    DEFECT, LOT, MACHINE, PROCESS, TABLES, UPSTREAM_PREFIX, UPSTREAM_TABLES,
    Row, check_shape, label, numeric_order, read_tables,
)


DETAIL_TABLES = {"machine": MACHINE, "defect": DEFECT}
IDENTITY_FIELDS = ("material_lot_no", "process_name", "process_key", "pass_order")
COLUMN_PATTERN = re.compile(r"^工序0_([^_]+?)([0-9]+)_(.+)$", re.DOTALL)
EXECUTION_PATTERN = re.compile(r"^执行([0-9]+)_(.+)$", re.DOTALL)
# 机台设备仍按“机台设备1”“机台设备2”编号；不良改用名称做列名，只保留“不良_名称”前缀。
MACHINE_DETAIL_PATTERN = re.compile(r"^机台设备([0-9]+)_(.+)$", re.DOTALL)
DEFECT_DETAIL_PATTERN = re.compile(r"^不良_(.+)$", re.DOTALL)


@dataclass(frozen=True)
class MaterialRule:
    name: str
    aliases: tuple[str, ...] = ()
    number_by: str = "execution"
    details: tuple[str, ...] = ()


UNKNOWN = MaterialRule("未识别工程", details=(MACHINE, DEFECT))
UNMATCHED = MaterialRule("未匹配工程", details=(MACHINE, DEFECT))
NO_PROCESS = MaterialRule("未提供工程", number_by="material_lot")
FALLBACK_RULES = (UNKNOWN, UNMATCHED, NO_PROCESS)


def normalize_name(name: str) -> str:
    """允许同一个工程名带空格、连接符或不同大小写。"""
    return re.sub(r"[\s_\-]+", "", name.casefold())


@lru_cache(maxsize=1)
def load_material_rules() -> tuple[MaterialRule, ...]:
    """配置按数组顺序排列，服务重启时重新加载。"""
    path = Path(__file__).with_name("material_rules.json")
    try:
        definitions = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(definitions, list) or not definitions:
            raise ValueError("需填写至少一种工程的规则")
        rules = []
        names = {rule.name for rule in FALLBACK_RULES}
        names.update(label for label, _ in TABLES.values())
        names.add("执行")
        known_aliases = set()
        for definition in definitions:
            name = definition["name"]
            aliases = definition["aliases"]
            number_by = definition["number_by"]
            details = definition["details"]
            if not re.fullmatch(r"[^_\r\n]+(?<!\d)", name) or name != name.strip() or name in names:
                raise ValueError(f"工程名无效或重复：{name}")
            if not isinstance(aliases, list) or not aliases:
                raise ValueError(f"{name} 需配置工程别名")
            normalized = tuple(dict.fromkeys(normalize_name(alias) for alias in aliases))
            if "" in normalized or known_aliases.intersection(normalized):
                raise ValueError(f"{name} 的别名为空或与其他工程重复")
            if number_by not in ("execution", "material_lot") or not isinstance(details, list):
                raise ValueError(f"{name} 的编号依据或明细列表无效")
            tables = tuple(dict.fromkeys(DETAIL_TABLES[detail] for detail in details))
            rules.append(MaterialRule(name, normalized, number_by, tables))
            names.add(name)
            known_aliases.update(normalized)
        return tuple(rules)
    except (OSError, KeyError, TypeError, ValueError, AttributeError) as error:
        raise ValueError(f"材料工程规则 {path.name} 无法加载：{error}") from error


def material_rule(process_name: str, rules: tuple[MaterialRule, ...]) -> MaterialRule:
    normalized = normalize_name(process_name)
    return next((rule for rule in rules if normalized in rule.aliases), UNKNOWN)


def material_column_key(column: str) -> tuple | None:
    """将列名拆成工程、编号、执行和字段，按数字比较各级编号。"""
    match = COLUMN_PATTERN.fullmatch(column)
    if match is None:
        return None
    name, number, field = match.groups()
    rules = load_material_rules() + FALLBACK_RULES
    order = next((i for i, rule in enumerate(rules) if rule.name == name), None)
    if order is None:
        # 制造数据也可能有道次 0，例如“工序0_材料1_seq”。
        return None
    execution = EXECUTION_PATTERN.fullmatch(field)
    execution_number = int(execution[1]) if execution else 1
    field = execution[2] if execution else field
    if field in IDENTITY_FIELDS:
        field_key = (0, IDENTITY_FIELDS.index(field), field)
    elif machine := MACHINE_DETAIL_PATTERN.fullmatch(field):
        field_key = (2, int(machine[1]), machine[2])
    elif defect := DEFECT_DETAIL_PATTERN.fullmatch(field):
        field_key = (3, 0, defect[1])
    else:
        field_key = (1, 0, field)
    return order, name, int(number), execution_number, *field_key


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


def defect_naming(records: list[Row]) -> tuple[Callable[[str, str], str], bool]:
    """不良名称适合做列名，但同名不同码或同码不同名时退回代码，避免歧义。

    与制造工程一致：名称唯一时直接用名称做列名，名称有歧义时补上代码区分。
    """
    name_codes: dict[str, set[str]] = defaultdict(set)
    code_names: dict[str, set[str]] = defaultdict(set)
    for row in records:
        name_codes[row["defect_name"]].add(row["defect_code"])
        code_names[row["defect_code"]].add(row["defect_name"])
    keep_name = any(
        not name or len(code_names[code]) > 1
        for name, codes in name_codes.items()
        for code in codes
    )

    def title(code: str, defect_name: str) -> str:
        if defect_name and len(code_names[code]) == 1:
            text = label(defect_name)
        else:
            text = "代码" + label(code)
        if defect_name and len(name_codes[defect_name]) > 1 and len(code_names[code]) == 1:
            text += "_代码" + label(code)
        return text

    return title, keep_name


def execution_fields(
    process: Row,
    details: dict[str, list[Row]],
    rule: MaterialRule,
    naming: tuple[Callable[[str, str], str], bool],
) -> Row:
    """展开一次执行。不良以种类做列名，数量写在列下，代码另起子列。"""
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
        if source == DEFECT:
            title_of, keep_name = naming
            repeats: Counter[str] = Counter()
            for record in records:
                code, defect_name = record["defect_code"], record["defect_name"]
                if not code:
                    raise ValueError("不良代码为空，无法确定项目身份")
                title = title_of(code, defect_name)
                repeats[title] += 1
                position = repeats[title]
                detail = "不良_" + title + (f"_记录{position}" if position > 1 else "")
                # 同一种不良若出现多次，用“记录2”等后缀区分，不累计数量。
                for field, value in record.items():
                    if field in ("lot_no", "process_key", "pass_order", "defect_qty"):
                        continue
                    if field == "defect_name":
                        if not keep_name:
                            continue
                        suffix = field
                    elif field == "defect_code":
                        suffix = "代码"
                    else:
                        if value == "":
                            continue
                        suffix = field
                    add(f"{detail}_{suffix}", value)
                add(detail, record.get("defect_qty", ""))
            continue
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
    naming = defect_naming(tables[DEFECT])
    for key, (process, rule) in sorted(parents.items(), key=lambda item: (item[0][0], item[0][2], item[0][1])):
        by_lot[key[0]].append(MaterialExecution(rule, execution_fields(process, details.get(key, {}), rule, naming)))

    for row in tables[LOT]:
        if row["lot_no"] not in by_lot:
            fields = execution_fields({"lot_no": row["lot_no"]}, {}, NO_PROCESS, naming)
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
