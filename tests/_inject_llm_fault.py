"""故障注入：真实 HTTP 429 → 真实降级到可用模型（批次 31 的验收）。

单测用假客户端验证"会不会降级"，这个脚本验证**真实链路**：
起一个本地 stub，对任何请求返回 429；主模型指向它，备选指向真实 DeepSeek。
跑通说明：异常真的被分类成 transient、真的换了客户端、真的拿到了正文。

用法（要有可用的 DEEPSEEK_API_KEY）：
    NO_PROXY='*' .venv/Scripts/python.exe tests/_inject_llm_fault.py
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

from backend.agents import performance_agent as pa


class _Stub429(BaseHTTPRequestHandler):
    """永远 429，模拟配额/限流。"""

    def do_POST(self):  # noqa: N802 - http.server 的约定
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        body = json.dumps({
            "error": {"message": "rate limited by stub", "type": "rate_limit_error",
                      "code": "rate_limit_exceeded"},
        }).encode()
        self.send_response(429)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静音
        return


def main() -> int:
    server = HTTPServer(("127.0.0.1", 0), _Stub429)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"stub 429 已在 127.0.0.1:{port}")

    from langchain_openai import ChatOpenAI

    stub_llm = ChatOpenAI(model="stub-model", api_key="stub-key",
                          base_url=f"http://127.0.0.1:{port}/v1", max_retries=0)

    agent = pa.PerformanceAgent.__new__(pa.PerformanceAgent)
    agent.session_id = "fault-injection"
    agent.model_name = "stub-model"
    agent.temperature = 0.3
    agent.top_p = 0.9
    agent.base_agent = SimpleNamespace(llm=stub_llm, model_name="stub-model", max_tokens=256)

    fallback = "deepseek-chat"
    built = {}

    def fake_llm_for_step(step_number, model_name=None):
        target = model_name or agent.model_name
        if target == "stub-model":
            return stub_llm
        if target not in built:
            from backend.agents.base_agent import build_llm_for
            built[target] = build_llm_for(target, 0.3, 0.9, 256)
        return built[target]

    agent._llm_for_step = fake_llm_for_step

    state = {"session_id": "fault-injection", "warnings": [],
             "system_config": {"fallback_models": [fallback]}}

    try:
        text = agent._invoke_llm(
            [{"role": "user", "content": "只回复两个字：收到"}], 12, state, timeout=60)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL 没有降级，直接失败：{type(exc).__name__}: {exc}")
        server.shutdown()
        return 1

    attempts = state.get("llm_attempts") or []
    print(f"返回正文：{text.strip()[:40]!r}")
    for item in attempts:
        print(f"  attempt#{item['attempt']} {item['model']} -> {item['outcome']} "
              f"({item['detail'] or 'ok'}, {item['latency_ms']} ms)")
    warnings = [w for w in state.get("warnings") or [] if "备选模型" in w]
    print(f"降级告警：{warnings}")

    server.shutdown()

    ok = (text.strip() and len(attempts) == 2
          and attempts[0]["outcome"] == "error" and attempts[0]["detail"].startswith("transient")
          and attempts[1]["outcome"] == "ok" and warnings)
    print("结论：", "PASS 真实 429 触发了降级并拿到正文" if ok else "FAIL 与预期不符")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
