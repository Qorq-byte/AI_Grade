"""探针：盘清「质量证据」的存量底子，供批次 27 = B3 质量门禁定判据。

做四件事，全部离线重放磁盘上的真实备份，不调模型、不花钱：

1. **台账可用率**：`standard_purity` / `step5_coverage` / `step6_table` /
   `score_disposition` / `document_coverage` / `truncated_steps` 这些字段
   到底有多少会话真有值 —— 没有的话质量门禁就只能靠重算，那就变成另一套检测。
2. **分布**：评分落地率、无佐证计分、资料直接引用率、标准夹带结论率的
   分位数。**阈值必须从这张表里读出来，不许拍。**
3. **每步产出字数**：找出「结构完整但接近空」的步骤（门禁要能区分
   「写得短」与「没生成」）。
4. **成本**：在列表页给 N 个会话现算质量结论要多久，决定能不能放进 `/reports`。

用法：
    python -m tests._probe_quality_evidence
    python -m tests._probe_quality_evidence --dir state_backups
"""
from __future__ import annotations

import argparse
import statistics
import time
from pathlib import Path

from backend.agents import performance_agent as pa

LEDGERS = (
    "standard_purity",
    "step5_coverage",
    "step6_table",
    "score_disposition",
    "document_coverage",
    "truncated_steps",
    "step7_length",
    "consistency",
)

STEP_NAMES = {
    1: "资料清单", 2: "有效资料", 3: "指标体系", 4: "分值分配",
    5: "评分标准", 6: "评价表", 7: "指标分析", 8: "经验做法",
    9: "问题分析", 10: "改进建议", 11: "综合评价", 12: "基本情况",
    13: "工作开展情况", 14: "最终报告",
}


def _pct(values, ratio):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * ratio))))
    return ordered[index]


def _line(label, values, unit=""):
    if not values:
        print(f"  {label:<22} 无样本")
        return
    print(f"  {label:<22} n={len(values):<3} "
          f"min={min(values):g}{unit} p25={_pct(values, 0.25):g}{unit} "
          f"中位={statistics.median(values):g}{unit} p75={_pct(values, 0.75):g}{unit} "
          f"max={max(values):g}{unit}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default="state_backups")
    args = parser.parse_args()

    files = sorted(Path(args.dir).glob("*.json"))
    print(f"扫描 {args.dir}：{len(files)} 份备份\n")
    if not files:
        return 1

    have_report = 0
    ledger_hits = {key: 0 for key in LEDGERS}
    landing_ratios, no_evidence, purity_ratios, used_ratios = [], [], [], []
    cover5, cover6, truncated_counts = [], [], []
    landing_pairs = []
    step_chars = {step: [] for step in STEP_NAMES}
    missing_steps = {step: 0 for step in STEP_NAMES}

    t0 = time.perf_counter()
    for path in files:
        state = pa.PerformanceAgent.load_state_backup(path.stem)
        if not state:
            continue
        for key in LEDGERS:
            value = state.get(key)
            # 「字段缺失」与「值是空容器」是两件事：后者说明算过了、结果就是 0
            if value not in (None, ""):
                ledger_hits[key] += 1
        if not (state.get("final_report") or "").strip():
            continue
        have_report += 1

        disp = state.get("score_disposition") or {}
        total = disp.get("total") or 0
        if total:
            ratio = (disp.get("counts") or {}).get("accepted", 0) / total
            landing_ratios.append(ratio)
            landing_pairs.append((path.stem, total, disp.get("counts") or {},
                                  disp.get("no_evidence_scored") or 0))
            no_evidence.append(disp.get("no_evidence_scored") or 0)

        purity = state.get("standard_purity") or {}
        if purity.get("leaves"):
            purity_ratios.append(purity.get("with_verdict", 0) / purity["leaves"])

        coverage = state.get("document_coverage") or {}
        if isinstance(coverage, dict) and coverage.get("total_chars"):
            used_ratios.append(float(coverage.get("used_ratio") or 0))

        for key, bucket in (("step5_coverage", cover5), ("step6_table", cover6)):
            item = state.get(key) or {}
            if item.get("leaves"):
                bucket.append(item.get("covered", 0) / item["leaves"])

        truncated_counts.append(len(state.get("truncated_steps") or []))

        # 备份里 step_results 的键是 JSON 字符串，直接 .get(int) 永远取不到
        results = {int(k): v for k, v in (state.get("step_results") or {}).items()
                   if str(k).isdigit()}
        for step in STEP_NAMES:
            item = results.get(step) or {}
            text = item.get("content") if isinstance(item, dict) else ""
            if not str(text or "").strip():
                missing_steps[step] += 1
            else:
                step_chars[step].append(len(str(text)))
    elapsed = time.perf_counter() - t0

    print(f"有报告的会话：{have_report}/{len(files)}")
    print(f"读盘 + 处理总耗时：{elapsed * 1000:.0f} ms"
          f"（平均 {elapsed / max(1, len(files)) * 1000:.1f} ms/份）\n")

    print("一、台账可用率（有值的会话数 / 全部会话）")
    for key in LEDGERS:
        hit = ledger_hits[key]
        print(f"  {key:<20} {hit:>3}/{len(files)}  ({hit / len(files) * 100:.0f}%)")

    print("\n二、分布（只在有报告的会话里统计）")
    _line("评分落地率", [round(x, 3) for x in landing_ratios])
    _line("无佐证计分(项)", no_evidence)
    _line("标准夹带结论率", [round(x, 3) for x in purity_ratios])
    _line("资料直接引用率", [round(x, 3) for x in used_ratios])
    _line("步骤5覆盖叶子率", [round(x, 3) for x in cover5])
    _line("步骤6覆盖叶子率", [round(x, 3) for x in cover6])
    _line("被截断步骤数", truncated_counts)

    print("\n三、逐会话的评分落地（看是不是双峰）")
    for sid, total, counts, no_ev in sorted(landing_pairs, key=lambda item: item[1]):
        accepted = counts.get("accepted", 0)
        ratio = accepted / total if total else 0
        reasons = "、".join(f"{k}={v}" for k, v in counts.items()
                           if k != "accepted" and v)
        bar = "#" * int(round(ratio * 20))
        print(f"  {sid[:8]} {accepted:>3}/{total:<3} {ratio:>5.2f} {bar:<20} "
              f"无佐证={no_ev:<3} {reasons}")

    print("\n四、每步产出字数（只统计有内容的会话）")
    for step, name in STEP_NAMES.items():
        values = step_chars[step]
        if not values:
            print(f"  步骤{step:<2} {name:<8} 无样本（缺失 {missing_steps[step]} 份）")
            continue
        print(f"  步骤{step:<2} {name:<8} 缺失 {missing_steps[step]:>2} 份  "
              f"min={min(values):<6} p25={_pct(values, 0.25):<6} "
              f"中位={int(statistics.median(values)):<6} max={max(values)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
