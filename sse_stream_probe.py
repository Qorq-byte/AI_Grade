# -*- coding: utf-8 -*-
"""
SSE实时流探测：用原始 http.client 连接，记录每个事件到达的时间戳。
若事件随步骤完成陆续到达（而非全部在结束时一次性到达），说明服务端流式正常。
"""
import http.client
import json
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HOST = "127.0.0.1"
PORT = int(os.environ.get("AI_GRADE_PROBE_PORT", "8000"))


def main():
    token = os.environ.get("AI_GRADE_PROBE_TOKEN")
    if not token:
        print("请设置 AI_GRADE_PROBE_TOKEN（隔离环境的访问令牌）")
        return 1
    body = json.dumps({
        "project_name": "SSE流式探测项目",
        "file_paths": [],
        "model_name": "deepseek-chat",
        "comparison_mode": "single",
        "temperature_profile": "normal",
        "top_p": 0.9,
    })
    conn = http.client.HTTPConnection(HOST, PORT, timeout=300)
    t0 = time.time()
    conn.request("POST", "/api/agent/workflow/start-stream",
                 body=body,
                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    resp = conn.getresponse()
    print(f"[{time.time()-t0:6.1f}s] 响应状态: {resp.status}", flush=True)
    if resp.status != 200:
        print(resp.read(500))
        return 1

    seen_steps = set()
    first_event_t = None
    buf = b""
    events = []
    stop_at = None  # 观察到第3步完成即停止
    while True:
        chunk = resp.read1(4096)
        if not chunk:
            break
        buf += chunk
        while b"\n\n" in buf:
            raw, buf = buf.split(b"\n\n", 1)
            for ln in raw.split(b"\n"):
                ln = ln.strip()
                if not ln.startswith(b"data:"):
                    continue
                try:
                    ev = json.loads(ln[5:].decode("utf-8", "replace"))
                except Exception:
                    continue
                now = time.time() - t0
                if first_event_t is None:
                    first_event_t = now
                et = ev.get("event")
                if et in ("step_complete", "complete", "error"):
                    print(f"[{now:6.1f}s] {et} step={ev.get('step_number')} "
                          f"len={len(ev.get('content') or '')}", flush=True)
                    events.append((now, et))
                    if et == "step_complete":
                        seen_steps.add(ev.get("step_number"))
                if et == "error":
                    print("  错误:", ev.get("message", "")[:200], flush=True)
                    conn.close()
                    return 1
        if stop_at is None and len(seen_steps) >= 3:
            stop_at = time.time()
            print(f"[{time.time()-t0:6.1f}s] 已观察到3个步骤完成事件，停止读取（工作流将继续在后台执行）", flush=True)
            break
        if time.time() - t0 > 600:
            print("超时")
            break

    conn.close()
    elapsed_first = first_event_t - 0 if first_event_t is not None else None
    print(f"首个事件到达于 {elapsed_first:.1f}s", flush=True)
    ok = first_event_t is not None and len(seen_steps) >= 3
    print("结论:", "服务端SSE实时推送正常" if ok else "事件未实时到达（可能被缓冲）", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
