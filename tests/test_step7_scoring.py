"""步骤 7 评分落地与处置台账（Batch 11）。

改前实测（真实 14 步运行，23 个叶子）：只有 2 条进了 `indicator_score_map`。
逐条核对后，23 条的实际分布是：

  * 16 条 `actual_score` 写「待核实」——模型按提示词要求作出的**合法拒绝**；
  * 2 条编号被自由文本包裹（`P1-10-1、P1-12-1、资料「三、绩效目标」`）——被精确匹配丢掉；
  * 3 条引用原文小节名（`资料「四、资金安排」`）——同上被丢掉；
  * 2 条精确命中——正常落地。

所以本批要同时保证两件事：真实评分不再被误杀，且**不把闸门整个放开**
（编造编号、虚构章节、空引用仍然必须拒）。用例按这个口径写。
"""
import json
from types import SimpleNamespace

from backend.agents import performance_agent

MATERIAL_TABLE = "| 一级指标 | 分值 | 证据 |\n| 决策 | 10 | P1-14-1 |\n"

CATEGORIZED = {
    "项目概况": "一、项目概况\n本项目为城市主干道改造工程，总投资5000万元。\n"
                "二、建设内容\n改造主路3.2公里。\n"
                "三、绩效目标\n资金到位率100%。\n"
                "四、资金安排\n财政拨款4000万元，专项债1000万元。\n",
}


def _tree(leaf_ids, full=10.0):
    leaves = [{
        "id": leaf_id, "level": 3, "name": f"指标{index}", "parent_id": "L2_01_01",
        "score": full, "explanation": "说明", "scoring_standard": "标准", "children": [],
    } for index, leaf_id in enumerate(leaf_ids, 1)]
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


def _agent(monkeypatch, payload):
    agent = performance_agent.PerformanceAgent.__new__(performance_agent.PerformanceAgent)
    agent.session_id = "step7-fixture"
    agent.model_name = "fixture"
    agent.base_agent = SimpleNamespace(llm=None)
    text = "评分分析正文。\n\n```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```\n"
    monkeypatch.setattr(agent, "_execute_step", lambda state, step: text)
    monkeypatch.setattr(agent, "_invoke_llm", lambda *a, **k: text)
    return agent


def _state(leaf_ids, **extra):
    base = {
        "session_id": "step7-fixture",
        "completed_steps": [],
        "step_results": {},
        "warnings": [],
        "indicator_system": _tree(leaf_ids),
        "evaluation_table": MATERIAL_TABLE,
        "effective_data_categorized": dict(CATEGORIZED),
        "step_evidence": {"7": [{"id": "P1-10-1", "kind": "project", "text": "目标1"},
                                {"id": "P1-12-1", "kind": "project", "text": "目标3"},
                                {"id": "P1-15-1", "kind": "project", "text": "五、存在问题"}]},
    }
    base.update(extra)
    return base


# ═══════════════════════════════════════════════════════
#  真实被误杀的三种引用形态，现在必须能落地
# ═══════════════════════════════════════════════════════

def test_heading_citation_now_lands(monkeypatch):
    """`资料「四、资金安排」` 引用的是下发原文的小节名，不是检索块编号。"""
    payload = {"scores": {"L3_01_01_01": {
        "actual_score": 2.0, "evidence_id": "资料「四、资金安排」",
        "reason": "资金来源合计与总投资一致，但专项债用途合规性未提供，扣2分"}}}
    agent = _agent(monkeypatch, payload)
    state = _state(["L3_01_01_01"])

    agent._node_step7(state)

    entry = state["indicator_score_map"]["L3_01_01_01"]
    assert entry["actual_score"] == 2.0
    assert entry["evidence_anchor"]["kind"] == "heading"
    assert entry["evidence_id"] == "资料「四、资金安排」", "原始引用必须保留，不能改写成别的编号"
    assert state["score_disposition"]["counts"]["accepted"] == 1


