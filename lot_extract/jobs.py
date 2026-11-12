"""后台导出任务管理；任务状态放内存，结果和进度清单放磁盘。"""

from __future__ import annotations

import os
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock
from uuid import uuid4

from .batch_export import error_message, export_batches
from .database import BASE, Filters


class ExportJob:
    def __init__(self, directory: Path):
        self.directory = directory
        self.cancel = Event()
        self.future: Future | None = None
        self._lock = Lock()
        self._state = {
            "status": "queued", "stage": "等待后台执行",
            "found_lots": 0, "rows": 0, "parts": [],
        }

    def update(self, state: dict) -> None:
        with self._lock:
            self._state = {**state, "parts": list(state.get("parts", []))}

    def snapshot(self) -> dict:
        with self._lock:
            return {**self._state, "parts": list(self._state.get("parts", []))}


class JobManager:
    """一次只运行一个数据库导出，避免多用户同时拉取年度数据压垮生产库。"""

    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lot-export")
        self._lock = Lock()
        self._active: ExportJob | None = None

    def submit(self, filters: Filters, product: str) -> ExportJob:
        filters.where()
        with self._lock:
            if self._active is not None and (self._active.future is None or not self._active.future.done()):
                raise ValueError("服务器已有导出任务运行，请等待完成或取消当前任务。")
            query_id = "batch_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex
            directory = Path(os.getenv("EXPORT_DIR") or BASE / "outputs" / "queries") / product / query_id
            job = ExportJob(directory)
            self._active = job
            job.future = self._executor.submit(self._run, job, filters, product)
            return job

    @staticmethod
    def _run(job: ExportJob, filters: Filters, product: str) -> None:
        try:
            result = export_batches(filters, product, directory=job.directory, progress=job.update, cancel=job.cancel)
            job.update(result)
        except Exception as error:
            state = job.snapshot()
            state["status"] = "cancelled" if job.cancel.is_set() else "failed"
            state["error"] = state["stage"] = error_message(error)
            job.update(state)

    def close(self) -> None:
        """用于测试或服务有序关闭，不强行终止数据库线程。"""
        if self._active is not None:
            self._active.cancel.set()
        self._executor.shutdown(wait=True)


# ASGI 下载入口与页面共享同一服务进程；关闭服务器时也能取消后台任务。
job_manager = JobManager()
