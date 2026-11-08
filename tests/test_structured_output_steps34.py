"""批次 17 / A2：缩水闸门扩到步骤 3 / 4（Batch 17）。

改前实测（26 份真实备份）：
  * 步骤 3 的输出是三级指标树，`coverage_of_payload` 只认 `scores` → **每份都数出 0 条**；
    `3c6070b9` 还因为顶层混着 `warning/legal_basis/total_score` 被当成扁平分值映射，数成 5 条；
  * 步骤 4 的 `scores` 能数（26–42 个键），但 `shrink_report` 里一句
    `if step != 7: return "只对步骤 7 校验缩水"` 让判据根本没跑；
  * 两份步骤的正文长度：步骤 3 7.9k–15.4k 字、步骤 4 最长 5.8k 字，
    而修复回喂上限只有 6,000 字 —— 步骤 3 必然「只看得到半截去写」，
    与 B14 给步骤 7 放大上限是同一个机械原因。

所以本批要的不是「再加一条检查」，而是让闸门在这两步**真的有基准可比**：
步骤 3 按叶子指标计数（叶子 = 没有 children 的节点），步骤 4 沿用 scores 口径。
"""
import json
from types import SimpleNamespace

import pytest

from backend.agents import performance_agent
from backend.agents.tools import structured_output as so


# ═══════════════════════════════════════════════════════
#  构造器：与真实输出同形（步骤 3 是树，步骤 4 是扁平映射）
# ═══════════════════════════════════════════════════════

def _leaf(n, score=5.0):
    return {
        "id": f"L3_01_01_{n:02d}", "level": 3, "name": f"指标{n}", "parent_id": "L2_01_01",
        "score": score, "explanation": "解释", "scoring_standard": "标准", "children": [],
    }


def _tree(leaf_count, score=5.0):
    leaves = [_leaf(i, score) for i in range(1, leaf_count + 1)]
    return {
        "indicator_type": "项目支出指标体系",
        "level1_indicators": [{
            "id": "L1_01", "level": 1, "name": "决策", "order": 1, "score": 20,
            "children": [{
                "id": "L2_01_01", "level": 2, "name": "项目立项", "parent_id": "L1_01",
                "score": 20, "children": leaves,
            }],
        }],
    }


def _fence(payload):
    return "指标体系如下。\n\n```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```\n"


def _prose_with_ids(n, json_blob=None):
    body = "\n".join(f"**L3_01_01_{i:02d}**：该指标 5 分。" for i in range(1, n + 1))
    if json_blob is not None:
        return body + "\n\n```json\n" + json_blob + "\n```\n"
    return body


# ═══════════════════════════════════════════════════════
#  一、步骤 3 的覆盖度：数叶子，不数顶层键
# ═══════════════════════════════════════════════════════

def test_step3_coverage_counts_leaves():
    cov = so.coverage_of_payload(_tree(5), step=3)
    assert (cov.entries, cov.numeric) == (5, 5)
    assert cov.shape == "tree", "步骤 3 必须走树形口径，不能落到 scores 分支"


def test_step3_extra_top_level_keys_are_not_mistaken_for_scores():
    """`3c6070b9` 的 payload 顶层混着 warning/legal_basis/total_score，
    旧实现把它当成扁平分值映射，数出 5 条（而真实叶子是 24 条）。"""
    payload = dict(_tree(24))
    payload.update({"warning": "资料有限", "legal_basis": "财预〔2020〕10号", "total_score": 100})
    cov = so.coverage_of_payload(payload, step=3)
    assert cov.entries == 24
    assert cov.shape == "tree"


def test_step3_leaf_without_children_counts_even_at_odd_depth():
    """叶子按「没有 children」判定，不按 `level == 3`——模型偶尔把层级标错。"""
    payload = {
        "level1_indicators": [{
            "id": "L1_01", "level": 1, "name": "决策", "score": 10,
            "children": [{"id": "L3_01_01_01", "level": 2, "name": "错标的叶子", "score": 10,
                          "children": []}],
        }],
    }
    cov = so.coverage_of_payload(payload, step=3)
    assert cov.entries == 1


def test_step3_without_tree_is_empty_coverage():
    assert so.coverage_of_payload({"scores": {"L1_01": 20}}, step=3).entries == 0