def test_id_inside_free_text_now_lands(monkeypatch):
    payload = {"scores": {"L3_01_01_01": {
        "actual_score": 4.0, "evidence_id": "P1-10-1、P1-12-1、资料「三、绩效目标」",
        "reason": "三项绩效目标均量化可考核"}}}
    agent = _agent(monkeypatch, payload)
    state = _state(["L3_01_01_01"])

    agent._node_step7(state)

    assert state["indicator_score_map"]["L3_01_01_01"]["evidence_anchor"]["kind"] == "evidence_id"


def test_id_exposed_in_evaluation_table_now_lands(monkeypatch):
    """P1-14-1 在提示词的指标体系表里外露，但不在本步骤检索池。"""
    payload = {"scores": {"L3_01_01_01": {
        "actual_score": 3.0, "evidence_id": "P1-14-1", "reason": "支出进度64%，与文本一致"}}}
    agent = _agent(monkeypatch, payload)
    state = _state(["L3_01_01_01"])

    agent._node_step7(state)

    assert state["indicator_score_map"]["L3_01_01_01"]["evidence_anchor"]["kind"] == "material_id"


# ═══════════════════════════════════════════════════════
#  闸门不能放开：编造的东西仍然必须拒
# ═══════════════════════════════════════════════════════

def test_fabricated_id_still_rejected(monkeypatch):
    payload = {"scores": {"L3_01_01_01": {
        "actual_score": 5.0, "evidence_id": "P9-99-9", "reason": "查无此处"}}}
    agent = _agent(monkeypatch, payload)
    state = _state(["L3_01_01_01"])

    agent._node_step7(state)

    assert state["indicator_score_map"] == {}
    item = state["score_disposition"]["items"]["L3_01_01_01"]
    assert item["status"] == "anchor_unresolved"
    assert item["cited_ids"] == ["P9-99-9"]


def test_unknown_heading_still_rejected(monkeypatch):
    payload = {"scores": {"L3_01_01_01": {
        "actual_score": 5.0, "evidence_id": "资料「八、考核评分」", "reason": "依据该章节"}}}
    agent = _agent(monkeypatch, payload)
    state = _state(["L3_01_01_01"])

    agent._node_step7(state)

    assert state["indicator_score_map"] == {}
    assert state["score_disposition"]["items"]["L3_01_01_01"]["status"] == "anchor_unresolved"


def test_empty_evidence_still_rejected(monkeypatch):
    payload = {"scores": {"L3_01_01_01": {
        "actual_score": 5.0, "evidence_id": "", "reason": "无出处"}}}
    agent = _agent(monkeypatch, payload)
    state = _state(["L3_01_01_01"])

    agent._node_step7(state)

    assert state["indicator_score_map"] == {}
    assert state["score_disposition"]["items"]["L3_01_01_01"]["status"] == "anchor_unresolved"


# ═══════════════════════════════════════════════════════
#  处置台账：六种原因必须分得开
# ═══════════════════════════════════════════════════════

def test_disposition_separates_pending_from_missing(monkeypatch):
    payload = {"scores": {
        "L3_01_01_01": {"actual_score": "待核实", "evidence_id": "资料「一、项目概况」",
                        "reason": "未提供立项批复"},
    }}
    agent = _agent(monkeypatch, payload)
    state = _state(["L3_01_01_01", "L3_01_01_02"])

    agent._node_step7(state)

    items = state["score_disposition"]["items"]
    assert items["L3_01_01_01"]["status"] == "model_pending"
    assert items["L3_01_01_02"]["status"] == "not_mentioned", "模型没提 ≠ 模型说资料不足"
    assert state["indicator_score_map"] == {}


def test_null_score_is_treated_as_model_refusal(monkeypatch):
    """实测：模型用 JSON null 表示不打分（一轮 18/25 条），不能算「分值不合法」。"""
    payload = {"scores": {
        "L3_01_01_01": {"actual_score": None, "evidence_id": None,
                        "reason": "待核实：现有资料未提供立项批复文件"},
        "L3_01_01_02": {"actual_score": 4.0, "evidence_id": "P1-10-1", "reason": "有据"},
    }}
    agent = _agent(monkeypatch, payload)
    state = _state(["L3_01_01_01", "L3_01_01_02"])

    agent._node_step7(state)

    counts = state["score_disposition"]["counts"]
    assert counts["model_pending"] == 1
    assert counts["score_invalid"] == 0, "null 是拒绝，不是非法值"
    assert counts["accepted"] == 1
    assert state["score_disposition"]["items"]["L3_01_01_01"]["actual_score"] == "待核实"


