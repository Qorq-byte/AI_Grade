"""内容索引：Step 2 的「核心内容 ↔ 原文出处」索引是否真的建起来了。

背景（Batch 10 的起因）：
`ContentIndexer.__init__` 里 `self.index: Dict[str, IndexEntry] = {}` 与同名方法
`def index(self, raw_texts)` 撞名，实例属性遮蔽方法 → `indexer.index({...})` 必然
`TypeError: 'dict' object is not callable`，且被 `_node_step2` 的 `except` 吞成一条
warning（"非致命"）。

后果不是"少了个功能"：`effective_data_categorized` 是步骤 3/7/9 **提示词里唯一的资料入口**。
它恒为 `{}`，这三步就在看不到项目资料的条件下构建指标体系、评分、分析问题。

本文件同时守住第二件事：修好之后**不能把整份语料原样灌进提示词**——
分类内容是段落原文按类别重排，体积≈语料本身（实测 9,925 字语料 → 10,415 字符 JSON），
四个步骤各灌一遍既贵又和同提示词里的 raw_text 重复。所以注入必须封顶并声明省略量。
"""
from types import SimpleNamespace

import pytest

from backend.agents import performance_agent
from backend.agents.tools.content_indexer import ContentIndexer

CORPUS = {
    "立项批复.txt": (
        "项目名称：城西片区雨污分流改造工程。项目单位：城西市政建设服务中心。\n\n"
        "投资估算：项目总投资 4,860 万元，其中工程费用 3,912 万元。\n\n"
        "资金来源：市级财政拨款 3,400 万元，区级配套 1,460 万元。"
    ),
    "验收报告.txt": (
        "项目已于 2025 年 11 月完成竣工验收，验收结论为合格。\n\n"
        "合同金额 4,712 万元，结算金额 4,698 万元，节约资金 14 万元。"
    ),
}


def _indexer():
    return ContentIndexer(use_semantic=False)


# ═══════════════════════════════════════════════════════
#  索引方法本身
# ═══════════════════════════════════════════════════════

def test_index_method_is_callable():
    """回归：实例属性不能遮蔽同名方法。"""
    indexer = _indexer()
    assert callable(indexer.index), "index 被实例属性遮蔽，调用处必然 TypeError"
    entries = indexer.index(CORPUS)
    assert isinstance(entries, dict) and entries


def test_every_effective_chunk_gets_an_entry():
    indexer = _indexer()
    indexer.index(CORPUS)
    effective = [chunk for chunk in indexer.chunks if chunk.is_effective]
    assert len(effective) == len(indexer.get_index())
    # 每条索引都能回指到「文件::第N段」，这是"可追溯"的全部意义
    for source in indexer.get_index().values():
        assert "::第" in source and source.split("::")[0] in CORPUS


def test_categorized_content_keeps_every_effective_chunk():
    indexer = _indexer()
    indexer.index(CORPUS)
    merged = "\n\n".join(indexer.get_categorized_content().values())
    for chunk in indexer.chunks:
        if chunk.is_effective:
            assert chunk.text in merged


def test_template_jargon_is_indexed_out_but_kept_as_chunk():
    indexer = _indexer()
    indexer.index({"函.txt": "经研究决定，同意立项。\n\n项目总投资 100 万元。"})
    indexed = "\n".join(indexer.get_index().keys())
    assert "经研究决定" not in indexed
    assert any(not chunk.is_effective for chunk in indexer.chunks)


def test_structured_index_is_sorted_and_numbered():
    indexer = _indexer()
    indexer.index(CORPUS)
    rows = indexer.get_index_structured()
    assert [row["序号"] for row in rows] == list(range(1, len(rows) + 1))
    assert all(row["原文出处"].startswith(tuple(CORPUS)) for row in rows)


def test_contradicting_status_is_detected():
    indexer = _indexer()
    indexer.index({"a.txt": "项目建设内容为管网改造，目前已完工验收。",
                   "b.txt": "项目建设内容为管网改造，目前在建，形象进度 60%。"})
    items = indexer.get_contradictions()
    assert [item["type"] for item in items] == ["事实矛盾"]
    # 同一个互斥对只能报一条（原来双向循环会报两条）
    assert len(items) == 1


def test_numbers_are_not_reported_as_contradictions():
    """同类别里数值差 2 倍不是矛盾：工程费/其他费/预备费本来就不相等。"""
    indexer = _indexer()
    indexer.index({"概算.txt": "工程费用 3,912 万元，工程建设其他费 486 万元，基本预备费 462 万元。",
                   "决算.txt": "工程费用 3,900 万元，工程建设其他费 490 万元，基本预备费 460 万元。"})
    assert indexer.get_contradictions() == []


def test_empty_corpus_does_not_raise():
    indexer = _indexer()
    assert indexer.index({}) == {}
    assert indexer.get_index() == {}


# ═══════════════════════════════════════════════════════
#  注入提示词的预算
# ═══════════════════════════════════════════════════════

