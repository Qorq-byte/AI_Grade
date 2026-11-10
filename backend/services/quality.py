"""步骤级质量事实与「这份报告要不要复核」的结论（批次 27 = B3）。

设计上刻意**不打分**。交接文档里原写的是「汇总成 quality_score」，取证之后改了：
38 份真实备份里，能支撑打分的台账一半是缺的（`standard_purity` **0%** 落盘、
`step5_coverage` / `step6_table` / `step7_length` 只有 16%），另一小半**恒为满分**
（`document_coverage.used_ratio` 与步骤 5/6 覆盖率在全部样本上都是 1.0）。
在这样的数据上算出一个 0–100 的数，只会给人「78 分」这种精确的错觉 ——
而它既不能排序（分布如此双峰），也不能指导动作。

所以这里只输出三类东西：

1. **每步的字数事实**：`step_results[n].content` 的长度与状态。取数口径只有这一条，
   不猜「内容是否合格」；偏少的判据挂在**该步自己的中位数**上（见 `STEP_MEDIAN_CHARS`）。
2. **可解释的 flags**：每条都带 `id`、`code`、`level`、具体数字，
   并且**只放能改变行为的**（`warn` / `block`）。纯披露项（无佐证计分、步骤被截断）
   用 `info`，不触发复核。
3. **`unavailable`**：台账缺失时明确说「无从判断」。**没数据不等于通过** ——
   它既不会进 `verified`，也不会被算成"查过了没问题"。

`verdict` 因此有四态，而不是三态：

| verdict | 含义 | 前端 |
|---|---|---|
| `review` | 有一条 `warn`/`block` 命中，需要人看一眼 | 「建议复核」 |
| `partial` | 能查的都通过了，但有几项历史上没落盘、无从判断 | 「部分项未验证」 |
| `pass` | 该查的都查了，全部通过 | 不显示 |
| `unknown` | 一条判据都没能跑（空会话、备份损坏） | 「未验证」 |

把 `partial` 单列是因为存量数据：38 份备份里 `standard_purity` **0 份**有值（批次 27
之前没落盘）、`score_disposition` 只有 53%。若把这种情况一律算 `unknown`，
55% 的存量报告会变成一句"不知道"，而这批会话的 14 步产出其实都查过了、也都没问题。
反过来把它算 `pass` 又是撒谎 —— 所以它既不是前者也不是后者。

与 `consistency` 的分工：结构性缺陷（缺章、步骤没跑完、报告过短）由
`consistency` 判，本模块**只把它的 blocking 项搬上来**，不重算、也不重复报
warning 级的口径提示 —— 两套结论互相打架比没有结论更糟。
"""
from typing import Any, Dict, List, Optional

from backend.services import consistency

#: 步骤名（与 `STEP_META` 同义，这里不复用是为了让本模块能被单独导入、不拉起重依赖）
STEP_NAMES = {
    1: "资料清单", 2: "有效资料", 3: "指标体系", 4: "分值分配",
    5: "评分标准", 6: "评价表", 7: "指标分析", 8: "经验做法",
    9: "问题分析", 10: "改进建议", 11: "综合评价", 12: "基本情况",
    13: "工作开展情况", 14: "最终报告",
}

#: 每步正文的中位字数（32 份真实备份实测，`tests/_probe_quality_evidence.py`）。
#: 不用一个数管 14 步：步骤 3 的正文中位数 12,765 字、步骤 12 只有 2,063 字，
#: 同一条「低于 1,000 字算少」的规则会把正常的基本情况判成残次品。
STEP_MEDIAN_CHARS = {
    1: 3493, 2: 3629, 3: 12765, 4: 3162, 5: 8976, 6: 10183, 7: 4898,
    8: 2396, 9: 4294, 10: 6343, 11: 4309, 12: 2063, 13: 2114, 14: 34711,
}

