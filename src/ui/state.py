import streamlit as st


def initialize() -> None:
    for key, value in {
        "authenticated": False,
        "trace_state": "locked",
        "trace_result": None,
        "submitted_extraction": None,
        "query_result": None,
        "query_error": None,
        "failed_attempts": 0,
        "locked_until": 0.0,
    }.items():
        st.session_state.setdefault(key, value)


def clear_business() -> None:
    st.session_state.update(
        trace_state="idle",
        trace_result=None,
        submitted_extraction=None,
        query_result=None,
        query_error=None,
    )


def logout() -> None:
    st.session_state.clear()