def test_missing_actual_score_field_is_invalid(monkeypatch):
    """列了指标却没写 actual_score 字段：结构缺字段，与「显式写 null 拒绝」不同。"""
    payload = {"scores": {"L3_01_01_01": {"evidence_id": "P1-10-1", "reason": "漏了分值"}}}
    agent = _agent(monkeypatch, payload)
    state = _state(["L3_01_01_01"])

    agent._node_step7(state)

    item = state["score_disposition"]["items"]["L3_01_01_01"]
    assert item["status"] == "score_invalid"
    assert item["note"] == "缺少 actual_score 字段"


def test_anchor_falls_back_to_reason_when_evidence_id_is_null(monkeypatch):
    """实测：模型把出处写进 reason，evidence_id 留 null。引用确实存在，不能按字段判死。"""
    payload = {"scores": {"L3_01_01_01": {
        "actual_score": 2.0, "evidence_id": None,
        "reason": "仅有总投资总额，未提供分项预算测算依据，扣2.00分（fe_wf_sample.txt「四、资金安排」）"}}}
    agent = _agent(monkeypatch, payload)
    state = _state(["L3_01_01_01"])

    agent._node_step7(state)

    entry = state["indicator_score_map"]["L3_01_01_01"]
    assert entry["actual_score"] == 2.0
    assert entry["evidence_anchor"]["from"] == "reason"
    assert entry["evidence_anchor"]["kind"] == "heading"


def test_reason_fallback_does_not_accept_invented_citation(monkeypatch):
    """回退到 reason 找锚点，不等于放行：引不到材料里的仍然拒。"""
    payload = {"scores": {"L3_01_01_01": {
        "actual_score": 2.0, "evidence_id": None,
        "reason": "依据《不存在的专项债管理办法》第9条扣2分"}}}
    agent = _agent(monkeypatch, payload)
    state = _state(["L3_01_01_01"])

    agent._node_step7(state)

    assert state["indicator_score_map"] == {}
    assert state["score_disposition"]["items"]["L3_01_01_01"]["status"] == "anchor_unresolved"
    assert state["score_disposition"]["items"]["L3_01_01_01"]["anchor_from"] == "reason"


def test_non_object_entry_is_not_labelled_as_missing(monkeypatch):
    """模型提到了这个指标但给的是字符串：不能说成「模型未提及」。"""
    payload = {"scores": {"L3_01_01_01": "良好"}}
    agent = _agent(monkeypatch, payload)
    state = _state(["L3_01_01_01", "L3_01_01_02"])

    agent._node_step7(state)

    items = state["score_disposition"]["items"]
    assert items["L3_01_01_01"]["status"] == "score_invalid"
    assert items["L3_01_01_01"]["note"] == "条目不是对象"
    assert items["L3_01_01_02"]["status"] == "not_mentioned"


def test_unknown_indicator_ids_are_recorded_not_swallowed(monkeypatch):
    """真实一轮里 6 条评分有 4 条用了体系里不存在的 id（L3_01_01_03 等）。

    这些必须单独计数：否则「模型编了 id」与「模型没提这个指标」在对外口径里一模一样。
    """
    payload = {"scores": {
        "L3_01_01_01": {"actual_score": 3.0, "evidence_id": "P1-10-1", "reason": "有据"},
        "L3_01_01_99": {"actual_score": 5.0, "evidence_id": "P1-10-1", "reason": "编的 id"},
        "L3_09_09_09": {"actual_score": 2.0, "evidence_id": "P1-10-1", "reason": "编的 id"},
    }}
    agent = _agent(monkeypatch, payload)
    state = _state(["L3_01_01_01"])

    agent._node_step7(state)

    unmatched = state["score_disposition"]["unmatched"]
    assert [item["indicator_id"] for item in unmatched] == ["L3_01_01_99", "L3_09_09_09"]
    assert unmatched[0]["actual_score"] == 5.0
    assert "另有 2 条评分对应的指标 ID 不在当前指标体系内" in state["score_disposition"]["summary_line"]
    assert list(state["indicator_score_map"]) == ["L3_01_01_01"], "编的 id 不能进评分表"


