"""探针：结构化修复时，模型到底能看见原输出的多少条？

背景（Batch 13）：实测 b425eb90 那轮步骤 7 首次输出 4,458 tokens（≈20 个叶子指标），
触发一次修复后只剩 5 条、正文从 6,000+ 字掉到 1,059 字。三个叠加原因里有一个是
「回喂原文被截断到 6,000 字」——如果原始输出长于 6,000 字，模型**根本没看到后半段**，
那它「删掉」的那些条目其实是被程序吃掉的，不是它不听话。

本探针不调模型、不读网络，纯字符串，用来把这件事量化成可复现的数字。

用法：
    python tests/_probe_repair_echo.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.agents.tools import structured_output as so  # noqa: E402


def build_output(n_indicators: int = 20, reason_chars: int = 560) -> str:
    """造一段形态真实的步骤 7 输出：解释文字 + JSON。

    长度按「20 个叶子 × 每条约 640 字」≈ 12,000 字，刻意跨过旧的 6,000 字回显上限，
    这样截断与否才看得出来。
    """
    ids = [f"L1_{i:02d}_01_01" for i in range(1, n_indicators + 1)]
    lines = [
        "### 指标评分明细",
        "依据项目资料，对抽取出的三级指标逐项评分如下。",
        "",
        "```json",
        json.dumps(
            {
                "scores": {
                    iid: {
                        "actual_score": 1.0,
                        "evidence_id": "P1-3-1",
                        "reason": (
                            f"{iid} 的依据来自项目资料第三部分。"
                            + "该项目明确了建设范围、技术标准与验收要求，" * (reason_chars // 24)
                        ),
                    }
                    for iid in ids
                }
            },
            ensure_ascii=False,
        ),
        "```",
    ]
    return "\n".join(lines)


def echoed_text(step: int, raw: str) -> str:
    """从 repair_messages 里抠出真正回喂给模型的那段原文。"""
    messages = so.repair_messages(step, raw, ["scores 中缺少 actual_score"])
    human = getattr(messages[-1], "content", None)
    if human is None:  # langchain 缺失时的 dict 形态
        human = messages[-1]["content"]
    marker = "【原始输出】\n```\n"
    start = human.index(marker) + len(marker)
    end = human.rindex("\n```\n\n请直接输出修复后的 JSON 对象。")
    return human[start:end]


def report(label: str, step: int, raw: str):
    seen = echoed_text(step, raw)
    total_ids = so.mentioned_indicator_ids(raw)
    seen_ids = so.mentioned_indicator_ids(seen)
    lost = sorted(total_ids - seen_ids)
    print(f"  {label}")
    print(f"    原文 {len(raw):>6} 字，回显 {len(seen):>6} 字"
          f"（{len(seen) / max(len(raw), 1):.0%}）")
    print(f"    原文提及 {len(total_ids):>2} 个指标，回显后模型只能看见 {len(seen_ids):>2} 个")
    if lost:
        print(f"    被截断看不见的：{len(lost)} 个 → {', '.join(lost[:6])}"
              + (" …" if len(lost) > 6 else ""))
    return len(total_ids), len(seen_ids)


def main() -> int:
    raw = build_output()
    print("=== 步骤 7 修复回显：改前（6,000 字上限） vs 改后（24,000 字上限） ===")

    saved = dict(so.MAX_ECHO_CHARS_BY_STEP)
    try:
        so.MAX_ECHO_CHARS_BY_STEP.clear()
        before = report("改前：所有步骤统一 6,000 字", 7, raw)
        so.MAX_ECHO_CHARS_BY_STEP.update(saved)
        after = report("改后：步骤 7 单独 24,000 字", 7, raw)
    finally:
        so.MAX_ECHO_CHARS_BY_STEP.clear()
        so.MAX_ECHO_CHARS_BY_STEP.update(saved)

    print()
    print(f"结论：同样一段 {len(raw)} 字的输出，改前模型只能看到 "
          f"{before[1]}/{before[0]} 个指标，改后能看到 {after[1]}/{after[0]} 个。")

    # 缩水判定：把「只看得到 5 条」的结果喂回去，确认会被拦下
    short = "```json\n" + json.dumps(
        {"scores": {iid: {"actual_score": 1.0, "evidence_id": "P1-3-1", "reason": "x"}
                    for iid in list(so.mentioned_indicator_ids(raw))[:5]}},
        ensure_ascii=False,
    ) + "\n```"
    outcome = so.parse(7, short)
    print(f"缩水判定：原文 {len(so.mentioned_indicator_ids(raw))} 个指标，"
          f"修复结果 {len(outcome.payload['scores'])} 个 → "
          f"repair_shrunk={so.repair_shrunk(7, raw, outcome)}"
          f"（True 表示拒绝采用修复版本）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
