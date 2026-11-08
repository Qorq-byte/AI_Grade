"""结构化输出：形状校验 + 一次修复重试（步骤 3/4/7）。

这些用例只用桩替换 LLM 调用，验证的是「判定与降级路径」，
不验证模型本身——模型质量属于 eval 范畴，见 tests/rag_eval 等。
"""
import json
from types import SimpleNamespace

import pytest

from backend.agents import performance_agent
from backend.agents.tools import structured_output as so


def _tree(name="决策", identifier="L1_01"):
    return {
        "indicator_type": "项目支出指标体系",
        "level1_indicators": [{
            "id": identifier, "level": 1, "name": name, "order": 1, "score": 100,
            "children": [{
                "id": "L2_01_01", "level": 2, "name": "项目立项", "parent_id": identifier,
                "score": 100, "children": [{
                    "id": "L3_01_01_01", "level": 3, "name": "立项依据充分性",
                    "parent_id": "L2_01_01", "score": 100,
                    "explanation": "依据是否充分", "scoring_standard": "无依据扣分", "children": [],
                }],
            }],
        }],
    }


def _fence(payload) -> str:
    return "以下是指标体系：\n\n```json\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n```\n"


def _tree_with(ids):
    """按给定叶子 id 列表造一棵最少层级的三级指标树（每叶满分 10）。"""
    leaves = [{
        "id": leaf_id, "level": 3, "name": f"指标{index}", "parent_id": "L2_01_01",
        "score": 10, "explanation": "说明", "scoring_standard": "标准", "children": [],
    } for index, leaf_id in enumerate(ids, 1)]
    return {
        "indicator_type": "项目支出指标体系",
        "level1_indicators": [{
            "id": "L1_01", "level": 1, "name": "决策", "order": 1, "score": 100,
            "children": [{
                "id": "L2_01_01", "level": 2, "name": "项目立项", "parent_id": "L1_01",
                "score": 100, "children": leaves,
            }],
        }],
    }


# ═══════════════════════════════════════════════════════
#  形状判定
# ═══════════════════════════════════════════════════════

def test_valid_indicator_system_passes():
    outcome = so.parse(3, _fence(_tree()))
    assert outcome.status == "ok"
    assert outcome.ok and not outcome.errors
    assert outcome.payload["level1_indicators"][0]["name"] == "决策"


def test_truncated_json_is_manual_review():
    raw = _fence(_tree())[:120]  # 模拟被长度上限截断
    outcome = so.parse(3, raw)
    assert outcome.status == "manual_review"
    assert "JSON 解析失败" in outcome.errors[0]
    assert outcome.payload is None


def test_missing_required_field_is_manual_review():
    tree = _tree()
    tree["level1_indicators"][0].pop("name")
    outcome = so.parse(3, _fence(tree))
    assert outcome.status == "manual_review"
    assert "没有任何一级指标" in outcome.errors[0]


def test_wrong_container_type_is_manual_review():
    outcome = so.parse(3, '{"level1_indicators": "决策、过程、产出、效益"}')
    assert outcome.status == "manual_review"
    assert "level1_indicators" in outcome.errors[0]


def test_partial_bad_entries_become_warnings_not_failures():
    tree = _tree()
    tree["level1_indicators"].append({"id": "L1_02", "score": 30})  # 缺 name
    outcome = so.parse(3, _fence(tree))
    assert outcome.status == "ok"
    assert len(outcome.warnings) == 1 and "name" in outcome.warnings[0]


def test_wrapped_payload_is_unwrapped():
    outcome = so.parse(3, _fence({"indicator_system": _tree()}))
    assert outcome.status == "ok"
    assert outcome.payload["level1_indicators"][0]["id"] == "L1_01"


def test_no_json_at_all_is_manual_review():
    outcome = so.parse(3, "抱歉，我无法根据现有资料构建指标体系。")
    assert outcome.status == "manual_review"
    assert "未找到 JSON" in outcome.errors[0]


