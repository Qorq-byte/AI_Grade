"""真浏览器探针：工作流页的「正在运行」状态与实时输出（批次 55）。

**它不是 CI 用例**：要本机装了 Chrome、要起后端，跑一次十几秒。
它回答的是单测回答不了的问题 —— 用户点来点去之后，DOM 到底长什么样。

复现路径（就是用户报的那条）：
  1. 模拟 SSE：step_start(4) + 若干 token_delta；
  2. 模拟轮询：pollStatus 每 5 秒调一次 renderStepList()，这里直接调它；
  3. 点击别的步骤（showWorkflowStep(5)），再点回运行中的步骤（showWorkflowStep(4)）。

断言：
  A. 14 行都渲染出来（说明 workflow-steps.js 加载成功、没抛异常）；
  B. **renderStepList() 之后第 4 行仍然是 running**（回归点）；
  C. 实时面板有内容，且点走再点回来内容还在；
  D. 长文本时面板内部会自动滚到底；用户上翻后不再抢滚动；
  E. step_complete 用完整正文替换流式累积。

用法：
    .venv/Scripts/python.exe -m tests._probe_workflow_live_panel
    .venv/Scripts/python.exe -m tests._probe_workflow_live_panel --keep   # 不关浏览器
"""
import argparse
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    str(Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe"),
]
BASE = "http://127.0.0.1:8000"
PORT = 9333


def _chrome() -> str:
    for path in CHROME_CANDIDATES:
        if Path(path).exists():
            return path
    raise SystemExit("找不到 Chrome，跳过探针（这是环境缺失，不是用例失败）")


