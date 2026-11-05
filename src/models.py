from dataclasses import dataclass
from datetime import datetime
from typing import ContextManager, Protocol
import pandas as pd


@dataclass(frozen=True)
class TraceRequest:
    request_id: str
    root_lot_no: str
    config_version: str
    data_mode: str


@dataclass(frozen=True)
class ExtractionRequest:
    request_id: str
    based_on_trace_id: str
    all_processes: bool
    process_codes: tuple[str, ...]
    selected_field_ids: tuple[str, ...]
    output_datasets: tuple[str, ...]
    config_version: str
    data_mode: str


@dataclass(frozen=True)
class MaterialRecord:
    lot_no: str
    material_lot_no: str | None
    process_key: str | None
    pass_order: int
    seq: int
    source_record_id: str


@dataclass
class TraceResult:
    request: TraceRequest
    trace_id: str
    nodes: pd.DataFrame
    edges: pd.DataFrame
    paths: pd.DataFrame
    material_records: pd.DataFrame
    process_records: pd.DataFrame
    notices: list[dict]
    is_complete: bool
    stop_reasons: tuple[str, ...]
    generated_at: datetime


@dataclass
class QueryResult:
    request: ExtractionRequest
    trace: TraceResult
    datasets: dict[str, pd.DataFrame]
    column_labels: dict[str, tuple[tuple[str, str], ...]]
    csv_bytes_by_dataset: dict[str, bytes]
    generated_at: datetime
    config_version: str


@dataclass(frozen=True)
class Limits:
    max_depth: int = 10
    max_nodes: int = 5000
    max_edges: int = 50000
    max_path_rows: int = 100000
    max_material_rows: int = 100000
    max_source_rows: int = 100000
    max_fields: int = 60
    preview_rows: int = 100
    max_csv_bytes: int = 10485760
    operation_seconds: float = 60


class TraceDataSource(Protocol):
    def get_lots(self, lot_nos: tuple[str, ...], row_limit: int) -> pd.DataFrame: ...
    def get_material_records(
        self, lot_nos: tuple[str, ...], row_limit: int, field_ids: tuple[str, ...] = ()
    ) -> pd.DataFrame: ...
    def get_process_records(
        self, lot_nos: tuple[str, ...], row_limit: int
    ) -> pd.DataFrame: ...
    def get_process_records_by_keys(
        self, process_keys: tuple[str, ...], row_limit: int
    ) -> pd.DataFrame: ...
    def get_detail_records(
        self,
        source_id: str,
        lot_nos: tuple[str, ...],
        field_ids: tuple[str, ...],
        row_limit: int,
    ) -> pd.DataFrame: ...


class DataSourceFactory(Protocol):
    def open_read_session(
        self, deadline_at: float
    ) -> ContextManager[TraceDataSource]: ...