def test_disposition_counts_reconcile(monkeypatch):
    payload = {"scores": {
        "L3_01_01_01": {"actual_score": 4.0, "evidence_id": "P1-10-1", "reason": "有据"},
        "L3_01_01_02": {"actual_score": 2.0, "evidence_id": "资料「四、资金安排」", "reason": "有据"},
        "L3_01_01_03": {"actual_score": "待核实", "evidence_id": "资料「一、项目概况」", "reason": "缺资料"},
        "L3_01_01_04": {"actual_score": 3.0, "evidence_id": "P9-99-9", "reason": "编的"},
        "L3_01_01_05": {"actual_score": 99.0, "evidence_id": "P1-10-1", "reason": "超满分"},
        "L3_01_01_06": {"actual_score": 1.0, "evidence_id": "P1-10-1", "reason": "   "},
    }}
    agent = _agent(monkeypatch, payload)
    state = _state([f"L3_01_01_0{i}" for i in range(1, 7)])

    agent._node_step7(state)

    disposition = state["score_disposition"]
    counts = disposition["counts"]
    assert counts["accepted"] == 2
    assert counts["model_pending"] == 1
    assert counts["anchor_unresolved"] == 1
    assert counts["score_invalid"] == 1
    assert counts["reason_missing"] == 1
    assert sum(counts.values()) == disposition["total"] == 6
    assert disposition["accepted_score"] == 6.0
    assert set(state["indicator_score_map"]) == {"L3_01_01_01", "L3_01_01_02"}


def test_disposition_summary_line_is_wired_into_content_and_warnings(monkeypatch):
    payload = {"scores": {"L3_01_01_01": {"actual_score": "待核实",
                                          "evidence_id": "资料「一、项目概况」",
                                          "reason": "未提供立项批复"}}}
    agent = _agent(monkeypatch, payload)
    state = _state(["L3_01_01_01", "L3_01_01_02"])

    agent._node_step7(state)
    line = state["score_disposition"]["summary_line"]

    assert line.startswith("步骤 7 评分落地 0/2 项")
    assert "模型判定资料不足（待核实） 1 项" in line
    assert state["indicator_analysis"].startswith(line)
    assert line in state["warnings"]
    assert "未确认分值列为待核实" not in state["indicator_analysis"], "旧的笼统说法应被分项结论取代"

    agent._node_step7(state)
    assert state["warnings"].count(line) == 1, "重复执行不应重复堆积同一条告警"


def test_all_landed_means_no_prefix_and_no_warning(monkeypatch):
    payload = {"scores": {"L3_01_01_01": {"actual_score": 4.0, "evidence_id": "P1-10-1",
                                          "reason": "有据"}}}
    agent = _agent(monkeypatch, payload)
    state = _state(["L3_01_01_01"])

    agent._node_step7(state)

    assert state["score_disposition"]["counts"]["accepted"] == 1
    assert state["indicator_analysis"].startswith("评分分析正文")
    assert state["warnings"] == []


def test_rerun_is_idempotent_and_keeps_previous_scores(monkeypatch):
    payload = {"scores": {"L3_01_01_01": {"actual_score": 4.0, "evidence_id": "P1-10-1",
                                          "reason": "有据"}}}
    agent = _agent(monkeypatch, payload)
    state = _state(["L3_01_01_01"])

    agent._node_step7(state)
    first = dict(state["indicator_score_map"]["L3_01_01_01"])
    agent._node_step7(state)

    assert state["indicator_score_map"]["L3_01_01_01"]["actual_score"] == first["actual_score"]
    assert state["score_disposition"]["counts"]["accepted"] == 1
    assert state["score_disposition"]["items"]["L3_01_01_01"]["note"] == "沿用已有评分"


