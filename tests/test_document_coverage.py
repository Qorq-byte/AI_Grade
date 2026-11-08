"""长文档覆盖度：分块定位、预算选择、覆盖度声明、未引用部分压缩。

不验证模型质量，验证的是「模型到底看到了多少资料」这件事是否可判定、可展示、不被静默丢弃。
"""
from types import SimpleNamespace

import pytest

from backend.agents import performance_agent
from backend.services import doc_coverage as dc


def _pages(count=6, chars_per_page=2000):
    """构造一份带 PDF 页码标记的文本。"""
    return "\n\n".join(
        f"【PDF 第{index}页】\n" + (f"第{index}页正文。" * (chars_per_page // 6))
        for index in range(1, count + 1)
    )


# ═══════════════════════════════════════════════════════
#  切块与定位
# ═══════════════════════════════════════════════════════

def test_page_markers_become_locators():
    chunks = dc.split_documents({"合同.pdf": _pages(3, 300)}, target_chars=500)
    assert [chunk.locator.split("（")[0] for chunk in chunks] == ["第1页", "第2页", "第3页"]
    assert all(chunk.source == "合同.pdf" for chunk in chunks)
    assert chunks[0].index == 0 and chunks[-1].index == len(chunks) - 1


def test_headings_become_locators_when_no_page_markers():
    text = "## 一、项目概况\n内容甲。\n\n## 二、资金使用\n内容乙。\n\n## 三、产出\n内容丙。"
    chunks = dc.split_documents({"说明.docx": text}, target_chars=500)
    assert [chunk.locator for chunk in chunks] == ["一、项目概况", "二、资金使用", "三、产出"]
    assert "内容乙" in chunks[1].text


def test_small_paragraphs_merge_up_to_target():
    text = "\n\n".join(["短段落。" * 20] * 12)  # 每段 80 字
    chunks = dc.split_documents({"a.txt": text}, target_chars=400)
    assert all(chunk.chars <= 400 + 60 for chunk in chunks), [chunk.chars for chunk in chunks]
    assert len(chunks) > 1


def test_oversized_paragraph_is_split_at_sentence_boundary():
    text = "这是一句很长的正文。" * 2000  # 约 20000 字，无空行
    chunks = dc.split_documents({"b.txt": text}, target_chars=1000)
    assert len(chunks) > 1
    assert all(chunk.chars <= 2000 for chunk in chunks)
    assert all(chunk.text.endswith("。") for chunk in chunks[:-1])


# ═══════════════════════════════════════════════════════
#  预算选择与声明
# ═══════════════════════════════════════════════════════

def test_short_document_is_fully_used():
    plan = dc.build_plan({"短资料.txt": "只有一点点内容。"}, budget_chars=12000)
    assert plan.omitted_chunks == []
    assert plan.mode == "full"
    assert "已全部纳入" in plan.statement()
    assert plan.used_ratio == 1.0


def test_budget_keeps_head_and_tail_and_records_omission():
    plan = dc.build_plan({"长报告.pdf": _pages(10, 3000)}, budget_chars=12000)
    assert plan.used_chars <= 12000
    assert plan.omitted_chars > 0
    assert plan.used_chars + plan.omitted_chars == plan.total_chars
    assert plan.mode == "head_tail"
    # 文末必须被取到（结论/验收信息常在最后）
    used_locators = [chunk.locator for chunk in plan.used_chunks]
    assert any("第10页" in locator for locator in used_locators), used_locators
    # 中间部分被省略
    omitted_locators = [chunk.locator for chunk in plan.omitted_chunks]
    assert omitted_locators and any("第5页" in locator for locator in omitted_locators)


def test_statement_states_numbers_and_omitted_refs():
    plan = dc.build_plan({"长报告.pdf": _pages(10, 3000)}, budget_chars=12000)
    text = plan.statement()
    assert "【资料覆盖度】" in text
    assert str(plan.total_chars) in text and str(plan.used_chars) in text
    assert f"{plan.used_ratio * 100:.1f}%" in text
    assert "未直接引用的部分" in text
    assert "长报告.pdf 第" in text


def test_statement_tells_model_not_to_guess_when_nothing_compressed():
    plan = dc.build_plan({"长报告.pdf": _pages(10, 3000)}, budget_chars=12000)
    assert "不要猜测" in plan.statement()
    plan.digest = "- 预算 20 万元 [第4页]"
    plan.digest_chars_covered = plan.omitted_chars
    assert "要点摘要" in plan.statement()
    assert "不要猜测" not in plan.statement()


def test_to_dict_is_lightweight_and_serialisable():
    plan = dc.build_plan({"长报告.pdf": _pages(10, 3000)}, budget_chars=12000)
    payload = plan.to_dict()
    assert payload["total_chars"] == plan.total_chars
    assert payload["omitted"] and "text" not in payload["omitted"][0]
    assert set(payload) >= {"total_chars", "used_chars", "omitted_chars", "used_ratio", "mode", "statement"}


# ═══════════════════════════════════════════════════════
#  map-reduce 提示词
# ═══════════════════════════════════════════════════════

def test_digest_batches_respect_limits_and_keep_locators():
    plan = dc.build_plan({"长报告.pdf": _pages(20, 3000)}, budget_chars=8000)
    batches = dc.digest_batches(plan, batch_chars=6000, max_batches=3)
    assert 1 <= len(batches) <= 3
    assert all(len(batch) <= 6000 + 400 for batch in batches)
    assert all("[长报告.pdf 第" in batch for batch in batches)


def test_reduce_digest_dedupes_and_truncates():
    parts = ["- 预算 20 万元 [第3页]\n- 验收合格 [第8页]", "- 预算 20 万元 [第3页]\n- 延期两个月 [第9页]"]
    digest = dc.reduce_digest(parts, max_chars=200)
    assert digest.count("预算 20 万元") == 1
    assert "延期两个月" in digest
    assert len(dc.reduce_digest(parts, max_chars=12)) <= 13


def test_digest_prompt_forbids_invention():
    messages = dc.digest_messages("[合同.pdf 第2页] 项目预算 20 万元")
    assert messages[0]["role"] == "system" and "不编造" in messages[0]["content"]
    assert "项目预算 20 万元" in messages[1]["content"]


# ═══════════════════════════════════════════════════════
#  与 Agent / 提示词的联动
# ═══════════════════════════════════════════════════════

def _agent(monkeypatch, replies=None):
    agent = performance_agent.PerformanceAgent.__new__(performance_agent.PerformanceAgent)
    agent.session_id = "coverage-fixture"
    agent.model_name = "fixture"
    agent.base_agent = SimpleNamespace(llm=None)
    agent.use_rag = False
    agent.temperature_profile = SimpleNamespace(name="fixture")
    calls = []
    queue = list(replies or [])

    def fake_invoke(messages, step_number, state, timeout=300, stream=None):
        calls.append({"messages": messages, "step": step_number, "stream": stream})
        return queue.pop(0) if queue else "- 预算 20 万元 [第4页]"

    monkeypatch.setattr(agent, "_invoke_llm", fake_invoke)
    return agent, calls


def _state(text, **extra):
    state = {
        "session_id": "coverage-fixture", "project_name": "长文档项目",
        "raw_text": text, "all_raw_texts": {"长报告.pdf": text},
        "completed_steps": [], "step_results": {}, "warnings": [],
        "step_control": {"comparison_mode": "single"},
    }
    state.update(extra)
    return state


def test_human_vars_use_chunked_budget_and_expose_coverage(monkeypatch):
    agent, calls = _agent(monkeypatch)
    text = _pages(12, 3000)
    state = _state(text)

    variables = agent._build_human_vars(state, 3)

    assert len(text) > 30000
    assert len(variables["raw_text_full"]) <= agent.DOC_FULL_BUDGET + 200
    assert len(variables["raw_text_preview"]) <= agent.DOC_PREVIEW_BUDGET + 200
    assert variables["raw_text_preview"] and variables["raw_text_preview"] in variables["raw_text_full"]
    assert "【资料覆盖度】" in variables["document_coverage"]
    assert "未直接引用" in variables["document_coverage"]
    # 未引用部分被压缩：按批调用（map-reduce），次数受 MAX_DIGEST_BATCHES 限制且不流式
    assert variables["omitted_digest"]
    assert 1 <= len(calls) <= dc.MAX_DIGEST_BATCHES
    assert all(call["stream"] is False for call in calls)
    assert state["document_coverage"]["omitted_chars"] > 0
    assert any("未直接引用" in warning for warning in state["warnings"])


def test_coverage_is_computed_once_per_session(monkeypatch):
    agent, calls = _agent(monkeypatch)
    state = _state(_pages(12, 3000))

    first = agent._build_human_vars(state, 1)
    after_first = len(calls)
    assert after_first >= 1 and first["omitted_digest"]

    agent._build_human_vars(state, 2)
    agent._build_human_vars(state, 7)

    assert len(calls) == after_first, "同一会话的覆盖度与摘要只应计算一次"


def test_preview_step_declares_only_what_it_really_received(monkeypatch):
    """步骤1只拿到前 8000 字，声明就必须说 8000，不能报满 12000 的预算。"""
    agent, _ = _agent(monkeypatch)
    state = _state(_pages(12, 3000))

    step1 = agent._build_human_vars(state, 1)
    step2 = agent._build_human_vars(state, 2)
    plan = state["document_coverage"]

    delivered = len(step1["raw_text_preview"])
    assert delivered <= agent.DOC_PREVIEW_BUDGET
    assert f"直接引用 {delivered} 字" in step1["document_coverage"]
    assert f"其余 {plan['total_chars'] - delivered} 字" in step1["document_coverage"]
    assert "本步骤只放入前" in step1["document_coverage"]

    assert f"直接引用 {plan['used_chars']} 字" in step2["document_coverage"]
    assert "本步骤只放入前" not in step2["document_coverage"]


def test_statement_clipped_view_still_says_what_was_not_read():
    plan = dc.build_plan({"长报告.pdf": _pages(10, 3000)}, budget_chars=12000)

    text = plan.statement(delivered_chars=1000)

    assert "直接引用 1000 字" in text
    assert f"其余 {plan.total_chars - 1000} 字" in text
    assert "不要猜测" in text  # 未压缩时仍然要求模型不要补全
    assert "已全部纳入" not in text

    plan.digest = "- 预算 20 万元 [长报告.pdf 第4页]"
    plan.digest_chars_covered = plan.omitted_chars
    text = plan.statement(delivered_chars=1000)
    assert "要点摘要" in text
    # 节选砍掉的那部分既没进提示词也没进摘要，必须写明
    assert f"预算内还有 {plan.used_chars - 1000} 字未放入本步骤，也未做摘要" in text


def test_no_digest_for_small_documents(monkeypatch):
    agent, calls = _agent(monkeypatch)
    state = _state("小资料：项目预算 20 万元。")

    variables = agent._build_human_vars(state, 2)

    assert calls == [], "资料本身就放得下时不应额外调用模型"
    assert variables["omitted_digest"] == ""
    assert "已全部纳入" in variables["document_coverage"]


def test_execute_step_injects_coverage_and_digest(monkeypatch):
    captured = {}
    agent, _ = _agent(monkeypatch)
    text = _pages(12, 3000)
    state = _state(text)
    state["step_evidence"] = {}

    agent.prompt_manager = SimpleNamespace(
        get_system_prompt=lambda *a, **k: "系统提示",
        get_human_prompt=lambda *a, **k: "人类提示（正文）",
    )
    monkeypatch.setattr(performance_agent, "SessionLocal", lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(performance_agent, "retrieve_step_evidence", lambda *a, **k: [])

    def fake_invoke(messages, step_number, st, timeout=300, stream=None):
        # 压缩摘要走纯 dict 消息；主步骤走 LangChain 消息对象
        if isinstance(messages[0], dict):
            return "- 预算 20 万元 [长报告.pdf 第4页]"
        captured["messages"] = messages
        return "步骤输出"

    monkeypatch.setattr(agent, "_invoke_llm", fake_invoke)

    agent._execute_step(state, 3)

    human = captured["messages"][1].content
    assert "【资料覆盖度】" in human
    assert "未直接引用部分的要点（程序摘要" in human
    assert "不得当作证据编号引用" in human
    assert "[长报告.pdf 第" in human


def test_missing_files_do_not_break_coverage(monkeypatch):
    agent, calls = _agent(monkeypatch)
    state = {"session_id": "empty", "completed_steps": [], "step_results": {}, "warnings": [],
             "step_control": {"comparison_mode": "single"}}

    variables = agent._build_human_vars(state, 1)

    assert variables["raw_text_full"] == ""
    assert "未提供可解析的原始资料" in variables["document_coverage"]
    assert calls == []
    assert state["document_coverage"]["total_chars"] == 0
