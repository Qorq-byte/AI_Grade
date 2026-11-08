"""评分引用锚点解析（Batch 11）。

这些用例对着**实测取证过的真实引用字符串**写，不是构造出来的漂亮例子：
真实一轮 14 步里，步骤 7 的 23 条评分有 5 条本来可以落地，被
`evidence in evidence_ids` 挡掉——模型引用的是下发原文的小节名，或者被自由文本
包裹的有效编号。用例必须能区分「能落到材料里」与「落不到」，而不是把闸门整个放开。
"""
from backend.services import evidence_anchor as ea

MATERIAL = """## 指标体系表
| 一级指标 | 分值 | 证据 |

### 一、项目概况
本项目为城市主干道改造工程，总投资5000万元。

### 二、建设内容
1. 道路工程：改造主路3.2公里、辅路1.8公里；
2. 排水工程：新建雨水管网4.5公里；

### 三、绩效目标
目标2：资金到位率100% [P1-11-1]

### 四、资金安排
财政拨款4000万元，专项债1000万元 [P1-14-1]。
"""


def _resolve(text, pool=("P1-10-1", "P1-12-1", "P1-15-1"), material=MATERIAL):
    return ea.resolve(text, evidence_ids=pool, material=material)


# ═══════════════════════════════════════════════════════
#  标题归一化与抽取
# ═══════════════════════════════════════════════════════

def test_normalize_heading_strips_numbering_and_punctuation():
    assert ea.normalize_heading("（四）资金安排与使用") == "资金安排与使用"
    assert ea.normalize_heading("四、资金安排") == "资金安排"
    assert ea.normalize_heading("### 三、绩效目标") == "绩效目标"
    assert ea.normalize_heading(" 1. 道路工程 ") == "道路工程"


def test_material_headings_picks_sections_but_not_list_items():
    headings = ea.material_headings(MATERIAL)
    assert "项目概况" in headings and "资金安排" in headings
    # `1. 道路工程` 是列表项不是小节，不能被当成标题——否则引用校验会松到没有意义
    assert "道路工程改造主路32公里辅路18公里" not in headings
    assert all(len(item) >= 3 for item in headings)


def test_material_headings_dedupes():
    headings = ea.material_headings("### 一、项目概况\n一、项目概况\n")
    assert headings.count("项目概况") == 1


# ═══════════════════════════════════════════════════════
#  三类锚点：检索编号 / 材料内编号 / 原文小节名
# ═══════════════════════════════════════════════════════

def test_exact_evidence_id_is_strongest_anchor():
    anchor = _resolve("P1-12-1")
    assert anchor.ok and anchor.kind == "evidence_id"
    assert anchor.matched == ("P1-12-1",)


def test_id_wrapped_in_free_text_still_resolves():
    """真实引用：`P1-10-1、P1-12-1、资料「三、绩效目标」` 曾因精确匹配被整条丢弃。"""
    anchor = _resolve("P1-10-1、P1-12-1、资料「三、绩效目标」")
    assert anchor.ok and anchor.kind == "evidence_id"
    assert set(anchor.matched) == {"P1-10-1", "P1-12-1"}


def test_id_only_present_in_material_resolves_as_material_id():
    """P1-14-1 出现在下发的指标体系表里，但不在本步骤检索池——模型看得见就该算数。"""
    anchor = _resolve("P1-14-1")
    assert anchor.ok and anchor.kind == "material_id"
    assert anchor.matched == ("P1-14-1",)


def test_section_name_citation_resolves_as_heading():
    anchor = ea.resolve("资料「四、资金安排」", evidence_ids=(), material=MATERIAL)
    assert anchor.ok and anchor.kind == "heading"
    assert anchor.matched == ("资金安排",)


def test_heading_matches_when_citation_has_trailing_context():
    """真实引用：`资料「三、绩效目标」目标2`——引号外还有补充文字。"""
    anchor = ea.resolve("资料「三、绩效目标」目标2", evidence_ids=(), material=MATERIAL)
    assert anchor.ok and anchor.kind == "heading"


def test_evidence_id_beats_heading_when_both_present():
    anchor = _resolve("资料「四、资金安排」、P1-15-1")
    assert anchor.kind == "evidence_id"


def test_fullwidth_and_ascii_quotes_both_parsed():
    for text in ("资料「一、项目概况」", "资料『一、项目概况』", '资料"一、项目概况"',
                 "资料“一、项目概况”"):
        assert ea.resolve(text, evidence_ids=(), material=MATERIAL).ok, text


# ═══════════════════════════════════════════════════════
#  仍然必须拒绝的（不能为了提升落地率把闸门放开）
# ═══════════════════════════════════════════════════════

