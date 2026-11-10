"""批次 26 = B2：一致性结论要能「被拦截」，而不只是「被展示」。

背景（改动前实测 36 份真实备份）：
- `consistency.check_consistency` 能报 17 类矛盾，但每条 issue 没有 id、没有
  「能不能导出」的位；导出接口只在报告待复核时拦一次，**缺章、报告过短一律照导**；
- 「报告缺章」这件事其实有确定标记 —— `_assemble_final_report` 对空章节写
  「（本部分内容尚未生成）」，但没有任何检查读它；
- 步骤 9 的问题分析与步骤 10 的改进建议是两份自由文本，与评分之间没有任何校验：
  16 个有低分指标的会话里，131/167（78%）个低分指标在问题分析里一次都没被点名；
- `_node_step11` 会删掉模型自述的「综合得分：X 分」再写程序值，**删了不记账**。

这里锁住四件事：
1. 每条 issue 带稳定 id 与 blocking 位，`exportable` 只看结构性缺陷；
2. 缺章必须报出来、并且阻断导出，但用户能显式 force 覆盖（不缺章的人不该被卡住）；
3. 低分指标没被问题/建议点名时，**聚合成一条**（逐条报会变成噪音）；
4. 步骤 11 的替换必须留下痕迹，不许静默改写。
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from backend import database
from backend.agents import performance_agent
from backend.main import app
from backend.middleware import auth_middleware as auth
from backend.models.user_models import User, UserRole
from backend.routes import _shared_state
from backend.routes import agent_routes as agent
from backend.routes import export_routes
from backend.services import consistency

PROJECT = "某某区农村供水保障工程"
#: `_assemble_final_report` 给空章节写的占位文案 —— 缺章的确定标记
EMPTY_CHAPTER = "（本部分内容尚未生成）"


@compiles(MEDIUMTEXT, "sqlite")
def compile_mediumtext(element, compiler, **kwargs):
    return "TEXT"


# ═══════════════════════════════════════════════════════
#  构造状态：与 tests/test_consistency.py 同源的干净样本
# ═══════════════════════════════════════════════════════

def _system():
    """满分 100、每级等于下级之和，且不触发「产出+效益≥60」策略预警。"""
    def leaf(ident, name, score):
        return {"id": ident, "level": 3, "name": name, "score": score}

    def mid(ident, name, score, children):
        return {"id": ident, "level": 2, "name": name, "score": score, "children": children}

    def top(ident, name, score, children):
        return {"id": ident, "level": 1, "name": name, "score": score, "children": children}

    return {"level1_indicators": [
        top("l1_a", "决策", 40, [
            mid("l2_a", "立项规范", 40, [
                leaf("l3_a", "立项依据充分性", 20),
                leaf("l3_b", "立项程序规范性", 20),
            ]),
        ]),
        top("l1_b", "过程", 60, [
            mid("l2_b", "资金管理", 35, [
                leaf("l3_c", "资金到位率", 15),
                leaf("l3_d", "资金使用合规性", 20),
            ]),
            mid("l2_c", "组织实施", 25, [
                leaf("l3_e", "管理制度健全性", 10),
                leaf("l3_f", "制度执行有效性", 15),
            ]),
        ]),
    ]}


#: 全部达标（得分率均 ≥60%）的实得分，合计 88
HIGH_ACTUALS = {"l3_a": 18, "l3_b": 18, "l3_c": 12, "l3_d": 16, "l3_e": 9, "l3_f": 15}
#: 三项明显偏低：资金到位率 5/15、管理制度健全性 2/10、制度执行有效性 3/15
LOW_ACTUALS = {"l3_a": 20, "l3_b": 20, "l3_c": 5, "l3_d": 20, "l3_e": 2, "l3_f": 3}

SCORE_LINE = "综合得分：88.00 分，评价等级：良好"


def _grade(total):
    for threshold, label in ((90, "优秀"), (80, "良好"), (60, "合格")):
        if total >= threshold:
            return label
    return "不合格"


def _report(score_line=SCORE_LINE, extra="", size=400):
    body = "本次评价依据财政项目支出绩效评价管理办法开展，评价组通过资料审查与现场核查开展工作。" * max(1, size // 40)
    return (
        f"# {PROJECT}\n\n## 一、绩效评价工作开展情况\n\n{body}\n\n"
        f"## 五、综合评价分析及结论\n\n{score_line}\n\n{extra}"
    )


def _state(actuals=None, report=None, **overrides):
    actuals = actuals or HIGH_ACTUALS
    total = round(sum(actuals.values()), 2)
    grade = _grade(total)
    line = f"综合得分：{total:.2f} 分，评价等级：{grade}"
    state = {
        "session_id": "gate-fixture",
        "project_name": PROJECT,
        "indicator_system": _system(),
        "indicator_score_map": {k: {"actual_score": v} for k, v in actuals.items()},
        "score_summary": {"total_score": total, "complete": True, "grade": grade,
                          "scored_count": 6, "total_count": 6},
        "comprehensive_evaluation": f"{line}\n\n项目总体执行情况较好。",
        "final_report": report if report is not None else _report(line),
        "completed_steps": list(range(1, 15)),
        "document_coverage": {"total_chars": 12000, "used_chars": 10800,
                              "used_ratio": 0.9, "omitted_chars": 1200},
        "problems_analysis": "1. 立项依据材料有待补充。\n2. 资金到位进度偏慢。",
        "suggestions": "1. 完善立项依据材料归档。\n2. 加快资金拨付进度。",
        "warnings": [],
    }
    state.update(overrides)
    return state


def codes(report):
    return [item["code"] for item in report["issues"]]


# ═══════════════════════════════════════════════════════
#  issue 的 id / blocking / exportable
# ═══════════════════════════════════════════════════════

def test_each_issue_carries_a_stable_id_and_blocking_flag():
    report = consistency.check_consistency(_state(report="太短了"))

    assert "REPORT_TOO_SHORT" in codes(report)
    issue = next(item for item in report["issues"] if item["code"] == "REPORT_TOO_SHORT")
    assert issue["id"] == "REPORT_TOO_SHORT#1"
    assert issue["blocking"] is True
    assert report["blocking_count"] >= 1
    assert report["exportable"] is False


def test_clean_state_is_exportable():
    report = consistency.check_consistency(_state())

    assert report["issues"] == [], codes(report)
    assert report["blocking_count"] == 0
    assert report["exportable"] is True


def test_chapter_placeholder_is_detected_as_a_blocking_error():
    report = consistency.check_consistency(
        _state(report=_report(extra=f"## 六、主要经验做法\n\n{EMPTY_CHAPTER}\n"))
    )

    issue = next(item for item in report["issues"] if item["code"] == "REPORT_CHAPTER_MISSING")
    assert issue["severity"] == "error"
    assert issue["blocking"] is True
    assert "六、主要经验做法" in issue["message"]
    assert report["exportable"] is False


def test_chapter_check_counts_and_names_every_missing_chapter():
    body = (f"## 六、主要经验做法\n\n{EMPTY_CHAPTER}\n\n"
            f"## 八、改进建议\n\n{EMPTY_CHAPTER}\n")
    issue = next(item for item in consistency.check_consistency(_state(report=_report(extra=body)))["issues"]
                 if item["code"] == "REPORT_CHAPTER_MISSING")

    assert "2 章" in issue["message"]
    assert "六、主要经验做法" in issue["message"] and "八、改进建议" in issue["message"]


def test_step_failure_placeholder_also_counts_as_a_missing_chapter():
    """步骤失败时拼装写的是另一句（带「请检查AI模型配置」），同样是缺章。"""
    failed = "（本部分内容尚未生成，请检查AI模型配置后重新运行工作流）"
    report = _report(extra=f"## 六、主要经验做法\n\n{failed}\n")

    codes_ = codes(consistency.check_consistency(_state(report=report)))
    assert "REPORT_CHAPTER_MISSING" in codes_


# ═══════════════════════════════════════════════════════
#  低分指标 × 问题 / 建议的覆盖
# ═══════════════════════════════════════════════════════

def test_low_score_indicators_missing_from_problems_are_reported_once():
    report = consistency.check_consistency(_state(actuals=LOW_ACTUALS))

    gaps = [item for item in report["issues"] if item["code"] == "PROBLEM_COVERAGE_GAP"]
    assert len(gaps) == 1, codes(report)
    assert gaps[0]["severity"] == "warning"
    assert gaps[0]["blocking"] is False
    assert "3/3" in gaps[0]["message"] or "3 个" in gaps[0]["message"]
    assert "管理制度健全性" in gaps[0]["message"]


def test_naming_the_low_score_indicators_silences_the_gap():
    named = ("1. 管理制度健全性不足。\n2. 制度执行有效性偏弱。\n3. 资金到位率偏低。")
    report = consistency.check_consistency(
        _state(actuals=LOW_ACTUALS, problems_analysis=named, suggestions=named)
    )

    assert "PROBLEM_COVERAGE_GAP" not in codes(report)
    assert "SUGGESTION_COVERAGE_GAP" not in codes(report)


def test_coverage_check_stays_silent_when_steps_are_absent():
    """步骤 9/10 没跑（字段不存在）时不报覆盖缺口 —— 那是「还没写」，不是「写漏了」。"""
    state = _state(actuals=LOW_ACTUALS)
    state.pop("problems_analysis")
    state.pop("suggestions")

    assert "PROBLEM_COVERAGE_GAP" not in codes(consistency.check_consistency(state))


def test_suggestions_without_problems_is_reported():
    report = consistency.check_consistency(_state(problems_analysis="", suggestions="1. 加强过程监控。"))

    assert "SUGGESTION_WITHOUT_PROBLEM" in codes(report)


# ═══════════════════════════════════════════════════════
#  导出闸门
# ═══════════════════════════════════════════════════════

@pytest.fixture
def api(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'gate.db').as_posix()}")
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(auth, "SECRET_KEY", "test-only-signing-key")
    monkeypatch.setattr(performance_agent, "STATE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(_shared_state, "active_workflows", {})
    monkeypatch.setattr(_shared_state, "active_workflow_configs", {})
    monkeypatch.setattr(_shared_state, "_WORKFLOW_PROGRESS", {})
    # 渲染 Word/PDF 与本用例无关，闸门判断在渲染之前
    monkeypatch.setattr(export_routes, "_export_with_cache",
                        lambda **kwargs: (str(tmp_path / "fake.docx"), False))
    with factory.begin() as db:
        db.add(User(id=9, username="root", password_hash="unused",
                    role=UserRole.ADMIN, is_active=True))
    headers = {"Authorization": f"Bearer {auth.create_access_token({'user_id': 9})}"}
    yield TestClient(app), headers, tmp_path / "backups"
    engine.dispose()


def _seed_backup(backup_dir: Path, session_id: str, state: dict) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    payload = dict(state)
    payload["session_id"] = session_id
    (backup_dir / f"{session_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_export_is_blocked_when_a_chapter_is_missing(api):
    client, headers, backup_dir = api
    _seed_backup(backup_dir, "blocked", _state(
        report=_report(extra=f"## 六、主要经验做法\n\n{EMPTY_CHAPTER}\n")))

    resp = client.post("/api/agent/workflow/export",
                       json={"session_id": "blocked", "format": "word"}, headers=headers)

    assert resp.status_code == 409, resp.text
    assert resp.headers.get("X-Export-Blocked") == "1"
    assert "六、主要经验做法" in resp.json()["detail"]


def test_force_overrides_the_export_gate(api):
    client, headers, backup_dir = api
    _seed_backup(backup_dir, "blocked", _state(
        report=_report(extra=f"## 六、主要经验做法\n\n{EMPTY_CHAPTER}\n")))

    resp = client.post("/api/agent/workflow/export",
                       json={"session_id": "blocked", "format": "word", "force": True},
                       headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True


def test_complete_report_exports_without_extra_round_trip(api):
    client, headers, backup_dir = api
    _seed_backup(backup_dir, "clean", _state())

    resp = client.post("/api/agent/workflow/export",
                       json={"session_id": "clean", "format": "word"}, headers=headers)

    assert resp.status_code == 200, resp.text


def test_export_fails_open_when_workflow_state_is_missing(api, monkeypatch):
    """状态读不到时必须放行：闸门不能变成「谁都导不出去」的新故障。"""
    from fastapi import HTTPException

    client, headers, backup_dir = api
    _seed_backup(backup_dir, "orphan", _state())

    def missing(session_id):
        raise HTTPException(status_code=404, detail="工作流状态不存在")

    monkeypatch.setattr(agent, "_get_workflow_values", missing)
    # _read_report 也走备份；只有闸门依赖 _get_workflow_values
    resp = client.post("/api/agent/workflow/export",
                       json={"session_id": "orphan", "format": "word"}, headers=headers)

    assert resp.status_code == 200, resp.text


# ═══════════════════════════════════════════════════════
#  步骤 11：删掉模型自述的得分要记账
# ═══════════════════════════════════════════════════════

def _run_step11(monkeypatch, model_text):
    agent_instance = performance_agent.PerformanceAgent.__new__(performance_agent.PerformanceAgent)
    agent_instance.session_id = "step11-fixture"
    agent_instance.model_name = "test-model"
    monkeypatch.setattr(agent_instance, "_execute_step", lambda state, step: model_text)
    state = _state()
    state["warnings"] = []
    return agent_instance._node_step11(state), state


def test_step11_replaces_a_score_line_written_without_a_colon(monkeypatch):
    """模型不一定写成「综合得分：X 分」，也可能写「综合得分 99.00 分」。"""
    state, _ = _run_step11(monkeypatch, "综合得分 99.00 分，与上年基本持平。\n\n本项目总体执行较好。")

    assert state["comprehensive_evaluation"].startswith("综合得分：88.00 分，评价等级：良好")
    assert "99.00" not in state["comprehensive_evaluation"]


def test_step11_records_what_it_replaced(monkeypatch):
    state, _ = _run_step11(monkeypatch, "综合得分：99.00 分，评价等级：优秀\n\n本项目总体执行较好。")

    assert any("99.00" in line and "替换" in line for line in state["warnings"]), state["warnings"]


def test_step11_keeps_prose_that_merely_mentions_scores(monkeypatch):
    """出现「综合得分 / 评价等级」字样但没有具体分值的句子不是断言，不能删。"""
    prose = "各指标综合得分情况总体较好，评价等级说明见附件，未发现明显扣分项。"
    state, _ = _run_step11(monkeypatch, prose)

    assert prose in state["comprehensive_evaluation"]