@pytest.mark.parametrize("value,expected", [
    (20, 20.0), ("20", 20.0), ("20分", 20.0), ("1,200", 1200.0), ("20%", 20.0), (None, None), (True, None), ("高", None),
])
def test_number_coercion(value, expected):
    assert so._coerce_number(value) == expected


def test_score_allocation_accepts_numeric_strings():
    outcome = so.parse(4, '{"scores": {"L1_01": "20分", "L2_01_01": 10}, "allocation_rationale": "按产出权重"}')
    assert outcome.status == "ok"


def test_score_allocation_without_numbers_is_manual_review():
    outcome = so.parse(4, '{"scores": {"L1_01": "较高", "L2_01_01": "中等"}}')
    assert outcome.status == "manual_review"
    assert "没有任何可解析的数值" in outcome.errors[0]


def test_score_allocation_wrong_type_is_manual_review():
    outcome = so.parse(4, '{"scores": ["L1_01", 20]}')
    assert outcome.status == "manual_review"
    assert "类型不是对象" in outcome.errors[0]


def test_score_items_require_evidence_and_reason():
    payload = {"scores": {
        "L3_01_01_01": {"actual_score": 15, "evidence_id": "P1-1-1", "reason": "合同齐全"},
        "L3_01_01_02": {"actual_score": 5, "reason": "缺少依据编号"},
    }}
    outcome = so.parse(7, _fence(payload))
    assert outcome.status == "ok"
    assert len(outcome.warnings) == 1 and "evidence_id" in outcome.warnings[0]


def test_score_items_all_unusable_is_warning_not_failure():
    """条目内容不合规 ≠ 格式错误：下游会丢弃这些条目并按待核实处理，不需要重试。"""
    outcome = so.parse(7, '{"scores": {"L3_01_01_01": "良好", "L3_01_01_02": "一般"}}')
    assert outcome.status == "ok"
    assert not outcome.errors
    assert any("不是对象" in item for item in outcome.warnings)


def test_step7_empty_scores_is_legitimate():
    """实跑发现：没有可核实证据时模型会返回 {"scores": {}}，这是合法结果，不应触发修复。"""
    outcome = so.parse(7, '{"scores": {}}')
    assert outcome.status == "ok"
    assert any("按「待核实」处理" in item for item in outcome.warnings)


def test_step7_wrong_container_is_still_blocking():
    outcome = so.parse(7, '{"scores": ["L3_01_01_01", 18]}')
    assert outcome.status == "manual_review"
    assert "类型不是对象" in outcome.errors[0]


def test_repair_prompt_carries_errors_and_schema():
    messages = so.repair_messages(3, "坏掉的输出", ["缺少 level1_indicators 数组"])
    assert len(messages) == 2
    body = messages[1].content if hasattr(messages[1], "content") else messages[1]["content"]
    assert "缺少 level1_indicators 数组" in body
    assert "level1_indicators" in body and "只输出" in str(messages[0].content if hasattr(messages[0], "content") else messages[0])


# ═══════════════════════════════════════════════════════
#  与步骤节点的联动
# ═══════════════════════════════════════════════════════

def _agent(monkeypatch, outputs):
    """桩化 _execute_step / _invoke_llm：按顺序吐出 outputs 里的文本。"""
    agent = performance_agent.PerformanceAgent.__new__(performance_agent.PerformanceAgent)
    agent.session_id = "structured-fixture"
    agent.model_name = "fixture"
    agent.base_agent = SimpleNamespace(llm=None)
    queue = list(outputs)
    repair_calls = []

    monkeypatch.setattr(agent, "_execute_step", lambda state, step: queue.pop(0))

    def fake_invoke(messages, step_number, state, timeout=300, stream=None):
        repair_calls.append({"messages": messages, "stream": stream})
        return queue.pop(0)

    monkeypatch.setattr(agent, "_invoke_llm", fake_invoke)
    return agent, repair_calls


def _state(**extra):
    base = {"session_id": "structured-fixture", "completed_steps": [], "step_results": {}, "warnings": []}
    base.update(extra)
    return base


