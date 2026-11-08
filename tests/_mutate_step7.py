"""变异测试：确认 Batch 11 的用例真的能发现回归，而不是恰好通过。

做法：把每一处修复单独改回旧行为（或改坏），跑对应测试，要求**必须失败**。

安全措施（被真实事故逼出来的）：本批第一次运行时，脚本跑到一半被 SIGTERM 杀掉，
`finally` 没执行，`structured_output.py` 被留在了变异状态——而它未被 git 跟踪，
没有 `git checkout` 能救。所以现在：
  1. 动手前先把原文件写到 `.mutation_backup/`；
  2. 启动时若发现残留备份，先按备份还原（崩溃自恢复）；
  3. 注册 atexit + SIGINT/SIGTERM 处理器，尽量保证一定还原；
  4. 结束时逐字节校验四个文件与备份一致，不一致就报错退出。

用法：python tests/_mutate_step7.py
"""
import atexit
import hashlib
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKUP_DIR = ROOT / ".mutation_backup"
FILES = {
    "agent": ROOT / "backend" / "agents" / "performance_agent.py",
    "anchor": ROOT / "backend" / "services" / "evidence_anchor.py",
    "struct": ROOT / "backend" / "agents" / "tools" / "structured_output.py",
}

MUTATIONS = [
    (
        "把锚点校验改回精确匹配 evidence_ids",
        "agent",
        """                                             chunks=chunks, ordinals=ordinals,
                                             raw_texts=raw_texts, line_counts=line_counts)
            if not anchor.ok:""",
        """                                             chunks=chunks, ordinals=ordinals,
                                             raw_texts=raw_texts, line_counts=line_counts)
            if not (isinstance(evidence, str) and evidence in evidence_ids):""",
        "tests/test_step7_scoring.py",
    ),
    (
        "去掉「原文小节名」这一类锚点",
        "anchor",
        """        titles = tuple(headings) if headings is not None else material_headings(material)""",
        """        titles = ()
        if False:
            pass""",
        "tests/test_evidence_anchor.py tests/test_step7_scoring.py",
    ),
    (
        "锚点解析永不拒绝（等于把闸门放开）",
        "anchor",
        """    return _fail(raw, ids)


def _match_chunk_locator""",
        """    return Anchor(True, "heading", (), ids, raw)


def _match_chunk_locator""",
        "tests/test_evidence_anchor.py tests/test_step7_scoring.py",
    ),
    (
        "把「待核实」重新算成非法分值",
        "anchor",
        """    if text.lower() in PENDING_SCORE_WORDS:
        return "pending\"""",
        """    if text.lower() in PENDING_SCORE_WORDS:
        return "invalid\"""",
        "tests/test_evidence_anchor.py tests/test_step7_scoring.py",
    ),
    (
        "合法拒绝重新按格式错误逐条告警",
        "struct",
        """            if evidence_anchor.classify_score(item.get("actual_score")) == "pending":
                pending += 1
                continue
            warnings.append(f"scores['{key}'].actual_score 不是数字，已忽略")""",
        """            warnings.append(f"scores['{key}'].actual_score 不是数字，已忽略")""",
        "tests/test_step7_scoring.py tests/test_structured_output.py",
    ),
    (
        "校验基准退回只用证据池（不看下发材料）",
        "agent",
        """        return "\\n".join([
            state.get("evaluation_table") or "",
            json.dumps(state.get("indicator_system") or {}, ensure_ascii=False),
            self._categorized_excerpt(state, step_number=7),
        ])""",
        """        return "\\n".join(
            hit.get("text", "") for hit in state.get("step_evidence", {}).get("7", [])
        )""",
        "tests/test_step7_scoring.py",
    ),
    (
        "warnings 不再去重",
        "agent",
        """            warnings = state.setdefault("warnings", [])
            if line not in warnings:
                warnings.append(line)
        state["indicator_analysis"] = content""",
        """            state.setdefault("warnings", []).append(line)
        state["indicator_analysis"] = content""",
        "tests/test_step7_scoring.py",
    ),
    (
        "体系外指标 id 重新静默丢弃",
        "agent",
        """            unmatched.append({
                "indicator_id": key,""",
        """            continue
            unmatched.append({
                "indicator_id": key,""",
        "tests/test_step7_scoring.py",
    ),
    (
        "把 JSON null 重新算成非法分值",
        "anchor",
        """    if value is None:
        return "pending\"""",
        """    if value is None:
        return "invalid\"""",
        "tests/test_evidence_anchor.py tests/test_step7_scoring.py",
    ),
    (
        "取消 reason 里的引用回退",
        "agent",
        """            if not (isinstance(evidence, str) and evidence.strip()):
                # 实测：模型会把出处写进 reason""",
        """            if False:
                # 实测：模型会把出处写进 reason""",
        "tests/test_step7_scoring.py",
    ),
    (
        "去掉「文件名+行号」这一类锚点",
        "anchor",
        """        (lambda seg: _match_chunk_locator(seg, chunks), "evidence_locator"),
""",
        """        (lambda seg: None, "evidence_locator_disabled"),
""",
        "tests/test_evidence_anchor.py tests/test_step7_scoring.py",
    ),
    (
        "去掉「第N部分」这一类锚点",
        "anchor",
        """        (lambda seg: _match_ordinal_section(seg, material, ordinals), "section_ordinal"),
""",
        """        (lambda seg: None, "section_ordinal_disabled"),
""",
        "tests/test_evidence_anchor.py tests/test_step7_scoring.py",
    ),
    (
        "定位串不再校验文件名（错引用也能落地）",
        "anchor",
        """    if _FILE_EXT.search(segment):
        return None""",
        """    if False:
        return None""",
        "tests/test_evidence_anchor.py tests/test_step7_scoring.py",
    ),
    (
        "小节序号重新接受阿拉伯数字列表项",
        "anchor",
        """        match = _SECTION_LINE.match(candidate)""",
        """        match = re.match(r"^[\\s（(【\\[]*([一二三四五六七八九十百]{1,3}|\\d{1,3})"
                         r"[\\s）)】\\]]*\\s*[、.．]\\s*(.+)$", candidate)""",
        "tests/test_evidence_anchor.py",
    ),
    (
        "台账 accepted 不再带 evidence_anchor",
        "agent",
        """                          "anchor_from": anchor_from,
                          # 「按未取得佐证计分」和「实测表现得分」在分数上长得一样，
                          # 但对报告结论的含义完全相反：前者必须写进评价局限。
                          # 靠模型在 reason 里的固定前缀识别，不改任何既有字段语义。
                          "no_evidence": reason_text.startswith(NO_EVIDENCE_MARKER),
                          # 与 refresh_disposition 写入的字段同名同形：否则人工补分前后
                          # 台账字段集合不同，前端读 matched 会时有时无
                          "evidence_anchor": {**anchor.as_dict(), "from": anchor_from}}""",
        """                          "anchor_from": anchor_from,
                          "no_evidence": reason_text.startswith(NO_EVIDENCE_MARKER)}""",
        "tests/test_step7_scoring.py",
    ),
    (
        "弱定位直接算作已落地（总分被抬高）",
        "agent",
        """            if anchor.kind == "material_locator":""",
        """            if False and anchor.kind == "material_locator":""",
        "tests/test_step7_scoring.py",
    ),
    (
        "行号越界也放行（编个行号就能过）",
        "anchor",
        """        if 1 <= wanted <= total:""",
        """        if True:""",
        "tests/test_evidence_anchor.py tests/test_step7_scoring.py",
    ),
    (
        "标题改回从整份材料抽（自己引自己）",
        "agent",
        """        headings = evidence_anchor.material_headings(source_text)
        ordinals = evidence_anchor.section_ordinals(source_text)""",
        """        headings = evidence_anchor.material_headings(material)
        ordinals = evidence_anchor.section_ordinals(material)""",
        "tests/test_step7_scoring.py",
    ),
    (
        "弱定位忽略重放用的行数投影",
        "anchor",
        """    for name, total in (line_counts or {}).items():""",
        """    for name, total in {}.items():""",
        "tests/test_evidence_anchor.py tests/test_step7_scoring.py",
    ),
    (
        "修复提示词改回「宁可少写」",
        "struct",
        """        "必须逐条保留原输出里的每一个条目，不得删减、合并或省略；\"""",
        """        "若原输出因过长被截断，请只保留能确定的部分，宁可少写条目也不要编造。\"""",
        "tests/test_structured_output.py",
    ),
    (
        "缩水的修复结果照单全收",
        "agent",
        """            if second.ok and report.shrunk:""",
        """            if second.ok and False:""",
        "tests/test_structured_output.py",
    ),
    (
        "修复后丢弃首次正文",
        "agent",
        """            if original and original != content:""",
        """            if False:""",
        "tests/test_structured_output.py",
    ),
    (
        "步骤7 回显上限退回 6000（看不见全部条目）",
        "struct",
        """MAX_ECHO_CHARS_BY_STEP = {7: 24000}""",
        """MAX_ECHO_CHARS_BY_STEP = {}""",
        "tests/test_structured_output.py",
    ),
    (
        "缩水基准退回只看编号正则（编号一变就失效）",
        "struct",
        """    raw = text or ""
    blob = extract_json(raw)
    if blob:
        try:
            from_json = coverage_of_payload(json.loads(blob))
        except ValueError:
            from_json = _EMPTY_COVERAGE
        if from_json.entries:
            return from_json
""",
        """    raw = text or ""
""",
        "tests/test_structured_output.py",
    ),
    (
        "只比条目数、不比数值分（分值被清零也放行）",
        "struct",
        """    if before.numeric and after.numeric < before.numeric:
        problems.append(f"可计分数值分 {before.numeric}→{after.numeric}")
""",
        """""",
        "tests/test_structured_output.py",
    ),
    (
        "判不了缩水就静默放行",
        "struct",
        """        return ShrinkReport(
            step, False, True,
            "原输出里既没有可解析的 scores，也没有识别到指标编号，无法核验是否缩水",
            before, after,
        )""",
        """        return ShrinkReport(step, False, False, "", before, after)""",
        "tests/test_structured_output.py",
    ),
    (
        "不认数组形态的 scores（数成 0 条）",
        "struct",
        """    if isinstance(scores, list):
        items = []""",
        """    if False:
        items = []""",
        "tests/test_structured_output.py",
    ),
    (
        "不认扁平映射形态（只给 id→分）",
        "struct",
        """    if scores is None:
        # 允许直接给映射 {"L1_01": 20, ...}
        if payload and all(isinstance(k, str) for k in payload) and any(
            _coerce_number(v) is not None for v in payload.values()
        ):
            scores = payload
        else:
            return _EMPTY_COVERAGE""",
        """    if scores is None:
        return _EMPTY_COVERAGE""",
        "tests/test_structured_output.py",
    ),
]


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def backup_all() -> dict:
    BACKUP_DIR.mkdir(exist_ok=True)
    originals = {}
    for key, path in FILES.items():
        text = path.read_text(encoding="utf-8")
        originals[key] = text
        (BACKUP_DIR / f"{key}.py").write_text(text, encoding="utf-8")
    return originals


