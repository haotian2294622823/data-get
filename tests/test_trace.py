from dataclasses import replace
import pytest

from src.bootstrap import build_service
from src.data_access.mock import MockFactory
from src.models import TraceRequest, ExtractionRequest
from src.errors import DataContractError, LotNotFound


def service(tables=None, **limits):
    result = build_service("mock", MockFactory(tables=tables))
    result.limits = replace(result.limits, **limits)
    return result


def trace(s, lot="ROOT_A"):
    return s.run_trace(TraceRequest("test", lot, s.version, "mock"))


def extract(s, old, **kwargs):
    values = dict(
        request_id="extract",
        based_on_trace_id=old.trace_id,
        all_processes=True,
        process_codes=(),
        selected_field_ids=tuple(f["field_id"] for f in s.catalog["fields"]),
        output_datasets=tuple(s.catalog["datasets"]),
        config_version=s.version,
        data_mode="mock",
    )
    values.update(kwargs)
    return s.run_extraction(ExtractionRequest(**values), old)


def test_baseline():
    result = trace(service())
    assert result.is_complete
    assert [
        len(getattr(result, key))
        for key in ("nodes", "edges", "paths", "process_records", "material_records")
    ] == [5, 6, 8, 5, 6]
    assert result.paths.depth.tolist() == [1, 1, 1, 2, 2, 2, 2, 2]
    assert result.paths.terminal_reason.value_counts().to_dict() == {
        "CONTINUE": 3,
        "NO_UPSTREAM": 3,
        "UNMATCHED_INTERNAL_LOT": 2,
    }
    assert result.edges.event_id.is_unique
    assert result.paths.path_id.is_unique
    assert result.nodes.query("lot_no == 'MAT_D'").min_depth.iloc[0] == 2


@pytest.mark.parametrize(
    "kind", ["blank", "missing", "foreign", "empty_ref", "incomplete_ref"]
)
def test_material_anomalies(kind):
    tables = MockFactory().load()
    row = dict(
        lot_no="ROOT_A",
        pass_order=3,
        seq=1,
        material_lot_no="MAT_D",
        process_key="P_MISSING",
        input_qty=1,
    )
    status = "PROCESS_KEY_NOT_FOUND"
    if kind == "blank":
        row.update(material_lot_no=None, process_key="P_ROOT_2", pass_order=2, seq=2)
        status = "PROCESS_MATCHED"
    if kind == "foreign":
        row["process_key"] = "P_FOREIGN"
        tables["process_records"].append(
            dict(
                process_key="P_FOREIGN",
                lot_no="LOT_Z",
                pass_order=3,
                process_code="OTHER",
                process_name="图外",
            )
        )
        status = "PROCESS_REFERENCE_CONFLICT"
    if kind == "empty_ref":
        row["process_key"] = None
        status = "PROCESS_REFERENCE_EMPTY"
    if kind == "incomplete_ref":
        row["process_key"] = "P_INCOMPLETE"
        tables["process_records"].append(
            dict(
                process_key="P_INCOMPLETE",
                lot_no="ROOT_A",
                pass_order=None,
                process_code="OTHER",
                process_name="待核实",
            )
        )
        status = "PROCESS_REFERENCE_INCOMPLETE"
    tables["material_records"].append(row)
    s = service(tables)
    r = trace(s)
    assert len(r.material_records) == 7
    assert len(r.edges) == (6 if kind == "blank" else 7)
    assert len(r.paths) == (8 if kind == "blank" else 9)
    assert len(r.nodes) == 5
    assert r.material_records.iloc[-1].reference_status == status
    q = extract(s, r, all_processes=False, process_codes=("E010",))
    assert len(q.datasets["material_records"]) == (2 if kind == "blank" else 3)