# ═══════════════════════════════════════════════════════
#  告警噪音：合法拒绝不能再被说成「格式错误」
# ═══════════════════════════════════════════════════════

def test_pending_refusal_produces_no_format_warning(monkeypatch):
    payload = {"scores": {f"L3_01_01_0{i}": {"actual_score": "待核实",
                                             "evidence_id": "资料「一、项目概况」",
                                             "reason": "资料不足"} for i in range(1, 4)}}
    agent = _agent(monkeypatch, payload)
    state = _state([f"L3_01_01_0{i}" for i in range(1, 4)])

    agent._node_step7(state)

    joined = " ".join(state["warnings"])
    assert "不是数字" not in joined, "「待核实」是提示词要求的合法写法，不该报格式错误"
    assert state["step_results"][7]["structured_status"] == "ok"


# ═══════════════════════════════════════════════════════
#  校验基准必须与提示词同源
# ═══════════════════════════════════════════════════════

def test_step7_material_covers_the_three_prompt_variables(monkeypatch):
    agent = _agent(monkeypatch, {"scores": {}})
    state = _state(["L3_01_01_01"])

    material = agent._step7_material(state)

    assert "P1-14-1" in material, "evaluation_table 里的编号必须在校验基准里"
    assert "L3_01_01_01" in material, "indicator_system JSON 必须在校验基准里"
    assert "四、资金安排" in material, "分类原文的小节标题必须在校验基准里"


def test_step7_without_indicator_system_still_records_nothing(monkeypatch):
    agent = _agent(monkeypatch, {"scores": {}})
    state = _state(["L3_01_01_01"])
    state["indicator_system"] = {}

    agent._node_step7(state)

    assert state["indicator_score_map"] == {}
    assert state["score_disposition"] == {}
    assert state["indicator_analysis"].startswith("资料不足")


# ═══════════════════════════════════════════════════════
#  定位串与序号引用：第 6 轮 e2e 的真实形态
#  提示词里印的是 `[P1-10-1] fe_wf_sample.txt（提取文本第10行）`，
#  模型写成了 `fe_wf_sample.txt-第10段` / `fe_wf_sample.txt-第四部分`。
#  引用是真的，只是换了写法——不该被判成「引用无法定位」。
# ═══════════════════════════════════════════════════════

LOCATED_CHUNKS = [
    # kind 必须是 "project"：_node_step7 只把 project 检索块当作编号/定位串的比对基准
    # locator 用真实形态「提取文本第N行」（workflow_evidence._project_chunks 产出）
    {"id": "P1-10-1", "source": "fe_wf_sample.txt", "locator": "提取文本第10行",
     "kind": "project", "text": "财政拨款4000万元，专项债1000万元。"},
    {"id": "P1-12-1", "source": "fe_wf_sample.txt", "locator": "提取文本第12行",
     "kind": "project", "text": "资金到位率100%。"},
]


def _state_with_locators(leaf_ids, **extra):
    base = _state(leaf_ids)
    base["step_evidence"]["7"] = [dict(item) for item in LOCATED_CHUNKS]
    base.update(extra)
    return base


def test_locator_citation_lands_as_evidence_locator(monkeypatch):
    payload = {"scores": {"L3_01_01_01": {
        "actual_score": 4.0, "evidence_id": "fe_wf_sample.txt-第10段",
        "reason": "资金来源合计与总投资一致"}}}
    agent = _agent(monkeypatch, payload)
    state = _state_with_locators(["L3_01_01_01"])

    agent._node_step7(state)

    entry = state["indicator_score_map"]["L3_01_01_01"]
    assert entry["actual_score"] == 4.0
    assert entry["evidence_anchor"]["kind"] == "evidence_locator"
    assert entry["evidence_anchor"]["matched"] == ["P1-10-1"]
    assert state["score_disposition"]["counts"]["accepted"] == 1
    assert state["score_disposition"]["counts"]["anchor_unresolved"] == 0


