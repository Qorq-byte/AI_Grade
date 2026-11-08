"""检索评估跑分器。

用法：
    .venv/Scripts/python.exe -m tests.rag_eval.run              # 打印基线
    .venv/Scripts/python.exe -m tests.rag_eval.run --verbose    # 打印每条样本的命中详情
    .venv/Scripts/python.exe -m tests.rag_eval.run --save       # 把当前结果写入 baseline.json
    .venv/Scripts/python.exe -m tests.rag_eval.run --check      # 与 baseline.json 比对，低于基线则退出码 1

为什么要有这个东西：
  线上真正决定「模型看到哪些原文」的是 `workflow_evidence.retrieve_step_evidence`——
  它按**步骤号的固定词表**给每个 480 字窗口打分，取项目资料前 3 条 + 知识库前 2 条。
  在此之前，没有任何东西能回答「检索到底找得到证据吗」。改检索、换切块、调条数之前，
  必须先有一组固定的问题与期望证据，否则「优化」只是换个说法的赌博。

注意：当前检索器**不使用 query**（只吃 step + 步骤词表），所以本跑分测的是
「按步骤取词的基线」。`retrieve` 适配层已经预留了 query 参数，阶段三换成真正的查询检索后，
同一批样本可以直接对比。查询无关性指标（query_insensitivity）就是量化这一点的。
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from backend.services.workflow_evidence import _chunks, retrieve_step_evidence, render_evidence
from tests.rag_eval import corpus, samples

BASELINE_PATH = Path(__file__).with_name("baseline.json")
CUTOFFS = (1, 3, 5)
RETRIEVER = "workflow_evidence.retrieve_step_evidence（BM25 + 中文二元组，查询=步骤意图，项目≤3 条 + 知识≤2 条）"


def _norm(text: str) -> str:
    return "".join((text or "").split()).casefold()


def make_state(docs: Optional[Dict[str, str]] = None) -> dict:
    return {
        "all_raw_texts": dict(docs if docs is not None else corpus.DOCS),
        "indicator_type": "",
        "project_name": "检索评估样例项目",
    }


def retrieve(question: str, step: int, state: dict, include_knowledge: bool = False,
             limit: Optional[int] = None, use_question: bool = False) -> List[dict]:
    """当前检索器的适配层。

    `use_question=False`（默认）复刻线上行为：查询由「步骤意图」构成，与提问无关；
    `use_question=True` 是诊断模式：把问题本身当查询，用来量「如果流程能问出具体问题，
    检索能做到什么程度」——线上并没有这个问题输入。
    """
    return retrieve_step_evidence(
        None, state, step, include_knowledge,
        query=question if use_question else None,
        project_limit=limit,
    )


def _first_relevant_rank(hits: List[dict], anchors: Iterable[Tuple[str, str]]) -> int:
    wanted = [_norm(anchor) for _, anchor in anchors]
    for rank, hit in enumerate(hits, 1):
        text = _norm(hit["text"])
        if any(anchor in text for anchor in wanted):
            return rank
    return 0


def _relevant_count(hits: List[dict], anchors: Iterable[Tuple[str, str]]) -> int:
    wanted = [_norm(anchor) for _, anchor in anchors]
    return sum(1 for hit in hits if any(anchor in _norm(hit["text"]) for anchor in wanted))


def corpus_chunk_count(docs: Optional[Dict[str, str]] = None) -> int:
    docs = docs if docs is not None else corpus.DOCS
    return sum(1 for name, text in docs.items() for _ in _chunks("X", name, text, "project"))


def anchor_covered(anchor: str, docs: Optional[Dict[str, str]] = None) -> bool:
    """锚点是否落在某个块的文本里——不成立说明样本自身写错了，不是检索的锅。"""
    docs = docs if docs is not None else corpus.DOCS
    target = _norm(anchor)
    for name, text in docs.items():
        for chunk in _chunks("X", name, text, "project"):
            if target in _norm(chunk["text"]):
                return True
    return False


def evaluate(verbose: bool = False, limit: Optional[int] = None, use_question: bool = False) -> dict:
    state = make_state()
    rows = []
    for sample in samples.SAMPLES:
        hits = retrieve(sample["question"], sample["step"], state,
                        limit=limit, use_question=use_question)
        rank = _first_relevant_rank(hits, sample["expect"])
        rows.append({
            "id": sample["id"], "step": sample["step"], "question": sample["question"],
            "rank": rank, "returned": len(hits),
            "relevant": _relevant_count(hits, sample["expect"]),
            "hit_ids": [hit["id"] for hit in hits],
            "evidence_chars": sum(len(hit["text"]) for hit in hits),
        })

    total = len(rows)
    metrics = {
        "samples": total,
        "recall": {f"@{k}": round(sum(1 for r in rows if r["rank"] and r["rank"] <= k) / total, 4)
                   for k in CUTOFFS},
        "mrr": round(sum((1 / r["rank"]) if r["rank"] else 0 for r in rows) / total, 4),
        "precision": round(
            sum(r["relevant"] / r["returned"] for r in rows if r["returned"]) /
            max(sum(1 for r in rows if r["returned"]), 1), 4),
        "empty_rate": round(sum(1 for r in rows if not r["returned"]) / total, 4),
        "returned_avg": round(sum(r["returned"] for r in rows) / total, 2),
        "evidence_chars_avg": round(sum(r["evidence_chars"] for r in rows) / total, 1),
    }
    metrics["query_insensitivity"] = _query_insensitivity(rows)
    metrics["citation_verbatim_rate"] = _citation_verbatim_rate(state)

    # 注意：返回条数受 project_limit 限制（线上为 3），所以默认配置下 Recall@5 == Recall@3。
    # 保留 @5 是为了 `--limit 5` 时能看清「条数」这个杠杆的收益。
    per_step = {}
    for step in sorted({r["step"] for r in rows}):
        group = [r for r in rows if r["step"] == step]
        per_step[str(step)] = {
            "samples": len(group),
            "recall@3": round(sum(1 for r in group if r["rank"] and r["rank"] <= 3) / len(group), 4),
            "mrr": round(sum((1 / r["rank"]) if r["rank"] else 0 for r in group) / len(group), 4),
            "empty": sum(1 for r in group if not r["returned"]),
        }

    result = {
        "retriever": RETRIEVER,
        "mode": {"query": "question" if use_question else "step_intent",
                 "project_limit": limit or 3},
        "corpus": {"docs": len(corpus.DOCS), "chunks": corpus_chunk_count(),
                   "chars": sum(len(text) for text in corpus.DOCS.values())},
        "metrics": metrics,
        "per_step": per_step,
        "misses": [{"id": r["id"], "step": r["step"], "question": r["question"], "rank": r["rank"]}
                   for r in rows if not r["rank"]],
    }

    if verbose:
        for row in rows:
            mark = f"rank={row['rank']}" if row["rank"] else "MISS"
            print(f"  {row['id']:<4} step={row['step']:<3} {mark:<9} 返回{row['returned']}条  {row['question']}")
        print()
    return result


def _query_insensitivity(rows: List[dict]) -> float:
    """同一份资料、同一步骤、不同问题 → 返回的证据集合是否完全一致。

    当前实现按步骤取词，同一 step 的任意问题都会得到同一批证据，
    这个值会是 1.0，也就意味着「检索结果与用户问什么无关」。
    """
    by_step: Dict[int, List[dict]] = {}
    for row in rows:
        by_step.setdefault(row["step"], []).append(row)
    pairs = same = 0
    for group in by_step.values():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                pairs += 1
                if group[i]["hit_ids"] == group[j]["hit_ids"]:
                    same += 1
    return round(same / pairs, 4) if pairs else 0.0


def _citation_verbatim_rate(state: dict) -> float:
    """返回的证据文本是否逐字出现在原文里——防的是「检索层编造/改写原文」。"""
    joined = {name: _norm(text) for name, text in state["all_raw_texts"].items()}
    checked = kept = 0
    for sample in samples.SAMPLES:
        for hit in retrieve(sample["question"], sample["step"], state):
            checked += 1
            if _norm(hit["text"]) and _norm(hit["text"]) in joined.get(hit["source"], ""):
                kept += 1
    return round(kept / checked, 4) if checked else 0.0


def _print_report(result: dict) -> None:
    corpus_info = result["corpus"]
    metrics = result["metrics"]
    mode = result.get("mode") or {}
    print(f"检索器：{result['retriever']}")
    print(f"模式：查询={'问题本身（诊断）' if mode.get('query') == 'question' else '步骤意图（线上）'}"
          f"｜项目证据上限 {mode.get('project_limit')} 条")
    print(f"语料：{corpus_info['docs']} 份文档 / {corpus_info['chunks']} 个块 / {corpus_info['chars']} 字")
    print(f"样本：{metrics['samples']} 条\n")
    print("指标")
    print(f"  Recall@1 = {metrics['recall']['@1']:.3f}   "
          f"Recall@3 = {metrics['recall']['@3']:.3f}   Recall@5 = {metrics['recall']['@5']:.3f}")
    print(f"  MRR      = {metrics['mrr']:.3f}    "
          f"精确率 = {metrics['precision']:.3f}（返回条目中真正命中的比例）")
    print(f"  空结果率 = {metrics['empty_rate']:.3f}    平均返回 {metrics['returned_avg']} 条 / "
          f"{metrics['evidence_chars_avg']} 字")
    print(f"  引用逐字率 = {metrics['citation_verbatim_rate']:.3f}")
    print(f"  查询无关性 = {metrics['query_insensitivity']:.3f}"
          f"（1.0 = 同一步骤不同问题拿到完全相同的证据）\n")
    print("按步骤")
    for step, row in result["per_step"].items():
        print(f"  step {step:>2}: 样本 {row['samples']}   Recall@3={row['recall@3']:.2f}   "
              f"MRR={row['mrr']:.2f}   空结果 {row['empty']}")
    if result["misses"]:
        print(f"\n未命中 {len(result['misses'])} 条")
        for miss in result["misses"]:
            print(f"  {miss['id']} (step {miss['step']}): {miss['question']}")


def _check_against_baseline(result: dict) -> int:
    if not BASELINE_PATH.exists():
        print("没有 baseline.json，先跑 --save")
        return 2
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    base_metrics = baseline["metrics"]
    current = result["metrics"]
    regressions = []
    for key in ("recall", "mrr", "precision", "citation_verbatim_rate"):
        if key == "recall":
            for cutoff, value in base_metrics["recall"].items():
                if current["recall"][cutoff] < value:
                    regressions.append(f"Recall{cutoff}: {value} → {current['recall'][cutoff]}")
            continue
        if current[key] < base_metrics[key]:
            regressions.append(f"{key}: {base_metrics[key]} → {current[key]}")
    if current["empty_rate"] > base_metrics["empty_rate"]:
        regressions.append(f"empty_rate: {base_metrics['empty_rate']} → {current['empty_rate']}")
    if regressions:
        print("低于基线：")
        for item in regressions:
            print(f"  - {item}")
        return 1
    print("与基线一致或不低于基线。")
    return 0


def diagnostics() -> List[dict]:
    """2×2 诊断：瓶颈在「排序」「条数」还是「查询」？

    - 步骤意图 + 3 条：线上现状
    - 步骤意图 + 10 条：放开条数能拿回多少（纯排序质量的上界）
    - 问题本身 + 3 条：如果流程能问出具体问题
    - 问题本身 + 10 条：两者都放开
    """
    grid = []
    for use_question in (False, True):
        for limit in (3, 10):
            evaluation = evaluate(limit=limit, use_question=use_question)
            grid.append({
                "query": "question" if use_question else "step_intent",
                "project_limit": limit,
                "recall@3": evaluation["metrics"]["recall"]["@3"],
                "recall@5": evaluation["metrics"]["recall"]["@5"],
                "mrr": evaluation["metrics"]["mrr"],
                "precision": evaluation["metrics"]["precision"],
            })
    return grid


def _print_grid(grid: List[dict]) -> None:
    print("\n诊断（瓶颈定位）")
    print("  查询       条数   Recall@3  Recall@5  MRR     精确率")
    for row in grid:
        label = "问题本身" if row["query"] == "question" else "步骤意图"
        print(f"  {label:<8} {row['project_limit']:>4}   {row['recall@3']:.3f}     "
              f"{row['recall@5']:.3f}     {row['mrr']:.3f}   {row['precision']:.3f}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="检索评估基线")
    parser.add_argument("--verbose", action="store_true", help="打印逐条样本命中情况")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    parser.add_argument("--save", action="store_true", help="写入 baseline.json")
    parser.add_argument("--check", action="store_true", help="与 baseline.json 比对")
    parser.add_argument("--limit", type=int, default=None, help="项目证据条数（默认与线上一致：3）")
    parser.add_argument("--question", action="store_true", help="诊断模式：把问题本身当查询")
    parser.add_argument("--grid", action="store_true", help="打印 2×2 诊断表")
    args = parser.parse_args(argv)

    # 样本自检：锚点必须能被切块覆盖，否则是样本写错了，不是检索的锅
    for sample in samples.SAMPLES:
        for doc, anchor in sample["expect"]:
            if not anchor_covered(anchor):
                print(f"样本 {sample['id']} 的锚点无法被任何块覆盖：{doc} / {anchor}")
                return 3

    result = evaluate(verbose=args.verbose, limit=args.limit, use_question=args.question)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_report(result)

    if args.grid:
        _print_grid(diagnostics())

    if args.save:
        payload = {"recorded_at": date.today().isoformat(), **result}
        # 覆盖基线时把上一版留在 previous 里：改造前后的数字要能对照，不能只剩一个最新值
        if BASELINE_PATH.exists():
            try:
                old = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                old = {}
            if old.get("metrics") and old.get("metrics") != result["metrics"]:
                payload["previous"] = {
                    "recorded_at": old.get("recorded_at"),
                    "retriever": old.get("retriever"),
                    "metrics": old["metrics"],
                }
                if old.get("previous"):
                    payload["previous"]["previous"] = old["previous"]
        payload["diagnostics"] = diagnostics()
        BASELINE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\n已写入 {BASELINE_PATH.name}")
    if args.check:
        return _check_against_baseline(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
