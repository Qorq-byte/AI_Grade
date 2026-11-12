"""检索排序（BM25）与证据选取的行为约束。

这里不验证「检索准不准」（那是 tests/rag_eval 的活），验证的是：
分词不退化、排序靠相关性、查询真的影响结果、截断可见、缓存不串数据。
"""
import pytest

from backend.services import retrieval, workflow_evidence


def test_tokenize_cjk_bigrams_and_ascii_runs():
    assert retrieval.tokenize("总投资") == ["总投", "投资"]
    assert retrieval.tokenize("4,860 万元") == ["4860", "万元"]
    assert retrieval.tokenize("HDPE 管材") == ["hdpe", "管材"]
    assert retrieval.tokenize("") == []
    assert retrieval.tokenize("。。。") == []


def test_tokenize_drops_single_cjk_char_but_keeps_words():
    # 单字中文几乎无区分度，不入索引
    assert retrieval.tokenize("钱") == []
    assert retrieval.tokenize("a") == ["a"]


def _chunk(text, chunk_id="c1", kind="project", step=None):
    return {"id": chunk_id, "source": "s.txt", "locator": "第1页", "kind": kind,
            "text": text, "step_number": step}


def test_bm25_prefers_the_line_that_answers_the_query():
    boilerplate = _chunk("项目按规定推进，资金使用规范，进度符合计划要求。", "c1")
    answer = _chunk("合同价款 4,612.35 万元，工期 420 日历天。", "c2")
    index = retrieval.build_index([boilerplate, answer])

    hits = index.search("合同价款是多少，工期多久")

    assert hits, "相关查询不应零命中"
    assert hits[0][1].id == "c2", "包含答案的块必须排在第一"


def test_bm25_ignores_unknown_tokens_instead_of_crashing():
    index = retrieval.build_index([_chunk("项目总投资 4,860 万元。")])
    assert index.known_tokens("完全不相关的词汇") == 0
    assert index.search("完全不相关的词汇") == []


def test_bm25_length_normalisation_beats_keyword_density():
    long_boilerplate = _chunk("项目资金项目进度项目验收项目资料项目合同项目预算项目目标项目数量" * 3, "long")
    short_answer = _chunk("合同价款 4,612.35 万元", "short")
    index = retrieval.build_index([long_boilerplate, short_answer])
    assert index.search("合同价款")[0][1].id == "short"


# ═══════════════════════════════════════════════════════
#  retrieve_step_evidence：对外契约
# ═══════════════════════════════════════════════════════

DOCS = {
    "01_批复.txt": "【PDF 第1页】\n项目总投资 4,860 万元，资金来源为市级财政拨款 3,400 万元。\n项目单位：城西市政建设服务中心。",
    "02_合同.txt": "【PDF 第1页】\n合同价款 4,612.35 万元，工期 420 日历天。\n违约条款：每延误 1 日按合同价款的 0.5‰ 计违约金。",
}


def _state(docs=None, **extra):
    state = {"all_raw_texts": dict(docs or DOCS), "indicator_type": ""}
    state.update(extra)
    return state


def test_hit_shape_is_backward_compatible():
    hits = workflow_evidence.retrieve_step_evidence(None, _state(), 2)
    assert hits
    for hit in hits:
        assert set(hit) == {"id", "source", "locator", "kind", "text"}
    rendered = workflow_evidence.render_evidence(hits)
    assert "[P1-2-1]" in rendered and "项目资料" in rendered


def test_query_changes_ranking():
    state = _state()
    step = workflow_evidence.retrieve_step_evidence(None, state, 2)
    question = workflow_evidence.retrieve_step_evidence(
        None, state, 2, query="违约金怎么算")
    assert [hit["id"] for hit in step] != [hit["id"] for hit in question], \
        "传入具体查询后结果应当改变（这正是 Batch 6 基线暴露的问题）"
    assert "违约金" in question[0]["text"]


def test_default_query_comes_from_step_intent():
    assert "总投资" in workflow_evidence.step_query(_state(), 2)
    assert "满意度" in workflow_evidence.step_query(_state(), 2)
    assert "指标体系" in workflow_evidence.step_query(_state(), 3) or \
           "指标" in workflow_evidence.step_query(_state(), 3)
    with_type = workflow_evidence.step_query(_state(indicator_type="专项债指标体系"), 3)
    assert "专项债指标体系" in with_type


def test_project_limit_is_honoured():
    hits3 = workflow_evidence.retrieve_step_evidence(None, _state(), 2)
    hits5 = workflow_evidence.retrieve_step_evidence(None, _state(), 2, project_limit=5)
    assert len(hits3) <= workflow_evidence.PROJECT_LIMIT
    assert len(hits5) >= len(hits3)
    assert all(hit["kind"] == "project" for hit in hits5)


def test_degenerate_query_falls_back_instead_of_returning_nothing():
    state = _state()
    # 「的」是单字，二元组切不出任何已知词 → 走旧的关键词回退
    hits = workflow_evidence.retrieve_step_evidence(None, state, 2, query="的")
    assert hits, "退化查询不应直接空手而归"


def test_oversized_document_truncation_is_visible():
    huge = "【PDF 第1页】\n" + ("项目资金使用情况说明。" * 40000)
    state = _state({"巨大文件.txt": huge})

    workflow_evidence.retrieve_step_evidence(None, state, 2)

    assert any("未建立检索索引" in warning for warning in state.get("warnings", [])), \
        "索引截断必须写进 warnings，不能静默"


