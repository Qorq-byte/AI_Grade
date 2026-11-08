"""评分引用的锚点解析：把模型的自由文本引用落到「本次真实下发的材料」里的可定位位置。

为什么要单独做一层（Batch 11 实测取证，不是假设）：

同一轮真实 14 步运行里，步骤 7 的模型对 23 个叶子指标给出了 23 条评分。
其中 7 条带了**数字分值**：2 条精确命中检索池编号、正常落地，另外 5 条被丢弃：

  * `P1-10-1、P1-12-1、资料「三、绩效目标」` —— 有效证据编号被自由文本包裹；
  * `资料「四、资金安排」` —— 引用的是**下发原文里的小节名**，不是检索块编号。

而当时的判定是 `evidence in evidence_ids`，`evidence_ids` 只有本步骤检索到的 3 个 chunk
（P1-10-1 / P1-12-1 / P1-15-1）。问题在于：提示词同时下发了 `evaluation_table`，
里面外露着 P1-2-1、P1-11-1、P1-14-1 这些**不在检索池里的编号**，以及带「一、项目概况」
「四、资金安排」小节标题的原文。模型看到三种可引用形式，程序只认其中一种。

所以本模块的口径是：**引用必须能落到「模型实际看到的材料」里的某个可定位锚点**，
并把落到了哪一类记下来（`kind`），供审计。六类锚点按可信度排序：

  1. `evidence_id`    —— 落在本步骤检索池里的编号（最强，仍优先）；
  2. `material_id`    —— 落在下发材料正文里的编号（模型看得见，也指向原文）；
  3. `evidence_locator` —— 「文件名 + 行/段号」，如 `fe_wf_sample.txt-第10段`，
                        对应提示词里 `[P1-10-1] fe_wf_sample.txt（提取文本第10行）` 那一行；
  4. `section_ordinal`  —— 「第N部分」这类只给序号的引用，按材料里小节序号还原（`第四部分` → `资金安排`）；
  5. `material_locator` —— 「文件名 + 行号」但那一行**不在检索块里**，只校验行号确实存在于原文
                        （如 `fe_wf_sample.txt 第14行`）。**验证强度最弱**：
                        只证明「这一行存在」，不证明「这一行支持该评分」，
                        所以调用方必须把它记成弱定位（不计入总分），不能当已落地；
  6. `heading`        —— 引号里的小节名，如 `资料「四、资金安排」`。

不接受的：编造的编号（`P9-99-9`）、材料里不存在的小节名（`资料「八、考核评分」`）、
空引用、`资料「无」` 这类占位词。判定是纯程序、零 token，单次 O(材料长度)。

另有一个 `classify_score`：把 `actual_score` 的原始值分成 numeric / pending / invalid。
它存在的理由同样是实测——模型会用「待核实」字符串或 **JSON `null`** 表达拒绝，
把这两种算成「格式错误」，就会把合法拒绝报成系统故障。
"""
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple

# 模型用来表达「不给分」的写法——这些不是格式错误，是提示词明确要求的合法拒绝
PENDING_SCORE_WORDS = frozenset({
    "待核实", "无法评价", "无法判断", "待定", "资料不足", "不适用", "n/a", "na", "-", "—", "/",
})

# 证据编号形态：P1-10-1 / P1-2-1 / A12-3-4。前后不能紧邻字母数字，避免从长串里截半截。
_ID_PATTERN = re.compile(r"(?<![0-9A-Za-z])([A-Za-z]{1,3}\d{1,3}(?:-\d{1,3}){1,3})(?![0-9])")
# 引用里的书名号/引号片段：资料「四、资金安排」、原文『一、项目概况』、"二、建设内容"
_QUOTE_PATTERN = re.compile(r"[「『\"“]([^」』\"”]{1,40})[」』\"”]")
# 行/段/页号：模型会把「提取文本第10行」写成「第10段」「第10页」
_LINE_NO = re.compile(r"第\s*(\d{1,6})\s*[行段页条]")
# 章节序号引用：「第四部分」「第2节」——只给序号不给名字
_ORDINAL_SECTION = re.compile(r"第\s*([一二三四五六七八九十百]{1,3}|\d{1,3})\s*(?:部分|章节|节|章|篇)")
# 复合引用的切分符：`第1段及第四部分`、`P1-1-1、P1-2-1`
_SEGMENT_SPLIT = re.compile(r"[、，,；;/／|｜]|以及|及|和|与")
# 归一化时只保留汉字与字母数字（用于比对文件名/定位串）
_ALNUM_CJK = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]")
_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
# 标题编号前缀：（四）/ (四) / 四、 / 4. / 【四】
_HEADING_PREFIX = re.compile(r"^[\s（(【\[]*[一二三四五六七八九十百0-9]{1,3}[\s）)】\]]*\s*[、.．,，:：]?\s*")
# 归一化时去掉的符号（保留中文与字母数字）
_HEADING_NOISE = re.compile(r"[\s\u3000·・\-—_/\\|、。，,．.:：；;！!？?（）()【】\[\]「」『』《》<>\"'“”‘’]")
# 形如小标题的整行：不超过 40 字、不含句末标点
_HEADING_LINE = re.compile(r"^[^\n]{2,40}$", re.M)
# 「小节标题」只认 Markdown 标题与中文序号（一、/（二）），不认 `1.` 这类列表项——
# 否则正文里每一行编号都会被当成标题，引用校验会松到没有意义。
_CN_SECTION = re.compile(r"^[\s（(【\[]*[一二三四五六七八九十百]{1,3}[\s）)】\]]*\s*[、.．]")
# 小节行（带标题）：与 _CN_SECTION 同口径，只认中文序号，额外捕获标题部分
_SECTION_LINE = re.compile(r"^[\s（(【\[]*([一二三四五六七八九十百]{1,3})[\s）)】\]]*\s*[、.．]\s*(.+)$")
_MIN_HEADING_CHARS = 3
# 引用里是否明确写了文件名：用来区分「没写文件名」（可靠行号唯一兜底）
# 与「写了个对不上的文件名」（必须拒绝，不能退回唯一候选去猜）
_FILE_EXT = re.compile(r"\.(?:txt|pdf|docx?|xlsx?|pptx?|csv|md|json|html?)\b", re.I)


def normalize_heading(text: str) -> str:
    """去掉编号前缀与标点，只留可比对的中文/字母数字。"""
    value = (text or "").strip()
    value = value.lstrip("#").strip()
    value = _HEADING_PREFIX.sub("", value)
    return _HEADING_NOISE.sub("", value)


def cn_number(text: str) -> Optional[int]:
    """把「四」「十二」「2」这类序号转成整数；转不了返回 None。"""
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    if raw == "十":
        return 10
    if raw.startswith("十") and len(raw) == 2 and raw[1] in _CN_DIGITS:
        return 10 + _CN_DIGITS[raw[1]]
    if len(raw) == 2 and raw[0] in _CN_DIGITS and raw[1] == "十":
        return _CN_DIGITS[raw[0]] * 10
    if len(raw) == 3 and raw[0] in _CN_DIGITS and raw[1] == "十" and raw[2] in _CN_DIGITS:
        return _CN_DIGITS[raw[0]] * 10 + _CN_DIGITS[raw[2]]
    if len(raw) == 1 and raw in _CN_DIGITS:
        return _CN_DIGITS[raw]
    return None


def section_ordinals(material: str) -> Dict[int, str]:
    """从材料里抽出「序号 → 小节标题」，用于解析「第四部分」这类只给序号的引用。

    只认以**中文序号**开头的小节（`四、资金安排`、`（四）资金安排与使用`），
    不认 `1. 道路工程` 这类阿拉伯数字列表项——正文里每行编号都算小节的话，
    「第1部分」就会解析到第一个列表项上，比拒绝更糟。
    这条口径与 `material_headings` 的 `_CN_SECTION` 保持一致，避免两处判断打架。
    """
    found: Dict[int, str] = {}
    for line in (material or "").splitlines():
        candidate = line.strip().lstrip("#").strip()
        if not candidate or len(candidate) > 60:
            continue
        match = _SECTION_LINE.match(candidate)
        if not match:
            continue
        number = cn_number(match.group(1))
        title = normalize_heading(match.group(2))
        if number is None or len(title) < _MIN_HEADING_CHARS or number in found:
            continue
        found[number] = title
    return found


def material_headings(material: str) -> Tuple[str, ...]:
    """从下发材料里抽出可用于定位的小节标题（归一化后，已去重、已过滤过短的）。"""
    found: list = []
    seen = set()
    for line in (material or "").splitlines():
        candidate = line.strip()
        if not candidate or len(candidate) > 60:
            continue
        # 标题行：Markdown 标题，或以中文序号/括号开头的短行
        is_heading = candidate.startswith("#") or bool(_CN_SECTION.match(candidate))
        if not is_heading or not _HEADING_LINE.match(candidate):
            continue
        normalized = normalize_heading(candidate)
        if len(normalized) < _MIN_HEADING_CHARS or normalized in seen:
            continue
        seen.add(normalized)
        found.append(normalized)
    return tuple(found)


@dataclass(frozen=True)
class Anchor:
    """一次引用解析的结果。`ok=False` 时 `kind="none"`，`matched` 为空。"""

    ok: bool
    kind: str
    matched: Tuple[str, ...]
    ids: Tuple[str, ...]
    raw: str

    def as_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "kind": self.kind, "matched": list(self.matched), "ids": list(self.ids)}


_FAILED_KIND = "none"


def _fail(raw: str, ids: Sequence[str]) -> Anchor:
    return Anchor(False, _FAILED_KIND, (), tuple(ids), raw)


def resolve(evidence_text: Any, *, evidence_ids: Iterable[str], material: str,
            headings: Optional[Sequence[str]] = None,
            chunks: Optional[Sequence[Any]] = None,
            ordinals: Optional[Dict[int, str]] = None,
            raw_texts: Optional[Dict[str, str]] = None,
            line_counts: Optional[Dict[str, int]] = None) -> Anchor:
    """把一条引用解析成锚点。

    evidence_ids: 本步骤检索池里的编号（可信度最高）
    material:     本次真实下发给模型的材料全文
    headings:     预抽好的标题列表；不传则现抽（批量调用时请传入以免重复解析）
    chunks:       本步骤的检索命中原件（含 source / locator），用于解析「文件名+行号」引用
    ordinals:     预抽好的「序号 → 小节标题」；不传则现抽
    raw_texts:    原文映射 {文件名: 全文}，用于校验「行号是否真的存在」
    line_counts:  原文行数投影 {文件名: 非空行数}；重放时没有原文，用它代替 raw_texts
    """
    raw = evidence_text if isinstance(evidence_text, str) else ""
    text = raw.strip()
    if not text:
        return _fail(raw, ())

    ids = tuple(dict.fromkeys(_ID_PATTERN.findall(text)))
    pool = {str(item) for item in evidence_ids}

    hit_pool = tuple(item for item in ids if item in pool)
    if hit_pool:
        return Anchor(True, "evidence_id", hit_pool, ids, raw)

    if ids and material:
        hit_material = tuple(item for item in ids if item in material)
        if hit_material:
            return Anchor(True, "material_id", hit_material, ids, raw)

    # 段内锚点按**强度**排序，不是按出现顺序取第一个。
    # 反例（实测 b20e9f85）：`fe_wf_sample.txt-第1段及第四部分` 若按先到先得，
    # 「第1段」会命中弱定位，而「第四部分」本来能落到小节序号（更强），
    # 结果把 6.0 分那条的证据强度无谓拉低了。
    segments = [seg.strip() for seg in _SEGMENT_SPLIT.split(text) if seg.strip()]
    for matcher, kind in (
        (lambda seg: _match_chunk_locator(seg, chunks), "evidence_locator"),
        (lambda seg: _match_ordinal_section(seg, material, ordinals), "section_ordinal"),
        (lambda seg: _match_material_locator(seg, raw_texts, line_counts), "material_locator"),
    ):
        for segment in segments:
            hit = matcher(segment)
            if hit:
                return Anchor(True, kind, (hit,), ids, raw)

    if material:
        titles = tuple(headings) if headings is not None else material_headings(material)
        for quoted in _QUOTE_PATTERN.findall(text):
            normalized = normalize_heading(quoted)
            if len(normalized) < _MIN_HEADING_CHARS:
                continue
            for title in titles:
                if normalized in title or title in normalized:
                    return Anchor(True, "heading", (title,), ids, raw)

    return _fail(raw, ids)


def _match_chunk_locator(segment: str, chunks: Optional[Sequence[Any]]) -> Optional[str]:
    """`fe_wf_sample.txt-第10段` → 命中的 chunk id。

    提示词里每条证据都印着 `[P1-10-1] fe_wf_sample.txt（提取文本第10行）`，
    模型经常写「文件名+行号」而不是编号。这种引用能落到具体一行，是可定位的——
    只要行号与该 chunk 的 locator 对得上。

    三种情形分开处理，避免「猜」：
      1. 写了文件名且能对上     → 接受
      2. 写了文件名但对不上     → 拒绝（模型可能引了不存在的文件）
      3. 没写文件名，行号只命中一个 chunk → 接受；命中多个 → 拒绝（无法判断是哪个文件）
    """
    if not chunks:
        return None
    line = _LINE_NO.search(segment)
    if not line:
        return None
    wanted = int(line.group(1))
    normalized_segment = _ALNUM_CJK.sub("", segment).lower()

    by_line = []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        found = _LINE_NO.search(str(chunk.get("locator") or ""))
        if not found or int(found.group(1)) != wanted:
            continue
        by_line.append(chunk)
    if not by_line:
        return None

    named = []
    for chunk in by_line:
        source = _ALNUM_CJK.sub("", str(chunk.get("source") or "")).lower()
        if source and source in normalized_segment:
            named.append(chunk)
    if named:
        return str(named[0].get("id") or "") or None

    # 引用里明确写了别的文件名，却没有一个 chunk 对得上 → 不落地
    if _FILE_EXT.search(segment):
        return None
    if len(by_line) == 1:
        return str(by_line[0].get("id") or "") or None
    return None


def _match_ordinal_section(segment: str, material: str,
                           ordinals: Optional[Dict[int, str]]) -> Optional[str]:
    """`第四部分` → 材料里序号为四的小节标题。"""
    match = _ORDINAL_SECTION.search(segment)
    if not match:
        return None
    number = cn_number(match.group(1))
    if number is None:
        return None
    table = ordinals if ordinals is not None else section_ordinals(material)
    return table.get(number)


def doc_label(name: str) -> str:
    """去掉上传前缀 `u1_<32hex>_`，与 `workflow_evidence._project_chunks` 的 `source` 同口径。

    两处必须一致：模型引用的是 `fe_wf_sample.txt`，而 `all_raw_texts` 的 key 带着
    上传前后缀 `u1_be8b..._fe_wf_sample.txt`。不统一就会「文件名永远对不上」。
    """
    return re.sub(r"^u\d+_[a-f0-9]{32}_", "", os.path.basename(str(name or "")))


def doc_line_counts(raw_texts: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """原文 → {文件名(去前缀): 非空行数}。只数非空行，避免末尾空行把范围放大。"""
    counts: Dict[str, int] = {}
    for name, text in (raw_texts or {}).items():
        label = doc_label(name)
        if not label:
            continue
        counts[label] = len([line for line in str(text or "").splitlines() if line.strip()])
    return counts


def _match_material_locator(segment: str, raw_texts: Optional[Dict[str, Any]],
                            line_counts: Optional[Dict[str, int]]) -> Optional[str]:
    """`fe_wf_sample.txt 第14行` → `fe_wf_sample.txt:第14行`（行号必须真的存在于原文）。

    为什么需要这一类（实测第 7 轮 e2e）：提示词只下发了 3 个检索块（第 10/12/15 行），
    模型却引用了第 14 行——因为它在提示词里看到的是**分类原文全文**。第 14 行确实是
    「截至2025年6月已支出3200万元，支出进度64%」，与该条评分的理由完全对得上，
    属于真实引用。但那一行不在检索块里，`evidence_locator` 匹配不上。

    这一类的验证强度**低于** `evidence_locator`：它只能证明「这一行存在」，
    不能证明「这一行支持该评分」。所以调用方必须把它记成弱定位（不计入总分），
    不能当成已落地——否则「无法评价、暂按基础分」那几条会借同一个口子混进总分。

    拿不到原文也拿不到行数时返回 None：宁可拒绝，也不能只看「有没有写第N行」就放行。
    """
    match = _LINE_NO.search(segment)
    if not match:
        return None
    wanted = int(match.group(1))
    if wanted < 1:
        return None

    counts = doc_line_counts(raw_texts)
    for name, total in (line_counts or {}).items():
        try:
            value = int(total)
        except (TypeError, ValueError):
            continue
        counts.setdefault(doc_label(name), value)

    normalized_segment = _ALNUM_CJK.sub("", segment).lower()
    for label, total in counts.items():
        if not label or total <= 0:
            continue
        source = _ALNUM_CJK.sub("", label).lower()
        if not source or source not in normalized_segment:
            continue
        if 1 <= wanted <= total:
            return f"{label}:第{wanted}行"
    return None


def classify_score(value: Any) -> str:
    """把 `actual_score` 的原始值分成：numeric / pending / invalid。

    这三种必须分开：`pending` 是模型按提示词要求作出的**合法拒绝**，不是格式错误；
    把它和 `invalid` 混在一起报「已忽略」，会让用户以为系统丢了真实评分。

    `None` 与空串算 `pending` 而不是 `invalid`——实测模型会用 **JSON `null`** 表达
    「这个指标我不打分」，一轮里 18/25 条这么写。那是拒绝，不是给了个非法值。
    真正非法的（"很高"、"优秀"）才归 `invalid`。
    """
    if isinstance(value, bool):
        return "invalid"
    if isinstance(value, (int, float)):
        return "numeric"
    if value is None:
        return "pending"
    text = str(value).strip()
    if not text:
        return "pending"
    if text.lower() in PENDING_SCORE_WORDS:
        return "pending"
    try:
        float(text.rstrip("分"))
    except ValueError:
        return "pending" if any(word in text for word in ("待核实", "无法", "资料不足")) else "invalid"
    return "numeric"


ANCHOR_KINDS = ("evidence_id", "material_id", "evidence_locator", "section_ordinal", "heading")
ANCHOR_LABELS = {
    "evidence_id": "检索证据编号",
    "material_id": "材料内编号",
    "evidence_locator": "证据行号定位",
    "section_ordinal": "小节序号",
    "heading": "原文小节名",
}
