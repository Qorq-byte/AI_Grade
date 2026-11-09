"""批次 20 / A7：步骤 7 的输出长度纪律（Batch 20）。

改前实测（29 份备份 + `token_usage` 表）：
  * 会话 `72111c2c` 的步骤 7 输出 12,862 字，`completion_tokens` 正好 **8,192**
    （DeepSeek 单次输出硬顶），JSON 在第 3,568 字处断在 `"P1-7-1;P1` 上；
  * 断在 JSON 上的直接原因是**顺序**：正文（约 9,294 字）在前、JSON 在后，
    撞上限时先被砍掉的是评分本身；
  * 配置 `MAX_TOKENS=4000` 从未生效：`langchain-openai 1.2.1` 发的是
    `max_completion_tokens`，DeepSeek 不认这个字段。实测同一句提示词：
    传 `max_completion_tokens=60` → 输出 79 token、`finish_reason=stop`；
    传 `extra_body.max_tokens=60` → 输出 60 token、`finish_reason=length`。

所以本批锁四件事：
  1. 输出契约必须**先 JSON 后正文**——截断只砍正文，评分仍然可解析；
  2. 理由与正文都有字数上限，且写明「长度不够时先省正文、JSON 不可省」；
  3. 步骤 7 的输出预算经 `extra_body.max_tokens` 下发（DeepSeek 只认这个字段）；
  4. 真的被截断时必须记账（`truncated_steps` + warnings + 日志），不许静默。
"""
import json
from types import SimpleNamespace

import pytest

from backend.agents import performance_agent
from backend.agents.tools import structured_output
from backend.agents.prompts.default_prompts import (
    PromptTemplateFactory,
    TemperatureProfile,
)


# ═══════════════════════════════════════════════════════
#  一、系统提示词：顺序与长度纪律
# ═══════════════════════════════════════════════════════

def _step7_system() -> str:
    return str(PromptTemplateFactory.build_system_message(7, TemperatureProfile.NORMAL).content)


def _step7_human() -> str:
    return PromptTemplateFactory.build_raw_human_prompt(7)


def test_step7_system_requires_json_before_prose():
    """先 JSON 后正文：撞上限时被砍的是正文，不是评分。"""
    text = _step7_system()
    assert "先输出评分 JSON，再写正文" in text
    assert "JSON 被截断" in text


def test_step7_system_caps_reason_length():
    """理由限字数，且不许把评分标准原文抄一遍。"""
    text = _step7_system()
    assert "每条不超过 60 字" in text
    assert "不抄评分标准原文" in text


def test_step7_system_caps_prose_length():
    """正文字数上限写在提示词里，否则模型会按「越详细越好」写到 7,000 字。"""
    text = _step7_system()
    assert "不超过 1,200 字" in text
    assert "不要在正文里逐指标重复理由" in text


def test_step7_system_declares_drop_priority():
    """长度不够时的舍弃顺序要明说，否则模型先省 JSON 条目。"""
    text = _step7_system()
    assert "JSON 的条目与 actual_score 绝不可省" in text


def test_step7_system_keeps_scoring_rules_intact():
    """本批只动输出形态，不得顺手削弱批次 16 立的举证责任条款。"""
    text = _step7_system()
    assert "只要资料中存在与该指标相关的事实" in text
    assert "未取得佐证时的处置" in text
    assert "【严禁编造 — 与前文同等优先级】" in text


# ═══════════════════════════════════════════════════════
#  二、Human 提示词：JSON 代码块必须在正文之前
# ═══════════════════════════════════════════════════════

def test_step7_human_puts_json_block_first():
    raw = _step7_human()
    contract = raw[raw.find("【必须遵守的输出契约】"):]
    json_pos = contract.find("```json")
    prose_pos = contract.find("汇总")  # 正文部分要求的第一样东西
    assert json_pos >= 0
    assert prose_pos >= 0
    assert json_pos < prose_pos, "JSON 契约必须排在正文要求之前"


def test_step7_human_no_longer_puts_json_at_the_end():
    """改前这句「正文之后附一个 JSON 对象」是截断丢评分的直接推手。"""
    raw = _step7_human()
    assert "正文之后附一个 JSON 对象" not in raw
    assert "正文之后" not in raw


def test_step7_human_keeps_object_contract():
    """重排顺序不许把批次 16 的契约一起丢掉。"""
    raw = _step7_human()
    assert "以指标 ID 为键的对象" in raw
    assert "不是数组" in raw
    assert "每一个三级指标" in raw


