"""检索评估基线的回归保护。

这里的断言分三类，别混着看：
  1. **样本自检**：锚点必须能被切块覆盖——不成立是样本写错了，不是检索差；
  2. **绝对指标**：引用必须逐字来自原文（citation_verbatim_rate == 1.0），这条不容许下降；
  3. **基线比较**：Recall/MRR/精确率不得低于 `baseline.json`，空结果率不得上升。

允许的「变好」路径：如果检索改成真正的查询检索（query_insensitivity 下降），
Recall@3 必须比基线至少高 5 个百分点，否则不算改进——避免「改得更聪明但更查不到」。
"""
import json

import pytest

from tests.rag_eval import run as rag

BASELINE = json.loads(rag.BASELINE_PATH.read_text(encoding="utf-8"))
BASE = BASELINE["metrics"]


@pytest.fixture(scope="module")
def result():
    return rag.evaluate()


def test_samples_are_well_formed():
    assert len(rag.samples.SAMPLES) >= 20, "样本太少，测不出东西"
    ids = [sample["id"] for sample in rag.samples.SAMPLES]
    assert len(ids) == len(set(ids)), "样本 ID 重复"
    for sample in rag.samples.SAMPLES:
        assert sample["question"].strip()
        assert sample["expect"], f"{sample['id']} 没有期望证据"
        for _doc, anchor in sample["expect"]:
            assert rag.anchor_covered(anchor), f"{sample['id']} 的锚点不在任何块里：{anchor}"


def test_corpus_is_not_tiny():
    """语料至少要覆盖「有用信息淹没在干扰页里」的情形。"""
    assert rag.corpus_chunk_count() >= 100
    assert sum(len(text) for text in rag.corpus.DOCS.values()) >= 8000


def test_citation_is_verbatim(result):
    assert result["metrics"]["citation_verbatim_rate"] == 1.0, "检索返回了原文里没有的文本"


def test_metrics_do_not_drop_below_baseline(result):
    metrics = result["metrics"]
    for cutoff, value in BASE["recall"].items():
        assert metrics["recall"][cutoff] >= value, f"Recall{cutoff} 低于基线"
    assert metrics["mrr"] >= BASE["mrr"], "MRR 低于基线"
    assert metrics["precision"] >= BASE["precision"], "精确率低于基线"
    assert metrics["empty_rate"] <= BASE["empty_rate"], "空结果率高于基线"


def test_query_aware_retrieval_must_actually_help(result):
    """现状是「检索与问题无关」（1.0）。将来若改成查询检索，必须换来召回提升。"""
    metrics = result["metrics"]
    if metrics["query_insensitivity"] < 1.0:
        assert metrics["recall"]["@3"] >= BASE["recall"]["@3"] + 0.05, (
            "检索开始区分问题了，但 Recall@3 没有实质提升"
        )


def test_baseline_records_retriever_and_date():
    assert BASELINE["recorded_at"]
    assert "retrieve_step_evidence" in BASELINE["retriever"]
    assert BASELINE["corpus"]["chunks"] >= 100