def restore_from_disk():
    """崩溃自恢复：把备份写回原文件。"""
    restored = []
    for key, path in FILES.items():
        saved = BACKUP_DIR / f"{key}.py"
        if saved.exists():
            path.write_text(saved.read_text(encoding="utf-8"), encoding="utf-8")
            restored.append(path.name)
    return restored


_ORIGINALS = {}


def restore_all():
    for key, text in _ORIGINALS.items():
        path = FILES[key]
        try:
            if path.read_text(encoding="utf-8") != text:
                path.write_text(text, encoding="utf-8")
        except OSError:
            pass


def _on_signal(signum, frame):
    restore_all()
    raise SystemExit(128 + signum)


def run(tests):
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *tests.split(), "-x", "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    tail = (result.stdout or "").strip().splitlines()
    summary = next((line for line in reversed(tail)
                    if "passed" in line or "failed" in line or "error" in line), "")
    return result.returncode, summary


def main():
    stale = restore_from_disk() if BACKUP_DIR.exists() else []
    if stale:
        print(f"检测到上次中断的变异，已从备份还原：{', '.join(stale)}")

    _ORIGINALS.update(backup_all())
    atexit.register(restore_all)
    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), _on_signal)
            except (ValueError, OSError):
                pass

    print("=== 基线（未变异） ===")
    code, summary = run("tests/test_evidence_anchor.py tests/test_step7_scoring.py "
                        "tests/test_structured_output.py")
    print(f"exit={code} {summary}")
    if code != 0:
        print("基线就不绿，先修好再跑变异测试")
        return 1

    survivors = []
    print("\n=== 逐项变异 ===")
    for index, (name, key, original, mutated, tests) in enumerate(MUTATIONS, 1):
        path = FILES[key]
        text = _ORIGINALS[key]
        if original not in text:
            print(f"{index}. {name}: 找不到锚点文本，跳过（脚本与代码已不同步）")
            survivors.append(name + "（锚点失效）")
            continue
        path.write_text(text.replace(original, mutated, 1), encoding="utf-8")
        try:
            code, summary = run(tests)
        finally:
            path.write_text(text, encoding="utf-8")
        caught = code != 0
        print(f"{index}. {name}: {'被抓到 ✓' if caught else '没抓到 ✗'}  {summary}")
        if not caught:
            survivors.append(name)

    print()
    drifted = [name for key, path in FILES.items() if _digest(path) != _digest(BACKUP_DIR / f"{key}.py")]
    if drifted:
        print(f"文件未还原到原状：{drifted}")
        return 1
    if survivors:
        print(f"有 {len(survivors)} 个变异存活，用例覆盖不足：")
        for name in survivors:
            print("  -", name)
        return 1
    print(f"全部 {len(MUTATIONS)} 个变异都被抓到，且三个文件均已还原。")
    import shutil
    shutil.rmtree(BACKUP_DIR, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