def test_step7_human_renders_as_valid_json_example():
    rendered = _step7_human().format(
        project_name="测试项目",
        evaluation_table_markdown="表格",
        indicator_system_full_json="{}",
        effective_data_categorized="资料",
    )
    assert '{"scores": {"<三级指标ID>": {"actual_score": 3.5' in rendered
    assert "{{" not in rendered


# ═══════════════════════════════════════════════════════
#  三、运行期收尾语：也必须是「先 JSON」
# ═══════════════════════════════════════════════════════

def _capture_human_prompt(monkeypatch, step_number):
    agent = performance_agent.PerformanceAgent.__new__(performance_agent.PerformanceAgent)
    agent.session_id = "prompt-fixture"
    agent.model_name = "fixture"
    agent.temperature_profile = TemperatureProfile.NORMAL
    agent.temperature = 0.7
    agent.top_p = 0.9
    agent.use_rag = False
    agent.base_agent = SimpleNamespace(llm=None)
    agent.prompt_manager = __import__(
        "backend.agents.prompts.prompt_manager", fromlist=["get_prompt_manager"]
    ).get_prompt_manager()
    captured = {}

    def fake_invoke(messages, step, state, **kwargs):
        captured["messages"] = messages
        return "{}"

    monkeypatch.setattr(agent, "_invoke_llm", fake_invoke)
    state = {
        "session_id": "prompt-fixture",
        "project_name": "测试项目",
        "completed_steps": [],
        "step_results": {},
        "warnings": [],
        "raw_text": "资料" * 200,
        "indicator_system": {"level1_indicators": []},
        "step_evidence": {},
    }
    agent._execute_step(state, step_number)
    return captured["messages"][1].content


def test_step7_suffix_declares_json_first(monkeypatch):
    """程序追加的收尾语里也有一句「正文后可附 JSON」，必须一起改掉。"""
    human = _capture_human_prompt(monkeypatch, 7)
    assert "先输出 JSON" in human
    assert "正文后可附 JSON" not in human
    assert "必须是以指标 ID 为键的对象" in human


def test_other_steps_get_no_length_clause(monkeypatch):
    """只动步骤 7：其它步骤不该被塞进评分专用的长度纪律。"""
    human = _capture_human_prompt(monkeypatch, 8)
    assert "先输出 JSON" not in human


# ═══════════════════════════════════════════════════════
#  四、输出预算：只有步骤 7 有，且必须走 extra_body
# ═══════════════════════════════════════════════════════

class _Recorder:
    """记录 bind 参数与每次 invoke 实际带着哪套 kwargs。"""

    def __init__(self, content="输出", finish_reason="stop", chunks=None, _calls=None):
        self.content = content
        self.finish_reason = finish_reason
        self.chunks = chunks
        self.bind_kwargs = None
        self.calls = _calls if _calls is not None else []

    def bind(self, **kwargs):
        clone = _Recorder(self.content, self.finish_reason, self.chunks, self.calls)
        clone.bind_kwargs = kwargs
        return clone

    def invoke(self, messages, config=None):
        self.calls.append(dict(self.bind_kwargs or {}))
        return SimpleNamespace(content=self.content,
                               response_metadata={"finish_reason": self.finish_reason},
                               usage_metadata={"input_tokens": 1, "output_tokens": 1,
                                               "total_tokens": 2})

    def stream(self, messages, config=None):
        for text in (self.chunks or []):
            yield SimpleNamespace(content=text, response_metadata={},
                                  usage_metadata=None)
        yield SimpleNamespace(content="", response_metadata={"finish_reason": self.finish_reason},
                              usage_metadata=None)


