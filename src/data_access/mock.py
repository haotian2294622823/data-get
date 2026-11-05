"""模拟源读取原始 CSV，每次操作冻结，不返回预先算好的追溯结果。"""

from contextlib import contextmanager
from copy import deepcopy
import csv
import time
from typing import Iterator
import pandas as pd
from src.catalog import BASE
from src.errors import LimitExceeded

SCHEMAS = {
    "lots": ["lot_no"],
    "material_records": [
        "lot_no",
        "material_lot_no",
        "process_key",
        "pass_order",
        "seq",
        "input_qty",
    ],
    "process_records": [
        "lot_no",
        "process_key",
        "pass_order",
        "process_code",
        "process_name",
    ],
    "machine_records": ["lot_no", "process_key", "pass_order", "machine_code"],
    "defect_records": [
        "lot_no",
        "process_key",
        "pass_order",
        "defect_code",
        "defect_qty",
    ],
}


class MockSource:
    def __init__(self, tables: dict, deadline: float):
        self.tables, self.deadline = tables, deadline

    def read(self, source: str, key: str, values: tuple, limit: int) -> pd.DataFrame:
        if time.monotonic() >= self.deadline:
            raise LimitExceeded("操作超时")
        allowed = set(values)
        rows = [r for r in self.tables[source] if r.get(key) in allowed][:limit]
        # dtype=object 保留含空值的整数键，避免 pandas 把道次改成浮点数。
        return pd.DataFrame(rows, columns=SCHEMAS[source], dtype=object)

    def get_lots(self, lot_nos: tuple[str, ...], row_limit: int) -> pd.DataFrame:
        return self.read("lots", "lot_no", lot_nos, row_limit)

    def get_material_records(
        self, lot_nos: tuple[str, ...], row_limit: int, field_ids: tuple[str, ...] = ()
    ) -> pd.DataFrame:
        return self.read("material_records", "lot_no", lot_nos, row_limit)

    def get_process_records(
        self, lot_nos: tuple[str, ...], row_limit: int
    ) -> pd.DataFrame:
        return self.read("process_records", "lot_no", lot_nos, row_limit)

    def get_process_records_by_keys(
        self, process_keys: tuple[str, ...], row_limit: int
    ) -> pd.DataFrame:
        return self.read("process_records", "process_key", process_keys, row_limit)

    def get_detail_records(
        self,
        source_id: str,
        lot_nos: tuple[str, ...],
        field_ids: tuple[str, ...],
        row_limit: int,
    ) -> pd.DataFrame:
        return self.read(source_id, "lot_no", lot_nos, row_limit)


class MockFactory:
    def __init__(self, tables: dict | None = None):
        self.tables = tables

    def load(self) -> dict:
        result = {}
        for source in SCHEMAS:
            with (BASE / "data/demo" / f"{source}.csv").open(
                encoding="utf-8", newline=""
            ) as stream:
                rows = list(csv.DictReader(stream))
            for row in rows:
                for key, value in row.items():
                    row[key] = (
                        None
                        if value == ""
                        else (
                            int(value)
                            if key in ("pass_order", "seq", "input_qty", "defect_qty")
                            else value
                        )
                    )
            result[source] = rows
        return result

    @contextmanager
    def open_read_session(self, deadline_at: float) -> Iterator[MockSource]:
        yield MockSource(
            deepcopy(self.load() if self.tables is None else self.tables), deadline_at
        )
