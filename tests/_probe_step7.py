"""用真实备份重放步骤 7：改前 / 改后各落地几项。

为什么不在真实 14 步里验证：一轮要 4 万多 token。步骤 7 的输入（indicator_system /
evaluation_table / effective_data_categorized / step_evidence / 模型原始输出）全在
状态备份里，本地重放能**逐字复现**当时的判定，且零 token。

用法：
    python tests/_probe_step7.py                 # 默认备份
    python tests/_probe_step7.py <session_id>    # 指定备份
"""
import json
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.agents import performance_agent  # noqa: E402
from backend.agents.tools.questionnaire_gen import indicator_leaves  # noqa: E402
from backend.agents.tools.score_validator import actual_indicator_score  # noqa: E402
from backend.services import evidence_anchor  # noqa: E402

DEFAULT_SESSION = "dc0c6294-4d94-43d1-9867-3c86d653151f"


def load(session_id):
    path = ROOT / "state_backups" / f"{session_id}.json"
    if not path.exists():
        raise SystemExit(f"找不到备份：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def fill_line_counts(state):
    """旧备份没有 `raw_line_counts`（该字段是 Batch 12 才进白名单的）。

    从 `uploads/` 目录按文件名补算行数，否则重放会判「拿不到原文 → 弱定位一律拒绝」，
    看起来像新逻辑没生效。只补备份里缺的部分，已有的不覆盖。
    """
    if state.get("raw_line_counts"):
        return state["raw_line_counts"], "备份自带"
    counts = {}
    for path in (ROOT / "uploads").glob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        counts[evidence_anchor.doc_label(path.name)] = len(
            [line for line in text.splitlines() if line.strip()])
    if counts:
        state["raw_line_counts"] = counts
        return counts, "从 uploads/ 补算"
    return {}, "无可用原文"


def old_rule(state, candidates):
    """改前的判定：证据必须是检索池里的精确 id。"""
    ids = {item["id"]: item for item in indicator_leaves(
        state.get("indicator_system", {}).get("level1_indicators", []))}
    pool = {hit["id"] for hit in state.get("step_evidence", {}).get("7", [])
            if hit["kind"] == "project"}
    accepted, why = {}, Counter()
    for key, item in (candidates or {}).items():
        if key not in ids or not isinstance(item, dict):
            why["指标 id 不在体系内"] += 1
            continue
        evidence, reason = item.get("evidence_id"), item.get("reason")
        value = actual_indicator_score(ids[key], {key: item})
        if value is None:
            why["模型未给数值（待核实/非法）"] += 1
            continue
        if not (isinstance(evidence, str) and evidence in pool):
            why["引用不是检索池里的精确 id"] += 1
            continue
        if not (isinstance(reason, str) and reason.strip()):
            why["缺评分理由"] += 1
            continue
        accepted[key] = value
        why["落地"] += 1
    return accepted, why


def main(session_id=DEFAULT_SESSION):
    state = load(session_id)
    line_counts, counts_from = fill_line_counts(state)
    raw = state.get("indicator_analysis") or ""
    payload = _extract_payload(raw)
    candidates = (payload or {}).get("scores") or {}
    leaves = list(indicator_leaves(state.get("indicator_system", {}).get("level1_indicators", [])))

    print(f"备份：{session_id}")
    print(f"原文行数（{counts_from}）：{line_counts}")
    print(f"叶子指标 {len(leaves)} 个 | 模型给出 {len(candidates)} 条 | "
          f"备份里已落地 {len(state.get('indicator_score_map') or {})} 条")
    print()

    accepted_old, why_old = old_rule(state, candidates)
    print("=== 改前（精确匹配 evidence_ids） ===")
    print(f"落地 {len(accepted_old)}/{len(leaves)} 项，合计 {round(sum(accepted_old.values()), 2)} 分")
    for reason, count in why_old.most_common():
        print(f"   {count:>3}  {reason}")
    print()

    # 重放：清空 score_map，让模型输出重新走一遍当前实现
    replay = dict(state)
    replay["indicator_score_map"] = {}
    replay.pop("score_disposition", None)
    agent = performance_agent.PerformanceAgent.__new__(performance_agent.PerformanceAgent)
    agent.session_id = session_id
    agent.model_name = "replay"
    agent.base_agent = SimpleNamespace(llm=None)
    agent._execute_step = lambda st, step: raw
    agent._invoke_llm = lambda *a, **k: raw
    agent._node_step7(replay)

    dispo = replay.get("score_disposition") or {}
    counts = dispo.get("counts") or {}
    print("=== 改后（锚点解析 + 处置台账） ===")
    print(dispo.get("summary_line"))
    kinds = Counter(item.get("anchor_kind") or "-" for item in (dispo.get("items") or {}).values())
    labels = {"evidence_id": "检索证据编号", "material_id": "材料内编号", "heading": "原文小节名", "-": "（未落地）"}
    for kind, count in kinds.most_common():
        print(f"   {count:>3}  {labels.get(kind, kind)}")
    print()
    print(f"落地 {counts.get('accepted', 0)}/{dispo.get('total', 0)} 项，"
          f"合计 {dispo.get('accepted_score', 0)} 分")
    print()
    print("=== 与备份原始结论对照 ===")
    original = state.get("score_summary") or {}
    print(f"备份 score_summary：得分 {original.get('total_score')} / "
          f"{original.get('scored_count')} 项 / 等级 {original.get('grade')}")
    print(f"重放后：得分 {dispo.get('accepted_score')} / {counts.get('accepted', 0)} 项")


def _extract_payload(text):
    """从模型原始输出里把 JSON 对象抠出来（正文中间，不在开头）。"""
    pos = text.find("{")
    while pos >= 0:
        depth = 0
        in_str = esc = False
        for i in range(pos, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[pos:i + 1])
                    except json.JSONDecodeError:
                        break
        pos = text.find("{", pos + 1)
    return None


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SESSION)
