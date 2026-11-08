"""批次 19 / A6：让步骤 7 的引用有编号可引，并把「引用太粗」从「引用无法定位」里拆出来。

取证（`python tests/_probe_anchor_gaps.py`，29 份真实备份）：

  * 台账里记成「引用无法定位」的 48 条，用**今天的**解析器重放，12 条已经能解析
    ——那是旧代码的历史快照，不是现状缺陷；
  * 真正还剩 36 条，其中 **24 条是「只写到文件名或段落」**（`fe_wf_sample.txt`、
    `fe_wf_sample.txt-第1段`）。它们落不下去的机制不是模型偷懒：
    材料 16 行，`PROJECT_LIMIT = 3` 只下发 3 个块（实测证据合计 **53 字**），
    而分类原文里**一个块编号都没有** —— 模型根本没有更细的东西可引；
  * 剩下 8 条是编造编号（`P1-5-1` 在任何一个步骤的证据池里都不存在），
    2 条是空引用，2 条行号无法核验（重放缺原文）。

所以本批做两件事：**给模型编号可引**（小材料时下发全部块），
**让台账说清楚是哪一种失败**（只到文件/段 ≠ 引用无法定位 ≠ 没写引用）。
"""
import json
from types import SimpleNamespace

import pytest

from backend.agents import performance_agent
from backend.services import evidence_anchor as ea
from backend.services import workflow_evidence as we

# wf_e2e.py 里上传的那份样例材料（16 行）。备份没存 all_raw_texts，只能这样重建。
SAMPLE = ("云海市政道路建设工程项目\n一、项目概况\n本项目为城市主干道改造工程，总投资5000万元，"
          "建设周期2年（2024年1月至2025年12月），由市住建局牵头实施。\n"
          "二、建设内容\n1. 道路工程：改造主路3.2公里、辅路1.8公里；\n2. 排水工程：新建雨水管网4.5公里；\n"
          "3. 照明工程：安装路灯220盏；\n4. 绿化工程：种植行道树1800株。\n"
          "三、绩效目标\n目标1：2025年底前完成全部工程并通车，通车率达到100%；\n"
          "目标2：项目资金使用规范，资金到位率100%，支出进度不低于90%；\n"
          "目标3：群众满意度不低于85%；\n"
          "四、资金安排\n财政拨款4000万元，专项债1000万元。截至2025年6月已支出3200万元，支出进度64%。\n"
          "五、存在问题\n部分路段施工进度滞后约1个月；个别标段监理记录不完整。")

CATEGORIZED = {"资金管理": SAMPLE}

LOCATED_CHUNKS = [
    {"id": "P1-10-1", "kind": "project", "source": "fe_wf_sample.txt",
     "locator": "提取文本第10行", "text": "目标1：2025年底前完成全部工程并通车"},
    {"id": "P1-12-1", "kind": "project", "source": "fe_wf_sample.txt",
     "locator": "提取文本第12行", "text": "目标3：群众满意度不低于85%"},
    {"id": "P1-15-1", "kind": "project", "source": "fe_wf_sample.txt",
     "locator": "提取文本第15行", "text": "五、存在问题"},
]


# ═══════════════════════════════════════════════════════
#  一、覆盖：小材料必须把块发全，大材料保持 3 条
# ═══════════════════════════════════════════════════════

def _state_for_retrieval(raw_texts):
    return {
        "session_id": "coverage-fixture",
        "all_raw_texts": raw_texts,
        "indicator_type": "项目支出指标体系",
        "project_name": "云海市政道路建设工程项目",
    }


def test_small_material_sends_every_block():
    state = _state_for_retrieval({"fe_wf_sample.txt": SAMPLE})
    total = len(we._project_chunks(state))
    hits = we.retrieve_step_evidence(None, state, 7, False)
    project = [hit for hit in hits if hit["kind"] == "project"]
    assert total > we.PROJECT_LIMIT, "样例材料本身就应多于 3 个块，否则这条用例没有意义"
    assert len(project) == total, f"16 行材料应块块可见，实际只发了 {len(project)}/{total}"
    assert len(project) > we.PROJECT_LIMIT


def test_large_material_still_sends_three():
    """材料大时不能全发：预算之外仍按 3 条，避免每步提示词被撑爆。"""
    big = "\n".join(SAMPLE.splitlines() * 20)
    state = _state_for_retrieval({"big.txt": big})
    hits = we.retrieve_step_evidence(None, state, 7, False)
    project = [hit for hit in hits if hit["kind"] == "project"]
    assert len([h for h in we._project_chunks(state)]) > we.MAX_ADAPTIVE_PROJECT_BLOCKS
    assert len(project) == we.PROJECT_LIMIT


def test_other_steps_keep_three():
    """只放开步骤 7：这一步有锚点校验，别的步骤没有实测证据支持改动。"""
    state = _state_for_retrieval({"fe_wf_sample.txt": SAMPLE})
    hits = we.retrieve_step_evidence(None, state, 6, False)
    project = [hit for hit in hits if hit["kind"] == "project"]
    assert len(project) == we.PROJECT_LIMIT


