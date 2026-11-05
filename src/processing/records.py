import json
from numbers import Integral
from typing import Any
import pandas as pd
from src.errors import DataContractError


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def clean_rows(frame: pd.DataFrame) -> list[dict]:
    return [
        {
            k: None
            if v is None or (not isinstance(v, (list, dict)) and pd.isna(v))
            else v
            for k, v in row.items()
        }
        for row in frame.to_dict("records")
    ]


def unique(rows: list[dict], keys: list[str]) -> list[dict]:
    found: dict[tuple, dict] = {}
    for row in rows:
        key = tuple(row.get(k) for k in keys)
        if any(v is None for v in key):
            raise DataContractError(f"来源唯一键缺失：{keys}")
        if key in found and found[key] != row:
            raise DataContractError(f"同一来源键内容冲突：{keys}")
        found[key] = row
    return list(found.values())


def material_id(row: dict) -> str:
    if not isinstance(row.get("lot_no"), str) or not row["lot_no"]:
        raise DataContractError("材料 LOT 必须是非空文本。")
    for k in ("pass_order", "seq"):
        if not isinstance(row.get(k), Integral) or isinstance(row[k], bool):
            raise DataContractError("材料道次与序号必须是非空整数。")
    return canonical(
        ["public.prass_process_material", row["lot_no"], row["pass_order"], row["seq"]]
    )


def reference_status(row: dict, processes: dict) -> str:
    key = row.get("process_key")
    if key is None or key == "":
        return "PROCESS_REFERENCE_EMPTY"
    p = processes.get(key)
    if p is None:
        return "PROCESS_KEY_NOT_FOUND"
    for k in ("lot_no", "pass_order"):
        if row.get(k) is not None and p.get(k) is not None and row[k] != p[k]:
            return "PROCESS_REFERENCE_CONFLICT"
    if any(r.get(k) is None for r in (row, p) for k in ("lot_no", "pass_order")):
        return "PROCESS_REFERENCE_INCOMPLETE"
    return "PROCESS_MATCHED"