def _token() -> str:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from _e2e_auth import ensure_e2e_admin

    user, pwd = ensure_e2e_admin(BASE)
    req = urllib.request.Request(
        BASE + "/api/auth/login",
        data=json.dumps({"username": user, "password": pwd}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())["data"]["access_token"]


class CDP:
    def __init__(self, ws_url: str):
        import websocket

        self.ws = websocket.create_connection(ws_url, timeout=20)
        self.seq = 0

    def send(self, method: str, params: dict = None):
        self.seq += 1
        self.ws.send(json.dumps({"id": self.seq, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") == self.seq:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})

    def eval(self, expression: str, await_promise: bool = False):
        result = self.send("Runtime.evaluate", {
            "expression": expression, "returnByValue": True,
            "awaitPromise": await_promise, "userGesture": True,
        })
        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"]
            raise RuntimeError(f"页面里抛异常：{detail.get('text')} {detail.get('exception', {})}")
        return result.get("result", {}).get("value")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="工作流页实时输出的真浏览器探针")
    parser.add_argument("--keep", action="store_true", help="跑完不关浏览器")
    args = parser.parse_args(argv)

    profile = tempfile.mkdtemp(prefix="wf-probe-")
    proc = subprocess.Popen([
        _chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
        "--no-default-browser-check", f"--remote-debugging-port={PORT}",
        f"--user-data-dir={profile}", "--window-size=1440,900", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    checks = []

    def check(name: str, ok: bool, detail=""):
        checks.append((name, bool(ok), detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + str(detail)) if detail else ''}")

    try:
        ws_url = ""
        for _ in range(60):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=2) as resp:
                    targets = json.loads(resp.read())
                page = next((t for t in targets if t.get("type") == "page"), None)
                if page:
                    ws_url = page["webSocketDebuggerUrl"]
                    break
            except Exception:
                time.sleep(0.3)
        if not ws_url:
            raise SystemExit("Chrome 调试端口没起来")

        cdp = CDP(ws_url)
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")
        cdp.send("Emulation.setDeviceMetricsOverride",
                 {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        token = _token()
        cdp.send("Page.addScriptToEvaluateOnNewDocument", {
            "source": f"try{{sessionStorage.setItem('token', {json.dumps(token)});"
                      f"sessionStorage.removeItem('aiGradeWorkflowSession');}}catch(e){{}}"
        })
        cdp.send("Page.navigate", {"url": BASE + "/workflow"})
        time.sleep(2.0)

        # A. 页面渲染
        check("14 个步骤行都渲染出来", cdp.eval(
            "document.querySelectorAll('#step-list .step-row').length") == 14,
            cdp.eval("document.querySelectorAll('#step-list .step-row').length"))
        check("没有内联脚本异常", cdp.eval("typeof WorkflowSteps === 'object'"))

        # 1) 模拟 SSE：第 4 步开始 + 三段增量
        cdp.eval("handleSSEEvent({event:'step_start',step_number:4,step_name:'生成各指标分值'})")
        cdp.eval("""['第一段：指标A 8分。\\n','第二段：指标B 6分。\\n','第三段：指标C 7分。\\n'].forEach(
                    t => handleSSEEvent({event:'token_delta',step_number:4,delta:t}))""")
        cdp.eval("new Promise(r => setTimeout(r, 200))", await_promise=True)

        check("运行中的行是 running", "running" in cdp.eval(
            "document.getElementById('step-4').className"))
        live = cdp.eval("(document.querySelector('#step-detail-content .live-output')||{}).textContent||''")
        check("实时面板拿到了增量", "指标C" in live, repr(live[-24:]))

        # 2) 模拟轮询重画（回归点：以前这里会把 running 抹成 ⏳）
        cdp.eval("renderStepList()")
        check("轮询重画后第 4 行仍是 running", "running" in cdp.eval(
            "document.getElementById('step-4').className"))
        check("轮询重画后第 5 行仍是 pending", "pending" in cdp.eval(
            "document.getElementById('step-5').className"))

        # 3) 点别的步骤，再点回来
        cdp.eval("showWorkflowStep(5)")
        check("点未开始的步骤有明确提示", "尚未开始" in cdp.eval(
            "document.getElementById('workflow-step-content').textContent"))
        cdp.eval("showWorkflowStep(4)")
        check("点回运行中的步骤：面板可见", cdp.eval(
            "!document.getElementById('step-detail-panel').classList.contains('hidden')"))
        check("点回运行中的步骤：内容还在", "指标C" in cdp.eval(
            "(document.querySelector('#step-detail-content .live-output')||{}).textContent||''"))

        # 4) 长文本的自动跟随 + 用户上翻后不抢
        cdp.eval("""(() => { const pre = document.querySelector('#step-detail-content .live-output');
                    handleSSEEvent({event:'token_delta',step_number:4,
                                    delta: Array.from({length:400},(_,i)=>'行'+i+'\\n').join('')}); })()""")
        cdp.eval("new Promise(r => setTimeout(r, 200))", await_promise=True)
        gap = cdp.eval("""(() => { const p = document.querySelector('#step-detail-content .live-output');
                           return Math.round(p.scrollHeight - p.scrollTop - p.clientHeight); })()""")
        check("长文本自动跟到最底", gap is not None and gap <= 24, f"gap={gap}")
        cdp.eval("document.querySelector('#step-detail-content .live-output').scrollTop = 0")
        cdp.eval("handleSSEEvent({event:'token_delta',step_number:4,delta:'尾巴\\n'})")
        cdp.eval("new Promise(r => setTimeout(r, 200))", await_promise=True)
        top = cdp.eval("document.querySelector('#step-detail-content .live-output').scrollTop")
        check("用户上翻后不再被拽回底部", top == 0, f"scrollTop={top}")

        # 5) step_complete 用完整正文替换
        cdp.eval("""handleSSEEvent({event:'step_complete',step_number:4,
                    step_name:'生成各指标分值',content:'最终正文：共 23 项指标。'})""")
        final = cdp.eval("(document.querySelector('#step-detail-content .live-output')||{}).textContent||''")
        check("完成后用完整正文替换流式累积", final.strip() == "最终正文：共 23 项指标。", repr(final[-20:]))
        check("完成后该行变 completed", "completed" in cdp.eval(
            "document.getElementById('step-4').className"))

        # 6) 换步骤：step_start 那一刻就该清屏换标题（不等第一个增量）
        cdp.eval("handleSSEEvent({event:'step_start',step_number:5,step_name:'生成各指标评分标准'})")
        title5 = cdp.eval("document.getElementById('step-detail-title').textContent")
        pre5 = cdp.eval("(document.querySelector('#step-detail-content .live-output')||{}).textContent||''")
        check("step_start 后立刻清屏并换标题", pre5 == "" and "Step 5" in title5,
              f"{title5!r} / {pre5[:12]!r}")

        cdp.eval("handleSSEEvent({event:'token_delta',step_number:5,delta:'新步骤的开头'})")
        cdp.eval("new Promise(r => setTimeout(r, 200))", await_promise=True)
        text5 = cdp.eval(
            "(document.querySelector('#step-detail-content .live-output')||{}).textContent||''")
        check("换步骤后不带上一句残留",
              "新步骤的开头" in text5 and "最终正文" not in text5, repr(text5[:24]))

        if not args.keep:
            proc.terminate()
    finally:
        if not args.keep:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()

    failed = [name for name, ok, _ in checks if not ok]
    print(f"\n{len(checks) - len(failed)} 通过 / {len(failed)} 失败")
    if failed:
        print("失败：", failed)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