def test_fabricated_id_is_rejected():
    anchor = _resolve("P9-99-9")
    assert not anchor.ok and anchor.kind == "none"
    assert anchor.ids == ("P9-99-9",), "要留下模型写了什么，便于排查"


def test_unknown_section_is_rejected():
    anchor = _resolve("资料「八、考核评分」")
    assert not anchor.ok


def test_placeholder_word_is_rejected():
    assert not _resolve("无").ok
    assert not _resolve("资料「无」").ok
    assert not _resolve("N/A").ok


def test_empty_and_none_are_rejected():
    assert not _resolve("").ok
    assert not _resolve("   ").ok
    assert not _resolve(None).ok
    assert not _resolve({"id": "P1-10-1"}).ok


def test_two_char_heading_is_too_short_to_match():
    """过短的章节名包含关系几乎是恒真，必须设下限。"""
    assert not ea.resolve("资料「资金」", evidence_ids=(), material=MATERIAL).ok


def test_indicator_ids_are_not_mistaken_for_evidence_ids():
    """L3_01_01_01 这类指标 ID 不能被当成证据编号。"""
    anchor = _resolve("L3_01_01_01")
    assert not anchor.ok
    assert anchor.ids == ()


# ═══════════════════════════════════════════════════════
#  分值分类
# ═══════════════════════════════════════════════════════

def test_classify_score_separates_pending_from_invalid():
    assert ea.classify_score(4.0) == "numeric"
    assert ea.classify_score(0) == "numeric"
    assert ea.classify_score("4") == "numeric"
    assert ea.classify_score("4分") == "numeric"
    assert ea.classify_score("待核实") == "pending"
    assert ea.classify_score("无法评价") == "pending"
    assert ea.classify_score("资料不足，无法判断") == "pending"
    assert ea.classify_score("高分") == "invalid"
    assert ea.classify_score(True) == "invalid"


def test_json_null_is_a_refusal_not_an_illegal_value():
    """实测：模型会用 JSON `null` 表达「这个指标我不打分」，一轮里 18/25 条这么写。

    把它算成「分值不合法」会让台账把 18 条合法拒绝报成系统问题。
    """
    assert ea.classify_score(None) == "pending"
    assert ea.classify_score("") == "pending"
    assert ea.classify_score("   ") == "pending"


def test_anchor_as_dict_is_json_safe():
    payload = _resolve("资料「四、资金安排」").as_dict()
    assert payload == {"ok": True, "kind": "heading", "matched": ["资金安排"], "ids": []}


# ═══════════════════════════════════════════════════════
#  定位串与序号：实测第 6 轮 e2e 暴露的新形态
#  （20 条引用写的是 fe_wf_sample.txt-第10段 / fe_wf_sample.txt-第四部分）
# ═══════════════════════════════════════════════════════

CHUNKS = [
    {"id": "P1-10-1", "source": "fe_wf_sample.txt", "locator": "fe_wf_sample.txt-第10段"},
    {"id": "P1-1-1", "source": "fe_wf_sample.txt", "locator": "fe_wf_sample.txt-第1段"},
    {"id": "P2-10-1", "source": "kpi_table.xlsx", "locator": "kpi_table.xlsx-第10段"},
]


def _resolve_loc(text, pool=()):
    """带检索原件与预抽序号的解析（步骤 7 真实调用形态）。"""
    return ea.resolve(
        text, evidence_ids=pool, material=MATERIAL,
        headings=ea.material_headings(MATERIAL),
        chunks=CHUNKS,
        ordinals=ea.section_ordinals(MATERIAL),
    )


def test_section_ordinals_map_number_to_title():
    table = ea.section_ordinals(MATERIAL)
    assert table[1] == "项目概况"
    assert table[4] == "资金安排"
    # `1. 道路工程` 是正文列表项，不能当成小节
    assert 1 not in ea.section_ordinals("1. 道路工程\n2. 排水工程")


def test_locator_with_filename_resolves_to_chunk_id():
    anchor = _resolve_loc("fe_wf_sample.txt-第10段")
    assert anchor.ok
    assert anchor.kind == "evidence_locator"
    assert anchor.matched == ("P1-10-1",)


def test_locator_accepts_行_段_页_synonyms():
    assert _resolve_loc("fe_wf_sample.txt-第1行").matched == ("P1-1-1",)
    assert _resolve_loc("fe_wf_sample.txt-第1段").matched == ("P1-1-1",)


