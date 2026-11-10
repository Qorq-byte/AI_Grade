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
    """前 `fail_times` 次返回 429，之后返回一句正常回复（模拟限流恢复）。"""

    fail_times = 10 ** 6
    calls = 0

    def do_POST(self):  # noqa: N802 - http.server 的约定
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        type(self).calls += 1
        if type(self).calls > type(self).fail_times:
            body = json.dumps({
                "id": "chatcmpl-stub", "object": "chat.completion", "created": 0,
                "model": "stub-model",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "收到"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
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


def _agent_for(stub_port: int):
    """主模型指向 stub，备选指向真实 DeepSeek。"""
    from langchain_openai import ChatOpenAI
    from types import SimpleNamespace

    stub_llm = ChatOpenAI(model="stub-model", api_key="stub-key",
                          base_url=f"http://127.0.0.1:{stub_port}/v1", max_retries=0)
    agent = pa.PerformanceAgent.__new__(pa.PerformanceAgent)
    agent.session_id = "fault-injection"
    agent.model_name = "stub-model"
    agent.temperature = 0.3
    agent.top_p = 0.9
    agent.base_agent = SimpleNamespace(llm=stub_llm, model_name="stub-model", max_tokens=256)

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
    return agent


def _state(fallbacks):
    return {"session_id": "fault-injection", "warnings": [],
            "system_config": {"fallback_models": fallbacks}}


def _show(title, state, text):
    attempts = state.get("llm_attempts") or []
    print(f"\n[{title}] 返回正文：{text.strip()[:40]!r}")
    for item in attempts:
        print(f"  attempt#{item['attempt']} {item['model']} -> {item['outcome']} "
              f"({item['detail'] or 'ok'}, {item['latency_ms']} ms)")
    warns = [w for w in state.get("warnings") or [] if "备选模型" in w]
    print(f"  降级告警：{'有' if warns else '无'}")
    return attempts, warns


def main() -> int:
    import os

    os.environ["LLM_RETRY_BACKOFF_SECONDS"] = "0"   # 别真等 2 秒
    server = HTTPServer(("127.0.0.1", 0), _Stub429)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"stub 已在 127.0.0.1:{port}")

    # ── 场景 A：只失败一次 → 重试同一模型成功，不该换模型 ──
    _Stub429.calls = 0
    _Stub429.fail_times = 1
    agent = _agent_for(port)
    state_a = _state(["deepseek-chat"])
    text_a = agent._invoke_llm([{"role": "user", "content": "只回复两个字：收到"}], 12,
                               state_a, timeout=60)
    attempts_a, warns_a = _show("A 重试成功", state_a, text_a)
    ok_a = (len(attempts_a) == 2
            and [a["model"] for a in attempts_a] == ["stub-model", "stub-model"]
            and [a["outcome"] for a in attempts_a] == ["error", "ok"]
            and not warns_a)

    # ── 场景 B：一直失败 → 重试一次后降级到真实模型 ──
    _Stub429.calls = 0
    _Stub429.fail_times = 10 ** 6
    agent_b = _agent_for(port)
    state_b = _state(["deepseek-chat"])
    text_b = agent_b._invoke_llm([{"role": "user", "content": "只回复两个字：收到"}], 12,
                                 state_b, timeout=60)
    attempts_b, warns_b = _show("B 重试后降级", state_b, text_b)
    ok_b = (len(attempts_b) == 3
            and [a["model"] for a in attempts_b] == ["stub-model", "stub-model", "deepseek-chat"]
            and [a["outcome"] for a in attempts_b] == ["error", "error", "ok"]
            and bool(warns_b))

    server.shutdown()

    print()
    print("场景 A（重试成功、不换模型）：", "PASS" if ok_a else "FAIL")
    print("场景 B（重试后降级、写告警）：", "PASS" if ok_b else "FAIL")
    return 0 if (ok_a and ok_b) else 1


if __name__ == "__main__":
    sys.exit(main())
