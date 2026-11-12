"""登录后签发限时文件下载凭证，HTTP 按块发送完整文件并支持 Range。"""

from __future__ import annotations

import asyncio
import secrets
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Lock
from time import monotonic

from starlette.requests import Request
from starlette.responses import FileResponse, Response

TOKEN_TTL = 3600


@dataclass(frozen=True)
class DownloadGrant:
    owner: str
    path: Path
    filename: str
    mime: str
    expires: float


class DownloadRegistry:
    """凭证只对应服务端明确注册的文件，不接受请求提供的路径。"""

    def __init__(self):
        self._lock = Lock()
        self._grants: dict[str, DownloadGrant] = {}
        self._keys: dict[tuple, str] = {}

    @staticmethod
    def _key(grant: DownloadGrant) -> tuple:
        return grant.owner, grant.path, grant.filename, grant.mime

    def _remove(self, token: str) -> None:
        grant = self._grants.pop(token)
        self._keys.pop(self._key(grant), None)

    def issue(self, owner: str, path: Path, filename: str, mime: str) -> str:
        now = monotonic()
        path = path.resolve()
        key = owner, path, filename, mime
        with self._lock:
            for token, grant in list(self._grants.items()):
                if grant.expires <= now:
                    self._remove(token)
            token = self._keys.get(key)
            if token is None:
                token = secrets.token_urlsafe(32)
                self._keys[key] = token
                self._grants[token] = DownloadGrant(owner, path, filename, mime, now + TOKEN_TTL)
            else:
                self._grants[token] = replace(self._grants[token], expires=now + TOKEN_TTL)
            return token

    def get(self, token: str) -> DownloadGrant | None:
        with self._lock:
            grant = self._grants.get(token)
            if grant is not None and grant.expires <= monotonic():
                self._remove(token)
                return None
            return grant

    def revoke(self, owner: str) -> None:
        with self._lock:
            for token, grant in list(self._grants.items()):
                if grant.owner == owner:
                    self._remove(token)


downloads = DownloadRegistry()


async def download_file(request: Request) -> Response:
    grant = downloads.get(request.path_params["token"])
    if grant is None:
        return Response("Download unavailable or expired.", status_code=404)
    try:
        details = await asyncio.to_thread(grant.path.stat)
    except OSError:
        return Response("Download unavailable.", status_code=404)
    if not stat.S_ISREG(details.st_mode):
        return Response("Download unavailable.", status_code=404)
    # 不进入 Streamlit 内存媒体缓存；FileResponse 按块读盘，原生支持断点 Range 请求。
    return FileResponse(
        grant.path, filename=grant.filename, media_type=grant.mime, stat_result=details,
        headers={
            "Cache-Control": "private, no-store",
            "Content-Encoding": "identity",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )
