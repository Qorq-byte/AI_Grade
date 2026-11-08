"""
结构化输出：形状校验 + 一次修复重试

为什么需要：
  步骤 3（指标体系）、4（分值分配）、7（指标评分）都要求模型输出 JSON。此前这三个节点用
  `json.loads(self._extract_json(content))` 硬解，失败就 `except ValueError` 静默回落成
  空字典，接着下游默默用上一版数据继续跑——用户看到的是一份「看起来正常、其实步骤没生效」的报告。

本模块把「形状」当作可判定的工程问题：
  1. 校验分两级：**阻断性错误**（JSON 解析不出来 / 顶层结构不对 / 一条可用条目都没有）
     与**非阻断问题**（个别条目缺字段、类型不符——这些条目本来就会被下游丢弃）。
  2. 只有阻断性错误才触发**一次**修复重试：把具体错误 + 原始输出回喂模型，要求只重出 JSON。
  3. 修复后仍不合规，返回 `manual_review`：调用方把「待人工复核」写进步骤内容与 warnings，
     **原始输出完整保留**（step_results 里仍有全文），不静默降级、不靠正则硬凑。

状态语义（返回值 StructuredOutcome.status）：
  - `ok`            ：一次通过
  - `repaired`      ：首次不合规，修复后通过
  - `manual_review` ：两次都不合规，交人工
"""
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from backend.services import evidence_anchor

from loguru import logger

# 只把前若干条错误回喂模型，避免修复提示词过长反而干扰输出
MAX_REPORTED_ERRORS = 5
# 回喂的原始输出上限。太小会让模型「看不到全部条目 → 只能写看得到的」，
# 步骤 7 的正文实测可达 8,000+ 字，用 6000 必然截断，所以按步骤给不同上限。
# Batch 17 量了 26 份备份：步骤 3 的正文 7,939–15,431 字、步骤 4 最长 5,841 字。
# 这两步一旦扩了缩水闸门，截断就会变成「机械缩水」——修复看着半截写，闸门必然拒收，
# 用户拿到的是「待人工复核」而不是「修好了」。同一个理由，同一套做法。
MAX_ECHO_CHARS = 6000
MAX_ECHO_CHARS_BY_STEP = {3: 24000, 4: 12000, 7: 24000}

# 三级指标 id：L3_01_01_01。前后不能紧邻字母数字，避免从长串里截半截。
_INDICATOR_ID = re.compile(r"(?<![0-9A-Za-z])(L\d_\d{2}_\d{2}_\d{2})(?![0-9])")

SCHEMA_TITLES = {3: "三级绩效评价指标体系", 4: "指标分值分配", 7: "指标评分明细"}

SCHEMA_HINTS = {
    3: (
        '{"indicator_type": "项目支出指标体系", "level1_indicators": ['
        '{"id": "L1_01", "level": 1, "name": "一级指标", "order": 1, "score": 20, "children": ['
        '{"id": "L2_01_01", "level": 2, "name": "二级指标", "parent_id": "L1_01", "score": 10, '
        '"children": [{"id": "L3_01_01_01", "level": 3, "name": "三级指标", "parent_id": "L2_01_01", '
        '"score": 10, "explanation": "指标解释", "scoring_standard": "评分标准", "children": []}]}]}]}'
    ),
    4: '{"scores": {"L1_01": 20, "L2_01_01": 10, "L3_01_01_01": 5}, "allocation_rationale": "分配思路"}',
    7: '{"scores": {"L3_01_01_01": {"actual_score": 8, "evidence_id": "P1-1-1", "reason": "依据说明"}}}',
}


@dataclass
class StructuredOutcome:
    """一次结构化输出的判定结果"""

    step: int
    payload: Optional[Dict[str, Any]]
    status: str
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    raw: str = ""
    # 修复前的首次输出。修复成功时 raw 会被替换成修复结果，
    # 没有这一份就再也查不到「模型第一次到底写了什么」——实测丢过一次。
    original: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "repaired")

    def summary(self) -> str:
        if self.errors:
            return "；".join(self.errors[:MAX_REPORTED_ERRORS])
        return "输出格式不符合要求"


# ═══════════════════════════════════════════════════════
#  基础工具
# ═══════════════════════════════════════════════════════

def extract_json(text: str) -> Optional[str]:
    """从模型输出里取出 JSON 文本：优先代码块，其次首个 { 到最后一个 }。"""
    if not text:
        return None
    fenced = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if fenced:
        blob = fenced.group(1).strip()
        if blob:
            return blob
    start = text.find('{')
    end = text.rfind('}')
    if start >= 0 and end > start:
        return text[start:end + 1]
    return None