def test_ordinal_section_citation_lands_as_section_ordinal(monkeypatch):
    """`fe_wf_sample.txt-第四部分` 要能落到分类原文里的小节「资金安排」。"""
    payload = {"scores": {"L3_01_01_01": {
        "actual_score": 3.0, "evidence_id": "fe_wf_sample.txt-第四部分",
        "reason": "专项债用途合规性未提供"}}}
    agent = _agent(monkeypatch, payload)
    state = _state_with_locators(["L3_01_01_01"])

    agent._node_step7(state)

    entry = state["indicator_score_map"]["L3_01_01_01"]
    assert entry["evidence_anchor"]["kind"] == "section_ordinal"
    assert entry["evidence_anchor"]["matched"] == ["资金安排"]
    assert entry["evidence_id"] == "fe_wf_sample.txt-第四部分", "原始引用必须原样保留"


def test_filename_without_line_number_is_still_rejected(monkeypatch):
    """只给文件名落不到具体位置，不能因为「宽容」就放行。"""
    payload = {"scores": {"L3_01_01_01": {
        "actual_score": 4.0, "evidence_id": "fe_wf_sample.txt",
        "reason": "资料齐全"}}}
    agent = _agent(monkeypatch, payload)
    state = _state_with_locators(["L3_01_01_01"])

    agent._node_step7(state)

    assert state["indicator_score_map"] == {}
    assert state["score_disposition"]["counts"]["anchor_unresolved"] == 1


def test_locator_with_unknown_filename_is_still_rejected(monkeypatch):
    """行号对得上但文件名对不上——这是错引用，不是可定位引用。"""
    payload = {"scores": {"L3_01_01_01": {
        "actual_score": 4.0, "evidence_id": "other_project.txt-第10段",
        "reason": "资料齐全"}}}
    agent = _agent(monkeypatch, payload)
    state = _state_with_locators(["L3_01_01_01"])

    agent._node_step7(state)

    assert state["indicator_score_map"] == {}
    assert state["score_disposition"]["counts"]["anchor_unresolved"] == 1


def test_locator_anchor_is_recorded_in_ledger_detail(monkeypatch):
    """台账明细要能看出「这条是靠定位串落地的」，便于审计。"""
    payload = {"scores": {"L3_01_01_01": {
        "actual_score": 4.0, "evidence_id": "fe_wf_sample.txt-第12段",
        "reason": "到位率与目标一致"}}}
    agent = _agent(monkeypatch, payload)
    state = _state_with_locators(["L3_01_01_01"])

    agent._node_step7(state)

    item = state["score_disposition"]["items"]["L3_01_01_01"]
    assert item["status"] == "accepted"
    assert item["evidence_anchor"]["kind"] == "evidence_locator"
    assert item["evidence_anchor"]["matched"] == ["P1-12-1"]


# ═══════════════════════════════════════════════════════
#  弱定位：引用能落到原文行号，但那一行不在检索证据里
#  实测第 7 轮 e2e：`fe_wf_sample.txt 第14行`，原文 16 行，第 14 行是
#  「截至2025年6月已支出3200万元，支出进度64%」，与该条评分理由完全对得上。
#  ⚠️ 关键约束：这类**不能计入总分**。它只证明「这一行存在」，
#  不证明「这一行支持该评分」；一旦算 accepted，「无法评价、暂按基础分」那几条
#  会借同一个口子把总分抬高 7.5 分。
# ═══════════════════════════════════════════════════════

RAW_TEXT = "\n".join([
    "云海市政道路建设工程项目",
    "一、项目概况",
    "本项目为城市主干道改造工程，总投资5000万元。",
    "二、建设内容",
    "1. 道路工程：改造主路3.2公里；",
    "2. 排水工程：新建雨水管网4.5公里；",
    "3. 照明工程：安装路灯220盏；",
    "4. 绿化工程：种植行道树1800株。",
    "三、绩效目标",
    "目标1：2025年底前完成全部工程并通车；",
    "目标2：资金到位率100%，支出进度不低于90%；",
    "目标3：群众满意度不低于85%；",
    "四、资金安排",
    "财政拨款4000万元，专项债1000万元。截至2025年6月已支出3200万元，支出进度64%。",
    "五、存在问题",
    "部分路段施工进度滞后约1个月。",
])


