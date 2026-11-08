"""批次 18 探针：把 29 份真实备份的指标体系过一遍新的 purity 审计。

要回答四个问题：
  1. 审计是不是盲的——能不能数出叶子（分母），能不能数出命中（分子）；
  2. 口径对不对——命中集合是否覆盖「待核实/无法判断/资料不足」的朴素统计，
     多出来的部分必须能指认是哪些长尾词；
  3. 命中条目分几种形态：字段整栏只有结论（最严重）/ 标准后面挂了一句结论 /
     把「资料不足怎么办」写成了规则分支（仍属违规，但性质不同）；
  4. 有没有真的误报——即含标记词、但整段读下来是一句完整判分规则的条目，
     这类需要人工看，探针只把它们挑出来。

用法：python tests/_probe_standard_purity.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from backend.agents.performance_agent import (  # noqa: E402
    STANDARD_VERDICT_MARKERS, audit_standard_purity,
)
from backend.agents.tools.questionnaire_gen import indicator_leaves  # noqa: E402

PLAIN = ("待核实", "无法判断", "资料不足")
# 判分用语：出现其一，说明这一栏除了结论之外还写了「尺子」
RULE_HINT = ("满分", "扣", "档", "按", "计分", "评分", "×", "比例", "达到", "不低于", "以上")
# 把「资料不足怎么办」写成规则分支的特征说法
BRANCH_HINT = ("时标注", "时按", "时视为", "暂按", "不计入", "按无法判断处理", "按待核实处理")


def classify(std):
    if any(hint in std for hint in BRANCH_HINT):
        return "branch"
    if any(hint in std for hint in RULE_HINT):
        return "mixed"
    return "only_verdict"


def main():
    files = sorted((ROOT / "state_backups").glob("*.json"))
    total_leaves = total_hits = 0
    blind, mismatch, longtail_rows = [], [], []
    buckets = {"only_verdict": [], "mixed": [], "branch": []}

    for path in files:
        state = json.loads(path.read_text(encoding="utf-8"))
        system = state.get("indicator_system") or {}
        rows = list(indicator_leaves(system.get("level1_indicators") or []))
        if not rows:
            continue
        sid = path.stem[:8]
        purity = audit_standard_purity(system)
        plain = {leaf.get("id") for leaf in rows
                 if any(w in str(leaf.get("scoring_standard") or "") for w in PLAIN)}

        total_leaves += purity["leaves"]
        total_hits += purity["with_verdict"]
        if purity["leaves"] != len(rows):
            blind.append((sid, purity["leaves"], len(rows)))

        strict = set(purity["ids"])
        if not plain <= strict:
            mismatch.append((sid, sorted(plain - strict)))
        for leaf in rows:
            if leaf.get("id") in strict - plain:
                longtail_rows.append((sid, leaf.get("id"),
                                      str(leaf.get("scoring_standard"))[:70]))
            if leaf.get("id") in strict:
                std = str(leaf.get("scoring_standard") or "")
                buckets[classify(std)].append((sid, leaf.get("id"), std[:90]))

    print(f"备份 {len(files)} 份，叶子合计 {total_leaves}，命中 {total_hits}"
          f"（{round(total_hits / total_leaves, 3) if total_leaves else 0}）")
    print(f"词表：{list(STANDARD_VERDICT_MARKERS)}")
    print(f"数不出叶子（审计盲）：{blind or '无'}")
    print(f"漏掉朴素口径已命中的条目：{mismatch or '无'}")
    print(f"仅由长尾词命中：{len(longtail_rows)} 条 {longtail_rows[:3]}")
    for name, label in (("only_verdict", "字段整栏只有结论"),
                        ("branch", "把「资料不足」写成规则分支"),
                        ("mixed", "标准后挂一句结论")):
        print(f"  {label}：{len(buckets[name])} 条")
    print("\n各桶抽样（人工判是否误报）：")
    for name in ("only_verdict", "branch", "mixed"):
        for row in buckets[name][:2]:
            print(f"  [{name}] {row[0]} {row[1]}: {row[2]}")
    return 0 if not (blind or mismatch) else 1


if __name__ == "__main__":
    raise SystemExit(main())