def _coerce_number(value: Any) -> Optional[float]:
    """把 20 / "20" / "20分" / "1,200" / "20%" 统一成 float；布尔与其它一律视为非法。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "").replace("，", "")
        cleaned = re.sub(r"(分|%)$", "", cleaned).strip()
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _unwrap(payload: Any, wrappers: Tuple[str, ...]) -> Any:
    """容忍模型多包一层：{"indicator_system": {...}} / {"data": {...}}"""
    if not isinstance(payload, dict):
        return payload
    for key in wrappers:
        inner = payload.get(key)
        if isinstance(inner, dict):
            return inner
    return payload


def _as_children(node: Dict[str, Any]) -> List[Any]:
    children = node.get("children")
    if children is None:
        return []
    return children if isinstance(children, list) else []


# ═══════════════════════════════════════════════════════
#  分步校验
# ═══════════════════════════════════════════════════════

def _validate_indicators(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    payload = _unwrap(payload, ("indicator_system", "data", "result"))
    nodes = payload.get("level1_indicators")
    if not isinstance(nodes, list):
        return payload, ["缺少 level1_indicators 数组（或类型不是数组）"], warnings
    if not nodes:
        return payload, ["level1_indicators 为空"], warnings

    def walk(items: List[Any], depth: int, path: str) -> int:
        valid = 0
        for index, node in enumerate(items):
            where = f"{path}[{index}]"
            if not isinstance(node, dict):
                warnings.append(f"{where} 不是对象，已忽略")
                continue
            missing = [key for key in ("id", "name") if not str(node.get(key) or "").strip()]
            if missing:
                warnings.append(f"{where} 缺少 {'/'.join(missing)}，已忽略")
                continue
            valid += 1
            children = _as_children(node)
            if children and depth < 3:
                walk(children, depth + 1, where + ".children")
            elif children and depth >= 3:
                warnings.append(f"{where} 层级超过三层，下级已忽略")
        return valid

    valid_level1 = walk(nodes, 1, "level1_indicators")
    if valid_level1 == 0:
        errors.append("没有任何一级指标同时具备 id 与 name")
    return payload, errors, warnings


def _validate_scores(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    payload = _unwrap(payload, ("score_allocation", "data", "result"))
    if "scores" in payload and not isinstance(payload.get("scores"), dict):
        return payload, ["scores 的类型不是对象"], warnings
    scores = payload.get("scores")
    if scores is None:
        # 允许直接给映射 {"L1_01": 20, ...}
        if payload and all(isinstance(k, str) for k in payload) and any(
            _coerce_number(v) is not None for v in payload.values()
        ):
            scores = payload
        else:
            return payload, ["缺少 scores 对象（或类型不是对象）"], warnings
    if not scores:
        return payload, ["scores 为空"], warnings

    numeric = 0
    for key, value in scores.items():
        number = _coerce_number(value)
        if number is None:
            warnings.append(f"scores['{key}'] 不是数字，已忽略")
            continue
        numeric += 1
    if numeric == 0:
        errors.append("scores 中没有任何可解析的数值分值")
    return payload, errors, warnings


def _validate_score_items(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """步骤 7 只校验「形状」，不校验「内容是否够用」。

    实跑发现模型在缺少可核实证据时会返回 `{"scores": {}}`——这是**合法结果**（提示词要求
    「没有依据的指标写待核实，不输出分值」），不是格式错误，不该触发修复重试。
    同理，条目缺少 evidence_id / reason 的，下游本来就会丢弃，这里只记 warning。

    Batch 11 修正：`actual_score` 写成「待核实」的条目同样是**按提示词要求作出的合法拒绝**。
    实测一轮真实运行里 16/23 条是这种写法，原先逐条记「不是数字，已忽略」，
    对外变成「有 16 条输出未进入结构化结果」——而 `structured_status` 同时是 `ok`，
    自相矛盾，也让人误以为系统丢了真实评分。现在这种写法只计数、不再逐条告警。
    """
    errors: List[str] = []
    warnings: List[str] = []
    payload = _unwrap(payload, ("indicator_scores", "data", "result"))
    if "scores" in payload and not isinstance(payload.get("scores"), dict):
        return payload, ["scores 的类型不是对象"], warnings
    scores = payload.get("scores")
    if scores is None:
        return payload, ["缺少 scores 对象（或类型不是对象）"], warnings
    if not scores:
        warnings.append("scores 为空（模型未给出任何可核实分值），按「待核实」处理")
        return payload, errors, warnings

    usable = 0
    pending = 0
    for key, item in scores.items():
        if not isinstance(item, dict):
            warnings.append(f"scores['{key}'] 不是对象，已忽略")
            continue
        if _coerce_number(item.get("actual_score")) is None:
            if evidence_anchor.classify_score(item.get("actual_score")) == "pending":
                pending += 1
                continue
            warnings.append(f"scores['{key}'].actual_score 不是数字，已忽略")
            continue
        if not str(item.get("evidence_id") or "").strip():
            warnings.append(f"scores['{key}'] 缺少 evidence_id，已忽略")
            continue
        if not str(item.get("reason") or "").strip():
            warnings.append(f"scores['{key}'] 缺少 reason，已忽略")
            continue
        usable += 1
    if usable == 0:
        warnings.append(
            f"scores 中没有可用的数值分值（{pending} 项标注待核实）"
            if pending else "scores 中没有任何同时具备 actual_score/evidence_id/reason 的条目"
        )
    return payload, errors, warnings


_VALIDATORS = {3: _validate_indicators, 4: _validate_scores, 7: _validate_score_items}


def parse(step: int, text: str) -> StructuredOutcome:
    """解析并校验某一步的输出；不改写 payload，判定只用于决定是否修复/交人工。"""
    raw = text or ""
    validator = _VALIDATORS.get(step)
    if validator is None:
        return StructuredOutcome(step, None, "ok", [], [], raw)

    blob = extract_json(raw)
    if not blob:
        if "{" in raw:
            return StructuredOutcome(
                step, None, "manual_review",
                ["JSON 解析失败（输出被截断：只有 { 没有配对的 }）"], [], raw,
            )
        return StructuredOutcome(step, None, "manual_review", ["输出中未找到 JSON 对象"], [], raw)
    try:
        payload = json.loads(blob)
    except ValueError as exc:
        hint = ""
        if "Unterminated string" in str(exc) or "Expecting" in str(exc):
            hint = "（常见于输出被长度上限截断）"
        return StructuredOutcome(
            step, None, "manual_review", [f"JSON 解析失败{hint}：{exc}"], [], raw
        )
    if not isinstance(payload, dict):
        return StructuredOutcome(step, None, "manual_review", ["JSON 顶层不是对象"], [], raw)

    payload, errors, warnings = validator(payload)
    status = "manual_review" if errors else "ok"
    return StructuredOutcome(step, payload, status, errors, warnings, raw)


def mention_limit(step: int) -> int:
    """回喂原文的长度上限（按步骤）。"""
    return MAX_ECHO_CHARS_BY_STEP.get(step, MAX_ECHO_CHARS)


def mentioned_indicator_ids(text: str) -> set:
    """文本里出现过的三级指标 id（去重）。

    用途只是**兜底**：当原输出里解析不出 JSON 时，用它粗略估一下覆盖了哪些指标。
    真正的缩水基准是 `coverage_of_text` 里的 JSON 条目——编号写法一变，本函数会
    静默返回空集，靠它当基准等于让闸门跟着编号规则一起失效（Batch 14 修正）。
    """
    return set(_INDICATOR_ID.findall(str(text or "")))


# ═══════════════════════════════════════════════════════
#  缩水判定（Batch 13 建立，Batch 14 补齐两处盲区）
#  Batch 13 只看「原文提到几个 id」与「修复后有几个键」。两个失效方向：
#    ① 编号写法一变，`mentioned_indicator_ids` 恒为空集 → 直接放行（不报错）；
#    ② 键数没变、分值全变成「待核实」也算没缩水（实测 effa217d 修完 11 条 0 分值）。
# ═══════════════════════════════════════════════════════

@dataclass(frozen=True)
class Coverage:
    """一次输出「覆盖了几个条目、其中几个真的带了可计分数值」。

    两种形状的「条目」不同，这是本批（Batch 17）必须写清的口径：
      * `scores`（步骤 4 / 7）：`scores` 映射的键数；
      * `tree`（步骤 3）：**叶子指标数**——叶子 = 没有 children 的节点，
        按结构判定而不是按 `level == 3`，因为模型偶尔把层级标错（实测有 level=2 的叶子）。
    """

    entries: int
    numeric: int
    source: str  # json / mention / none
    shape: str = "scores"  # scores / tree / mention / none


_EMPTY_COVERAGE = Coverage(0, 0, "none", "none")
_SOURCE_LABEL = {
    "json": "原输出里的 JSON 条目",
    "mention": "正文里提到的编号（原输出没有可用 JSON）",
    "none": "（无基准）",
}
_ENTRY_LABEL = {"tree": "叶子指标", "scores": "条目"}
_INDICATOR_KEY_ALIASES = ("indicator_id", "id", "key")
_TREE_KEY = "level1_indicators"


def _iter_leaves(nodes):
    """深度优先取叶子：没有 children 的节点（空列表也算叶子）。"""
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        children = _as_children(node)
        if children:
            yield from _iter_leaves(children)
        else:
            yield node


def _tree_coverage(payload) -> Optional[Coverage]:
    """步骤 3 的形状：按叶子指标计数；没有指标树时返回 None（交给调用方判空）。"""
    if not isinstance(payload, dict):
        return None
    nodes = payload.get(_TREE_KEY)
    if not isinstance(nodes, list) or not nodes:
        return None
    leaves = list(_iter_leaves(nodes))
    if not leaves:
        return None
    scored = sum(1 for leaf in leaves if _coerce_number(leaf.get("score")) is not None)
    return Coverage(len(leaves), scored, "json", "tree")


def _score_items(scores):
    """把 `scores` 的三种真实形态统一成 (键, 条目) 序列。

    三种形态都实测出现过：对象（标准）、数组（模型图省事）、扁平映射（只给 id→分）。
    """
    if isinstance(scores, dict):
        return list(scores.items())
    if isinstance(scores, list):
        items = []
        for index, item in enumerate(scores):
            if not isinstance(item, dict):
                continue
            key = ""
            for alias in _INDICATOR_KEY_ALIASES:
                if str(item.get(alias) or "").strip():
                    key = str(item[alias]).strip()
                    break
            items.append((key or f"#{index}", item))
        return items
    return []


def _count_numeric(items, aliases=("actual_score",)) -> int:
    """数「带可计分数值」的条目。

    `aliases` 按步骤给：步骤 7 用 `actual_score`；步骤 4 的数组形态里模型写的是
    `score`（实测真实输出如此），只认 `actual_score` 会把它当成「没有数值」——
    于是「键数没变、分值被清零」这条盲区在步骤 4 上永远判不出来。
    """
    total = 0
    for _, item in items:
        if isinstance(item, dict):
            value = None
            for alias in aliases:
                if item.get(alias) is not None:
                    value = item[alias]
                    break
        else:
            value = item
        if _coerce_number(value) is not None:
            total += 1
    return total


def coverage_of_payload(payload, step: Optional[int] = None) -> Coverage:
    """从已解析的 payload 里数条目；形态不认时返回空 Coverage（不抛异常）。

    `step` 用来消歧：
      * 3    → 只走指标树口径。这一步的 payload 顶层可能混着 `warning/legal_basis/
               total_score`（实测 `3c6070b9`），落到扁平分值映射分支会数出 5 条假条目；
      * 4/7  → 只走 `scores` 口径（含模型偶尔给的数组/扁平形态）；
      * None → 先认指标树（有 `level1_indicators` 就是树），否则回到 `scores`，
               保持旧调用方的行为。
    """
    if not isinstance(payload, dict):
        return _EMPTY_COVERAGE
    if step == 3 or (step is None and isinstance(payload.get(_TREE_KEY), list)):
        return _tree_coverage(payload) or _EMPTY_COVERAGE

    scores = payload.get("scores")
    if scores is None:
        # 允许直接给映射 {"L1_01": 20, ...}
        if payload and all(isinstance(k, str) for k in payload) and any(
            _coerce_number(v) is not None for v in payload.values()
        ):
            scores = payload
        else:
            return _EMPTY_COVERAGE
    items = _score_items(scores)
    if not items:
        return _EMPTY_COVERAGE
    aliases = ("actual_score", "score") if step == 4 else ("actual_score",)
    return Coverage(len(items), _count_numeric(items, aliases), "json", "scores")


def coverage_of_text(text: str, step: Optional[int] = None) -> Coverage:
    """以原文里的 JSON 为基准；解析不出来才退回「正文提到的编号」。

    退回时 `source="mention"`、`numeric=0`——因为没有可信的分值基准，
    调用方必须据此跳过「分值不得减少」这条判据，而不是拿 0 去比。
    """
    raw = text or ""
    blob = extract_json(raw)
    if blob:
        try:
            from_json = coverage_of_payload(json.loads(blob), step)
        except ValueError:
            from_json = _EMPTY_COVERAGE
        if from_json.entries:
            return from_json
    mentions = mentioned_indicator_ids(raw)
    if mentions:
        return Coverage(len(mentions), 0, "mention", "mention")
    return _EMPTY_COVERAGE


@dataclass(frozen=True)
class ShrinkReport:
    """一次修复的缩水核查结论。`reason` 直接进 warning，给人看得懂的话。"""

    step: int
    shrunk: bool
    unverifiable: bool
    reason: str
    before: Coverage
    after: Coverage


SHRINK_CHECKED_STEPS = (3, 4, 7)


def shrink_report(step: int, original_text: str, repaired) -> ShrinkReport:
    """核查修复有没有偷偷少给东西：条目数不得减少，可计分数值分也不得减少。

    Batch 17 起覆盖三个条目型步骤：
      * 3（指标体系，树）——按叶子指标计数；
      * 4（分值分配，扁平映射）与 7（评分明细，对象）——按 `scores` 键与数值计数。
    其余步骤不产出可无损对比的条目结构，返回**明确的不适用**（不是「没问题」）。
    判不了（原输出既没有可解析的 JSON 也没有可识别编号）时置 `unverifiable`，
    由调用方**明说**「无法核验」——静默放行是 Batch 13 的遗留缺陷。
    """
    payload = getattr(repaired, "payload", None) or {}
    after = coverage_of_payload(payload, step)
    if step not in SHRINK_CHECKED_STEPS:
        return ShrinkReport(
            step, False, False,
            f"步骤 {step} 不产出条目型结构，不做缩水校验", _EMPTY_COVERAGE, after,
        )

    before = coverage_of_text(original_text, step)
    if before.entries == 0:
        return ShrinkReport(
            step, False, True,
            "原输出里既没有可解析的 JSON 条目，也没有识别到指标编号，无法核验是否缩水",
            before, after,
        )

    label = _ENTRY_LABEL.get(before.shape, "条目")
    problems = []
    if after.entries < before.entries:
        problems.append(f"{label} {before.entries}→{after.entries}")
    if before.numeric and after.numeric < before.numeric:
        problems.append(f"可计分数值分 {before.numeric}→{after.numeric}")
    if problems:
        return ShrinkReport(
            step, True, False,
            f"{'、'.join(problems)}（基准：{_SOURCE_LABEL[before.source]}）",
            before, after,
        )
    return ShrinkReport(step, False, False, "", before, after)


def repair_shrunk(step: int, original_text: str, repaired) -> bool:
    """修复后覆盖少于原文 → True。保留旧接口，内部走 `shrink_report`。"""
    return shrink_report(step, original_text, repaired).shrunk


def repair_messages(step: int, raw: str, errors: List[str]):
    """构造「只重出 JSON」的修复对话。"""
    system = (
        "你是 JSON 修复器。你只输出一个 JSON 对象：不要 markdown 代码块，不要解释，不要正文。"
        "必须逐条保留原输出里的每一个条目，不得删减、合并或省略；"
        "拿不准的条目照原样抄录（分值、引用、理由都不要改），不要因为不确定就删掉。"
    )
    human = (
        f"下面这段输出本应是「{SCHEMA_TITLES.get(step, '结构化结果')}」的 JSON，但没有通过程序校验。\n\n"
        "【校验错误】\n"
        + "\n".join(f"- {item}" for item in errors[:MAX_REPORTED_ERRORS])
        + "\n\n【要求的 JSON 结构】\n"
        + SCHEMA_HINTS.get(step, '{"scores": {}}')
        + "\n\n【原始输出】\n```\n"
        + (raw or "")[:mention_limit(step)]
        + "\n```\n\n请直接输出修复后的 JSON 对象。"
    )
    return _as_messages(system, human)


def _as_messages(system: str, human: str):
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        return [SystemMessage(content=system), HumanMessage(content=human)]
    except Exception:  # pragma: no cover - langchain 缺失时用最小 dict 形态
        return [{"role": "system", "content": system}, {"role": "user", "content": human}]


def log_outcome(outcome: StructuredOutcome) -> None:
    if outcome.status == "ok":
        if outcome.warnings:
            logger.info(f"[Step {outcome.step}] 结构化输出通过，忽略 {len(outcome.warnings)} 条不合规条目")
        return
    if outcome.status == "repaired":
        logger.warning(f"[Step {outcome.step}] 结构化输出经一次修复后通过：{outcome.summary()}")
        return
    logger.warning(f"[Step {outcome.step}] 结构化输出两次不合规，转人工复核：{outcome.summary()}")
