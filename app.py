import hmac
import os

import psycopg
import streamlit as st

from query_service import PRODUCT_FIELDS, Filters, archive_query, load_settings, merged_download

st.set_page_config(page_title="制造数据抽取")
load_settings()
st.title("制造数据抽取")

password = os.getenv("APP_PASSWORD")
if not password:
    st.error("请先在 .env 设置 APP_PASSWORD。")
    st.stop()
if not st.session_state.get("authenticated"):
    with st.form("login"):
        entered = st.text_input("访问密码", type="password")
        login = st.form_submit_button("登录")
    if login:
        if hmac.compare_digest(entered.encode(), password.encode()):
            st.session_state.authenticated = True
            st.rerun()
        st.error("密码错误。")
    st.stop()

if st.button("退出登录"):
    st.session_state.clear()
    st.rerun()

with st.form("extract"):
    lot_no = st.text_input("LOT 号")
    product_field = st.selectbox("品番类型", PRODUCT_FIELDS)
    product = st.text_input("品番")
    dates = st.date_input("批次实际结束日期", value=())
    submitted = st.form_submit_button("查询", type="primary")

if submitted:
    # A new attempt replaces the previous result, even when it returns no LOT.
    st.session_state.pop("download", None)
    try:
        if len(dates) == 1:
            raise ValueError("请选择开始和结束日期。")
        start, end = dates if dates else (None, None)
        with st.spinner("正在查询并合并数据…"):
            archive, count = archive_query(Filters(lot_no, product_field, product, start, end))
            if count:
                content, columns, notices = merged_download(archive)
                st.session_state.download = (archive.name, count, columns, content, len(notices))
            else:
                st.info("没有找到符合条件的 LOT。")
    except ValueError as error:
        st.error(str(error))
    except psycopg.Error as error:
        st.error(f"数据库读取失败，请检查连接配置和六张表的读取权限（{error.sqlstate or type(error).__name__}）。")
    except OSError:
        st.error("文件保存或读取失败，请检查服务器导出目录的权限和剩余空间。")

if result := st.session_state.get("download"):
    query_id, count, columns, content, notice_count = result
    st.success(f"已合并 {count} 个 LOT，共 {columns} 列。")
    if notice_count:
        st.warning(f"有 {notice_count} 条字段或工序差异提示，相关数据已保留。")
    st.download_button(
        "下载合并宽表 CSV",
        content,
        file_name=f"批次全字段宽表_{query_id}.csv",
        mime="text/csv",
        on_click="ignore",
    )
