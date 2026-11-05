"""按层 BFS：读取缓存、事件事实、路径到达使用三个独立集合。"""

from datetime import datetime, timezone
import time
from uuid import uuid4
import pandas as pd

from src.catalog import FIXED
from src.errors import DataContractError, LimitExceeded, LotNotFound
from src.models import Limits, TraceDataSource, TraceRequest, TraceResult
from src.processing.records import (
    canonical,
    clean_rows,
    material_id,
    reference_status,
    unique,
)


def check_time(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise LimitExceeded("操作超过总时限")


def bounded(frame: pd.DataFrame, allowance: int, deadline: float) -> list[dict]:
    check_time(deadline)
    if len(frame) > allowance:
        raise LimitExceeded("来源累计行数超限")
    return clean_rows(frame)


def frame(rows: list[dict], columns: list[str], sort: list[str]) -> pd.DataFrame:
    extras = list(dict.fromkeys(k for row in rows for k in row if k not in columns))
    result = pd.DataFrame(rows, columns=columns + extras, dtype=object)
    return (
        result.sort_values(sort, kind="stable", na_position="last").reset_index(
            drop=True
        )
        if len(result)
        else result
    )


def run_trace(
    source: TraceDataSource,
    request: TraceRequest,
    limits: Limits,
    deadline: float,
    material_fields: tuple[str, ...] = (),
) -> TraceResult:
    root = request.root_lot_no
    nodes: dict[str, dict] = {}
    edges: dict[str, dict] = {}
    materials: dict[str, dict] = {}
    paths: list[dict] = []
    processes: list[dict] = []
    notices: list[dict] = []
    notice_keys: set[tuple] = set()
    stop: list[str] = []
    known: dict[str, bool] = {}
    fetched: set[str] = set()
    material_cache: dict[str, list[dict]] = {}
    raw_material_count = 0
    material_columns = list(FIXED["material_records"])

    def notice(code: str, lot: str, record: dict | None = None) -> None:
        item: dict = {"code": code, "message": code, "lot_no": lot}
        if record:
            item.update({k: record.get(k) for k in ("source_record_id", "process_key")})
        key = (code, lot, item.get("source_record_id"), item.get("process_key"))
        if key not in notice_keys:
            notice_keys.add(key)
            notices.append(item)

    try:
        roots = unique(bounded(source.get_lots((root,), 2), 1, deadline), ["lot_no"])
        if not roots:
            raise LotNotFound("LOT_NOT_FOUND：当前数据源未找到该 LOT。")
        known[root] = True
        nodes[root] = dict(root_lot=root, lot_no=root, is_internal=True, min_depth=0)
        # 元素是(当前LOT, LOT序列, 事件序列, 到达本节点的路径行)。根不单独导出路径行。
        frontier: list[tuple[str, list[str], list[str], dict | None]] = [
            (root, [root], [], None)
        ]
        while frontier:
            check_time(deadline)
            need = tuple(
                sorted({lot for lot, _, _, _ in frontier if lot not in fetched})
            )
            if need:
                allowance = limits.max_material_rows - raw_material_count
                material_frame = source.get_material_records(
                    need, allowance + 1, material_fields
                )
                material_columns = list(
                    dict.fromkeys(material_columns + list(material_frame.columns))
                )
                raw = bounded(material_frame, allowance, deadline)
                raw_material_count += len(raw)
                raw = unique(raw, ["lot_no", "pass_order", "seq"])
                for lot in need:
                    material_cache[lot] = []
                for row in raw:
                    if row["lot_no"] not in need:
                        raise DataContractError("材料源返回了本次未请求的 LOT。")
                    row = {
                        **row,
                        "root_lot": root,
                        "source_record_id": material_id(row),
                    }
                    upstream = row.get("material_lot_no")
                    if upstream is not None and not isinstance(upstream, str):
                        raise DataContractError("材料 LOT 必须是文本或 NULL。")
                    row["match_status"] = (
                        "NO_UPSTREAM_LOT"
                        if upstream is None or not upstream.strip()
                        else None
                    )
                    materials[row["source_record_id"]] = row
                    material_cache[row["lot_no"]].append(row)
                fetched.update(need)
                unknown = sorted(
                    {
                        r["material_lot_no"]
                        for r in raw
                        if r.get("material_lot_no") is not None
                        and r["material_lot_no"].strip()
                        and r["material_lot_no"] not in known
                    }
                )
                if unknown:
                    # 唯一节点额度也约束待判定节点，防止一个批次带来无限候选批号。
                    if len(set(nodes) | set(unknown)) > limits.max_nodes:
                        raise LimitExceeded("唯一节点数超限")
                    found = bounded(
                        source.get_lots(tuple(unknown), len(unknown) + 1),
                        len(unknown),
                        deadline,
                    )
                    internal = {r["lot_no"] for r in unique(found, ["lot_no"])}
                    known.update({lot: lot in internal for lot in unknown})
            following: list[tuple[str, list[str], list[str], dict | None]] = []
            for lot, lot_path, event_path, incoming in frontier:
                check_time(deadline)
                relations = [
                    r
                    for r in material_cache[lot]
                    if r["match_status"] != "NO_UPSTREAM_LOT"
                ]
                if not relations and incoming is not None:
                    incoming["terminal_reason"] = "NO_UPSTREAM"
                if relations and len(event_path) >= limits.max_depth:
                    if incoming is not None:
                        incoming["terminal_reason"] = "TRUNCATED"
                    raise LimitExceeded("追溯深度超限")
                for row in sorted(relations, key=lambda r: (r["pass_order"], r["seq"])):
                    check_time(deadline)
                    upstream, event = row["material_lot_no"], row["source_record_id"]
                    row["match_status"] = (
                        "INTERNAL_LOT" if known[upstream] else "UNMATCHED_INTERNAL_LOT"
                    )
                    if event not in edges and len(edges) >= limits.max_edges:
                        raise LimitExceeded("唯一事件数超限")
                    if len(paths) >= limits.max_path_rows:
                        raise LimitExceeded("路径行数超限")
                    if upstream not in nodes and len(nodes) >= limits.max_nodes:
                        raise LimitExceeded("唯一节点数超限")
                    edges[event] = dict(
                        root_lot=root,
                        event_id=event,
                        downstream_lot=lot,
                        upstream_lot=upstream,
                        **{
                            k: row[k]
                            for k in (
                                "process_key",
                                "pass_order",
                                "seq",
                                "match_status",
                            )
                        },
                    )
                    depth = len(event_path) + 1
                    if upstream not in nodes:
                        nodes[upstream] = dict(
                            root_lot=root,
                            lot_no=upstream,
                            is_internal=known[upstream],
                            min_depth=depth,
                        )
                    else:
                        nodes[upstream]["min_depth"] = min(
                            nodes[upstream]["min_depth"], depth
                        )
                    reason = (
                        "CYCLE_DETECTED"
                        if upstream in lot_path
                        else "CONTINUE"
                        if known[upstream]
                        else "UNMATCHED_INTERNAL_LOT"
                    )
                    events = event_path + [event]
                    path = dict(
                        root_lot=root,
                        path_id=canonical([root, events]),
                        parent_path_id=canonical([root, event_path]),
                        event_id=event,
                        depth=depth,
                        lot_path=lot_path + [upstream],
                        event_path=events,
                        terminal_reason=reason,
                    )
                    paths.append(path)
                    if reason == "CONTINUE":
                        following.append(
                            (upstream, lot_path + [upstream], events, path)
                        )
                    else:
                        notice(reason, upstream, row)
            frontier = following

        internal_lots = tuple(sorted(lot for lot in nodes if known[lot]))
        processes = unique(
            bounded(
                source.get_process_records(internal_lots, limits.max_source_rows + 1),
                limits.max_source_rows,
                deadline,
            ),
            ["process_key"],
        )
        if any(p["lot_no"] not in internal_lots for p in processes):
            raise DataContractError("工序源返回了未请求 LOT。")
        for p in processes:
            p.update(root_lot=root, min_depth=nodes[p["lot_no"]]["min_depth"])
        by_key = {p["process_key"]: p for p in processes}
        missing_keys = tuple(
            sorted(
                {
                    r["process_key"]
                    for r in materials.values()
                    if r.get("process_key") and r["process_key"] not in by_key
                }
            )
        )
        if missing_keys:
            refs = unique(
                bounded(
                    source.get_process_records_by_keys(
                        missing_keys, limits.max_source_rows + 1
                    ),
                    limits.max_source_rows,
                    deadline,
                ),
                ["process_key"],
            )
            # 图外工序仅用于核验，绝不加入工序导出或继续追溯。
            by_key.update({p["process_key"]: p for p in refs})
        for r in materials.values():
            check_time(deadline)
            r["reference_status"] = reference_status(r, by_key)
            if r["match_status"] is None:
                r["match_status"] = (
                    "INTERNAL_LOT"
                    if known.get(r["material_lot_no"])
                    else "UNMATCHED_INTERNAL_LOT"
                )
            if r["reference_status"] != "PROCESS_MATCHED":
                notice(r["reference_status"], r["lot_no"], r)
            if r["match_status"] == "NO_UPSTREAM_LOT":
                notice("NO_UPSTREAM_LOT", r["lot_no"], r)
        for edge in edges.values():
            check_time(deadline)
            edge["reference_status"] = materials[edge["event_id"]]["reference_status"]
        process_lots = {p["lot_no"] for p in processes}
        for lot in internal_lots:
            check_time(deadline)
            if lot not in process_lots:
                notice("NO_PROCESS_RECORD", lot)
        if "E6422036" in nodes:
            notice("HISTORICAL_ETL_RISK", "E6422036")
    except LimitExceeded as error:
        stop.append(str(error))
        notice("INCOMPLETE_TRACE", root)
        for p in paths:
            if p["terminal_reason"] == "CONTINUE":
                p["terminal_reason"] = "TRUNCATED"

    result = TraceResult(
        request,
        uuid4().hex,
        frame(
            list(nodes.values()),
            ["root_lot", "lot_no", "is_internal", "min_depth"],
            ["min_depth", "lot_no"],
        ),
        frame(
            list(edges.values()),
            FIXED["genealogy_edges"],
            ["downstream_lot", "pass_order", "seq", "event_id"],
        ),
        frame(paths, FIXED["genealogy_paths"], ["depth", "path_id"]),
        frame(
            list(materials.values()), material_columns, ["lot_no", "pass_order", "seq"]
        ),
        frame(
            processes,
            FIXED["process_records"],
            ["min_depth", "lot_no", "pass_order", "process_key"],
        ),
        notices,
        not stop,
        tuple(stop),
        datetime.now(timezone.utc),
    )
    # 排序/构造 DataFrame 同样计入整次操作。超时结果仅供页面诊断，不可抽取。
    if time.monotonic() >= deadline and result.is_complete:
        result.is_complete = False
        result.stop_reasons = ("结果整理超过总时限",)
        notice("INCOMPLETE_TRACE", root)
    return result