# ═══════════════════════════════════════════════════════
#  二、步骤 3 的缩水判定
# ═══════════════════════════════════════════════════════

def _outcome(step, payload):
    return so.parse(step, _fence(payload))


def test_step3_fewer_leaves_is_shrunk():
    before = _prose_with_ids(6, json.dumps({"level1_indicators": "不是数组"}))
    report = so.shrink_report(3, before, _outcome(3, _tree(3)))
    assert report.shrunk
    assert "6→3" in report.reason
    assert report.unverifiable is False


def test_step3_stripped_scores_is_shrunk():
    """条目一条没少，分值被抹掉也算缩水——B14 在步骤 7 踩过同一个坑。

    这里刻意让「首次输出」的 JSON 仍然可读、只是结构不合规（一级指标缺 id）：
    只有 JSON 可读，才存在「数值基准」可比；否则闸门连比的对象都没有，
    只能比条目数（见 `test_step3_without_any_baseline_is_unverifiable`）。
    """
    broken = _tree(6)
    broken["level1_indicators"][0].pop("id")
    first = so.parse(3, _fence(broken))
    assert first.status == "manual_review", "首次输出必须真的不合规，否则不会触发修复"

    stripped = _tree(6)
    for node in stripped["level1_indicators"][0]["children"]:
        for leaf in node["children"]:
            leaf.pop("score")
    report = so.shrink_report(3, _fence(broken), _outcome(3, stripped))
    assert report.before.numeric == 6
    assert report.shrunk
    assert "6→0" in report.reason


def test_step3_full_repair_passes():
    before = _prose_with_ids(4, json.dumps({"level1_indicators": "不是数组"}))
    report = so.shrink_report(3, before, _outcome(3, _tree(4)))
    assert report.shrunk is False
    assert report.unverifiable is False


def test_step3_without_any_baseline_is_unverifiable():
    report = so.shrink_report(3, "只有结论，没有编号也没有 JSON。", _outcome(3, _tree(2)))
    assert report.shrunk is False
    assert report.unverifiable is True, "判不了必须明说，不能静默放行"


# ═══════════════════════════════════════════════════════
#  三、步骤 4 的缩水判定
# ═══════════════════════════════════════════════════════

def _scores_payload(n, value=5):
    return {"scores": {f"L3_01_01_{i:02d}": value for i in range(1, n + 1)}}


def test_step4_coverage_counts_keys_and_numbers():
    cov = so.coverage_of_payload(_scores_payload(8), step=4)
    assert (cov.entries, cov.numeric) == (8, 8)
    assert cov.shape == "scores"


def test_step4_fewer_keys_is_shrunk():
    before = _prose_with_ids(8, json.dumps({"scores": "不是对象"}))
    report = so.shrink_report(4, before, _outcome(4, _scores_payload(3)))
    assert report.shrunk
    assert "8→3" in report.reason


def test_step4_numeric_values_cleared_is_shrunk():
    """键数没变、分值全空，旧实现会当成没缩水（B14 在步骤 7 的同一盲区）。

    首次输出用**数组形态**（步骤 4 的阻塞性错误，实测模型会这么写），
    这样 JSON 可读、数值基准存在，才能比「可计分数值分」。
    """
    first = {"scores": [{"id": f"L3_01_01_{i:02d}", "score": 5} for i in range(1, 9)]}
    assert so.parse(4, _fence(first)).status == "manual_review", "数组形态对步骤 4 是阻断性错误"
    cleared = {"scores": {f"L3_01_01_{i:02d}": None for i in range(1, 9)}}
    report = so.shrink_report(4, _fence(first), so.parse(4, _fence(cleared)))
    assert report.before.numeric == 8
    assert report.shrunk, "键数没变、分值全空，必须判缩水"


def test_step4_numeric_baseline_absent_does_not_cry_wolf():
    """没法比数值时不许瞎报：原文只有编号、没有数值基准 → 只比条目数。"""
    before = _prose_with_ids(3, json.dumps({"scores": "不是对象"}))
    report = so.shrink_report(4, before, _outcome(4, _scores_payload(3)))
    assert report.before.numeric == 0
    assert report.shrunk is False


