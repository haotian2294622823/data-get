from pathlib import Path
import hashlib
import yaml
from src.models import Limits
from src.errors import DataContractError

BASE = Path(__file__).resolve().parents[1]
FIXED = {
    "genealogy_edges": [
        "root_lot",
        "event_id",
        "downstream_lot",
        "upstream_lot",
        "process_key",
        "pass_order",
        "seq",
        "match_status",
        "reference_status",
    ],
    "genealogy_paths": [
        "root_lot",
        "path_id",
        "parent_path_id",
        "event_id",
        "depth",
        "lot_path",
        "event_path",
        "terminal_reason",
    ],
    "process_records": [
        "root_lot",
        "lot_no",
        "process_key",
        "min_depth",
        "pass_order",
        "process_code",
        "process_name",
    ],
    "material_records": [
        "root_lot",
        "source_record_id",
        "lot_no",
        "material_lot_no",
        "process_key",
        "pass_order",
        "seq",
        "match_status",
        "reference_status",
    ],
    "machine_records": [
        "root_lot",
        "source_id",
        "source_record_id",
        "lot_no",
        "process_key",
        "pass_order",
        "machine_code",
        "reference_status",
    ],
    "defect_records": [
        "root_lot",
        "source_id",
        "source_record_id",
        "lot_no",
        "process_key",
        "pass_order",
        "defect_code",
        "reference_status",
    ],
}
KEYS = {
    "genealogy_edges": ["root_lot", "event_id"],
    "genealogy_paths": ["root_lot", "path_id"],
    "process_records": ["root_lot", "lot_no", "process_key"],
    "material_records": ["root_lot", "source_record_id"],
    "machine_records": ["root_lot", "source_id", "source_record_id"],
    "defect_records": ["root_lot", "source_id", "source_record_id"],
}
LABELS = {
    "root_lot": "根LOT",
    "lot_no": "LOT",
    "downstream_lot": "下游LOT",
    "upstream_lot": "上游LOT",
    "material_lot_no": "材料LOT",
    "process_key": "工序键",
    "pass_order": "道次",
    "seq": "序号",
    "match_status": "批号匹配状态",
    "reference_status": "工序引用状态",
    "min_depth": "最小深度",
    "process_code": "工序代码",
    "process_name": "工序名称",
    "depth": "深度",
    "terminal_reason": "终止原因",
    "lot_path": "LOT路径",
    "event_path": "事件路径",
    "machine_code": "设备代码",
    "defect_code": "不良代码",
}


def load_config(mode: str) -> tuple[dict, Limits, str]:
    if mode not in ("mock", "real"):
        raise DataContractError("DATA_SOURCE_MODE 只能为 mock 或 real。")
    paths = [
        BASE / "config" / f"fields_{'demo' if mode == 'mock' else 'real'}.yaml",
        BASE / "config/limits.yaml",
    ]
    raw = [p.read_bytes() for p in paths]
    catalog, values = [yaml.safe_load(v) for v in raw]
    ids = [f["field_id"] for f in catalog["fields"]]
    if len(ids) != len(set(ids)) or not set(catalog["datasets"]) <= FIXED.keys():
        raise DataContractError("字段目录存在重复 ID 或非法数据集。")
    for f in catalog["fields"]:
        if (
            not f["verified"]
            or f["aggregation"] != "identity"
            or f["source_id"] != f["dataset"]
        ):
            raise DataContractError("字段未经核实，或字段来源/粒度不受支持。")
    limits = Limits(**values)
    if (
        any(v <= 0 for k, v in values.items() if k != "max_depth")
        or limits.max_depth < 0
    ):
        raise DataContractError("限额配置不合法。")
    version = (
        catalog["config_version"] + "-" + hashlib.sha256(b"".join(raw)).hexdigest()[:12]
    )
    return catalog, limits, version