def _state_with_raw_text(leaf_ids, **extra):
    base = _state(leaf_ids)
    base["all_raw_texts"] = {"u1_be8b81782a0e487da7cc7aa092057f63_fe_wf_sample.txt": RAW_TEXT}
    base.update(extra)
    return base


def test_line_locator_becomes_anchor_weak_not_accepted(monkeypatch):
    payload = {"scores": {"L3_01_01_01": {
        "actual_score": 2.0, "evidence_id": "fe_wf_sample.txt 第14行",
        "reason": "截至2025年6月已支出3200.00万元，支出进度64.00%，距90%目标差26个百分点"}}}
    agent = _agent(monkeypatch, payload)
    state = _state_with_raw_text(["L3_01_01_01"])

    agent._node_step7(state)

    item = state["score_disposition"]["items"]["L3_01_01_01"]
    assert item["status"] == "anchor_weak"
    assert item["evidence_anchor"]["kind"] == "material_locator"
    assert item["evidence_anchor"]["matched"] == ["fe_wf_sample.txt:第14行"]
    assert state["score_disposition"]["counts"]["accepted"] == 0
    assert state["score_disposition"]["counts"]["anchor_weak"] == 1


def test_anchor_weak_does_not_enter_score_map_or_total(monkeypatch):
    """弱定位绝不能进 indicator_score_map——否则 total_score 会被抬高。"""
    payload = {"scores": {"L3_01_01_01": {
        "actual_score": 5.0, "evidence_id": "fe_wf_sample.txt 第14行",
        "reason": "总投资5000万元、已支出3200万元，未突破总投资"}}}
    agent = _agent(monkeypatch, payload)
    state = _state_with_raw_text(["L3_01_01_01"])

    agent._node_step7(state)

    assert state["indicator_score_map"] == {}
    assert state["score_disposition"]["accepted_score"] == 0


def test_anchor_weak_is_counted_and_labelled(monkeypatch):
    """台账要说清这一类是什么，不能和「引用无法定位」混成一句话。"""
    payload = {"scores": {"L3_01_01_01": {
        "actual_score": 2.0, "evidence_id": "fe_wf_sample.txt 第14行", "reason": "有实质理由"}}}
    agent = _agent(monkeypatch, payload)
    state = _state_with_raw_text(["L3_01_01_01"])

    agent._node_step7(state)

    dispo = state["score_disposition"]
    assert "原文行" in dispo["summary_line"], dispo["summary_line"]
    assert sum(dispo["counts"].values()) == dispo["total"] == 1


def test_line_beyond_document_is_not_weak_either(monkeypatch):
    """行号越界既不是弱定位也不是落地，必须仍是 anchor_unresolved。"""
    payload = {"scores": {"L3_01_01_01": {
        "actual_score": 2.0, "evidence_id": "fe_wf_sample.txt 第99行", "reason": "编个行号"}}}
    agent = _agent(monkeypatch, payload)
    state = _state_with_raw_text(["L3_01_01_01"])

    agent._node_step7(state)

    assert state["score_disposition"]["counts"]["anchor_unresolved"] == 1
    assert state["score_disposition"]["counts"]["anchor_weak"] == 0


def test_chunk_hit_still_counts_as_accepted(monkeypatch):
    """同一份状态里，命中检索块的仍按 accepted 落地——弱定位不许把强命中降级。"""
    payload = {"scores": {"L3_01_01_01": {
        "actual_score": 4.0, "evidence_id": "fe_wf_sample.txt-第10段", "reason": "理由充分"}}}
    agent = _agent(monkeypatch, payload)
    state = _state_with_raw_text(["L3_01_01_01"])
    state["step_evidence"]["7"] = [dict(item) for item in LOCATED_CHUNKS]

    agent._node_step7(state)

    assert state["score_disposition"]["counts"]["accepted"] == 1
    assert state["indicator_score_map"]["L3_01_01_01"]["actual_score"] == 4.0