def test_explicit_limit_is_not_overridden():
    state = _state_for_retrieval({"fe_wf_sample.txt": SAMPLE})
    hits = we.retrieve_step_evidence(None, state, 7, False, project_limit=1)
    assert len([hit for hit in hits if hit["kind"] == "project"]) == 1


def test_adaptive_block_count_is_capped():
    """块多但每块很短时，别把两千个块全塞进提示词。"""
    many = "\n".join(f"第{i}项" for i in range(1, 400))
    state = _state_for_retrieval({"many.txt": many})
    hits = we.retrieve_step_evidence(None, state, 7, False)
    project = [hit for hit in hits if hit["kind"] == "project"]
    assert len(project) <= we.MAX_ADAPTIVE_PROJECT_BLOCKS


# ═══════════════════════════════════════════════════════
#  二、失败原因：五种情形必须分得开
# ═══════════════════════════════════════════════════════

def _resolve_kwargs():
    return {
        "evidence_ids": {hit["id"] for hit in LOCATED_CHUNKS},
        "material": SAMPLE + json.dumps({"level1_indicators": []}, ensure_ascii=False),
        "headings": ea.material_headings(SAMPLE),
        "chunks": LOCATED_CHUNKS,
        "ordinals": ea.section_ordinals(SAMPLE),
        "raw_texts": {"fe_wf_sample.txt": SAMPLE},
        "line_counts": {"fe_wf_sample.txt": 16},
    }


def test_ok_anchor_has_no_reason():
    hit = ea.resolve("P1-10-1", **_resolve_kwargs())
    assert hit.ok and hit.reason == ""


def test_empty_citation_reason_is_empty():
    assert ea.resolve("   ", **_resolve_kwargs()).reason == "empty"
    assert ea.resolve(None, **_resolve_kwargs()).reason == "empty"


def test_unknown_id_reason_is_unknown_id():
    hit = ea.resolve("P9-99-9", **_resolve_kwargs())
    assert not hit.ok and hit.reason == "unknown_id"


def test_filename_only_reason_is_too_coarse():
    hit = ea.resolve("fe_wf_sample.txt", **_resolve_kwargs())
    assert not hit.ok and hit.reason == "too_coarse"


def test_paragraph_number_is_read_as_line_number():
    """「第N段」在既有口径里等同「第N行」（模型把「提取文本第10行」写成「第10段」）。

    所以它**不是**「粒度粗」：行数可核验时会落成弱定位（material_locator）。
    这条用例守住「别为了分桶好看，把本来能落的引用打到 too_coarse 里」。
    """
    hit = ea.resolve("fe_wf_sample.txt-第1段", **_resolve_kwargs())
    assert hit.ok and hit.kind == "material_locator"


def test_unknown_file_is_not_mistaken_for_too_coarse():
    """`other_project.txt-第10段` 是**错引用**，不是「粒度粗」——两者要分开报。"""
    hit = ea.resolve("other_project.txt-第10段", **_resolve_kwargs())
    assert not hit.ok and hit.reason == "unknown_file"


def test_line_out_of_range_reason():
    hit = ea.resolve("fe_wf_sample.txt 第99行", **_resolve_kwargs())
    assert not hit.ok and hit.reason == "line_out_of_range"


def test_quoted_unknown_section_reason_is_no_match():
    hit = ea.resolve("资料「八、考核评分」", **_resolve_kwargs())
    assert not hit.ok and hit.reason == "no_match"


# ═══════════════════════════════════════════════════════
#  三、台账：新状态必须出现，且仍然拒绝落地
# ═══════════════════════════════════════════════════════

def _tree(leaf_ids):
    leaves = [{
        "id": leaf_id, "level": 3, "name": f"指标{i}", "parent_id": "L2_01_01",
        "score": 10.0, "explanation": "说明", "scoring_standard": "标准", "children": [],
    } for i, leaf_id in enumerate(leaf_ids, 1)]
    return {"indicator_type": "项目支出指标体系", "level1_indicators": [{
        "id": "L1_01", "level": 1, "name": "决策", "order": 1, "score": 100,
        "children": [{"id": "L2_01_01", "level": 2, "name": "项目立项", "parent_id": "L1_01",
                      "score": 100, "children": leaves}]}]}


def _agent(monkeypatch, payload):
    agent = performance_agent.PerformanceAgent.__new__(performance_agent.PerformanceAgent)
    agent.session_id = "anchor-coverage-fixture"
    agent.model_name = "fixture"
    agent.base_agent = SimpleNamespace(llm=None)
    text = "评分分析正文。\n\n```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```\n"
    monkeypatch.setattr(agent, "_execute_step", lambda state, step: text)
    monkeypatch.setattr(agent, "_invoke_llm", lambda *a, **k: text)
    return agent


