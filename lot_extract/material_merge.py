"""材料工程：读取展示规则，并按用料关系展开制造宽表的工序0。"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from .tables import (
    DEFECT, LOT, MACHINE, PROCESS, TABLES, UPSTREAM_PREFIX, UPSTREAM_TABLES,
    BATCH_FIELDS, PROCESS_FIELDS, PROFILE_CODE, QUANTITY_FIELDS,
    LotPolicy, Row, check_shape, clean_qty, label, load_lot_policy, note_inheritance, numeric_order, read_tables,
)


DETAIL_TABLES = {"machine": MACHINE, "defect": DEFECT}
IDENTITY_FIELDS = ("material_lot_no", "process_name", "process_key", "pass_order")
COLUMN_PATTERN = re.compile(r"^工序0_([^_]+?)([0-9]+)_(.+)$", re.DOTALL)
EXECUTION_PATTERN = re.compile(r"^执行([0-9]+)_(.+)$", re.DOTALL)
# 机台设备仍按“机台设备1”“机台设备2”编号；不良按稳定代码排列。
MACHINE_DETAIL_PATTERN = re.compile(r"^机台设备([0-9]+)_(.+)$", re.DOTALL)
DEFECT_DETAIL_PATTERN = re.compile(r"^不良_(.+)$", re.DOTALL)


@dataclass(frozen=True)
class MaterialRule:
    """一类材料工程的展示约定，字段与产品 JSON 的每个对象对应。

    execution 按每次工程执行编号；material_lot 按不同材料编号，额外执行留在组内。
    details 保存允许展开的表名，设备和不良是否展示由配置决定。
    """

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


def parse_material_rules(definitions: list) -> tuple[MaterialRule, ...]:
    try:
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
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise ValueError(f"材料工程规则无效：{error}") from error


@lru_cache(maxsize=32)
def load_material_rules(product: str = "DFEC") -> tuple[MaterialRule, ...]:
    """加载产品专用规则；缺少新产品文件时明确失败，防止误套 DFEC 规则。

    缓存只用于减少重复解析，改动 JSON 后需重启服务。已创建任务还会保存快照，
    后续离线重做优先使用快照，不受当前配置文件变化影响。
    """
    if not PROFILE_CODE.fullmatch(product):
        raise ValueError("产品代码格式无效。")
    name = "material_rules.json" if product == "DFEC" else f"material_rules_{product.lower()}.json"
    path = Path(__file__).with_name(name)
    try:
        return parse_material_rules(json.loads(path.read_text(encoding="utf-8-sig")))
    except (OSError, ValueError) as error:
        raise ValueError(f"无法加载 {product} 的材料规则 {name}：{error}") from error


@dataclass(frozen=True)
class ExportRules:
    """绑定产品、LOT 筛选和材料展示规则，作为任务配置快照的唯一来源。"""

    product: str
    lot: LotPolicy
    materials: tuple[MaterialRule, ...]

    def snapshot(self) -> dict:
        # JSON 只记录业务规则，不包含数据库地址、账号或密码。
        detail_names = {table: name for name, table in DETAIL_TABLES.items()}
        return {
            "version": 1, "product": self.product,
            "lot_prefix": self.lot.prefix, "lot_prefix_once": self.lot.once,
            "materials": [
                {"name": rule.name, "aliases": list(rule.aliases), "number_by": rule.number_by,
                 "details": [detail_names[table] for table in rule.details]}
                for rule in self.materials
            ],
        }

    @classmethod
    def from_snapshot(cls, snapshot: dict) -> ExportRules:
        if snapshot.get("version") != 1 or not PROFILE_CODE.fullmatch(snapshot.get("product", "")):
            raise ValueError("任务的规则快照版本或产品代码无效。")
        return cls(snapshot["product"], LotPolicy(snapshot["lot_prefix"], snapshot["lot_prefix_once"]),
                   parse_material_rules(snapshot["materials"]))


def load_export_rules(product: str = "DFEC") -> ExportRules:
    return ExportRules(product, load_lot_policy(product), load_material_rules(product))


def archive_rules(manifest: dict, product: str | None = None) -> ExportRules:
    """优先还原存档快照；--product 不能覆盖快照中的产品身份。

    无快照的存档才读取指定产品的当前配置。旧 DFEC 存档默认沿用原 E 前缀规则，
    保证仅修改本地环境变量不会让历史文件在离线拼接时突然丢行。
    """
    if "rules" in manifest:
        rules = ExportRules.from_snapshot(manifest["rules"])
        if product is not None and product != rules.product:
            raise ValueError("指定产品与存档中的规则快照不一致。")
        return rules
    recorded = manifest.get("product", "DFEC")
    if product is not None and "product" in manifest and product != recorded:
        raise ValueError("指定产品与存档的产品代码不一致。")
    if product is not None or recorded != "DFEC":
        return load_export_rules(product or recorded)
    # 老存档使用当时固定的 E 规则；未提供产品时不套用当前环境变量。
    return ExportRules("DFEC", LotPolicy(), load_material_rules())


def material_rule(process_name: str, rules: tuple[MaterialRule, ...]) -> MaterialRule:
    normalized = normalize_name(process_name)
    return next((rule for rule in rules if normalized in rule.aliases), UNKNOWN)


def material_column_key(column: str, rules: tuple[MaterialRule, ...] | None = None) -> tuple | None:
    """将列名拆成工程、编号、执行和字段，按数字比较各级编号。"""
    match = COLUMN_PATTERN.fullmatch(column)
    if match is None:
        return None
    name, number, field = match.groups()
    rules = (load_material_rules() if rules is None else rules) + FALLBACK_RULES
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
    inheritance: dict[str, str | None]


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


def execution_fields(
    process: Row,
    details: dict[str, list[Row]],
    rule: MaterialRule,
    *,
    matched: bool,
) -> tuple[Row, dict[str, str | None]]:
    """保留明细原值，同时记录哪些列可从同一执行的工艺字段还原。

    返回值分别是该次执行的字段，以及“明细字段 -> 父字段/None”的比较结果。
    此处不直接删列：本材料相同的字段，在其他材料或后续分片中仍可能不同。
    """
    fields = {field: process.get(field, "") for field in IDENTITY_FIELDS}
    fields["material_lot_no"] = process["lot_no"]
    inheritance: dict[str, str | None] = {}

    def add(name: str, field: str, value: str) -> None:
        if name in fields:
            raise ValueError(f"材料工程字段名冲突：{name}")
        fields[name] = clean_qty(value) if field in QUANTITY_FIELDS else value

    for field, value in process.items():
        if value == "" or field in ("lot_no", "process_name", "process_key", "pass_order"):
            continue
        add("工艺_material_lot_no" if field == "material_lot_no" else field, field, value)

    for source in rule.details:
        code_field = "machine_code" if source == MACHINE else "defect_code"
        records = sorted(details.get(source, []), key=lambda row: (numeric_order(row[code_field]), row[code_field]))
        repeats: Counter[str] = Counter()
        for number, record in enumerate(records, 1):
            if source == DEFECT:
                # 名称可能因改名或空白不同而变化；用原始代码固定列身份，名称放单元格。
                code = record[code_field]
                if not code:
                    raise ValueError("不良代码为空，无法确定项目身份")
                repeats[code] += 1
                detail = "不良_代码" + label(code)
                if repeats[code] > 1:
                    detail += f"_记录{repeats[code]}"
                add(detail, "defect_qty", record["defect_qty"])
                add(detail + "_名称", "defect_name", record["defect_name"])
                if not (record["defect_qty"] or record["defect_name"]):
                    # 数量与名称都为空时，用原代码区分实际发生的记录与空槽位。
                    add(detail + "_代码", "defect_code", code)
            else:
                detail = f"{TABLES[source][0]}{number}"
            for field, value in record.items():
                if source == DEFECT and field in ("defect_qty", "defect_name", "defect_code"):
                    continue
                inheritable = field in BATCH_FIELDS or field in PROCESS_FIELDS
                if not inheritable and value == "" and field != code_field:
                    continue
                column = f"{detail}_{field}"
                add(column, field, value)
                if inheritable:
                    parent = "material_lot_no" if field == "lot_no" else field
                    # 缺少机台代码时保留 LOT 作为实际明细存在的标识。
                    has_identity = field != "lot_no" or bool(record[code_field])
                    # 空字符串也要参与比较，不能把“父字段有值、明细为空”当成可继承。
                    same = matched and has_identity and field in process and fields.get(parent) == value
                    inheritance[column] = parent if same else None
    return fields, inheritance


def material_executions(tables: dict[str, list[Row]], rules: tuple[MaterialRule, ...]) -> tuple[dict[str, list[MaterialExecution]], list[str]]:
    """为每个材料建立执行索引，供使用同一材料的各个制造 LOT 复用。"""
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
    for key, (process, rule) in sorted(parents.items(), key=lambda item: (item[0][0], item[0][2], item[0][1])):
        fields, inheritance = execution_fields(process, details.get(key, {}), rule, matched=rule != UNMATCHED)
        by_lot[key[0]].append(MaterialExecution(rule, fields, inheritance))

    for row in tables[LOT]:
        if row["lot_no"] not in by_lot:
            fields, inheritance = execution_fields({"lot_no": row["lot_no"]}, {}, NO_PROCESS, matched=False)
            by_lot[row["lot_no"]].append(MaterialExecution(NO_PROCESS, fields, inheritance))
            counts["没有工程记录的材料 LOT"] += 1

    notices = [f"前工程有 {count} 条{reason}，已保留在对应列组" for reason, count in counts.items()]
    return by_lot, notices


def merge_and_attach_materials(
    base: Path,
    columns: list[str],
    rows: list[Row],
    records: list[Row],
    *,
    rules: tuple[MaterialRule, ...],
    inheritance: dict[str, str | None],
    max_columns: int | None = None,
    max_cells: int | None = None,
) -> tuple[list[str], list[str]]:
    usages = material_usages(records)
    _, tables = read_tables(base, UPSTREAM_TABLES, UPSTREAM_PREFIX, pass_order_key=numeric_order)
    requested = {lot for materials in usages.values() for lot in materials}
    if any(record["lot_no"] not in requested for table in tables.values() for record in table):
        raise ValueError("前工程 CSV 包含用料关系中不存在的 LOT，请核对查询目录。")
    executions, notices = material_executions(tables, rules)
    missing = requested - executions.keys()
    if missing:
        notices.append(f"有 {len(missing)} 个材料 LOT 未查到前工程数据，制造用料列保留原编号")

    primary = set(columns)
    added: set[str] = set()
    repeated: set[tuple[str, str]] = set()
    for row in rows:
        # 编号在每个制造 LOT 内重新开始，不按整次任务累计，避免材料槽位无界增长。
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
                    if field in event.inheritance:
                        parent = event.inheritance[field]
                        note_inheritance(inheritance, column, prefix + parent if parent is not None else None)

    for name, count in sorted(Counter(name for name, _ in repeated).items()):
        notices.append(f"{name}有 {count} 个材料 LOT 出现多次执行，已保留在同一材料列组")
    return [*columns, *sorted(added, key=lambda column: material_column_key(column, rules))], notices