#: 低于该步中位数的这个比例 = 有产出但明显偏少。
#: 0.2 的依据：32 份样本里步骤 7 的 155 字（那次 JSON 断在半截）是唯一的极端值，
#: 155 / 4898 = 3%；其余步骤的最小值都在中位数的 30% 以上。取 0.2 是为了**只抓
#: 断在半截**这类真事故，不去评判「写得不够细」。
THIN_RATIO = 0.20

#: 评分落地率低于此值 → 报告里的分数主要由少数指标支撑（建议复核）。
#: 依据：20 份有台账的会话是明显双峰 —— 11 份 0.04–0.31、8 份 0.97–1.00，
#: 中间只有一个 0.73。0.5 正好落在空档里，不是拍的。
MIN_LANDING_RATIO = 0.5

#: 少于这么多个指标时落地率没有统计意义（3 个里有 1 个落地不等于"报告不可信"）
MIN_LANDING_SAMPLE = 5

#: 触发「建议复核」的级别；`info` 只披露，不触发
REVIEW_LEVELS = ("warn", "block")

#: 「没有结论」的形状。列表页、sitemap 这类批量场景用它兜底 ——
#: 算不出来就说算不出来，**不许降级成 pass**。
UNCHECKED: Dict[str, Any] = {
    "steps": [], "checked": False, "flags": [], "unavailable": [], "verified": [],
    "facts": {}, "verdict": "unknown", "review_required": False, "review_reasons": [],
}

#: 现算结论的缓存：`(路径, mtime, 大小) -> 结论`。
#: 列表页一次要读 N 份备份（实测 1.2 ms/份，38 份 45 ms），没有缓存时每次刷新都重读。
#: 键里带路径是为了让测试换目录时不会命中上一轮的结论。
_CACHE: Dict[str, tuple] = {}
_CACHE_LIMIT = 128


def check_session(session_id: str) -> Dict[str, Any]:
    """读磁盘投影现算一份结论；读不到 / 解析失败时返回 `UNCHECKED`，不抛异常。

    列表页用它：一份坏掉的备份不该让「我的报告」整页 500。
    """
    from backend.agents import performance_agent

    path = performance_agent.STATE_BACKUP_DIR / f"{session_id}.json"
    try:
        stat = path.stat()
    except OSError:
        return dict(UNCHECKED)
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _CACHE.get(session_id)
    if cached and cached[0] == key:
        return cached[1]
    try:
        state = performance_agent.PerformanceAgent.load_state_backup(session_id)
    except Exception:
        return dict(UNCHECKED)
    if not state:
        return dict(UNCHECKED)
    try:
        report = check_quality(state)
    except Exception:
        return dict(UNCHECKED)
    if len(_CACHE) >= _CACHE_LIMIT:
        _CACHE.clear()
    _CACHE[session_id] = (key, report)
    return report


def review_brief(report: Dict[str, Any]) -> Dict[str, Any]:
    """列表页需要的四个字段（完整结论太大，一页几十条会撑爆响应）。"""
    return {
        "checked": report.get("checked", False),
        "verdict": report.get("verdict", "unknown"),
        "required": bool(report.get("review_required")),
        "reasons": list(report.get("review_reasons") or [])[:2],
    }


def _step_texts(state: Dict[str, Any]) -> Dict[int, str]:
    """把 `step_results` 拉平成长度可数的正文。

    键可能是 `int`（运行期状态）也可能是 `str`（备份 JSON）——按 int 直接取会全部落空，
    这是本批取证时踩到的第一个坑（探针第一版对 38 份备份一律返回"无样本"）。
    """
    texts: Dict[int, str] = {}
    for key, item in (state.get("step_results") or {}).items():
        try:
            step = int(key)
        except (TypeError, ValueError):
            continue
        if step not in STEP_NAMES:
            continue
        text = item.get("content") if isinstance(item, dict) else item
        texts[step] = str(text or "")
    return texts


