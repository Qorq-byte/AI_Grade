"""证据块的邻行扩展（批次 50）。

单行摘录经常缺主语：模型看到「78%」不知道是什么的 78%。
这里钉住四件事：扩了上下文、编号不变、副本不改缓存、边界不越界。
"""
from backend.services import workflow_evidence as we
from tests.rag_eval import corpus, run


def _hit(line: int, source="a.txt"):
    return {"id": "P1-3-1", "kind": "project", "source": source,
            "locator": f"【PDF 第1页】 · 提取文本第{line}行", "text": f"L{line}"}


def _state():
    return {"all_raw_texts": {"a.txt": "\n".join(f"L{i}" for i in range(1, 9))}}


def test_expansion_adds_neighbours_on_both_sides():
    out = we._expand_context([_hit(4)], _state())[0]

    assert out["text"] == "L2\nL3\nL4\nL5\nL6"


def test_expansion_keeps_id_and_locator():
    hit = _hit(4)
    out = we._expand_context([hit], _state())[0]

    assert out["id"] == hit["id"]
    assert out["locator"] == hit["locator"]


def test_expansion_returns_a_copy():
    hit = _hit(4)
    we._expand_context([hit], _state())

    assert hit["text"] == "L4"          # 原对象（可能来自进度缓存）不能被改


def test_expansion_is_clipped_at_document_edges():
    first = we._expand_context([_hit(1)], _state())[0]
    last = we._expand_context([_hit(8)], _state())[0]

    assert first["text"] == "L1\nL2\nL3"
    assert last["text"] == "L6\nL7\nL8"


def test_knowledge_hits_are_left_alone():
    hit = {"id": "K1", "kind": "knowledge", "source": "标准", "locator": "条款 1", "text": "原文"}
    out = we._expand_context([hit], _state())[0]

    assert out is hit


def test_real_retrieval_returns_more_than_one_line():
    """端到端：随便取一条样本，返回的项目证据都应是多行上下文。"""
    state = run.make_state()
    hits = run.retrieve("项目总投资是多少？", 2, state)
    project = [hit for hit in hits if hit.get("kind") == "project"]

    assert project
    assert all(len((hit.get("text") or "").splitlines()) > 1 for hit in project)


def test_corpus_docs_are_line_addressable():
    """扩展依赖「locator 里的行号能对上原文」——这条不成立，扩展就会错位。"""
    for name, text in corpus.DOCS.items():
        assert text.splitlines(), name


def test_radius_is_configurable_for_grid():
    """半径网格要用同一个入口跑 0/1/2/3，不能各写一套。"""
    state = run.make_state()

    narrow = [hit for hit in run.retrieve("项目总投资是多少？", 2, state, radius=0)
              if hit.get("kind") == "project"]
    wide = [hit for hit in run.retrieve("项目总投资是多少？", 2, state, radius=2)
            if hit.get("kind") == "project"]

    assert narrow and wide
    assert all(len(hit["text"].splitlines()) == 1 for hit in narrow)
    assert sum(len(hit["text"]) for hit in wide) > sum(len(hit["text"]) for hit in narrow)
