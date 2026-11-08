"""探针：缩水闸门换基准后，历史备份上的判定变了吗？

Batch 14 把基准从「正则数正文里提到的编号」换成「原文 JSON 里的真实条目」，
并加了「可计分数值分不得减少」这条判据。换基准最容易出的事是**顺手改了历史结论**，
所以拿全部历史备份跑一遍新旧两套规则，逐条对比。

同时演示两个旧规则的盲区（这两条历史数据里没有，只能构造）：
  A. 编号写法一变，旧规则基准恒为空 → 直接放行；
  B. 条目数不变、分值全变「待核实」，旧规则放行。

用法：python tests/_probe_shrink_guard.py
"""
import glob
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.agents.tools import structured_output as so  # noqa: E402


def old_rule(original_text: str, repaired) -> bool:
    """Batch 13 的判定，原样复现（正则数提及 vs 修复后键数）。"""
    before = so.mentioned_indicator_ids(original_text)
    if not before:
        return False
    scores = (repaired.payload or {}).get("scores")
    after = set(scores) if isinstance(scores, dict) else set()
    return len(after) < len(before)


def main() -> int:
    rows, drift = [], 0
    for path in sorted(glob.glob(str(ROOT / "state_backups" / "*.json"))):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        step7 = (data.get("step_results") or {}).get("7") or {}
        text = step7.get("content") or ""
        if not text:
            continue
        repaired = so.parse(7, text)
        report = so.shrink_report(7, text, repaired)
        # 历史备份里 content 已经被修复覆盖，拿它当「原文」只能验证口径一致性；
        # 真正的原始正文只有 9141a214 那份 raw_content 有。
        old = old_rule(text, repaired)
        rows.append((os.path.basename(path)[:8], step7.get("structured_status"),
                     report.before.entries, report.before.numeric,
                     report.after.entries, report.after.numeric, old, report.shrunk))
        if old != report.shrunk:
            drift += 1

    print(f"{'备份':10s} {'状态':14s} {'基准条目':>8s} {'基准分':>6s} "
          f"{'修复条目':>8s} {'修复分':>6s} {'旧判定':>6s} {'新判定':>6s}")
    for name, status, be, bn, ae, an, old, new in rows:
        flag = "  ← 变了" if old != new else ""
        print(f"{name:10s} {str(status):14s} {be:8d} {bn:6d} {ae:8d} {an:6d} "
              f"{str(old):>6s} {str(new):>6s}{flag}")
    print(f"\n{len(rows)} 份备份，判定发生变化：{drift} 份")

    print("\n=== 旧规则的两个盲区（构造用例）===")
    # A. 编号写法改变：旧规则基准为空 → 放行
    alt_ids = [f"ZB-{i:02d}" for i in range(1, 13)]
    alt_text = "".join(f"**{i}**：2 分\n" for i in alt_ids) + "\n```json\n" + json.dumps(
        {"scores": {i: {"actual_score": 2.0, "evidence_id": "P1-10-1", "reason": "r"}
                    for i in alt_ids[:3]}}, ensure_ascii=False) + "\n```\n"
    shrunk_payload = so.parse(7, json.dumps({"scores": {i: {"actual_score": 2.0}} for i in alt_ids[:3]}))
    print(f"A 编号风格改为 ZB-xx：旧判定={old_rule(alt_text, shrunk_payload)} "
          f"新判定={so.shrink_report(7, alt_text, shrunk_payload).shrunk}"
          f"（基准 {so.coverage_of_text(alt_text).entries} 条）")

    # B. 分值被清零：条目数不变
    ids = [f"L3_01_01_{i:02d}" for i in range(1, 6)]
    base = {"scores": {i: {"actual_score": 2.0, "evidence_id": "P1-10-1", "reason": "r"} for i in ids}}
    wiped = {"scores": {i: {"actual_score": "待核实"} for i in ids}}
    wiped_outcome = so.parse(7, json.dumps(wiped, ensure_ascii=False))
    print(f"B 5 条分值全变待核实：旧判定={old_rule(json.dumps(base), wiped_outcome)} "
          f"新判定={so.shrink_report(7, json.dumps(base), wiped_outcome).shrunk}"
          f"（{so.shrink_report(7, json.dumps(base), wiped_outcome).reason}）")

    # 真实首次正文 vs 修复后（唯一一份有 raw_content 的）
    real = ROOT / "state_backups" / "9141a214-ad3f-45f5-b2aa-a853e8d54d4c.json"
    if real.exists():
        data = json.loads(real.read_text(encoding="utf-8"))
        step7 = data["step_results"]["7"]
        report = so.shrink_report(7, step7["raw_content"], so.parse(7, step7["content"]))
        print(f"\n真实对照（9141a214）：首次 {report.before.entries} 条 / "
              f"{report.before.numeric} 个数值分 → 修复后 {report.after.entries} 条 / "
              f"{report.after.numeric} 个 → 新判定 shrunk={report.shrunk}（不能误伤）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
