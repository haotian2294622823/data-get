from dataclasses import replace
import pandas as pd
import pytest
from tests.test_trace import service, trace, extract
from src.data_access.mock import MockFactory
from src.errors import (
    DataSourceError,
    DataSourceNotConfigured,
    DataContractError,
    LimitExceeded,
)
from src.bootstrap import build_service
from src.exporting.csv_export import csv_bytes, filename


def test_filter_preserves_graph_and_independent_details():
    s = service()
    q = extract(s, trace(s), all_processes=False, process_codes=("E010",))
    assert len(q.datasets["genealogy_edges"]) == 6
    assert len(q.datasets["genealogy_paths"]) == 8
    assert q.datasets["process_records"].process_key.tolist() == ["P_ROOT_1"]
    assert len(q.datasets["material_records"]) == 2
    assert len(q.datasets["machine_records"]) == 2
    assert q.datasets["defect_records"].defect_qty.sum() == 3


def test_fresh_snapshot_and_unselected_details_not_fetched():
    tables = MockFactory().load()
    s = service(tables)
    old = trace(s)
    tables["material_records"] = tables["material_records"][:2]
    q = extract(s, old, output_datasets=("material_records",))
    assert q.trace.trace_id != old.trace_id
    assert len(q.trace.edges) == 2
    assert len(q.datasets["material_records"]) == 2
    assert set(q.csv_bytes_by_dataset) == {"material_records"}
    assert q.request.based_on_trace_id == old.trace_id


def test_empty_dataset_and_csv_limit():
    s = service()
    q = extract(s, trace(s, "MAT_D"), output_datasets=("material_records",))
    assert q.datasets["material_records"].empty and not q.csv_bytes_by_dataset
    s.limits = replace(s.limits, max_csv_bytes=1)
    with pytest.raises(LimitExceeded):
        extract(s, trace(s))


def test_real_not_configured_never_falls_back(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(DataSourceNotConfigured):
        build_service("real")


def test_csv_preserves_ids_and_protects_formulas():
    data = pd.DataFrame({"lot": ["000001", "佐藤", "=1+1"], "value": [-2, None, 3]})
    raw = csv_bytes(data, (("lot", "LOT"), ("value", "数值")), 10000).decode(
        "utf-8-sig"
    )
    assert "000001" in raw and "佐藤" in raw and "'=1+1" in raw and "-2" in raw
    assert data.lot.iloc[-1] == "=1+1"
    assert "/" not in filename("../../A", "test", "stamp")


def test_configuration_and_trace_ownership_checked():
    s = service()
    old = trace(s)
    with pytest.raises(DataContractError):
        extract(s, old, based_on_trace_id="other-session")
    with pytest.raises(DataContractError):
        extract(s, old, config_version="obsolete")
    with pytest.raises(DataContractError):
        extract(s, old, selected_field_ids=("unknown",))
    with pytest.raises(DataContractError):
        extract(s, old, all_processes=False, process_codes=())


def test_csv_contains_all_rows_not_only_preview():
    tables = MockFactory().load()
    tables["material_records"] = [
        dict(
            lot_no="ROOT_A",
            pass_order=1,
            seq=i,
            material_lot_no=None,
            process_key="P_ROOT_1",
            input_qty=i,
        )
        for i in range(101)
    ]
    s = service(tables)
    q = extract(s, trace(s), output_datasets=("material_records",))
    assert len(q.datasets["material_records"]) == 101
    assert (
        len(q.csv_bytes_by_dataset["material_records"].decode("utf-8-sig").splitlines())
        == 102
    )


def test_source_failure_is_not_an_empty_result():
    from contextlib import contextmanager

    class FailingFactory:
        @contextmanager
        def open_read_session(self, deadline_at):
            raise DataSourceError("测试读取失败")
            yield  # 让该方法具备 contextmanager 的生成器协议。

    s = service()
    previous = trace(s)
    s.factory = FailingFactory()
    with pytest.raises(DataSourceError):
        trace(s)
    with pytest.raises(DataSourceError):
        extract(s, previous)
