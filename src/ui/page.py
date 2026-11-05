import os
from collections import defaultdict
from datetime import timedelta, timezone
from uuid import uuid4
import pandas as pd
import streamlit as st
import yaml

from src.bootstrap import build_service
from src.errors import AppError, LotNotFound
from src.exporting.csv_export import filename
from src.models import ExtractionRequest, QueryResult, TraceRequest, TraceResult
from src.processing.records import canonical
from src.ui.state import clear_business

DATASET_NAMES = {
    "genealogy_edges": "材料事件关系",
    "genealogy_paths": "追溯路径",
    "process_records": "工序履历",
    "material_records": "原始材料",
    "machine_records": "设备记录（模拟）",
    "defect_records": "不良记录（模拟）",
}
LOCAL_TIME = timezone(timedelta(hours=8))


def stamp(value) -> str:
    return value.astimezone(LOCAL_TIME).strftime("%Y-%m-%d %H:%M:%S UTC+08:00")


def render_trace(result: TraceResult) -> None:
    st.subheader("追溯结果")
    st.caption(
        f"LOT：{result.request.root_lot_no} · {stamp(result.generated_at)} · trace_id：{result.trace_id}"
    )
    columns = st.columns(4)
    for column, label, value in zip(
        columns,
        ["节点", "材料事件", "路径前缀", "工序"],
        [
            len(result.nodes),
            len(result.edges),
            len(result.paths),
            len(result.process_records),
        ],
    ):
        column.metric(label, value)
    if not result.is_complete:
        st.error(
            "INCOMPLETE_TRACE：结果不完整，禁止生成和下载 CSV。"
            + "；".join(result.stop_reasons)
        )
    st.caption(
        "完整性仅指本次目标库快照中可发现的记录已查完，不证明 ETL 与源库一致。未匹配批号仅为追溯边界。"
    )
    children = defaultdict(list)
    for row in result.paths.to_dict("records"):
        children[row["parent_path_id"]].append(row)

    def branch(parent: str) -> None:
        for row in children[parent]:
            label = f"{row['lot_path'][-1]} · 深度 {row['depth']} · {row['terminal_reason']}"
            box = st.expander(label, key="path_" + row["path_id"], on_change="rerun")
            # 展开只浏览当前会话结果，绝不访问数据库；避免渲染十万条隐藏节点。
            if box.open:
                with box:
                    st.text(" → ".join(row["lot_path"]))
                    st.caption("事件：" + row["event_id"])
                    branch(row["path_id"])

    branch(canonical([result.request.root_lot_no, []]))
    if result.notices:
        st.warning(f"本次有 {len(result.notices)} 条边界或数据质量提示，请核对状态列。")
        st.caption(f"告警列表预览前 100 条，总计 {len(result.notices)} 条。")
        st.table(
            pd.DataFrame(result.notices).head(100), alt="可定位到LOT和事件的追溯告警"
        )


def render_query(result: QueryResult, preview_rows: int) -> None:
    st.subheader("已生成的数据集")
    request = result.request
    st.caption(
        f"生成于 {stamp(result.generated_at)} · 模式 {request.data_mode} · 配置 {result.config_version}"
    )
    st.caption(
        f"本次提交：工序={'全部' if request.all_processes else request.process_codes}；字段={request.selected_field_ids}；数据集={request.output_datasets}"
    )
    st.caption("修改表单草稿后，当前下载仍属于上面的已提交条件；需再次生成才会更新。")
    dataset = st.selectbox(
        "查看已生成数据集",
        list(result.datasets),
        format_func=lambda d: DATASET_NAMES[d],
        key="view_dataset",
    )
    data = result.datasets[dataset]
    st.caption(
        f"完整记录 {len(data)} 行；页面最多预览 {preview_rows} 行，下载包含全部记录。"
    )
    labels = dict(result.column_labels[dataset])
    # 静态表不带隐式 CSV 工具栏，避免用户误下载前100行或绕过正式导出规则。
    st.table(data.head(preview_rows).rename(columns=labels), alt="当前数据集预览")
    if data.empty:
        st.info("NO_RECORD_FOR_DATASET：该数据集没有记录，其他非空数据集仍可下载。")
    for name, content in result.csv_bytes_by_dataset.items():
        st.download_button(
            "下载 " + DATASET_NAMES[name],
            content,
            file_name=filename(
                result.trace.request.root_lot_no,
                name,
                result.generated_at.astimezone(LOCAL_TIME).strftime("%Y%m%d_%H%M%S"),
            ),
            mime="text/csv",
            key="download_" + name,
            on_click="ignore",
        )
    st.caption(
        "UTF-8 BOM CSV。LOT 请在 Excel 导入时设为文本以保留前导零。公式起始文本添加单引号保护，会改变导出文本。"
    )