def test_locator_without_filename_needs_a_unique_candidate():
    """只写「第3段」而只有一个 chunk 命中 → 可落地。"""
    single = [{"id": "P1-3-1", "source": "fe_wf_sample.txt", "locator": "fe_wf_sample.txt-第3段"}]
    anchor = ea.resolve("第3段", evidence_ids=(), material=MATERIAL,
                        headings=ea.material_headings(MATERIAL), chunks=single,
                        ordinals=ea.section_ordinals(MATERIAL))
    assert anchor.ok and anchor.kind == "evidence_locator"

    # 两个文件都有「第10段」→ 无法判断是哪个，必须拒绝
    assert not _resolve_loc("第10段").ok


def test_locator_with_mismatched_filename_is_rejected():
    """写了文件名但对不上，不能退回「唯一候选」去猜——否则等于放行错引用。"""
    assert not _resolve_loc("other.txt-第10段").ok


def test_locator_without_line_number_is_rejected():
    """只给文件名、没有行号，落不到具体位置。"""
    assert not _resolve_loc("fe_wf_sample.txt").ok


def test_ordinal_section_resolves_to_title():
    anchor = _resolve_loc("第四部分")
    assert anchor.ok
    assert anchor.kind == "section_ordinal"
    assert anchor.matched == ("资金安排",)


def test_ordinal_section_rejects_nonexistent_section():
    assert not _resolve_loc("第八部分").ok


def test_composite_citation_resolves_first_locatable_segment():
    """`fe_wf_sample.txt-第1段及第四部分` 是模型真实的复合写法。"""
    anchor = _resolve_loc("fe_wf_sample.txt-第1段及第四部分")
    assert anchor.ok
    assert anchor.kind == "evidence_locator"
    assert anchor.matched == ("P1-1-1",)


def test_evidence_id_still_beats_locator_and_ordinal():
    """五类锚点排序不能因为新增两类而乱掉。"""
    pool = ("P1-10-1",)
    anchor = _resolve_loc("P1-10-1（见 fe_wf_sample.txt-第1段，第四部分）", pool=pool)
    assert anchor.kind == "evidence_id"
    assert anchor.matched == ("P1-10-1",)


def test_ordinals_precomputed_matches_recomputed():
    """批量调用传预抽表，结果必须与现抽一致（防止两套口径）。"""
    precomputed = ea.section_ordinals(MATERIAL)
    fresh = ea.resolve("第四部分", evidence_ids=(), material=MATERIAL, ordinals=precomputed)
    lazy = ea.resolve("第四部分", evidence_ids=(), material=MATERIAL)
    assert fresh == lazy



# ═══════════════════════════════════════════════════════
#  弱定位：文件名 + 行号，行号在原文范围内（第 6 类锚点）
#  实测来源：第 7 轮 e2e 的 `fe_wf_sample.txt 第14行`，原文 16 行，
#  第 14 行正是「截至2025年6月已支出3200万元，支出进度64%」——
#  与本步只召回的第 10/12/15 行不同，所以 chunk 匹配不上，但它确实指向原文真实位置。
# ═══════════════════════════════════════════════════════

RAW_TEXTS = {
    "u1_be8b81782a0e487da7cc7aa092057f63_fe_wf_sample.txt": "\n".join([
        "云海市政道路建设工程项目",
        "一、项目概况",
        "本项目为城市主干道改造工程，总投资5000万元。",
        "二、建设内容",
        "1. 道路工程：改造主路3.2公里、辅路1.8公里；",
        "2. 排水工程：新建雨水管网4.5公里；",
        "3. 照明工程：安装路灯220盏；",
        "4. 绿化工程：种植行道树1800株。",
        "三、绩效目标",
        "目标1：2025年底前完成全部工程并通车；",
        "目标2：资金到位率100%，支出进度不低于90%；",
        "目标3：群众满意度不低于85%；",
        "四、资金安排",
        "财政拨款4000万元，专项债1000万元。截至2025年6月已支出3200万元，支出进度64%。",
        "五、存在问题",
        "部分路段施工进度滞后约1个月。",
    ]),
}


def _resolve_weak(text, raw_texts=None, line_counts=None):
    kwargs = {"raw_texts": RAW_TEXTS if raw_texts is None else raw_texts,
              "line_counts": line_counts}
    return ea.resolve(text, evidence_ids=(), material=MATERIAL,
                      headings=ea.material_headings(MATERIAL),
                      chunks=CHUNKS, ordinals=ea.section_ordinals(MATERIAL), **kwargs)


def test_material_locator_matches_line_within_raw_text():
    """`fe_wf_sample.txt 第14行`（空格分隔）要能落到原文第 14 行。"""
    anchor = _resolve_weak("fe_wf_sample.txt 第14行")
    assert anchor.ok
    assert anchor.kind == "material_locator"
    assert anchor.matched == ("fe_wf_sample.txt:第14行",)