def test_cycle():
    tables = MockFactory().load()
    tables["lots"] = [{"lot_no": "A"}, {"lot_no": "B"}]
    tables["material_records"] = [
        dict(
            lot_no=a,
            material_lot_no=b,
            process_key=None,
            pass_order=1,
            seq=1,
            input_qty=1,
        )
        for a, b in [("A", "B"), ("B", "A")]
    ]
    tables["process_records"] = []
    r = trace(service(tables), "A")
    assert r.is_complete and (len(r.nodes), len(r.edges), len(r.paths)) == (2, 2, 2)
    assert r.paths.iloc[-1].terminal_reason == "CYCLE_DETECTED"


@pytest.mark.parametrize(
    "limits,complete",
    [
        ({"max_depth": 2}, True),
        ({"max_depth": 1}, False),
        ({"max_path_rows": 8}, True),
        ({"max_path_rows": 7}, False),
        ({"max_nodes": 5}, True),
        ({"max_nodes": 4}, False),
        ({"max_edges": 6}, True),
        ({"max_edges": 5}, False),
        ({"max_material_rows": 6}, True),
        ({"max_material_rows": 5}, False),
        ({"max_source_rows": 4}, False),
    ],
)
def test_limits(limits, complete):
    s = service(**limits)
    r = trace(s)
    assert r.is_complete == complete
    if not complete:
        assert r.stop_reasons
        with pytest.raises(DataContractError):
            extract(s, r)


def test_missing_lot_and_leaf_with_or_without_process():
    s = service()
    with pytest.raises(LotNotFound):
        trace(s, "NO_SUCH_LOT")
    assert len(trace(s, "MAT_D").process_records) == 1
    tables = MockFactory().load()
    tables["lots"].append({"lot_no": "EMPTY"})
    r = trace(service(tables), "EMPTY")
    assert r.is_complete and len(r.nodes) == 1 and r.process_records.empty


def test_conflicting_material_key_rejected():
    tables = MockFactory().load()
    tables["material_records"].append(
        {**tables["material_records"][0], "material_lot_no": "OTHER"}
    )
    with pytest.raises(DataContractError):
        trace(service(tables))


def test_deadline_during_reference_validation(monkeypatch):
    import time
    import src.services.trace_service as core

    original = core.reference_status

    def slow_reference(row, by_key):
        time.sleep(0.12)
        return original(row, by_key)

    monkeypatch.setattr(core, "reference_status", slow_reference)
    result = trace(service(operation_seconds=0.1))
    assert not result.is_complete
    assert result.stop_reasons


def test_material_budget_accumulates_across_layers():
    tables = MockFactory().load()
    tables["lots"] = [{"lot_no": "A"}, {"lot_no": "B"}]
    tables["material_records"] = [
        dict(
            lot_no=lot,
            material_lot_no=upstream,
            process_key=None,
            pass_order=1,
            seq=i,
            input_qty=1,
        )
        for lot, upstream in [("A", "B"), ("B", None)]
        for i in range(100)
    ]
    tables["process_records"] = []
    assert not trace(service(tables, max_material_rows=150), "A").is_complete


def test_stable_ids_and_duplicate_identical_facts():
    import json

    tables = MockFactory().load()
    tables["material_records"].append(dict(tables["material_records"][0]))
    s = service(tables)
    a, b = trace(s), trace(s)
    assert len(a.edges) == 6 and len(a.paths) == 8
    assert a.paths.path_id.tolist() == b.paths.path_id.tolist()
    for row in a.paths.to_dict("records"):
        assert json.loads(row["path_id"]) == ["ROOT_A", row["event_path"]]
        assert json.loads(row["parent_path_id"]) == ["ROOT_A", row["event_path"][:-1]]


def test_nonempty_upstream_is_matched_without_trimming():
    tables = MockFactory().load()
    tables["material_records"][0]["material_lot_no"] = " MAT_B "
    r = trace(service(tables))
    assert " MAT_B " in r.nodes.lot_no.tolist()
    assert not r.nodes.loc[r.nodes.lot_no == " MAT_B ", "is_internal"].iloc[0]