def test_step4_full_repair_passes():
    before = _prose_with_ids(5, json.dumps({"scores": "不是对象"}))
    report = so.shrink_report(4, before, _outcome(4, _scores_payload(5)))
    assert report.shrunk is False


# ═══════════════════════════════════════════════════════
#  四、边界：不适用的步骤要明说，不能假装「没问题」
# ═══════════════════════════════════════════════════════

def test_other_steps_are_explicitly_not_checked():
    report = so.shrink_report(9, "很长的问题分析", so.parse(9, "短结论"))
    assert report.shrunk is False
    assert report.unverifiable is False
    assert "不做缩水校验" in report.reason


def test_step7_semantics_unchanged():
    """步骤 7 的行为不能被这次扩展改掉。"""
    ids = [f"L3_01_01_{i:02d}" for i in range(1, 7)]
    payload = {"scores": {i: {"actual_score": 2, "evidence_id": "P1-10-1", "reason": "依据"}
                          for i in ids}}
    before = _prose_with_ids(6, json.dumps({"scores": [{"id": i} for i in ids]}))
    assert so.shrink_report(7, before, _outcome(7, payload)).shrunk is False
    partial = {"scores": {i: {"actual_score": 2, "evidence_id": "P1-10-1", "reason": "依据"}
                          for i in ids[:2]}}
    assert so.shrink_report(7, before, _outcome(7, partial)).shrunk is True


# ═══════════════════════════════════════════════════════
#  五、节点级联动：缩水的修复必须退回人工复核
# ═══════════════════════════════════════════════════════

def _agent(monkeypatch, outputs):
    agent = performance_agent.PerformanceAgent.__new__(performance_agent.PerformanceAgent)
    agent.session_id = "shrink34-fixture"
    agent.model_name = "fixture"
    agent.base_agent = SimpleNamespace(llm=None)
    queue = list(outputs)
    calls = []

    monkeypatch.setattr(agent, "_execute_step", lambda state, step: queue.pop(0))

    def fake_invoke(messages, step_number, state, timeout=300, stream=None):
        calls.append({"messages": messages, "stream": stream})
        return queue.pop(0)

    monkeypatch.setattr(agent, "_invoke_llm", fake_invoke)
    return agent, calls


def _state(**extra):
    base = {"session_id": "shrink34-fixture", "completed_steps": [], "step_results": [],
            "warnings": []}
    base.update(extra)
    return base


def test_step3_shrunk_repair_falls_back_to_manual_review(monkeypatch):
    original = _prose_with_ids(6, json.dumps({"level1_indicators": "不是数组"}))
    agent, calls = _agent(monkeypatch, [original, _fence(_tree(2))])
    state = _state()

    agent._node_step3(state)

    assert len(calls) == 1, "只应修复一次"
    result = state["step_results"][3]
    assert result["structured_status"] == "manual_review", "缩水的修复不能算通过"
    assert original in result["content"], "必须保留首次完整输出"
    assert "indicator_system" not in state
    assert any("缩水" in w for w in state["warnings"]), state["warnings"]


def test_step4_shrunk_repair_keeps_previous_scores(monkeypatch):
    original = _prose_with_ids(8, json.dumps({"scores": "不是对象"}))
    agent, calls = _agent(monkeypatch, [original, _fence(_scores_payload(3))])
    state = _state(indicator_system=_tree(8), indicator_scores={"L1_01": 20.0})

    agent._node_step4(state)

    assert len(calls) == 1
    assert state["indicator_scores"] == {"L1_01": 20.0}, "缩水的修复不能覆盖上一版分值"
    assert state["step_results"][4]["structured_status"] == "manual_review"


def test_step3_full_repair_is_still_accepted(monkeypatch):
    """不能把好修复也拦掉：叶子数与原文一致就该采用。"""
    original = _prose_with_ids(4, json.dumps({"level1_indicators": "不是数组"}))
    agent, calls = _agent(monkeypatch, [original, _fence(_tree(4))])
    state = _state()

    agent._node_step3(state)

    assert state["step_results"][3]["structured_status"] == "repaired"
    assert state["indicator_system"]["level1_indicators"][0]["children"][0]["children"], "叶子要在"