def _agent(monkeypatch, llm, model_name="deepseek-chat", session_id=""):
    agent = performance_agent.PerformanceAgent.__new__(performance_agent.PerformanceAgent)
    agent.session_id = session_id
    agent.model_name = model_name
    agent.base_agent = SimpleNamespace(llm=llm)
    monkeypatch.setattr(agent, "_record_token", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_record_usage", lambda *a, **k: None)
    return agent


def test_budget_table_only_covers_step7():
    """给全部步骤硬顶 4000 会截断 9 个步骤（步骤 5 实测 10,352 token），不能顺手改。"""
    assert performance_agent.STEP_OUTPUT_BUDGETS == {7: 6000}
    assert performance_agent.step_output_budget(8) is None


def test_budget_is_above_global_max_tokens(monkeypatch):
    from backend.config import settings
    assert performance_agent.step_output_budget(7) > settings.MAX_TOKENS


def test_bound_llm_sends_extra_body_for_deepseek(monkeypatch):
    """DeepSeek 只认请求体里的 max_tokens；langchain 1.x 发的是 max_completion_tokens。"""
    llm = _Recorder()
    agent = _agent(monkeypatch, llm)
    bound = agent._llm_for_step(7)
    assert bound.bind_kwargs["max_tokens"] == 6000
    assert bound.bind_kwargs["extra_body"] == {"max_tokens": 6000}


def test_bound_llm_leaves_non_deepseek_alone(monkeypatch):
    llm = _Recorder()
    agent = _agent(monkeypatch, llm, model_name="gpt-4o")
    bound = agent._llm_for_step(7)
    assert "extra_body" not in bound.bind_kwargs
    assert bound.bind_kwargs["max_tokens"] == 6000


def test_bound_llm_is_noop_without_budget(monkeypatch):
    llm = _Recorder()
    agent = _agent(monkeypatch, llm)
    assert agent._llm_for_step(8) is llm


def test_invoke_llm_uses_step_budget(monkeypatch):
    """_invoke_llm 必须走带预算的那份 llm，否则前面的 bind 白做。"""
    llm = _Recorder(content="完成")
    agent = _agent(monkeypatch, llm)
    agent._invoke_llm([{"role": "user", "content": "hi"}], 7, {})
    assert llm.calls == [{"max_tokens": 6000, "extra_body": {"max_tokens": 6000}}]


def test_invoke_llm_keeps_plain_llm_for_other_steps(monkeypatch):
    llm = _Recorder(content="完成")
    agent = _agent(monkeypatch, llm)
    agent._invoke_llm([{"role": "user", "content": "hi"}], 8, {})
    assert llm.calls == [{}]


# ═══════════════════════════════════════════════════════
#  五、截断必须记账
# ═══════════════════════════════════════════════════════

def test_truncation_marks_state_and_warns(monkeypatch):
    llm = _Recorder(content="半截输出", finish_reason="length")
    agent = _agent(monkeypatch, llm)
    state = {}
    agent._invoke_llm([{"role": "user", "content": "hi"}], 7, state)
    assert state["truncated_steps"] == [7]
    assert any("截断" in w for w in state["warnings"])


def test_truncation_not_flagged_on_normal_stop(monkeypatch):
    llm = _Recorder(content="完整输出", finish_reason="stop")
    agent = _agent(monkeypatch, llm)
    state = {}
    agent._invoke_llm([{"role": "user", "content": "hi"}], 7, state)
    assert "truncated_steps" not in state or state["truncated_steps"] == []
    assert not any("截断" in w for w in state.get("warnings", []))


def test_truncation_deduped_across_retries(monkeypatch):
    """修复重试也截断时，不能把同一步记两次、更不能刷两条一样的 warning。"""
    llm = _Recorder(content="半截输出", finish_reason="length")
    agent = _agent(monkeypatch, llm)
    state = {}
    agent._invoke_llm([{"role": "user", "content": "hi"}], 7, state)
    agent._invoke_llm([{"role": "user", "content": "hi"}], 7, state)
    assert state["truncated_steps"] == [7]
    assert len([w for w in state["warnings"] if "截断" in w]) == 1


def test_truncation_detected_in_stream(monkeypatch):
    from backend.services import progress_bus
    monkeypatch.setattr(progress_bus, "has_consumer", lambda sid: True)
    monkeypatch.setattr(progress_bus, "push_delta", lambda *a, **k: None)
    llm = _Recorder(content="", finish_reason="length", chunks=["半", "截"])
    agent = _agent(monkeypatch, llm, session_id="stream-fixture")
    state = {}
    out = agent._invoke_llm([{"role": "user", "content": "hi"}], 7, state)
    assert out == "半截"
    assert state["truncated_steps"] == [7]


# ═══════════════════════════════════════════════════════
#  六、长度审计口径
# ═══════════════════════════════════════════════════════

def _payload(scores):
    return {"scores": scores}


def test_audit_marks_json_first():
    text = "```json\n" + json.dumps(_payload({
        "L3_01_01_01": {"actual_score": 3, "evidence_id": "P1-1-1", "reason": "总投资5000万元，按标准得满分"},
    }), ensure_ascii=False) + "\n```\n\n## 汇总\n得分 3 分。\n"
    audit = structured_output.audit_step7_length(text)
    assert audit.json_first is True
    assert audit.json_parsed is True
    assert audit.items == 1
    assert audit.prose_chars > 0


def test_audit_flags_prose_first():
    """72111c2c 的形态：正文 9,294 字在前，JSON 在后 → 撞上限时 JSON 先死。"""
    body = "## 逐指标评分及理由\n" + ("指标说明。" * 200)
    text = body + "\n```json\n" + json.dumps(_payload({
        "L3_01_01_01": {"actual_score": 3, "evidence_id": "P1-1-1", "reason": "依据"},
    }), ensure_ascii=False) + "\n```\n"
    audit = structured_output.audit_step7_length(text)
    assert audit.json_first is False


def test_audit_counts_overlong_reasons():
    long_reason = "未提供佐证：" + "缺资金到位凭证与拨付记录，" * 8
    text = "```json\n" + json.dumps(_payload({
        "L3_01_01_01": {"actual_score": 0, "evidence_id": "P1-1-1", "reason": long_reason},
        "L3_01_01_02": {"actual_score": 2, "evidence_id": "P1-1-1", "reason": "总投资5000万元，满分"},
    }), ensure_ascii=False) + "\n```\n正文。\n"
    audit = structured_output.audit_step7_length(text)
    assert audit.items == 2
    assert audit.reason_over_limit == 1
    assert audit.reason_max_chars == len(long_reason)


def test_audit_reports_unparsable_json():
    """截断到一半的 JSON 也要给出可读的审计结论，而不是抛异常。"""
    text = "正文在前。\n```json\n{\"scores\": {\"L3_01_01_01\": {\"actual_score\": 0, \"evidence_id\": \"P1-7-1;P1"
    audit = structured_output.audit_step7_length(text)
    assert audit.json_parsed is False
    assert audit.items == 0
    assert audit.json_first is False


def test_audit_limits_are_declared_once():
    assert structured_output.STEP7_REASON_CHAR_LIMIT == 60
    assert structured_output.STEP7_PROSE_CHAR_LIMIT == 1200


# ═══════════════════════════════════════════════════════
#  七、核心主张：JSON 在前的输出即使被截断，评分也能落地
# ═══════════════════════════════════════════════════════

def test_truncated_but_json_first_still_lands_scores(monkeypatch):
    scores = {
        "L3_01_01_01": {"actual_score": 4, "evidence_id": "P1-14-1",
                        "reason": "总投资5000万元已列明，按标准得满分"},
        "L3_01_01_02": {"actual_score": 2, "evidence_id": "P1-14-1",
                        "reason": "资金到位率未提供，按未达标档计 2 分"},
    }
    text = ("```json\n" + json.dumps(_payload(scores), ensure_ascii=False) + "\n```\n"
            "## 一级指标汇总\n决策 6 分。\n## 整体分析\n资料仅覆盖部分指标，以下内容在长度上限处被截"
            )
    agent = performance_agent.PerformanceAgent.__new__(performance_agent.PerformanceAgent)
    agent.session_id = "step7-fixture"
    agent.model_name = "fixture"
    agent.base_agent = SimpleNamespace(llm=None)
    monkeypatch.setattr(agent, "_execute_step", lambda state, step: text)
    state = {
        "session_id": "step7-fixture",
        "completed_steps": [],
        "step_results": {},
        "warnings": [],
        "indicator_system": _tree(["L3_01_01_01", "L3_01_01_02"]),
        "evaluation_table": "| 指标 | 分值 |\n| 决策 | 10 |\n",
        "effective_data_categorized": {"项目概况": "总投资5000万元。\n"},
        "step_evidence": {"7": [{"id": "P1-14-1", "kind": "project", "text": "总投资5000万元"}]},
    }
    agent._node_step7(state)
    assert state["score_disposition"]["counts"]["accepted"] == 2
    assert state["step7_length"]["json_first"] is True


def test_backup_persists_length_fields():
    """备份是字段白名单：不把这两个字段写进去，重启后只看到「解析失败」看不到「被截断」。

    实测踩过：端到端那一轮的备份里 warnings 有截断告警，`truncated_steps` 却是 null
    —— 因为白名单没跟上（`score_disposition` 当年也是这么漏的）。
    """
    captured = {}
    agent = performance_agent.PerformanceAgent.__new__(performance_agent.PerformanceAgent)
    agent.session_id = "backup-fixture"
    agent.update_state_backup = lambda sid, backup: captured.update(backup)
    state = {
        "session_id": "backup-fixture",
        "truncated_steps": [7],
        "step7_length": {"json_first": True, "total_chars": 10},
    }
    agent._persist_state_backup(state)
    assert captured["truncated_steps"] == [7]
    assert captured["step7_length"]["json_first"] is True


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
