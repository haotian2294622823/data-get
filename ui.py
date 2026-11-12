"""Streamlit 页面：登录、后台导出状态，以及完整结果的同端口流式下载。"""

import hmac
import os
import secrets

import streamlit as st

from lot_extract.database import PRODUCT_FIELDS, Filters, load_settings, products
from lot_extract.downloads import downloads
from lot_extract.jobs import job_manager

st.set_page_config(page_title="制造数据抽取", page_icon=":material/database:")
load_settings()


def format_file_size(size: int) -> str:
    """按 1024 换算实际文件字节数，单位使用对应的 KiB / MiB / GiB。"""
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{int(value):,} B" if unit == "B" else f"{value:,.1f} {unit}"
        value /= 1024


password = os.getenv("APP_PASSWORD")
if not password:
    st.error("请先在 .env 设置 APP_PASSWORD。")
    st.stop()
if not st.session_state.get("authenticated"):
    st.caption(":blue[DATA / EXTRACT]")
    st.title("制造数据抽取", anchor=False)
    with st.form("login", width=420):
        st.subheader("登录", anchor=False)
        entered = st.text_input("访问密码", type="password")
        login = st.form_submit_button("进入工作台", type="primary", width="stretch")
    if login:
        if hmac.compare_digest(entered.encode(), password.encode()):
            st.session_state.authenticated = True
            st.rerun()
        st.error("密码错误。")
    st.stop()

owner = st.session_state.setdefault("download_owner", secrets.token_urlsafe(24))
try:
    profiles = products()
except ValueError as error:
    st.error(str(error))
    st.stop()

with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
    st.caption(":blue[DATA / EXTRACT]")
    if st.button("退出登录", icon=":material/logout:", type="tertiary"):
        downloads.revoke(owner)
        if job := st.session_state.get("export_job"):
            job.cancel.set()
        st.session_state.clear()
        st.rerun()
st.title("制造数据抽取", anchor=False)

with st.form("extract"):
    with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
        st.subheader("查询条件", anchor=False, width="content")
        if len(profiles) == 1:
            st.badge(profiles[0], icon=":material/database:", color="blue")
    database_product = st.selectbox("产品数据库", profiles) if len(profiles) > 1 else profiles[0]
    lot_col, date_col = st.columns(2)
    with lot_col:
        lot_no = st.text_input("系统 LOT 号", placeholder="输入完整 LOT 号")
    with date_col:
        dates = st.date_input("批次实际结束日期", value=(), format="YYYY/MM/DD")
    field_col, number_col = st.columns(2)
    with field_col:
        product_field = st.selectbox("品番类型", PRODUCT_FIELDS)
    with number_col:
        product_number = st.text_input("品番", placeholder="输入完整品番")
    st.caption("至少填写一项 · 多项条件同时匹配")
    st.caption("仅保留以大写 E 开头且整个 LOT 号只有一个大写 E 的系统批次，关联材料信息保留。")
    submitted = st.form_submit_button("查询并导出完整结果", icon=":material/search:", type="primary", width="stretch")

if submitted:
    try:
        if len(dates) == 1:
            raise ValueError("请选择开始和结束日期。")
        start, end = dates if dates else (None, None)
        filters = Filters(lot_no, product_field, product_number, start, end)
        st.session_state.export_job = job_manager.submit(filters, database_product)
        downloads.revoke(owner)
    except ValueError as error:
        st.error(str(error))


@st.fragment(run_every=2)
def export_status() -> None:
    job = st.session_state.get("export_job")
    if job is None:
        return
    state = job.snapshot()
    active = state["status"] in ("queued", "running")
    st.subheader("查询结果", anchor=False)
    if active:
        # 状态只读取后台快照。定时刷新页面不会重新执行数据库查询。
        label = "正在取消，请等待当前处理步骤结束" if job.cancel.is_set() else state["stage"]
        st.status(label, state="running")
    elif state["status"] == "complete":
        if state.get("final_csv"):
            st.success("查询完成，完整数据已准备好", icon="✅")
        else:
            st.info("没有找到符合条件的数据，请调整查询条件后重试。", icon="🔎")
    elif state["status"] == "cancelled":
        st.warning("查询已取消，未生成完整结果。", icon="⏹️")
    else:
        st.error(state.get("error", state["stage"]), icon="⚠️")

    if final_csv := state.get("final_csv"):
        with st.container(horizontal=True):
            st.metric("数据行数", f"{final_csv['rows']:,}", border=True)
            st.metric("数据列数", f"{final_csv['columns']:,}", border=True)
            st.metric("CSV 文件大小", format_file_size(final_csv["bytes"]), border=True)
        st.caption("每行对应一个系统 LOT，行数不包含表头。")
        notices = sum(part.get("notice_count", 0) for part in state["parts"])
        if notices:
            st.warning(f"本次导出有 {notices:,} 条字段或工序差异提示，相关记录已保留在完整结果中。")
        if final_csv.get("exceeds_excel_columns") or final_csv.get("exceeds_excel_rows"):
            st.warning("完整 CSV 超过 Excel 单工作表的行或列限制。数据未截断，请使用支持大文件的数据库工具读取。")

        directory = job.directory.resolve()
        path = (directory / final_csv["file"]).resolve()
        if path.parent != directory or not path.is_file():
            st.error("结果文件不存在或文件名无效。")
            return
        filename = f"{state['product']}_{job.directory.name}_{path.name}"
        token = downloads.issue(owner, path, filename, "text/csv")
        base = (st.get_option("server.baseUrlPath") or "").strip("/")
        url = f"/{base + '/' if base else ''}exports/{token}"
        # 链接请求由同端口文件接口按块发送，不经过 Streamlit 的内存下载缓存。
        st.link_button(
            "下载完整 CSV", url, icon="📥", width="stretch", type="primary",
        )
        return

    with st.container(horizontal=True):
        st.metric("已找到系统 LOT", f"{state.get('found_lots', state['rows']):,}", border=True)
        st.metric("已整理数据行数", f"{state['rows']:,}", border=True)
    if active:
        if state.get("phase") == "joining" and state["rows"]:
            # 查询已结束，总行数已知；这里展示 CSV 写入进度，而非整个任务的预计耗时。
            done, total = state.get("joined_rows", 0), state["rows"]
            st.progress(min(done / total, 1.0), text=f"生成完整 CSV · {done:,} / {total:,} 行")
        else:
            st.caption("每 2 秒刷新，数量随每批查询和整理结果更新。查询结束后生成完整 CSV。")
        if st.button("取消查询", key="cancel_export", disabled=job.cancel.is_set()):
            job.cancel.set()
            st.rerun(scope="fragment")


export_status()