def test_step3_repair_then_pass(monkeypatch):
    agent, repairs = _agent(monkeypatch, ["指标如下（没有JSON）", _fence(_tree())])
    state = _state()

    agent._node_step3(state)

    assert len(repairs) == 1, "只在首次不合规时修复一次"
    assert repairs[0]["stream"] is False, "修复补调不应再往逐字流里推第二份输出"
    assert state["indicator_system"]["level1_indicators"][0]["name"] == "决策"
    result = state["step_results"][3]
    assert result["structured_status"] == "repaired"
    assert "待人工复核" not in result["content"]


def test_step3_two_failures_fall_back_to_manual_review(monkeypatch):
    raw = "模型只给了结论，没有 JSON。"
    agent, repairs = _agent(monkeypatch, [raw, "还是不给 JSON"])
    state = _state()

    agent._node_step3(state)

    assert len(repairs) == 1
    assert "indicator_system" not in state
    result = state["step_results"][3]
    assert result["structured_status"] == "manual_review"
    assert raw in result["content"], "原始输出必须保留，不能静默丢弃"
    assert any("步骤 3 待人工复核" in item for item in state["warnings"])
    assert result["structured_errors"]


def test_step4_manual_review_keeps_previous_scores(monkeypatch):
    agent, repairs = _agent(monkeypatch, ["没有 JSON", "还是没有 JSON"])
    previous = _tree()
    state = _state(indicator_system=previous, indicator_scores={"L1_01": 20})

    agent._node_step4(state)

    assert len(repairs) == 1
    assert state["indicator_system"] == previous, "修复失败时不能覆盖上一版有效指标体系"
    assert state["indicator_scores"] == {"L1_01": 20}
    assert state["step_results"][4]["structured_status"] == "manual_review"
    assert any("步骤 4" in item for item in state["warnings"])


def test_step4_applies_valid_scores(monkeypatch):
    agent, repairs = _agent(monkeypatch, ['{"scores": {"L1_01": 100, "L2_01_01": 100, "L3_01_01_01": 100}}'])
    state = _state(indicator_system=_tree())

    agent._node_step4(state)

    assert repairs == []
    assert state["indicator_scores"] == {"L1_01": 100.0, "L2_01_01": 100.0, "L3_01_01_01": 100.0}


def test_step7_drops_unverifiable_items(monkeypatch):
    payload = {"scores": {
        "L3_01_01_01": {"actual_score": 18, "evidence_id": "P1-1-1", "reason": "合同与验收报告齐全"},
        "L3_01_01_02": {"actual_score": 10, "evidence_id": "P9-9-9", "reason": "证据不在本步集合内"},
    }}
    agent, repairs = _agent(monkeypatch, [_fence(payload)])
    state = _state(indicator_system=_tree(), step_evidence={"7": [{"id": "P1-1-1", "kind": "project"}]})

    agent._node_step7(state)

    assert repairs == []
    score_map = state["indicator_score_map"]
    assert set(score_map) == {"L3_01_01_01"}
    assert score_map["L3_01_01_01"]["actual_score"] == 18
    assert state["step_results"][7]["structured_status"] == "ok"


# ═══════════════════════════════════════════════════════
#  修复不得缩水（Batch 13）
#  实测 b425eb90：步骤7 首次输出 4,458 tokens（约 6,000+ 字，覆盖 20 个叶子），
#  「scores 类型不是对象」触发一次修复，修复只用了 463 tokens，
#  正文从 6,000+ 字变成 1,059 字，评分条目从 20 条变成 5 条——**且原始正文被丢弃**。
#  同提示词的另一轮（dc0c6294）没触发修复，8,824 字、23 条全在。
#  所以「模型只给几条」有一半是修复逻辑自己造成的。
# ═══════════════════════════════════════════════════════

def _ids(n):
    return [f"L3_01_01_{i:02d}" for i in range(1, n + 1)]


def _scores_payload(ids, score=2.0):
    return {"scores": {i: {"actual_score": score, "evidence_id": "P1-10-1",
                           "reason": "依据说明"} for i in ids}}