def _state(leaf_ids):
    return {
        "session_id": "anchor-coverage-fixture",
        "completed_steps": [], "step_results": {}, "warnings": [],
        "indicator_system": _tree(leaf_ids),
        "evaluation_table": "| 一级指标 | 分值 |\n| 决策 | 10 |\n",
        "effective_data_categorized": dict(CATEGORIZED),
        "step_evidence": {"7": [dict(hit) for hit in LOCATED_CHUNKS]},
        "raw_line_counts": {"fe_wf_sample.txt": 16},
    }


def test_empty_citation_keeps_reason_but_is_not_landed(monkeypatch):
    """`evidence_id` 为空时会回退去读 `reason`（B14 的既有设计），所以「没写引用」
    在台账里表现为 reason=`empty`，而不是另一个新状态——这也是本批**没有**加
    `anchor_missing` 的原因：加了也几乎没人会落进去。"""
    payload = {"scores": {"L3_01_01_01": {
        "actual_score": 5.0, "evidence_id": "", "reason": ""}}}
    agent = _agent(monkeypatch, payload)
    state = _state(["L3_01_01_01"])

    agent._node_step7(state)

    assert state["indicator_score_map"] == {}
    item = state["score_disposition"]["items"]["L3_01_01_01"]
    assert item["status"] == "anchor_unresolved"
    assert item["anchor_reason"] == "empty"


def test_filename_only_citation_is_labelled_anchor_too_coarse(monkeypatch):
    payload = {"scores": {"L3_01_01_01": {
        "actual_score": 4.0, "evidence_id": "fe_wf_sample.txt", "reason": "资料齐全"}}}
    agent = _agent(monkeypatch, payload)
    state = _state(["L3_01_01_01"])

    agent._node_step7(state)

    assert state["indicator_score_map"] == {}, "口径分开不等于放行"
    item = state["score_disposition"]["items"]["L3_01_01_01"]
    assert item["status"] == "anchor_too_coarse"
    assert item["evidence"] == "fe_wf_sample.txt"


def test_unknown_id_stays_anchor_unresolved(monkeypatch):
    payload = {"scores": {"L3_01_01_01": {
        "actual_score": 5.0, "evidence_id": "P9-99-9", "reason": "查无此处"}}}
    agent = _agent(monkeypatch, payload)
    state = _state(["L3_01_01_01"])

    agent._node_step7(state)

    assert state["indicator_score_map"] == {}
    assert state["score_disposition"]["items"]["L3_01_01_01"]["status"] == "anchor_unresolved"


def test_new_statuses_are_counted_and_reconciled(monkeypatch):
    payload = {"scores": {
        "L3_01_01_01": {"actual_score": 4.0, "evidence_id": "P1-10-1", "reason": "有据"},
        "L3_01_01_02": {"actual_score": 4.0, "evidence_id": "fe_wf_sample.txt", "reason": "粗引用"},
        "L3_01_01_03": {"actual_score": 4.0, "evidence_id": "other_project.txt", "reason": "引错文件"},
        "L3_01_01_04": {"actual_score": 4.0, "evidence_id": "P9-99-9", "reason": "编的"},
    }}
    agent = _agent(monkeypatch, payload)
    state = _state([f"L3_01_01_0{i}" for i in range(1, 5)])

    agent._node_step7(state)

    counts = state["score_disposition"]["counts"]
    assert counts["accepted"] == 1
    assert counts["anchor_too_coarse"] == 1, "只写文件名要单独计数"
    assert counts["anchor_unresolved"] == 2, "引错文件、编造编号仍算「无法定位」"
    assert sum(counts.values()) == state["score_disposition"]["total"] == 4


def test_reason_is_recorded_for_audit(monkeypatch):
    """同一个状态里混着不同原因，台账必须留得住原因，否则又回到「都是一个数」。"""
    payload = {"scores": {
        "L3_01_01_01": {"actual_score": 4.0, "evidence_id": "P9-99-9", "reason": "编的"},
        "L3_01_01_02": {"actual_score": 4.0, "evidence_id": "other_project.txt 第3行",
                        "reason": "引错文件"},
    }}
    agent = _agent(monkeypatch, payload)
    state = _state(["L3_01_01_01", "L3_01_01_02"])

    agent._node_step7(state)

    items = state["score_disposition"]["items"]
    assert items["L3_01_01_01"]["anchor_reason"] == "unknown_id"
    assert items["L3_01_01_02"]["anchor_reason"] == "unknown_file"


def test_summary_line_discloses_the_new_reasons(monkeypatch):
    payload = {"scores": {
        "L3_01_01_01": {"actual_score": 4.0, "evidence_id": "fe_wf_sample.txt",
                        "reason": "只写了文件名"},
    }}
    agent = _agent(monkeypatch, payload)
    state = _state(["L3_01_01_01"])

    agent._node_step7(state)

    line = state["score_disposition"]["summary_line"]
    assert "只到文件" in line or "只到文件/段落" in line, line