def test_replay_without_raw_texts_still_judges_by_line_counts(monkeypatch):
    """重放场景没有 all_raw_texts，只能靠 raw_line_counts；结论必须一致。"""
    payload = {"scores": {"L3_01_01_01": {
        "actual_score": 2.0, "evidence_id": "fe_wf_sample.txt 第14行", "reason": "有实质理由"}}}
    agent = _agent(monkeypatch, payload)
    state = _state(["L3_01_01_01"])
    state["raw_line_counts"] = {"fe_wf_sample.txt": 16}

    agent._node_step7(state)

    assert state["score_disposition"]["counts"]["anchor_weak"] == 1


def test_no_raw_info_at_all_stays_unresolved(monkeypatch):
    """既无原文也无行数时不许放行——否则等于只看「写了第N行」。"""
    payload = {"scores": {"L3_01_01_01": {
        "actual_score": 2.0, "evidence_id": "fe_wf_sample.txt 第14行", "reason": "有实质理由"}}}
    agent = _agent(monkeypatch, payload)
    state = _state(["L3_01_01_01"])

    agent._node_step7(state)

    assert state["score_disposition"]["counts"]["anchor_unresolved"] == 1


# ═══════════════════════════════════════════════════════
#  标题抽自「原文」，不是「步骤6自己生成的表格」
#  实测：从 _step7_material 抽出的标题里混着 `分值校验`、`资料缺口提示`、
#  `结构化JSON数据供导出WordPDF` —— 那是 evaluation_table 里的 Markdown 标题。
#  模型引用它们等于「引用自己上一步的产物」，不是项目资料证据。
# ═══════════════════════════════════════════════════════

TABLE_WITH_HEADINGS = (
    "## 一、评价指标体系表\n\n"
    "### 二、分值校验\n全部指标分值合计 100 分。\n\n"
    "### 三、资料缺口提示\n部分指标缺少佐证材料。\n\n"
    "| 一级指标 | 分值 | 证据 |\n| 决策 | 10 | P1-14-1 |\n"
)


def test_generated_table_headings_are_not_valid_anchors(monkeypatch):
    """`资料「分值校验」` 是步骤 6 自己写的标题，不能当证据锚点。"""
    payload = {"scores": {"L3_01_01_01": {
        "actual_score": 2.0, "evidence_id": "资料「分值校验」", "reason": "分值合计正确"}}}
    agent = _agent(monkeypatch, payload)
    state = _state(["L3_01_01_01"], evaluation_table=TABLE_WITH_HEADINGS)

    agent._node_step7(state)

    assert state["score_disposition"]["counts"]["accepted"] == 0
    assert state["score_disposition"]["counts"]["anchor_unresolved"] == 1


def test_source_section_ordinal_ignores_generated_table(monkeypatch):
    """`第二部分` 必须落到原文的「建设内容」，不能落到表格里的「分值校验」。"""
    from backend.services import evidence_anchor
    agent = _agent(monkeypatch, {})
    state = _state(["L3_01_01_01"], evaluation_table=TABLE_WITH_HEADINGS)

    ordinals = evidence_anchor.section_ordinals(agent._step7_source_text(state))

    assert ordinals.get(2) == "建设内容", f"实际={ordinals}"


def test_material_id_still_uses_full_material(monkeypatch):
    """编号校验仍对着整份下发材料：evaluation_table 里的编号模型确实看得到。"""
    payload = {"scores": {"L3_01_01_01": {
        "actual_score": 3.0, "evidence_id": "P1-14-1", "reason": "与指标表一致"}}}
    agent = _agent(monkeypatch, payload)
    state = _state(["L3_01_01_01"])

    agent._node_step7(state)

    assert state["score_disposition"]["items"]["L3_01_01_01"]["evidence_anchor"]["kind"] == "material_id"