def _prose_with_ids(ids, json_blob=None):
    """模拟模型输出：正文里逐个分析每个指标，最后给出 JSON。"""
    body = "\n".join(f"**{i}**：该指标得 2 分，依据是第 10 行。" for i in ids)
    if json_blob:
        return body + "\n\n```json\n" + json_blob + "\n```\n"
    return body


def test_repair_prompt_forbids_dropping_entries():
    """修复提示词不能教模型「宁可少写」——那句话直接把 20 条砍成 5 条。"""
    messages = so.repair_messages(7, "原始输出", ["scores 的类型不是对象"])
    system = messages[0].content if hasattr(messages[0], "content") else messages[0]["content"]
    human = messages[1].content if hasattr(messages[1], "content") else messages[1]["content"]
    both = str(system) + str(human)
    assert "宁可少写" not in both, "这句话会诱导模型删条目"
    assert "逐条保留" in both or "不得删减" in both, "必须明确要求保留原有条目"


def test_mentioned_indicator_ids_counts_what_model_covered():
    """能从原始文本里数出模型覆盖了多少个三级指标（修复前后对比用）。"""
    ids = _ids(6)
    text = _prose_with_ids(ids)
    assert so.mentioned_indicator_ids(text) == set(ids)
    # 重复出现只算一次
    assert so.mentioned_indicator_ids(text + text) == set(ids)


def test_shrunk_repair_is_rejected(monkeypatch):
    """修复后只覆盖 3 个指标、原文覆盖 12 个 → 不采用修复结果。"""
    ids = _ids(12)
    original = _prose_with_ids(ids, json.dumps({"scores": [{"id": i} for i in ids]}))  # scores 类型错
    repaired = _fence(_scores_payload(ids[:3]))
    agent, repairs = _agent(monkeypatch, [original, repaired])
    state = _state(indicator_system=_tree_with(ids), evaluation_table="| 决策 | P1-10-1 |",
                   step_evidence={"7": [{"id": "P1-10-1", "kind": "project", "text": "目标"}]})

    agent._node_step7(state)

    result = state["step_results"][7]
    assert result["structured_status"] == "manual_review", "缩水的修复结果不能算通过"
    assert original in result["content"], "必须保留首次的完整正文，不能被 3 条版本替换"
    assert len(state.get("indicator_score_map") or {}) == 0
    assert any("缩水" in w or "少于" in w for w in state["warnings"]), state["warnings"]


def test_full_repair_is_accepted(monkeypatch):
    """修复后条目数与原文一致 → 正常采用（不能把好修复也拦掉）。"""
    ids = _ids(4)
    original = _prose_with_ids(ids, json.dumps({"scores": [{"id": i} for i in ids]}))
    repaired = _fence(_scores_payload(ids))
    agent, repairs = _agent(monkeypatch, [original, repaired])
    state = _state(indicator_system=_tree_with(ids), evaluation_table="| 决策 | P1-10-1 |",
                   step_evidence={"7": [{"id": "P1-10-1", "kind": "project", "text": "目标"}]})

    agent._node_step7(state)

    assert state["step_results"][7]["structured_status"] == "repaired"
    assert len(state.get("indicator_score_map") or {}) == 4


def test_original_content_is_kept_even_when_repair_accepted(monkeypatch):
    """即使采用修复结果，首次正文也要留下（事后要能查「当时模型到底写了什么」）。"""
    ids = _ids(3)
    original = _prose_with_ids(ids, json.dumps({"scores": [{"id": i} for i in ids]}))
    repaired = _fence(_scores_payload(ids))
    agent, repairs = _agent(monkeypatch, [original, repaired])
    state = _state(indicator_system=_tree_with(ids), evaluation_table="| 决策 | P1-10-1 |",
                   step_evidence={"7": [{"id": "P1-10-1", "kind": "project", "text": "目标"}]})

    agent._node_step7(state)

    result = state["step_results"][7]
    assert original in (result.get("raw_content") or ""), "首次正文必须单独留档"


