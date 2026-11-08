"""
请求上下文中间件
为每个 HTTP 请求生成/透传 request_id，并把它注入三处：
  1. 响应头 X-Request-ID（前端可读，跨域时需 CORS expose_headers 配合）
  2. 日志（loguru patcher，一行日志即可定位到具体请求）
  3. 异常响应体（{"success": false, "detail": ..., "request_id": ...}）

实现刻意使用纯 ASGI 中间件而非 BaseHTTPMiddleware：
后者会在 StreamingResponse（SSE）外再包一层 anyio 任务，容易引入缓冲/断连时序问题。
"""
import re
import uuid
from contextvars import ContextVar
from typing import Optional

from loguru import logger
from starlette.datastructures import MutableHeaders

REQUEST_ID_HEADER = "X-Request-ID"
# 只接受可打印且长度合理的入站值，避免日志注入 / 超长头
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{4,80}$")

_request_id: ContextVar[str] = ContextVar("request_id", default="")


def get_request_id() -> str:
    """当前请求的 request_id（无上下文时返回空串）。"""
    return _request_id.get()


def set_request_id(value: str) -> str:
    """显式设置当前上下文的 request_id（测试或后台任务使用）。"""
    _request_id.set(value or "")
    return value


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def _incoming_request_id(scope) -> str:
    for key, value in scope.get("headers") or []:
        if key == b"x-request-id":
            try:
                candidate = value.decode("latin-1").strip()
            except Exception:
                return ""
            if _VALID_REQUEST_ID.match(candidate):
                return candidate
            return ""
    return ""


class RequestContextMiddleware:
    """纯 ASGI 中间件：分配 request_id，回写响应头，进入/退出时维护 contextvar。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope) or new_request_id()
        token = _request_id.set(request_id)
        # 让 FastAPI 的 Request.state.request_id 可用（异常处理器会读）
        state = scope.get("state")
        if state is None:
            state = {}
            scope["state"] = state
        state["request_id"] = request_id

        async def send_with_request_id(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(raw=message.setdefault("headers", []))
                headers[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            _request_id.reset(token)


def install_log_context() -> None:
    """让每条日志自动带上 request_id（无请求上下文时记为 "-"）。

    注意：patcher 对 loguru 所有 handler 生效，且必须保证 extra 一定有该键，
    否则 format 里的 {extra[request_id]} 会抛 KeyError。
    """
    def _patcher(record):
        record["extra"]["request_id"] = _request_id.get() or "-"

    logger.configure(patcher=_patcher)