def test_material_locator_ignores_upload_prefix():
    """原文 key 带 `u1_<32hex>_` 前缀，模型写的是去掉前缀的文件名——必须能对上。"""
    anchor = _resolve_weak("fe_wf_sample.txt-第3行")
    assert anchor.ok and anchor.kind == "material_locator"
    assert anchor.matched == ("fe_wf_sample.txt:第3行",)


def test_material_locator_rejects_line_beyond_document():
    """行号越界是最容易被编造的地方，必须拒绝。"""
    assert not _resolve_weak("fe_wf_sample.txt 第99行").ok
    assert not _resolve_weak("fe_wf_sample.txt 第0行").ok


def test_material_locator_rejects_unknown_file():
    assert not _resolve_weak("other_project.pdf-第3行").ok


def test_material_locator_needs_raw_text_or_line_counts():
    """拿不到原文行数时不能凭空放行——否则等于只看「有没有写第N行」。"""
    anchor = _resolve_weak("fe_wf_sample.txt 第14行", raw_texts={})
    assert not anchor.ok


def test_material_locator_works_from_line_counts_only():
    """重放场景没有原文，只有行数投影，判定结论必须一致。"""
    anchor = _resolve_weak("fe_wf_sample.txt 第14行", raw_texts={},
                           line_counts={"fe_wf_sample.txt": 16})
    assert anchor.ok and anchor.kind == "material_locator"
    assert anchor.matched == ("fe_wf_sample.txt:第14行",)


def test_line_counts_out_of_range_still_rejected_in_replay():
    anchor = _resolve_weak("fe_wf_sample.txt 第14行", raw_texts={},
                           line_counts={"fe_wf_sample.txt": 5})
    assert not anchor.ok


def test_chunk_hit_beats_material_locator():
    """同一行号若命中检索块，必须记成 evidence_locator（更强），不能降级成弱定位。"""
    anchor = _resolve_weak("fe_wf_sample.txt-第10段")
    assert anchor.kind == "evidence_locator"
    assert anchor.matched == ("P1-10-1",)


def test_material_locator_does_not_swallow_fabricated_id():
    """弱定位只补「行号」这一类，不能顺手把编造编号也放行。"""
    anchor = _resolve_weak("P9-99-9（fe_wf_sample.txt 第14行）")
    assert anchor.ok
    assert anchor.kind == "material_locator", "编号本身编造，但行号是真的——只能按弱定位放行"
    assert anchor.ids == ("P9-99-9",), "编造的编号要留在 ids 里供审计"


def test_composite_prefers_stronger_anchor_over_first_match():
    """`第1段及第四部分` 里，小节序号比弱定位更强，不能因为「第1段」在前就降级。

    实测 b20e9f85：`fe_wf_sample.txt-第1段及第四部分`（6.0 分）本来能落到
    section_ordinal（资金安排），加了弱定位后却先命中 material_locator（第1行），
    证据强度被无谓拉低。

    这里必须把 chunks 清空：真实那一轮只检索到第 10/12/15 行，第 1 段不在检索块里，
    否则测的就不是「弱定位 vs 小节序号」，而是「检索块命中」了。
    """
    anchor = ea.resolve(
        "fe_wf_sample.txt-第1段及第四部分", evidence_ids=(), material=MATERIAL,
        headings=ea.material_headings(MATERIAL), chunks=[],
        ordinals=ea.section_ordinals(MATERIAL),
        raw_texts=RAW_TEXTS,
    )
    assert anchor.kind == "section_ordinal", f"实际={anchor.kind} {anchor.matched}"
    assert anchor.matched == ("资金安排",)


def test_single_segment_still_uses_material_locator():
    """只有一段且只能弱定位时，仍按弱定位放行（不要为了排序把它丢掉）。"""
    anchor = ea.resolve(
        "fe_wf_sample.txt-第14段", evidence_ids=(), material=MATERIAL,
        headings=ea.material_headings(MATERIAL), chunks=[],
        ordinals=ea.section_ordinals(MATERIAL),
        raw_texts=RAW_TEXTS,
    )
    assert anchor.kind == "material_locator"


def test_chunk_locator_beats_ordinal_in_composite():
    """命中检索块的段仍是最强，优先于小节序号。"""
    anchor = _resolve_weak("fe_wf_sample.txt-第10段及第四部分")
    assert anchor.kind == "evidence_locator"
    assert anchor.matched == ("P1-10-1",)
