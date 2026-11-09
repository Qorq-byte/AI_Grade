"""步骤 5 / 6 的输出体量与完整性取证（批次 21 用）。

为什么单独量：步骤 7 在批次 20 已经有长度纪律了，但 `token_usage` 显示撞上限
最狠的其实是步骤 5（最大 10,352 token、2 次正好停在 8192）。步骤 6 的产物
（评价指标体系表）既是报告第三章、又是步骤 7 的输入之一，截断代价更大。

量四件事：
  1. 输出字数、结尾是否完整（表格是否收尾 / 是否断在半句）；
  2. 步骤 6 里「Markdown 表格」与「附带的 JSON」各占多少（模板要求写两遍）；
  3. 表格行数 vs 叶子指标数（少没少指标）；
  4. 步骤 5 覆盖到几个指标（按指标名出现次数），结尾是否完整。

用法：
    NO_PROXY='*' .venv/Scripts/python.exe -m tests._probe_step56_length
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.agents.tools import structured_output  # noqa: E402
from backend.agents.tools.questionnaire_gen import indicator_leaves  # noqa: E402

BACKUP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state_backups")


def content_of(data, step):
    node = (data.get("step_results") or {}).get(str(step)) or (data.get("step_results") or {}).get(step) or {}
    text = node.get("content") if isinstance(node, dict) else None
    return text if isinstance(text, str) else ""


def table_rows(text):
    """数 Markdown 表格的数据行（跳过表头与分隔线）。"""
    rows = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        if set(stripped) <= set("|-: "):
            continue
        cells = [c for c in stripped.strip("|").split("|")]
        if len(cells) < 3:
            continue
        if any("一级指标" in c and "二级" not in c for c in cells[:2]):
            continue
        rows += 1
    return rows


def load_all():
    for name in sorted(os.listdir(BACKUP_DIR)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(BACKUP_DIR, name), "r", encoding="utf-8") as fh:
                yield name[:-5], json.load(fh)
        except Exception:  # noqa: BLE001
            continue


def main():
    print(f"{'session':10}{'s5字':>7}{'s5指标':>6}{'s5尾':>5}"
          f"{'s6字':>8}{'表行':>5}{'叶子':>5}{'JSON字':>7}{'s6尾':>5}")
    rows = []
    for sid, data in load_all():
        s5, s6 = content_of(data, 5), content_of(data, 6)
        if not s5 and not s6:
            continue
        leaves = list(indicator_leaves((data.get("indicator_system") or {}).get("level1_indicators") or []))
        names = [str(leaf.get("name") or "") for leaf in leaves if leaf.get("name")]
        covered5 = sum(1 for name in names if name and name in s5)
        blob = structured_output.extract_json(s6) or ""
        try:
            parsed = json.loads(blob) if blob else None
        except Exception:  # noqa: BLE001
            parsed = None
        json_chars = len(blob) if len(blob) > 50 and parsed is not None else 0
        rows.append(dict(
            sid=sid[:8], s5=len(s5), covered5=covered5, leaves=len(leaves),
            s6=len(s6), rows=table_rows(s6), json_chars=json_chars,
            s5_tail=s5.rstrip()[-1:] if s5.strip() else "",
            s6_tail=s6.rstrip()[-1:] if s6.strip() else "",
        ))
    rows.sort(key=lambda r: r["s6"], reverse=True)
    for r in rows:
        print(f"{r['sid']:10}{r['s5']:>7}{r['covered5']:>4}/{r['leaves']:<2}"
              f"{r['s5_tail']:>5}{r['s6']:>8}{r['rows']:>5}{r['leaves']:>5}{r['json_chars']:>7}{r['s6_tail']:>5}")

    s6_rows = [r for r in rows if r["s6"]]
    print()
    print(f"有步骤 6 输出的会话：{len(s6_rows)}")
    print(f"表行数 < 叶子数：{len([r for r in s6_rows if r['rows'] < r['leaves']])}"
          f" -> {[(r['sid'], r['rows'], r['leaves']) for r in s6_rows if r['rows'] < r['leaves']][:8]}")
    print(f"步骤 6 带 JSON 副本：{len([r for r in s6_rows if r['json_chars']])}/{len(s6_rows)}，"
          f"JSON 字数中位 {sorted(r['json_chars'] for r in s6_rows if r['json_chars'])[len([r for r in s6_rows if r['json_chars']])//2] if any(r['json_chars'] for r in s6_rows) else 0}")
    print(f"步骤 6 结尾不是「表/行/句号」这类收尾符："
          f"{[r['sid'] for r in s6_rows if r['s6_tail'] not in ('|', '。', '分', '）', ')', '：', ':')][:10]}")
    s5_rows = [r for r in rows if r["s5"]]
    print(f"有步骤 5 输出的会话：{len(s5_rows)}，覆盖不全（<叶子）："
          f"{len([r for r in s5_rows if r['covered5'] < r['leaves']])}")


if __name__ == "__main__":
    main()
