"""两个提交各自打开新快照，成功后才返回可发布的完整结果。"""

from datetime import datetime, timezone
import time
from uuid import uuid4

from src.catalog import FIXED, KEYS, LABELS
from src.errors import DataContractError, LimitExceeded
from src.models import (
    DataSourceFactory,
    ExtractionRequest,
    QueryResult,
    TraceRequest,
    TraceResult,
)
from src.processing.records import canonical, reference_status, unique
from src.services.trace_service import bounded, check_time, frame, run_trace
from src.exporting.csv_export import csv_bytes


class QueryService:
    def __init__(
        self, mode: str, factory: DataSourceFactory, catalog: dict, limits, version: str
    ):
        self.mode, self.factory, self.catalog, self.limits, self.version = (
            mode,
            factory,
            catalog,
            limits,
            version,
        )

    def validate_version(self, request) -> None:
        if request.config_version != self.version or request.data_mode != self.mode:
            raise DataContractError("模式或配置已变化，请重新追溯。")

    def run_trace(self, request: TraceRequest) -> TraceResult:
        self.validate_version(request)
        if not request.root_lot_no or not request.root_lot_no.strip():
            raise DataContractError("请输入 LOT。")
        deadline = time.monotonic() + self.limits.operation_seconds
        with self.factory.open_read_session(deadline) as source:
            return run_trace(source, request, self.limits, deadline)

    def run_extraction(
        self, request: ExtractionRequest, previous: TraceResult
    ) -> QueryResult:
        self.validate_version(request)
        self.validate_version(previous.request)
        if request.based_on_trace_id != previous.trace_id or not previous.is_complete:
            raise DataContractError("本次提交不属于当前完整追溯。")
        if (
            not request.output_datasets
            or len(set(request.output_datasets)) != len(request.output_datasets)
            or not set(request.output_datasets) <= set(self.catalog["datasets"])
        ):
            raise DataContractError("请选择有效且不重复的数据集。")
        fields = {f["field_id"]: f for f in self.catalog["fields"]}
        if (
            len(request.selected_field_ids) > self.limits.max_fields
            or not set(request.selected_field_ids) <= fields.keys()
        ):
            raise DataContractError("字段选择无效或超限。")
        if not request.all_processes and not request.process_codes:
            raise DataContractError("关闭全部工序后，至少选择一个工序代码。")
        deadline = time.monotonic() + self.limits.operation_seconds
        selected = [
            f
            for f in self.catalog["fields"]
            if f["field_id"] in request.selected_field_ids
        ]
        with self.factory.open_read_session(deadline) as source:
            fresh_request = TraceRequest(
                uuid4().hex, previous.request.root_lot_no, self.version, self.mode
            )
            material_fields = tuple(
                f["field_id"] for f in selected if f["dataset"] == "material_records"
            )
            fresh = run_trace(
                source, fresh_request, self.limits, deadline, material_fields
            )
            if not fresh.is_complete:
                raise LimitExceeded("刷新追溯不完整，本次未生成任何 CSV。")
            by_key = {
                p["process_key"]: p for p in fresh.process_records.to_dict("records")
            }
            internal = tuple(fresh.nodes.loc[fresh.nodes.is_internal, "lot_no"])
            raw = {
                "genealogy_edges": fresh.edges,
                "genealogy_paths": fresh.paths,
                "process_records": fresh.process_records,
                "material_records": fresh.material_records,
            }
            for dataset in request.output_datasets:
                if dataset in raw:
                    continue
                detail_frame = source.get_detail_records(
                    dataset,
                    internal,
                    tuple(f["field_id"] for f in selected if f["dataset"] == dataset),
                    self.limits.max_source_rows + 1,
                )
                rows = unique(
                    bounded(detail_frame, self.limits.max_source_rows, deadline),
                    [
                        "lot_no",
                        "pass_order",
                        "machine_code"
                        if dataset == "machine_records"
                        else "defect_code",
                    ],
                )
                for r in rows:
                    if r["lot_no"] not in internal:
                        raise DataContractError("明细源返回了未请求 LOT。")
                    code = (
                        "machine_code"
                        if dataset == "machine_records"
                        else "defect_code"
                    )
                    r.update(
                        root_lot=fresh_request.root_lot_no,
                        source_id=dataset,
                        source_record_id=canonical(
                            [dataset, r["lot_no"], r["pass_order"], r[code]]
                        ),
                        reference_status=reference_status(r, by_key),
                    )
                columns = list(
                    dict.fromkeys(FIXED[dataset] + list(detail_frame.columns))
                )
                raw[dataset] = frame(rows, columns, ["lot_no", "source_record_id"])
            datasets, labels = {}, {}
            for dataset in request.output_datasets:
                current = raw[dataset].copy()
                if not request.all_processes:
                    if dataset == "process_records":
                        current = current[
                            current.process_code.isin(request.process_codes)
                        ]
                    elif dataset not in ("genealogy_edges", "genealogy_paths"):
                        # 无法定位的材料/明细保留；不能伪装成命中某个筛选工序。
                        selected_keys = {
                            k
                            for k, p in by_key.items()
                            if p["process_code"] in request.process_codes
                        }
                        current = current[
                            (current.reference_status != "PROCESS_MATCHED")
                            | current.process_key.isin(selected_keys)
                        ]
                optional = [f for f in selected if f["dataset"] == dataset]
                columns = FIXED[dataset] + [f["value_column"] for f in optional]
                if not set(columns) <= set(current.columns):
                    raise DataContractError("已选择的字段在本次来源中缺失。")
                current = current[columns].reset_index(drop=True)
                if (
                    current[KEYS[dataset]].isna().any().any()
                    or current.duplicated(KEYS[dataset]).any()
                ):
                    raise DataContractError("输出唯一键为空或重复。")
                optional_labels = {f["value_column"]: f["label"] for f in optional}
                labels[dataset] = tuple(
                    (c, f"{optional_labels.get(c, LABELS.get(c, c))} [{c}]")
                    for c in columns
                )
                datasets[dataset] = current
                if current.empty:
                    fresh.notices.append(
                        dict(
                            code="NO_RECORD_FOR_DATASET",
                            message=f"{dataset} 无记录",
                            lot_no=fresh_request.root_lot_no,
                        )
                    )
                for f in optional:
                    if current[f["value_column"]].isna().any():
                        fresh.notices.append(
                            dict(
                                code="EMPTY_VALUE",
                                message=f"{dataset}.{f['value_column']} 存在空值",
                                lot_no=fresh_request.root_lot_no,
                            )
                        )
            check_time(deadline)
        # 到这里快照事务已经释放。CSV 只使用固定的内存结果与列映射。
        exports = {
            name: csv_bytes(data, labels[name], self.limits.max_csv_bytes)
            for name, data in datasets.items()
            if not data.empty
        }
        check_time(deadline)
        return QueryResult(
            request,
            fresh,
            datasets,
            labels,
            exports,
            datetime.now(timezone.utc),
            self.version,
        )
