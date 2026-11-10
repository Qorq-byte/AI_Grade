"""A/B：漏项补全到底值不值多花一次调用（批次 29 步骤 9 / 批次 30 步骤 10）。

这是本批的**决策依据**，不是回归测试。它跑真实模型、花钱，所以不放进 CI，
也不放进 `tests/` 的常规收集（文件名以 `_` 开头）。

做法：拿一份真实会话的状态，同一输入跑两遍 —— 一遍不开补全，一遍开。
指标四项，全部客观：

1. **低分指标点名率**（核心）：`consistency.low_score_indicator_names` 算出的指标名
   有几个出现在正文里。批次 26 量出存量 78% 没被点名，这一项就是那条结论的复核口径。
2. **字数**：补全不该把正文写胖一倍。
3. **token 成本**：补全多花的量。
4. **闸门拒绝**：被拒的次数 —— 被拒说明模型改坏了，比"补上了"更值得看。

用法：
    python -m tests._ab_critique --step 9  --session 5eedf8e0 --rounds 2
    python -m tests._ab_critique --step 10 --session 5eedf8e0 --rounds 3
"""
from __future__ import annotations

import argparse
import copy
import statistics
from pathlib import Path

from backend.agents import performance_agent as pa
from backend.services import consistency


class _UsageSpy:
    """`_record_usage` 默认写库（token_usage 表）。A/B 只要增量，就把写库截下来累加。

    顺带说明：`state["token_records"]` 是另一条路径（多模型对比用），单模型下是空的，
    拿它当成本会把结果读成 0。
    """

    def __init__(self, agent):
        self.agent = agent
        self.total = 0
        self._original = agent._record_usage

    def __enter__(self):
        def spy(usage, step_number, state, estimated=False):
            self.total += int((usage or {}).get("total_tokens") or 0)
            return self._original(usage, step_number, state, estimated)

        self.agent._record_usage = spy
        return self

    def __exit__(self, *exc):
        self.agent._record_usage = self._original
        return False


def _named_ratio(state, text):
    names = consistency.low_score_indicator_names(state)
    if not names:
        return 1.0, names, []
    named = [name for name in names if name in (text or "")]
    return len(named) / len(names), names, [n for n in names if n not in (text or "")]


STEP_FIELDS = {9: ("problems_analysis", "critique_step9", "step9_critique"),
               10: ("suggestions", "critique_step10", "step10_critique")}


def _run_once(agent, state, step: int, critique: bool):
    state = copy.deepcopy(state)
    # 备份里的 step_control 可能缺键（老会话尤其如此），先用真实默认值补齐，
    # 再打开开关 —— 否则 `_record_step_result` 会在 step_statuses 上 KeyError
    field_name, flag, ledger_field = STEP_FIELDS[step]
    pa._ensure_step_control(state)
    state["step_control"][flag] = critique
    with _UsageSpy(agent) as spy:
        out = agent._node_step9(state) if step == 9 else agent._node_step10(state)
    text = out.get(field_name) or ""
    ratio, names, missing = _named_ratio(out, text)
    ledger = out.get(ledger_field) or {}
    return {
        "text": text,
        "chars": len(text.strip()),
        "ratio": ratio,
        "low_score": len(names),
        "missing": missing,
        "tokens": spy.total,
        "revised": bool(ledger.get("revised")),
        "rejected": ledger.get("rejected") or "",
        "calls": 2 if ledger.get("enabled") else 1,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", default="", help="用哪份真实备份当输入（前缀即可）")
    parser.add_argument("--model", default="", help="默认沿用会话里的模型")
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--step", type=int, default=9, choices=(9, 10))
    parser.add_argument("--dump-dir", default=".backup/ab_critique",
                        help="把两版正文落盘，便于人工抽检")
    args = parser.parse_args()

    files = sorted(Path(pa.STATE_BACKUP_DIR).glob("*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if args.session:
        files = [p for p in files if p.stem.startswith(args.session)]
    target = None
    for path in files:
        state = pa.PerformanceAgent.load_state_backup(path.stem)
        if state and (state.get("indicator_analysis") or "") and state.get("indicator_score_map"):
            target = (path.stem, state)
            break
    if not target:
        print("找不到可用作输入的会话（需要 indicator_analysis + indicator_score_map）")
        return 1

    if args.step not in STEP_FIELDS:
        print(f"只支持步骤 {list(STEP_FIELDS)}")
        return 1
    session_id, state = target
    model = args.model or state.get("model_name") or "deepseek-chat"
    print(f"输入会话：{session_id}")
    print(f"模型：{model}；轮次：{args.rounds}\n")

    agent = pa.PerformanceAgent(session_id=f"ab-{session_id[:8]}", model_name=model)
    field_name = STEP_FIELDS[args.step][0]
    base_ratio, names, _ = _named_ratio(state, state.get(field_name) or "")
    print(f"步骤 {args.step}：低分指标 {len(names)} 个；"
          f"该会话现有点名率 {base_ratio:.0%}\n")

    rows = []
    for index in range(args.rounds):
        off = _run_once(agent, state, args.step, critique=False)
        on = _run_once(agent, state, args.step, critique=True)
        rows.append((off, on))
        dump = Path(args.dump_dir)
        dump.mkdir(parents=True, exist_ok=True)
        (dump / f"round{index + 1}_off.md").write_text(off["text"], encoding="utf-8")
        (dump / f"round{index + 1}_on.md").write_text(on["text"], encoding="utf-8")
        print(f"第 {index + 1} 轮")
        print(f"  不开补全：点名 {off['ratio']:.0%}  字数 {off['chars']:>5}  "
              f"tokens {off['tokens']:>5}")
        print(f"  开启补全：点名 {on['ratio']:.0%}  字数 {on['chars']:>5}  "
              f"tokens {on['tokens']:>5}  revised={on['revised']}"
              + (f"  拒绝：{on['rejected']}" if on["rejected"] else ""))
        print()

    def avg(items, key):
        return statistics.mean(item[key] for item in items) if items else 0

    offs = [row[0] for row in rows]
    ons = [row[1] for row in rows]
    print("=" * 60)
    print(f"点名率     不开 {avg(offs, 'ratio'):.1%}  →  开启 {avg(ons, 'ratio'):.1%}")
    print(f"字数       不开 {avg(offs, 'chars'):.0f}  →  开启 {avg(ons, 'chars'):.0f}"
          f"（{avg(ons, 'chars') / max(1, avg(offs, 'chars')):.2f} 倍）")
    print(f"tokens     不开 {avg(offs, 'tokens'):.0f}  →  开启 {avg(ons, 'tokens'):.0f}"
          f"（{avg(ons, 'tokens') / max(1, avg(offs, 'tokens')):.2f} 倍）")
    print(f"补全生效   {sum(1 for item in ons if item['revised'])}/{len(ons)} 轮")
    print(f"被闸门拒绝 {sum(1 for item in ons if item['rejected'])}/{len(ons)} 轮")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