def test_truncation_warning_is_not_duplicated_per_step():
    huge = "【PDF 第1页】\n" + ("项目资金使用情况说明。" * 40000)
    state = _state({"巨大文件.txt": huge})
    for step in (1, 2, 3):
        workflow_evidence.retrieve_step_evidence(None, state, step)
    warnings = [w for w in state.get("warnings", []) if "未建立检索索引" in w]
    assert len(warnings) == 1


def test_index_is_cached_between_steps(monkeypatch):
    workflow_evidence.clear_index_cache()
    calls = []
    original = retrieval.build_index

    def counting(chunks):
        calls.append(1)
        return original(chunks)

    monkeypatch.setattr(retrieval, "build_index", counting)
    state = _state()
    workflow_evidence.retrieve_step_evidence(None, state, 1)
    workflow_evidence.retrieve_step_evidence(None, state, 2)
    workflow_evidence.retrieve_step_evidence(None, state, 7)
    assert len(calls) == 1, "同一份资料在一个会话里只应建一次索引"
    workflow_evidence.clear_index_cache()


def test_evidence_text_is_verbatim_from_source():
    """证据必须逐字来自原文（忽略空白）。

    批次 50 起证据块会带前后各两行上下文，块内保留换行；比较时把空白
    （含换行）统一去掉，校验的仍是"每个字都能在原文里找到"。
    """
    state = _state()
    source = "".join(state["all_raw_texts"].values()).replace("\n", "").replace(" ", "")
    for hit in workflow_evidence.retrieve_step_evidence(None, state, 2):
        assert hit["text"].replace("\n", "").replace(" ", "") in source


# ═══════════════════════════════════════════════════════
#  多取证面融合（Batch 8）
# ═══════════════════════════════════════════════════════

def test_explicit_query_bypasses_facet_plan(monkeypatch):
    """显式传 query 时走单查询排序；不传才是线上路径（多面融合）。"""

    def explode(*args, **kwargs):
        raise AssertionError("显式查询不应触发多面融合")

    monkeypatch.setattr(workflow_evidence, "_plan_rank", explode)
    workflow_evidence.retrieve_step_evidence(None, _state(), 2, query="工期多久")
    with pytest.raises(AssertionError):
        workflow_evidence.retrieve_step_evidence(None, _state(), 2)


def test_facet_plan_switch_controls_the_path(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("开关关闭时不应触发多面融合")

    monkeypatch.setattr(workflow_evidence, "USE_FACET_PLAN", False)
    monkeypatch.setattr(workflow_evidence, "_plan_rank", explode)
    assert workflow_evidence.retrieve_step_evidence(None, _state(), 2)


def test_facet_weight_zero_matches_single_query(monkeypatch):
    state = _state()
    single = workflow_evidence.retrieve_step_evidence(None, state, 3, query=workflow_evidence.step_query(state, 3))

    monkeypatch.setattr(workflow_evidence, "FACET_WEIGHT", 0.0)
    workflow_evidence.clear_index_cache()
    planned = workflow_evidence.retrieve_step_evidence(None, _state(), 3)

    assert [hit["id"] for hit in planned] == [hit["id"] for hit in single], \
        "面权重为 0 时应与单查询排序完全一致"


def test_facet_fusion_reorders_by_combined_score(monkeypatch):
    """直接验融合算式：一个块被多个取证面命中时，融合分应当超过基础分更高的块。"""
    a = retrieval.IndexedChunk(id="A", source="s", locator="p", kind="project", text="甲内容")
    b = retrieval.IndexedChunk(id="B", source="s", locator="p", kind="project", text="乙内容")

    class FakeIndex:
        """base 给 A 高分；两个取证面都给 B 高分。"""

        def search(self, query, limit=30):
            table = {
                "基础查询": [(10.0, a), (1.0, b)],
                "面一": [(10.0, b)],
                "面二": [(10.0, b)],
            }
            return table.get(query, [])[:limit]

    monkeypatch.setattr(workflow_evidence, "step_query", lambda *args, **kwargs: "基础查询")
    monkeypatch.setattr(workflow_evidence, "STEP_SUBQUERIES", {3: ("面一", "面二")})
    monkeypatch.setattr(workflow_evidence, "STEP_TERMS", {3: ()})
    monkeypatch.setattr(workflow_evidence, "FACET_WEIGHT", 0.5)

    fused = workflow_evidence._plan_rank(FakeIndex(), {}, 3, 3, 2)
    assert [hit["id"] for hit in fused] == ["B", "A"], "融合分 1+5+5=11 应超过 10"

    monkeypatch.setattr(workflow_evidence, "FACET_WEIGHT", 0.0)
    plain = workflow_evidence._plan_rank(FakeIndex(), {}, 3, 3, 2)
    assert [hit["id"] for hit in plain] == ["A", "B"], "面权重归零后应回到基础排序"


def test_facet_queries_exist_for_every_step_used_by_pipeline():
    for step in range(1, 15):
        assert step in workflow_evidence.STEP_SUBQUERIES, f"步骤 {step} 缺少取证面"
        assert all(query.strip() for query in workflow_evidence.STEP_SUBQUERIES[step])

