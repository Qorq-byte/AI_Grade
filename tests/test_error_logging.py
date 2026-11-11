"""全局异常处理器不能自己崩掉（批次 35 发现的真 bug）。

`backend/main.py` 的兜底处理器原来是：

    logger.error(f"未处理的异常 ...: {exc}", exc_info=True)

两个问题叠在一起：

1. loguru 没有 `exc_info` 参数，它把这个 kwarg 当成**格式化参数**；
2. 异常文本里只要出现花括号（`{'a': 1}`、JSON、dict repr 都会），
   `message.format(exc_info=True)` 就把花括号当成占位符 → `IndexError`。

结果是：端点 500 之后，兜底处理器**自己又抛了一次**，客户端拿到的是空响应体
（连 `{"detail": "服务器内部错误"}` 都没有），排查时看不到任何线索。
批次 35 搬报告域时就被这个坑挡住过一次：真正的错误是「Pydantic 模型没定义完」，
但现场只看到空 500。
"""
import asyncio
import json

from starlette.requests import Request

from backend.main import global_exception_handler


def _request() -> Request:
    return Request({
        "type": "http", "method": "GET", "path": "/boom",
        "headers": [], "query_string": b"", "scheme": "http", "server": ("test", 80),
    })


def test_handler_survives_exception_text_with_braces():
    """异常文本带花括号时，处理器不能二次抛出。"""
    response = asyncio.run(global_exception_handler(_request(), ValueError("{'a': 1}")))

    assert response.status_code == 500


def test_handler_still_returns_a_readable_detail():
    response = asyncio.run(global_exception_handler(_request(), ValueError("bad {'x': 2}")))
    body = json.loads(response.body)

    assert body["success"] is False
    assert "服务器内部错误" in body["detail"]
