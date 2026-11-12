"""聊天上下文评测跑分器（批次 42，B5 第三步）。

用法：
    .venv/Scripts/python.exe -m tests.chat_eval.run              # 打印指标
    .venv/Scripts/python.exe -m tests.chat_eval.run --verbose    # 每条缺了哪些锚点
    .venv/Scripts/python.exe -m tests.chat_eval.run --save       # 写入 baseline.json
    .venv/Scripts/python.exe -m tests.chat_eval.run --check      # 低于基线退出码 1

测什么：`chat_context.build_report_context` 把「用户问题 → 报告上下文」拼得对不对。
回答质量（措辞、是否编造）不在离线评测范围里 —— 那需要模型，且判定无法复现。
这里只回答一个硬问题：**回答这个问题所需的那句话，进没进上下文**。

为什么不用真实备份：备份是运行产物、不进 git，内容每次都不一样，指标没法比前后。
固定报告见 `fixture.py`，20 条问题见 `questions.py`。
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

from backend.services import chat_context
from tests.chat_eval import fixture, questions

BASELINE_PATH = Path(__file__).with_name("baseline.json")


def evaluate(verbose: bool = False) -> Dict:
    state = fixture.state()
    hits = 0
    details: List[Dict] = []
    chars = []

    for case in questions.CASES:
        context = chat_context.build_report_context(
            state, fixture.PROJECT, query=case["question"])
        text = context["text"]
        chars.append(len(text))

        if case["kind"] == "chitchat":
            ok = "【相关章节】" not in text
            missing = [] if ok else ["不该出现【相关章节】"]
        else:
            missing = [marker for marker in case["expect"] if marker not in text]
            ok = not missing

        hits += 1 if ok else 0
        details.append({"id": case["id"], "ok": ok, "missing": missing,
                        "question": case["question"]})

    metrics = {
        "recall": round(hits / len(questions.CASES), 4),
        "hits": hits,
        "total": len(questions.CASES),
        "avg_context_chars": round(sum(chars) / len(chars), 1),
        "max_context_chars": max(chars),
    }

    if verbose:
        for item in details:
            flag = "PASS" if item["ok"] else "FAIL"
            print(f"[{flag}] {item['id']} {item['question']}")
            if not item["ok"]:
                print(f"       缺：{item['missing']}")
    return {"metrics": metrics, "details": details}


def evaluate_without_snippets() -> Dict:
    """对照组：关掉章节检索，看命中率掉多少。

    这不是为了好看 —— 它回答的是「B39 到底有没有用」：
    如果关掉和不关掉差不多，那 20 条问题根本没在考章节检索。
    """
    original = chat_context.retrieve_chapter_snippets
    chat_context.retrieve_chapter_snippets = lambda *args, **kwargs: []
    try:
        return evaluate()
    finally:
        chat_context.retrieve_chapter_snippets = original


def _check_against_baseline(result: Dict) -> int:
    if not BASELINE_PATH.exists():
        print("没有 baseline.json，先跑 --save")
        return 1
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    base = baseline["metrics"]
    now = result["metrics"]
    failed = []

    if now["recall"] < base["recall"]:
        failed.append(f"recall {now['recall']} < 基线 {base['recall']}")
    # 上下文变大不是错误，但涨太多说明预算失控（会挤掉对话本身）
    if now["avg_context_chars"] > base["avg_context_chars"] * 1.2:
        failed.append(f"平均上下文 {now['avg_context_chars']} 字，超过基线 "
                      f"{base['avg_context_chars']} 的 1.2 倍")

    if failed:
        print("低于基线：")
        for item in failed:
            print(f"  - {item}")
        return 1
    print(f"PASS 聊天上下文评测：recall={now['recall']}（{now['hits']}/{now['total']}），"
          f"平均 {now['avg_context_chars']} 字")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="聊天上下文评测")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--save", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--control", action="store_true", help="同时跑「关掉章节检索」的对照组")
    args = parser.parse_args(argv)

    result = evaluate(verbose=args.verbose)
    metrics = result["metrics"]
    print(f"recall={metrics['recall']}（{metrics['hits']}/{metrics['total']}），"
          f"平均上下文 {metrics['avg_context_chars']} 字，"
          f"最长 {metrics['max_context_chars']} 字")

    if args.control:
        control = evaluate_without_snippets()["metrics"]
        print(f"对照组（关掉章节检索）：recall={control['recall']}"
              f"（{control['hits']}/{control['total']}）")

    if args.save:
        payload = {"date": date.today().isoformat(),
                   "fixture": "tests/chat_eval/fixture.py",
                   "metrics": metrics}
        BASELINE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")
        print(f"已写入 {BASELINE_PATH.name}")
        return 0
    if args.check:
        return _check_against_baseline(result)
    return 0 if metrics["recall"] == 1.0 else 1


if __name__ == "__main__":
    sys.exit(main())
