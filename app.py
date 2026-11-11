"""Streamlit 页面入口：登录、收集查询条件、显示提示并提供 CSV 下载。

数据库取数和宽表合并交给 lot_extract，不在页面里编写 SQL 或材料关联算法。
Streamlit 通常会在交互后重跑脚本，跨次运行的数据保存在当前会话的 session_state。
"""

import csv
import hmac
import os
from time import monotonic

import psycopg
import streamlit as st

from lot_extract.database import PRODUCT_FIELDS, Filters, load_settings, products
from lot_extract.service import archive_query, merged_download

st.set_page_config(page_title="制造数据抽取", page_icon=":material/database:")
load_settings()


@st.fragment
def brand_mark() -> None:
    """轻量署名彩蛋：十秒内点击标识五次后显示。"""
    # 只重跑这个小区域，避免点击彩蛋时刷新查询表单，丢失尚未提交的输入。
    if st.button(":blue[DATA / EXTRACT]", type="tertiary", key="brand_mark"):
        now = monotonic()
        started, count = st.session_state.get("_brand_taps", (now, 0))
        # 使用单调时钟计时，不受电脑系统时间调整影响。
        if now - started > 10:
            started, count = now, 0
        count += 1
        if count == 5:
            st.toast("By TZH BE xht ✨", icon="💙")
            # 触发后清零，下一次发现彩蛋仍需重新连续点击。
            st.session_state.pop("_brand_taps", None)
        else:
            st.session_state._brand_taps = (started, count)


password = os.getenv("APP_PASSWORD")
if not password:
    st.error("请先在 .env 设置 APP_PASSWORD。")
    st.stop()
if not st.session_state.get("authenticated"):
    # 登录成功前通过 st.stop() 结束本次页面执行，不渲染下面的查询入口。
    st.caption(":blue[DATA / EXTRACT]")
    st.title("制造数据抽取", anchor=False)
    with st.form("login", width=420):
        st.subheader("登录", anchor=False)
        entered = st.text_input("访问密码", type="password")
        login = st.form_submit_button("进入工作台", type="primary", width="stretch")
    if login:
        # compare_digest 用于密码比较；转成字节后也支持非 ASCII 密码。
        if hmac.compare_digest(entered.encode(), password.encode()):
            st.session_state.authenticated = True
            st.rerun()
        st.error("密码错误。")
    st.stop()

try:
    profiles = products()
except ValueError as error:
    st.error(str(error))
    st.stop()

with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
    brand_mark()
    if st.button("退出登录", icon=":material/logout:", type="tertiary"):
        # 一并清除登录状态、下载内容和彩蛋计数，避免后续登录沿用旧会话结果。
        st.session_state.clear()
        st.rerun()
st.title("制造数据抽取", anchor=False)

# 表单把多个输入打包提交；填写条件时不触发数据库查询。
with st.form("extract"):
    with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
        st.subheader("查询条件", anchor=False, width="content")
        if len(profiles) == 1:
            st.badge(profiles[0], icon=":material/database:", color="blue")
    if len(profiles) > 1:
        database_product = st.selectbox("产品数据库", profiles)
    else:
        database_product = profiles[0]
    lot_col, date_col = st.columns(2)
    with lot_col:
        lot_no = st.text_input("系统 LOT 号", placeholder="输入完整 LOT 号")
    with date_col:
        # 空元组表示未启用日期筛选；选完范围后返回（开始日期，结束日期）。
        dates = st.date_input("批次实际结束日期", value=(), format="YYYY/MM/DD")
    field_col, number_col = st.columns(2)
    with field_col:
        product_field = st.selectbox("品番类型", PRODUCT_FIELDS)
    with number_col:
        product_number = st.text_input("品番", placeholder="输入完整品番")
    st.caption("至少填写一项 · 多项条件同时匹配")
    submitted = st.form_submit_button("查询并合并", icon=":material/search:", type="primary", width="stretch")

if submitted:
    # 新查询清除上次的下载，避免失败或无结果时误下旧文件。
    st.session_state.pop("download", None)
    try:
        # 日期控件可能处于只选了起始日的中间状态，不能当作完整范围提交。
        if len(dates) == 1:
            raise ValueError("请选择开始和结束日期。")
        start, end = dates if dates else (None, None)
        with st.spinner("正在查询并合并数据…"):
            filters = Filters(lot_no, product_field, product_number, start, end)
            # 先存档系统七表和材料四表，再从本次存档离线合并，便于复查原始记录。
            archive, count = archive_query(filters, database_product)
            if count:
                content, columns, notices = merged_download(archive)
                # 下载内容放在本会话中，页面重跑时无需重新查询或重新合并。
                # 元组依次保存：产品、查询编号、系统 LOT 数、列数、CSV 字节、提示数。
                st.session_state.download = (
                    database_product, archive.name, count, columns, content, len(notices)
                )
            else:
                st.info("没有找到符合条件的 LOT。")
    except ValueError as error:
        st.error(str(error))
    except psycopg.Error as error:
        # 页面只显示错误代码或类型，不直接展示可能含连接信息的数据库异常全文。
        code = error.sqlstate or type(error).__name__
        st.error(f"数据库读取失败，请检查连接配置和七张表的读取权限（{code}）。")
    except csv.Error:
        st.error("CSV 记录无法读取，请检查源字段大小和内容。")
    except OSError:
        st.error("文件保存或读取失败，请检查服务器导出目录的权限和剩余空间。")

# := 同时取值并判断是否有下载结果；下划线表示这里不展示 LOT 数和列数。
if result := st.session_state.get("download"):
    product_code, query_id, _, _, content, notice_count = result
    if notice_count:
        st.warning(f"有 {notice_count} 条字段或工序差异提示，相关数据已保留。")
    st.download_button(
        "下载合并宽表 CSV",
        content,
        file_name=f"{product_code}_批次全字段宽表_{query_id}.csv",
        mime="text/csv",
        icon=":material/download:",
        # 下载现成文件，不为点击下载重新运行整页脚本。
        on_click="ignore",
        width="stretch",
    )
