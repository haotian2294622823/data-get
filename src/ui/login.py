import hashlib
import hmac
import os
import time
import streamlit as st
from src.ui.state import clear_business, logout


def configured_password() -> str:
    password = os.getenv("APP_PASSWORD", "")
    if password:
        return password
    try:
        return str(st.secrets.get("APP_PASSWORD", ""))
    except FileNotFoundError:
        return ""


def attempt(expected: str) -> None:
    if time.monotonic() < st.session_state.locked_until:
        return
    entered = st.session_state.get("password", "")
    st.session_state.password = ""
    if hmac.compare_digest(entered.encode("utf-8"), expected.encode("utf-8")):
        st.session_state.authenticated = True
        st.session_state.failed_attempts = 0
        clear_business()
    else:
        st.session_state.failed_attempts += 1
        if st.session_state.failed_attempts >= 5:
            st.session_state.locked_until = time.monotonic() + 30
            st.session_state.failed_attempts = 0


def require_login() -> None:
    expected = configured_password()
    stamp = hashlib.sha256(expected.encode("utf-8")).hexdigest()
    if st.session_state.get("password_stamp") != stamp:
        clear_business()
        st.session_state.authenticated = False
        st.session_state.password_stamp = stamp
    if not expected:
        st.session_state.trace_state = "locked"
        st.error(
            "尚未配置 APP_PASSWORD。请在 .env 或 Streamlit secrets 中设置共享访问密码。"
        )
        st.stop()
    if st.session_state.authenticated:
        with st.sidebar:
            st.button(
                "退出登录", key="logout", on_click=logout, icon=":material/logout:"
            )
        return
    st.session_state.trace_state = "locked"
    st.title("制造批次追溯与自助数据抽取")
    st.caption("共享密码访问 · 查询及下载仅在当前会话内有效")
    locked = time.monotonic() < st.session_state.locked_until
    with st.form("login_form"):
        st.text_input("访问密码", type="password", key="password")
        # 由服务端 attempt 检查锁定期限。保持按钮可提交，等待后无需刷新浏览器。
        st.form_submit_button(
            "登录", key="login_submit", on_click=attempt, args=(expected,)
        )
    if locked:
        st.warning("尝试次数过多，请等待 30 秒后重新操作。")
    elif st.session_state.failed_attempts:
        st.error("密码错误。")
    st.stop()
