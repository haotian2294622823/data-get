"""读取工序0的展示规则，并统一分片和最终宽表的列排序。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .schema import DEFECT, MACHINE, TABLES


DETAIL_TABLES = {"machine": MACHINE, "defect": DEFECT}
IDENTITY_FIELDS = ("material_lot_no", "process_name", "process_key", "pass_order")
COLUMN_PATTERN = re.compile(r"^工序0_([^_]+?)([0-9]+)_(.+)$", re.DOTALL)
EXECUTION_PATTERN = re.compile(r"^执行([0-9]+)_(.+)$", re.DOTALL)
DETAIL_PATTERN = re.compile(r"^(机台设备|不良)([0-9]+)_(.+)$", re.DOTALL)


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
    path = Path(__file__).with_suffix(".json")
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
    elif detail := DETAIL_PATTERN.fullmatch(field):
        source = 2 if detail[1] == TABLES[MACHINE][0] else 3
        field_key = (source, int(detail[2]), detail[3])
    else:
        field_key = (1, 0, field)
    return order, name, int(number), execution_number, *field_key