def test_echo_limit_is_enough_for_step7_long_outputs():
    """步骤7 的正文实测可达 8,000+ 字，回显上限 6,000 会截掉后半截条目。

    截断之后模型「只看得到前 20 条」，再怎么写都不可能覆盖 23 个叶子——
    这是「修复必缩水」的机械原因，不是模型不努力。
    """
    assert so.mention_limit(7) >= 20000, f"步骤7 的回显上限太小：{so.mention_limit(7)}"
    body = "x" * 12000 + "\n【末尾标记】"
    messages = so.repair_messages(7, body, ["scores 的类型不是对象"])
    human = messages[1].content if hasattr(messages[1], "content") else messages[1]["content"]
    assert "【末尾标记】" in human, "12,000 字的正文必须完整回喂"


def test_echo_limit_for_other_steps_stays_small():
    """其它步骤不必放大：修复提示词越长越容易干扰输出，只放大确有需要的步骤。"""
    assert so.mention_limit(3) == so.MAX_ECHO_CHARS
    assert so.mention_limit(4) == so.MAX_ECHO_CHARS


# ═══════════════════════════════════════════════════════
#  闸门不能自带盲区（Batch 14）
#  Batch 13 的缩水判定有两处「失效时不报错」：
#    ① 基准靠正则数「提及的编号」，编号写法一变就恒为空集 → 直接放行；
#    ② 只比条目数，不比数值分 → 20 条全变成「待核实」也算没缩水。
#  实测 effa217d 那轮修复后 11 条里 0 个数值分，正好落在 ② 的盲区里。
# ═══════════════════════════════════════════════════════

def test_baseline_does_not_depend_on_indicator_id_format(monkeypatch):
    """指标体系换一套编号写法，闸门仍要能判缩水。

    旧实现只认 `L\\d_\\d{2}_\\d{2}_\\d{2}`。编号一变，`mentioned_indicator_ids` 恒为空集，
    `repair_shrunk` 直接 `return False`——闸门静默失效，而且**没有任何日志**。
    """
    ids = [f"ZB-{i:02d}" for i in range(1, 13)]
    original = _prose_with_ids(ids, json.dumps({"scores": [{"indicator_id": i} for i in ids]}))
    repaired = _fence(_scores_payload(ids[:3]))
    agent, _ = _agent(monkeypatch, [original, repaired])
    state = _state(indicator_system=_tree_with(ids), evaluation_table="| 决策 | P1-10-1 |",
                   step_evidence={"7": [{"id": "P1-10-1", "kind": "project", "text": "目标"}]})

    agent._node_step7(state)

    result = state["step_results"][7]
    assert result["structured_status"] == "manual_review", "编号风格不该让缩水闸门失效"
    assert original in result["content"], "必须保留首次正文"
    assert any("缩水" in w or "少于" in w for w in state["warnings"]), state["warnings"]


def test_guard_rejects_repair_that_wipes_numeric_scores(monkeypatch):
    """条目数一条没少，但 5 个真实分值全变成「待核实」→ 同样是缩水。

    这种退化只比条目数看不出来：`scores` 仍有 5 个键，键数没变。
    """
    ids = _ids(5)
    original = _prose_with_ids(ids, json.dumps({"scores": [
        {"indicator_id": i, "actual_score": 2.0, "evidence_id": "P1-10-1", "reason": "r"} for i in ids
    ]}))
    wiped = _fence({"scores": {i: {"actual_score": "待核实", "evidence_id": "", "reason": ""}
                               for i in ids}})
    agent, _ = _agent(monkeypatch, [original, wiped])
    state = _state(indicator_system=_tree_with(ids), evaluation_table="| 决策 | P1-10-1 |",
                   step_evidence={"7": [{"id": "P1-10-1", "kind": "project", "text": "目标"}]})

    agent._node_step7(state)

    result = state["step_results"][7]
    assert result["structured_status"] == "manual_review", "分值被清零也是缩水"
    assert len(state.get("indicator_score_map") or {}) == 0
    assert any("数值分" in w or "缩水" in w for w in state["warnings"]), state["warnings"]


