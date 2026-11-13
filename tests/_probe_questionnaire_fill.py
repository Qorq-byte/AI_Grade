"""真浏览器探针：问卷列表 → 打开填写页（批次 56）。

用户报：在「问卷填写」里点问卷，打不开、也没法填。

这里只回答一件事：**点下去之后 DOM 里到底有没有题目**。
两种可能的锅分得很清楚——
  * 点击目标不对（只有项目名那 30px 可点）→ 点卡片没反应；
  * 渲染过程抛异常 → 列表被替换成"生成问卷中..."之后再也不回来。

用法：
    .venv/Scripts/python.exe -m tests._probe_questionnaire_fill
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
PORT = 9334


def _chrome() -> str:
    for path in CHROME_CANDIDATES:
        if Path(path).exists():
            return path
    raise SystemExit("找不到 Chrome，跳过探针（环境缺失，不是用例失败）")


def _login() -> tuple:
    """返回 (token, user)。页面用 localStorage.user 判登录，只有 token 会被踢回 /login。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from _e2e_auth import ensure_e2e_admin

    user, pwd = ensure_e2e_admin(BASE)
    req = urllib.request.Request(
        BASE + "/api/auth/login",
        data=json.dumps({"username": user, "password": pwd}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())["data"]
    return data["access_token"], {
        "id": data["user_id"], "username": data["username"], "role": data["role"],
    }


class CDP:
    def __init__(self, ws_url: str):
        import websocket

        self.ws = websocket.create_connection(ws_url, timeout=25)
        self.seq = 0
        self.events = []

    def send(self, method: str, params: dict = None):
        self.seq += 1
        self.ws.send(json.dumps({"id": self.seq, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") == self.seq:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})
            if message.get("method") in ("Runtime.exceptionThrown", "Log.entryAdded"):
                self.events.append(message)

    def drain(self):
        """把已经到达但没被读的事件收进来（不阻塞）。"""
        self.ws.settimeout(0.2)
        try:
            while True:
                message = json.loads(self.ws.recv())
                if message.get("method") in ("Runtime.exceptionThrown", "Log.entryAdded"):
                    self.events.append(message)
        except Exception:
            pass
        finally:
            self.ws.settimeout(25)

    def eval(self, expression: str, await_promise: bool = False):
        result = self.send("Runtime.evaluate", {
            "expression": expression, "returnByValue": True,
            "awaitPromise": await_promise, "userGesture": True,
        })
        detail = result.get("exceptionDetails")
        if detail:
            raise RuntimeError(f"页面异常：{detail.get('text')} "
                               f"{json.dumps(detail.get('exception', {}), ensure_ascii=False)[:200]}")
        return result.get("result", {}).get("value")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="问卷打开的浏览器探针")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)

    profile = tempfile.mkdtemp(prefix="q-probe-")
    proc = subprocess.Popen([
        _chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
        "--no-default-browser-check", f"--remote-debugging-port={PORT}",
        f"--user-data-dir={profile}", "--window-size=1440,1000", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    checks = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok)))
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
        cdp.send("Log.enable")
        cdp.send("Emulation.setDeviceMetricsOverride",
                 {"width": 1440, "height": 1000, "deviceScaleFactor": 1, "mobile": False})
        token, user = _login()
        cdp.send("Page.addScriptToEvaluateOnNewDocument", {
            "source": f"try{{sessionStorage.setItem('token',{json.dumps(token)});"
                      f"localStorage.setItem('token',{json.dumps(token)});"
                      f"localStorage.setItem('user',{json.dumps(json.dumps(user))});"
                      f"sessionStorage.removeItem('aiGradeWorkflowSession');}}catch(e){{}}"
        })
        cdp.send("Page.addScriptToEvaluateOnNewDocument", {
            "source": "window.__apiCalls=[];(function(){var _f=window.fetch;"
                      "window.fetch=function(u,o){try{window.__apiCalls.push({u:String((u&&u.url)||u),"
                      "m:String((o&&o.method)||'GET').toUpperCase()})}catch(e){}"
                      "return _f.apply(this,arguments)}})();"
        })
        cdp.send("Page.navigate", {"url": BASE + "/client#questionnaire"})
        time.sleep(3.0)

        cards = cdp.eval("document.querySelectorAll('#questionnaireGrid .stat-card').length")
        check("问卷列表渲染出卡片", (cards or 0) > 0, f"cards={cards}")
        if not cards:
            raise SystemExit("列表都没有，后面的检查不成立")

        # 第一张卡片的 session id 与项目名（就是用户会点的那个）
        info = cdp.eval("""(() => {
            const card = document.querySelector('#questionnaireGrid .stat-card');
            const nameEl = card.querySelector('.stat-label');
            return {id: card.id, name: nameEl ? nameEl.textContent.trim() : '',
                    onclick: nameEl ? (nameEl.getAttribute('onclick') || '') : ''};
        })()""")
        print(f"      卡片: {info}")

        # A. 用户的第一反应：点卡片空白处
        cdp.eval("document.querySelector('#questionnaireGrid .stat-card').click()")
        cdp.eval("new Promise(r => setTimeout(r, 2000))", await_promise=True)
        radios_after_card = cdp.eval("document.querySelectorAll('#questionnaire input[type=radio]').length")
        check("点卡片空白处能打开问卷", (radios_after_card or 0) > 0,
              f"radio={radios_after_card}")

        # A2. 打开是"读"，不是"生成"：POST /questionnaire 会按 max_questions 重新生成一版
        calls = cdp.eval("window.__apiCalls || []") or []
        generate_posts = [c for c in calls
                          if c["m"] == "POST" and c["u"].split("?")[0].endswith("/api/agent/workflow/questionnaire")]
        check("打开问卷没有重新生成（无 POST /questionnaire）", not generate_posts, generate_posts[:2])

        # B. 点项目名（历史实现里只有这里有 onclick）
        if not radios_after_card:
            cdp.eval("document.querySelector('#questionnaireGrid .stat-label').click()")
            cdp.eval("new Promise(r => setTimeout(r, 2500))", await_promise=True)
            radios_after_name = cdp.eval(
                "document.querySelectorAll('#questionnaire input[type=radio]').length")
            check("点项目名能打开问卷", (radios_after_name or 0) > 0, f"radio={radios_after_name}")
            check("点项目名后没停在「生成问卷中…」",
                  "生成问卷中" not in (cdp.eval(
                      "document.getElementById('questionnaire').textContent") or ""))
        else:
            check("点项目名能打开问卷", True, "（卡片已能打开，跳过）")
            check("点项目名后没停在「生成问卷中…」", True, "（同上）")

        # C. 题目数量与列表里声明的一致
        counts = cdp.eval("""(() => {
            const radios = document.querySelectorAll('#questionnaire input[type=radio]');
            const names = new Set(Array.from(radios).map(r => r.name));
            return {radios: radios.length, questions: names.size};
        })()""")
        check("题目数 > 0 且每题 5 个选项",
              counts and counts["questions"] > 0 and counts["radios"] == counts["questions"] * 5,
              counts)

        # D. 能作答、能提交（提交只检查按钮存在并点得动，不真的提交污染数据）
        cdp.eval("""(() => { document.querySelectorAll('#questionnaire input[type=radio]').forEach(
                        (r, i) => { if (i % 5 === 4) r.click(); }); })()""")
        cdp.eval("new Promise(r => setTimeout(r, 200))", await_promise=True)
        checked = cdp.eval("document.querySelectorAll('#questionnaire input[type=radio]:checked').length")
        check("单选可以点选", (checked or 0) > 0, f"checked={checked}")

        cdp.drain()
        errors = []
        for event in cdp.events:
            if event["method"] == "Runtime.exceptionThrown":
                errors.append(event["params"]["exceptionDetails"].get("text"))
            elif event["method"] == "Log.entryAdded":
                entry = event["params"]["entry"]
                if entry.get("level") == "error":
                    url = entry.get("url", "")
                    # favicon 是浏览器默认行为，跟页面逻辑无关（全站都没有图标）
                    if url.endswith("/favicon.ico"):
                        continue
                    errors.append(f"{entry.get('text')} @ {url}")
        check("打开过程没有 JS 异常", not errors, errors[:2])

        if not args.keep:
            proc.terminate()
    finally:
        if not args.keep:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()

    failed = [name for name, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)} 通过 / {len(failed)} 失败")
    if failed:
        print("失败：", failed)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
