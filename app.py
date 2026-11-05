"""Streamlit 入口只组织认证与页面，SQL 和追溯算法位于 src。"""

from dotenv import load_dotenv
import streamlit as st
from src.catalog import BASE
from src.ui.login import require_login
from src.ui.page import render_page
from src.ui.state import initialize

st.set_page_config(
    page_title="制造批次追溯", page_icon=":material/account_tree:", layout="wide"
)
load_dotenv(BASE / ".env", override=False)
initialize()
require_login()
render_page()
