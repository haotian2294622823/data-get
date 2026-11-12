"""同端口提供 Streamlit 页面和受限凭证保护的完整文件流式下载。"""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import streamlit as st
from starlette.routing import Route

from lot_extract.database import load_settings
from lot_extract.downloads import download_file
from lot_extract.jobs import job_manager

load_settings()


@asynccontextmanager
async def lifespan(_app):
    try:
        yield
    finally:
        await asyncio.to_thread(job_manager.close)


base = (st.get_option("server.baseUrlPath") or "").strip("/")
app = st.App(
    Path(__file__).with_name("ui.py"),
    routes=[Route(f"/{base + '/' if base else ''}exports/{{token}}", download_file, methods=["GET", "HEAD"])],
    lifespan=lifespan,
)