def _agent():
    agent = performance_agent.PerformanceAgent.__new__(performance_agent.PerformanceAgent)
    agent.session_id = "index-fixture"
    agent.model_name = "fixture"
    agent.base_agent = SimpleNamespace(llm=None)
    agent.use_rag = False
    agent.temperature_profile = SimpleNamespace(name="fixture")
    return agent


def _state(text="", categorized=None, **extra):
    state = {
        "session_id": "index-fixture", "project_name": "雨污分流改造工程",
        "raw_text": text, "all_raw_texts": {"资料.txt": text} if text else {},
        "completed_steps": [], "step_results": {}, "warnings": [],
        "step_control": {"comparison_mode": "single"},
        "effective_data_categorized": categorized or {},
    }
    state.update(extra)
    return state


def _big_categorized():
    return {
        "实施过程": "实" * 9000,
        "资金管理": "资" * 5000,
        "项目概况": "概" * 300,
        "组织管理": "组" * 2000,
    }


def test_injected_excerpt_is_bounded():
    agent = _agent()
    variables = agent._build_human_vars(_state(categorized=_big_categorized()), 3)
    injected = variables["effective_data_categorized"]
    assert len(injected) <= agent.CATEGORY_BUDGET + 400, len(injected)
    assert injected != "{}" and "实" in injected


def test_injected_excerpt_declares_what_was_left_out():
    agent = _agent()
    injected = agent._build_human_vars(_state(categorized=_big_categorized()), 3)[
        "effective_data_categorized"]
    # 每个被截断的类别要说清「共多少字、展示了多少」；整体还要交代没放进来的部分
    assert "未放入本步骤" in injected
    assert "字" in injected


def test_category_priority_puts_project_facts_first():
    """预算有限时先给「项目概况/资金管理」，别让「实施过程」把额度吃光。"""
    agent = _agent()
    injected = agent._build_human_vars(_state(categorized=_big_categorized()), 3)[
        "effective_data_categorized"]
    assert injected.index("项目概况") < injected.index("实施过程")


def test_empty_categorized_says_so_instead_of_empty_json():
    agent = _agent()
    injected = agent._build_human_vars(_state(), 3)["effective_data_categorized"]
    assert injected != "{}"
    assert "未" in injected or "暂无" in injected


def test_index_summary_lists_sources_for_traceability():
    agent = _agent()
    indexer = _indexer()
    indexer.index(CORPUS)
    state = _state(categorized={"项目概况": "x"}, content_index=indexer.get_index())
    summary = agent._build_human_vars(state, 3)["content_index_summary"]
    assert "立项批复.txt::第1段" in summary


def test_step2_actually_populates_index_and_prompt_input():
    """真正的回归点：跑一次 _node_step2，两个字段必须都有值。

    原来这个 bug 之所以能活下来，是因为没人从「步骤 2 跑完之后」往回看——
    索引器单独测、提示词单独测，中间被 except 吞掉的那一环没人管。
    """
    agent = _agent()
    agent.content_indexer = ContentIndexer(use_semantic=False)
    agent._execute_step = lambda state, step, **kw: "步骤2模型输出"
    agent._record_step_result = lambda state, step, content: None
    state = _state()
    state["all_raw_texts"] = dict(CORPUS)
    state["raw_text"] = "\n\n".join(CORPUS.values())

    result = agent._node_step2(state)

    assert result["content_index"], "content_index 为空：索引没有真正建立"
    assert len(result["content_index"]) >= 3
    assert result["effective_data_categorized"], "分类内容为空：步骤 3/7/9 会看不到资料"
    merged = "\n\n".join(result["effective_data_categorized"].values())
    assert "4,860 万元" in merged


def test_step_with_full_raw_text_skips_duplicate_injection():
    """步骤 12 已经有 raw_text_full，不再重复注入同一批文字。"""
    agent = _agent()
    state = _state(text="项目总投资 4,860 万元。", categorized=_big_categorized())
    assert "不再重复注入" in agent._build_human_vars(state, 12)["effective_data_categorized"]
    assert "实" in agent._build_human_vars(state, 3)["effective_data_categorized"]


def test_categorized_content_survives_restart(tmp_path, monkeypatch):
    """重启/续跑后步骤 3 不能再变瞎：分类内容必须进备份白名单。

    注意要断言的是 `_persist_state_backup`（按白名单过滤），不是 `update_state_backup`
    （直接写盘，不做过滤，用它断言等于什么都没测）。
    """
    monkeypatch.setattr(performance_agent, "STATE_BACKUP_DIR", tmp_path / "backups")
    agent = _agent()
    state = _state(categorized={"资金管理": "投资 4,860 万元"})
    state["session_id"] = "resume-fixture"

    agent._persist_state_backup(state)

    restored = performance_agent.PerformanceAgent.load_state_backup("resume-fixture")
    assert restored.get("effective_data_categorized") == {"资金管理": "投资 4,860 万元"}