def _expected_steps(state: Dict[str, Any], texts: Dict[int, str]) -> List[int]:
    """该检查哪些步骤。

    报告已生成 → 14 步都该有产出（缺一步就是缺一章，`consistency` 已在管）；
    报告还没生成 → 只看已完成的步骤，否则一个跑到步骤 5 的会话会被判 9 个"缺失"。
    """
    if (state.get("final_report") or "").strip():
        return list(range(1, 15))
    steps = set()
    for item in state.get("completed_steps") or []:
        try:
            step = int(item)
        except (TypeError, ValueError):
            continue
        if step in STEP_NAMES:
            steps.add(step)
    steps |= set(texts)
    return sorted(steps)


def _step_row(step: int, text: str, truncated: bool) -> Dict[str, Any]:
    chars = len(text.strip())
    median = STEP_MEDIAN_CHARS.get(step, 0)
    if not chars:
        status, note = "missing", "这一步还没有内容"
    elif median and chars < median * THIN_RATIO:
        status = "thin"
        note = (f"只有 {chars} 字，不足该步常见篇幅（约 {median} 字）的"
                f" {chars / median * 100:.0f}%，多半是产出被截断或没写完")
    else:
        status, note = "ok", ""
    return {
        "step": step,
        "name": STEP_NAMES.get(step, f"步骤{step}"),
        "chars": chars,
        "status": status,
        "truncated": truncated,
        "note": note,
    }


