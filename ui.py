"""Streamlit 页面：登录、后台导出状态，以及完整结果的同端口流式下载。"""

import hmac
import os
import secrets
from time import monotonic

import streamlit as st

from lot_extract.database import PRODUCT_FIELDS, Filters, load_settings, products
from lot_extract.downloads import downloads
from lot_extract.jobs import job_manager

st.set_page_config(page_title="制造数据抽取", page_icon=":material/database:")
load_settings()


@st.fragment
def brand_mark() -> None:
    
    if st.button(":blue[DATA / EXTRACT]", type="tertiary", key="brand_mark"):
        now = monotonic()
        started, count = st.session_state.get("_brand_taps", (now, 0))
        if now - started > 10:
            started, count = now, 0
        count += 1
        if count == 5:
            st.toast("By TZH BE xht ✨", icon="💙")
            st.session_state.pop("_brand_taps", None)
        else:
            st.session_state._brand_taps = (started, count)


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
    brand_mark()
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
        st.session_state.pop("download", None)
    except ValueError as error:
        st.error(str(error))


@st.fragment(run_every=2)
def export_status() -> None:
    job = st.session_state.get("export_job")
    if job is None:
        return
    state = job.snapshot()
    if state["status"] in ("queued", "running"):
        st.info(state["stage"])
        if st.button("取消导出", key="cancel_export"):
            job.cancel.set()
            st.info("已请求取消，将在当前读取或合并步骤结束后停止。")
    elif state["status"] == "complete":
        st.success(state["stage"])
    elif state["status"] == "cancelled":
        st.warning(state["stage"])
    else:
        st.error(state.get("error", state["stage"]))
    st.caption(f"已处理 {state['rows']} 个系统 LOT · 材料缓存命中 {state.get('cache_hits', 0)} 次")

    def download_link(info: dict, label: str, mime: str, primary: bool = False) -> None:
        directory = job.directory.resolve()
        path = (directory / info["file"]).resolve()
        if path.parent != directory or not path.is_file():
            st.error("结果文件不存在或文件名无效。")
            return
        filename = f"{state['product']}_{job.directory.name}_{path.name}"
        token = downloads.issue(owner, path, filename, mime)
        base = (st.get_option("server.baseUrlPath") or "").strip("/")
        url = f"/{base + '/' if base else ''}exports/{token}"
        # 链接请求由同端口文件接口按块发送，不经过 Streamlit 的内存下载缓存。
        st.link_button(
            label, url, icon=":material/download:", width="stretch",
            type="primary" if primary else "secondary",
        )

    if final_csv := state.get("final_csv"):
        st.subheader("完整结果", anchor=False)
        st.caption(f"一份完整宽表：{final_csv['rows']} 行 · {final_csv['columns']} 列")
        notices = sum(part["notice_count"] for part in state["parts"])
        if notices:
            st.warning(f"本次导出有 {notices} 条字段或工序差异提示，相关记录已保留在完整结果中。")
        download_link(final_csv, "下载完整宽表 CSV", "text/csv", primary=True)
        if final_zip := state.get("final_zip"):
            download_link(final_zip, "下载完整结果 ZIP（含一份最终 CSV）", "application/zip")
        if final_csv.get("exceeds_excel_columns") or final_csv.get("exceeds_excel_rows"):
            st.warning("完整 CSV 超过 Excel 单工作表的行或列限制。数据未截断，请使用支持大文件的数据库工具读取。")
    if not state["parts"]:
        return
    with st.expander("中间分片下载（备用）", expanded=False):
        part = st.selectbox(
            "选择下载分片", state["parts"],
            format_func=lambda item: f"{item['file']} · {item['rows']} 行 · {item['columns']} 列",
            key=f"export_part_{job.directory.name}",
        )
        if part["notice_count"]:
            st.warning(f"本分片有 {part['notice_count']} 条字段或工序差异提示，相关记录已保留。")
        download_link(part, "下载分片 CSV", "text/csv")
        if state["status"] not in ("queued", "running"):
            download_link({"file": "manifest.json"}, "下载任务清单", "application/json")


export_status()
