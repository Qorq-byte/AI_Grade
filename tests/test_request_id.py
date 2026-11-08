"""request_id 贯通：响应头 / 错误体 / 日志三处必须能串起同一个请求。"""
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from loguru import logger

from backend.main import app
from backend.middleware.request_context import (
    RequestContextMiddleware,
    get_request_id,
    install_log_context,
    set_request_id,
)

client = TestClient(app)


def test_every_response_carries_a_fresh_request_id():
    first = client.get("/health")
    second = client.get("/health")
    assert first.status_code == 200
    rid = first.headers["X-Request-ID"]
    assert len(rid) == 16
    assert second.headers["X-Request-ID"] != rid


def test_inbound_request_id_is_echoed_so_frontend_and_backend_agree():
    rid = "fe-trace-abc-123"
    response = client.get("/health", headers={"X-Request-ID": rid})
    assert response.headers["X-Request-ID"] == rid


@pytest.mark.parametrize("bad", ["has space", "x" * 200, "<script>alert(1)</script>"])
def test_invalid_inbound_request_id_is_replaced(bad):
    response = client.get("/health", headers={"X-Request-ID": bad})
    generated = response.headers["X-Request-ID"]
    assert generated != bad
    assert len(generated) == 16


def test_error_body_repeats_the_response_header_id():
    rid = "trace-0001-abcd"
    response = client.get("/api/agent/workflow/owned/status", headers={"X-Request-ID": rid})
    assert response.status_code in (401, 403)
    assert response.headers["X-Request-ID"] == rid
    assert response.json()["request_id"] == rid


def test_not_found_body_carries_request_id():
    response = client.get("/api/agent/definitely-not-a-route")
    assert response.status_code == 404
    assert response.json()["request_id"] == response.headers["X-Request-ID"]


def _mini_app() -> FastAPI:
    mini = FastAPI()
    mini.add_middleware(RequestContextMiddleware)

    @mini.get("/echo")
    async def echo():
        return {"rid": get_request_id(), "state_rid": "n/a"}

    return mini


def test_context_variable_is_visible_inside_the_request():
    mini_client = TestClient(_mini_app())
    response = mini_client.get("/echo")
    assert response.json()["rid"] == response.headers["X-Request-ID"]


def test_concurrent_requests_do_not_share_ids():
    mini_client = TestClient(_mini_app())
    results = {}
    errors = []

    def call(index):
        try:
            response = mini_client.get("/echo")
            results[index] = (response.json()["rid"], response.headers["X-Request-ID"])
        except Exception as exc:  # pragma: no cover - 只用于暴露并发问题
            errors.append(exc)

    threads = [threading.Thread(target=call, args=(i,)) for i in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert len(results) == 5
    for body_rid, header_rid in results.values():
        assert body_rid == header_rid
    assert len({rid for rid, _ in results.values()}) == 5


def test_logs_carry_request_id(tmp_path):
    install_log_context()
    sink_file = tmp_path / "log.txt"
    sink_id = logger.add(str(sink_file), format="{extra[request_id]}|{message}", level="INFO")
    try:
        set_request_id("log-trace-1")
        logger.info("with context")
        set_request_id("")
        logger.info("without context")
    finally:
        logger.remove(sink_id)

    lines = sink_file.read_text(encoding="utf-8").splitlines()
    assert "log-trace-1|with context" in lines
    assert "-|without context" in lines