def render_page() -> None:
    st.title("制造批次追溯与自助数据抽取")
    mode = os.getenv("DATA_SOURCE_MODE", "mock")
    st.caption(f"当前模式：{mode} · 单 LOT 向上游追溯 · V0.3")
    if mode == "mock":
        st.info(
            "模拟模式：所有数据均为构造样本。可输入 ROOT_A；真实系统使用 PostgreSQL 只读查询源。"
        )
    try:
        service = build_service(mode)
    except (
        AppError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        yaml.YAMLError,
    ) as error:
        clear_business()
        st.session_state.trace_state = "error"
        st.error(
            str(error)
            if isinstance(error, AppError)
            else "应用配置格式错误，请核对 config 目录。"
        )
        st.stop()
    context = (mode, service.version)
    if st.session_state.get("config_context") != context:
        clear_business()
        st.session_state.config_context = context
    with st.form("trace_form"):
        root = st.text_input("生产 LOT", key="root_lot", placeholder="例如 ROOT_A")
        submit = st.form_submit_button("开始追溯", key="trace_submit", type="primary")
    if submit:
        clear_business()
        st.session_state.trace_state = "tracing"
        try:
            with st.spinner("读取材料事实、追溯上游并核验工序引用…"):
                result = service.run_trace(
                    TraceRequest(uuid4().hex, root, service.version, mode)
                )
            st.session_state.update(
                trace_result=result,
                trace_state="traced" if result.is_complete else "incomplete",
            )
        except AppError as error:
            st.session_state.update(
                query_error=str(error),
                trace_state="empty" if isinstance(error, LotNotFound) else "error",
            )
    trace_slot = st.container()
    result = st.session_state.trace_result
    if result is not None and result.is_complete:
        st.subheader("抽取设置")
        codes = sorted(
            {c for c in result.process_records.process_code if c is not None}
        )
        with st.form("extract_form"):
            all_processes = st.checkbox("全部工序", value=True, key="all_processes")
            process_codes = st.multiselect(
                "指定工序代码（关闭全部工序时生效）", codes, key="process_codes"
            )
            datasets = st.multiselect(
                "输出数据集",
                service.catalog["datasets"],
                default=list(service.catalog["datasets"])[:4],
                format_func=lambda d: DATASET_NAMES[d],
                key="datasets",
            )
            st.caption("每种数据集的固定标识列始终保留，互不横向相乘。")
            if service.catalog["fields"]:
                st.table(
                    pd.DataFrame(service.catalog["fields"])[
                        [
                            "field_id",
                            "label",
                            "dataset",
                            "value_column",
                            "grain",
                            "verified",
                        ]
                    ],
                    alt="已核实可选字段及来源粒度",
                )
            by_id = {f["field_id"]: f for f in service.catalog["fields"]}
            fields = st.multiselect(
                "可选业务字段",
                list(by_id),
                default=list(by_id),
                format_func=lambda k: by_id[k]["label"],
                key="fields",
            )
            generate = st.form_submit_button(
                "生成数据集", key="extract_submit", type="primary"
            )
        if generate:
            st.session_state.update(
                query_result=None,
                submitted_extraction=None,
                query_error=None,
                trace_state="extracting",
            )
            request = ExtractionRequest(
                uuid4().hex,
                result.trace_id,
                all_processes,
                tuple(process_codes),
                tuple(fields),
                tuple(datasets),
                service.version,
                mode,
            )
            try:
                with st.spinner("在新快照中重新追溯并生成数据集…"):
                    query = service.run_extraction(request, result)
                # 一次性发布新图、请求摘要和 CSV，不能将新表格拼到旧图上。
                st.session_state.update(
                    trace_result=query.trace,
                    query_result=query,
                    submitted_extraction=request,
                    trace_state="ready",
                )
            except AppError as error:
                st.session_state.update(query_error=str(error), trace_state="error")
    if st.session_state.query_error:
        st.error(st.session_state.query_error)
        if st.session_state.trace_result is not None:
            st.caption("下方保留的是上次成功追溯；本次失败未保留旧 CSV。可重试生成。")
    if st.session_state.trace_result is not None:
        with trace_slot:
            render_trace(st.session_state.trace_result)
    if st.session_state.query_result is not None:
        render_query(st.session_state.query_result, service.limits.preview_rows)