def _landing(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """评分落地台账。缺失时返回 None —— 调用方要把它当作「无从判断」，不是 0。"""
    disposition = state.get("score_disposition") or {}
    total = disposition.get("total") or 0
    if not total:
        return None
    counts = disposition.get("counts") or {}
    accepted = int(counts.get("accepted", 0) or 0)
    return {
        "total": int(total),
        "accepted": accepted,
        "ratio": round(accepted / total, 3),
        "no_evidence": int(disposition.get("no_evidence_scored") or 0),
        "unmatched": len(disposition.get("unmatched") or []),
        "reasons": {key: value for key, value in counts.items()
                    if key != "accepted" and value},
    }


def _flag(code: str, level: str, message: str, steps: Optional[List[int]] = None
          ) -> Dict[str, Any]:
    """一条可解释的结论。`id` 在最后统一编号（`CODE#序号`），与 `consistency` 同构。"""
    return {"code": code, "level": level, "message": message,
            "steps": list(steps or []), "id": code}


def check_quality(state: Dict[str, Any]) -> Dict[str, Any]:
    """返回 {steps, facts, flags, unavailable, verdict, review_required, checked}。"""
    state = state or {}
    texts = _step_texts(state)
    truncated_steps = {int(step) for step in (state.get("truncated_steps") or [])
                       if str(step).lstrip("-").isdigit()}
    expected = _expected_steps(state, texts)

    steps = [_step_row(step, texts.get(step, ""), step in truncated_steps)
             for step in expected]
    flags: List[Dict[str, Any]] = []
    unavailable: List[Dict[str, str]] = []
    landing = _landing(state)
    purity = state.get("standard_purity") or {}

    has_report = bool((state.get("final_report") or "").strip())
    checked = bool(expected) or has_report
    if not checked:
        return {
            "steps": [], "checked": False, "flags": [], "unavailable": [],
            "verified": [],
            "facts": {"landing": landing, "standard_verdict": purity or None},
            "verdict": "unknown", "review_required": False, "review_reasons": [],
        }

    # 1. 结构性缺陷由 consistency 判，这里只搬运
    if has_report:
        blocking = [item for item in consistency.check_consistency(state).get("issues", [])
                    if item.get("blocking")]
        if blocking:
            detail = "；".join(f"{item['id']} {item['message']}" for item in blocking[:3])
            flags.append(_flag(
                "REPORT_BLOCKED", "block",
                f"报告还不能定稿：{detail}", [14]))

    # 2. 每步产出
    missing = [row["step"] for row in steps if row["status"] == "missing"]
    if missing:
        flags.append(_flag(
            "STEP_MISSING", "warn",
            f"步骤 {missing} 没有产出内容，报告里的对应章节会是空的", missing))
    thin = [row["step"] for row in steps if row["status"] == "thin"]
    if thin:
        detail = "；".join(f"步骤{row['step']}（{row['note']}）"
                          for row in steps if row["status"] == "thin")
        flags.append(_flag("STEP_THIN", "warn", f"有步骤明显偏短：{detail}", thin))

    # 3. 评分落地
    if landing is None:
        if has_report:
            unavailable.append({
                "code": "SCORE_NOT_LANDED", "label": "评分落地",
                "reason": "没有 score_disposition 台账（该会话未跑步骤 7，或来自不落盘的旧版本）",
            })
    elif landing["total"] >= MIN_LANDING_SAMPLE and landing["ratio"] < MIN_LANDING_RATIO:
        reasons = "、".join(f"{key} {value} 项" for key, value in landing["reasons"].items())
        flags.append(_flag(
            "SCORE_NOT_LANDED", "warn",
            f"评分只落地 {landing['accepted']}/{landing['total']} 项"
            f"（{reasons or '其余未落地'}），报告里的分数主要由少数指标支撑", [7]))
    elif landing["no_evidence"]:
        flags.append(_flag(
            "NO_EVIDENCE_SCORED", "info",
            f"{landing['no_evidence']} 项指标按「未取得佐证」计 0 分，"
            f"非实测结果，须在评价局限中说明", [7]))

    # 4. 评分标准是否夹带资料核对结论
    if not purity:
        if has_report:
            unavailable.append({
                "code": "STANDARD_CARRY_VERDICT", "label": "评分标准纯净度",
                "reason": "standard_purity 台账缺失（批次 27 之前未落盘，重启/重放后丢失）",
            })
    elif purity.get("with_verdict"):
        flags.append(_flag(
            "STANDARD_CARRY_VERDICT", "warn",
            f"{purity['with_verdict']}/{purity.get('leaves', 0)} 条评分标准里夹带了"
            f"资料核对结论（如「无法判断」），会让扣分表看起来有依据", [3, 5]))

    # 5. 截断披露：批次 20/21 之后存量 0 例，保留是为了不丢证据
    if truncated_steps:
        flags.append(_flag(
            "STEP_TRUNCATED", "info",
            f"步骤 {sorted(truncated_steps)} 的输出撞到长度上限被截断",
            sorted(truncated_steps)))

    counters: Dict[str, int] = {}
    for item in flags:
        counters[item["code"]] = counters.get(item["code"], 0) + 1
        item["id"] = f"{item['code']}#{counters[item['code']]}"

    # 「跑过的判据」与「跑不了的判据」必须分开列：前者才是"通过"的证据。
    # 两个列表由同一批条件决定（拿不到台账 → 既不进 verified、也不进 unavailable 之外的任何地方），
    # 所以这里不再做一次过滤 —— 用一条用例守住 `verified ∩ unavailable == ∅` 即可。
    verified = ["STEP_CONTENT", "STEP_TRUNCATED"]
    if has_report:
        verified.append("REPORT_BLOCKED")
    if landing is not None:
        verified.append("SCORE_NOT_LANDED")
    if purity:
        verified.append("STANDARD_CARRY_VERDICT")

    review = [item for item in flags if item["level"] in REVIEW_LEVELS]
    if review:
        verdict = "review"
    elif not verified:
        verdict = "unknown"
    elif unavailable:
        verdict = "partial"
    else:
        verdict = "pass"
    return {
        "steps": steps,
        "checked": True,
        "flags": flags,
        "unavailable": unavailable,
        "verified": verified,
        "facts": {
            "steps_total": len(steps),
            "steps_missing": missing,
            "steps_thin": thin,
            "truncated_steps": sorted(truncated_steps),
            "landing": landing,
            "standard_verdict": purity or None,
        },
        "verdict": verdict,
        "review_required": bool(review),
        # 只给前几条：列表页与徽标 tooltip 用得上，整段贴出来没人读
        "review_reasons": [item["message"] for item in review][:4],
    }