def test_guard_accepts_repair_that_keeps_every_entry_and_score(monkeypatch):
    """真实 9141a214 的形态：20 条 / 5 个数值分，修复前后一致 → 必须放行。

    首次把 `scores` 写成数组（校验失败），修复改成对象，条目与分值都没少。
    闸门如果连这种修复也拦，就等于关掉了修复功能。
    """
    ids = _ids(20)
    first = [{"indicator_id": i, "evidence_id": "P1-10-1", "reason": "依据说明"} for i in ids]
    for item in first[:5]:
        item["actual_score"] = 2.0
    for item in first[5:]:
        item["actual_score"] = "待核实"
    original = _prose_with_ids(ids, json.dumps({"scores": first}))
    repaired = _fence({"scores": {
        i: {"actual_score": (2.0 if n < 5 else "待核实"),
            "evidence_id": "P1-10-1", "reason": "依据说明"}
        for n, i in enumerate(ids)
    }})
    agent, _ = _agent(monkeypatch, [original, repaired])
    state = _state(indicator_system=_tree_with(ids), evaluation_table="| 决策 | P1-10-1 |",
                   step_evidence={"7": [{"id": "P1-10-1", "kind": "project", "text": "目标"}]})

    agent._node_step7(state)

    assert state["step_results"][7]["structured_status"] == "repaired", "正常修复不能被误伤"
    assert len(state.get("indicator_score_map") or {}) == 5


def test_unverifiable_baseline_is_reported_not_silent(monkeypatch):
    """原文既没有 JSON 也没有可识别的编号 → 判不了缩水，但必须**说出来**。

    静默放行是这一批要消灭的写法：判不了不等于没问题。
    """
    original = "模型只写了结论，没有任何编号。"
    repaired = _fence(_scores_payload(_ids(2)))
    agent, _ = _agent(monkeypatch, [original, repaired])
    state = _state(indicator_system=_tree_with(_ids(2)), evaluation_table="| 决策 | P1-10-1 |",
                   step_evidence={"7": [{"id": "P1-10-1", "kind": "project", "text": "目标"}]})

    agent._node_step7(state)

    assert state["step_results"][7]["structured_status"] == "repaired", "判不了就不要拒"
    assert any("无法核验" in w for w in state["warnings"]), state["warnings"]


def test_coverage_counts_json_entries_not_prose_mentions():
    """同一把尺子：正文提到 4 个指标、JSON 里只有 2 条，基准应当算 2 条。

    实测 21 份备份里「提及数」恒等于「条目数」，但那是模型老实；
    只要有一轮在正文里列了体系外编号，旧基准就会把好修复判成缩水。
    """
    ids = _ids(4)
    text = _prose_with_ids(ids, json.dumps(_scores_payload(ids[:2])))
    coverage = so.coverage_of_text(text)

    assert coverage.source == "json", "能解析出 JSON 就该用 JSON 作基准"
    assert coverage.entries == 2, f"应以上报的条目为准，实得 {coverage.entries}"
    assert coverage.numeric == 2


def test_coverage_reads_dict_list_and_flat_shapes():
    """三种真实出现过的形态都要认，不能只认一种。"""
    ids = _ids(3)

    as_dict = so.coverage_of_text(json.dumps(_scores_payload(ids)))
    as_list = so.coverage_of_text(json.dumps({"scores": [
        {"indicator_id": i, "actual_score": 1.0} for i in ids]}))
    flat = so.coverage_of_text(json.dumps({i: 2.0 for i in ids}))

    for name, coverage in (("dict", as_dict), ("list", as_list), ("flat", flat)):
        assert coverage.entries == 3, f"{name} 形态应数出 3 条，实得 {coverage.entries}"
        assert coverage.numeric == 3, f"{name} 形态应数出 3 个数值分，实得 {coverage.numeric}"


def test_repair_shrunk_keeps_working_for_other_steps():
    """旧接口保持语义：非步骤 7 一律不判缩水（结构不同，无法无损对比）。"""
    assert so.repair_shrunk(3, json.dumps({"level1_indicators": []}), so.parse(3, "{}")) is False
    assert so.repair_shrunk(4, json.dumps({"scores": {"L1_01": 1}}), so.parse(4, "{}")) is False
