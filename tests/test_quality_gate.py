"""批次 27 = B3：质量门禁 —— 让「这份报告值不值得复核」变成可解释的结论。

取证先说清楚（`tests/_probe_quality_evidence.py` 回放 38 份真实备份）：

- 现成的质量台账**可用率极不均衡**：`score_disposition` 53%、`document_coverage` 74%、
  `step5_coverage` / `step6_table` / `step7_length` 只有 16%（批次 20/21 才引入），
  `standard_purity` **0%** —— 它算了、存进了内存状态，但**不在备份白名单里**，重启即丢；
- `document_coverage.used_ratio`、`step5/6` 覆盖率在全部样本上**恒为 1.0**，做判据等于没有信息；
- 评分落地率是**明显双峰**：11 份老会话 0.04–0.31（`not_mentioned` / `model_pending` /
  `anchor_unresolved`），8 份新会话 0.97–1.00，中间只有一个 0.73。

所以这里**不打 0–100 分**（78 分改变不了任何人的行为，只会变成又一个没人看的数字），
只输出三类东西：每步的字数事实、可解释的 flags、以及明确标注「无从判断」的项。

锁住四件事：
1. 每步的字数与状态有确定取数口径（`step_results` → `content`，键可能是 str 也可能是 int）；
2. flags 的阈值按步定（步骤 3 的 1,500 字是残次，步骤 12 的 1,500 字是正常）；
3. **没数据不等于通过** —— 台账缺失时 verdict 是 `unknown`，不是 `pass`；
4. `standard_purity` 必须落盘，否则质量门禁的这一项对重放/重启后的会话永远是"无从判断"。
"""
import json

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
from backend.models.user_models import User, UserRole, WorkflowSession
from backend.routes import _shared_state
from backend.routes import agent_routes as agent
from backend.services import quality

PROJECT = "某某区农村供水保障工程"


@compiles(MEDIUMTEXT, "sqlite")
def compile_mediumtext(element, compiler, **kwargs):
    return "TEXT"


# ═══════════════════════════════════════════════════════
#  构造状态：14 步都有足够正文，台账齐全
# ═══════════════════════════════════════════════════════

def _steps_text(chars=None, steps=None):
    """按各步中位数的 1.5 倍造正文，保证默认不触发「偏少」。"""
    chars = chars or {}
    targets = steps or range(1, 15)
    results = {}
    for step in targets:
        size = chars.get(step, int(quality.STEP_MEDIAN_CHARS[step] * 1.5))
        results[step] = {"content": "评" * size, "status": "completed"}
    return results


def _system():
    """最小但自洽的指标体系：分值自下而上相等，且不触发策略维度预警。"""
    return {"level1_indicators": [
        {"id": "l1_a", "level": 1, "name": "决策", "score": 100, "children": [
            {"id": "l2_a", "level": 2, "name": "立项规范", "score": 100, "children": [
                {"id": "l3_a", "level": 3, "name": "立项依据充分性", "score": 50},
                {"id": "l3_b", "level": 3, "name": "立项程序规范性", "score": 50},
            ]},
        ]},
    ]}


def _state(**overrides):
    state = {
        "session_id": "quality-fixture",
        "project_name": PROJECT,
        "indicator_system": _system(),
        "indicator_score_map": {"l3_a": {"actual_score": 45}, "l3_b": {"actual_score": 45}},
        "score_summary": {"total_score": 90.0, "complete": True, "grade": "优秀",
                          "scored_count": 2, "total_count": 2},
        "step_results": _steps_text(),
        "completed_steps": list(range(1, 15)),
        "final_report": "# 报告\n\n" + "本次评价依据管理办法开展。" * 30,
        "score_disposition": {
            "step": 7, "total": 20,
            "counts": {"accepted": 20},
            "accepted_score": 88.0, "no_evidence_scored": 0,
            "unmatched": [],
        },
        "standard_purity": {"leaves": 26, "with_verdict": 0, "ids": []},
        "truncated_steps": [],
        "warnings": [],
    }
    state.update(overrides)
    return state


def _codes(report):
    return [item["code"] for item in report["flags"]]


def _flag(report, code):
    for item in report["flags"]:
        if item["code"] == code:
            return item
    return None


# ═══════════════════════════════════════════════════════
#  一、每步事实的取数口径
# ═══════════════════════════════════════════════════════

def test_each_step_carries_char_count_and_status():
    report = quality.check_quality(_state())

    assert len(report["steps"]) == 14
    first = report["steps"][0]
    assert set(first) >= {"step", "name", "chars", "status"}
    assert first["status"] == "ok"
    assert first["chars"] == len(_state()["step_results"][1]["content"])


def test_step_results_keys_may_be_strings():
    """备份里 `step_results` 的键是 JSON 字符串 —— 按 int 直接取会全部落空。"""
    state = _state()
    state["step_results"] = {str(k): v for k, v in state["step_results"].items()}

    report = quality.check_quality(state)

    assert all(item["status"] == "ok" for item in report["steps"])
    assert report["checked"] is True


def test_only_completed_steps_are_checked_for_missing():
    """只跑到步骤 5 的会话，不该被判 9 个「步骤缺失」—— 那是还没跑，不是缺。"""
    state = _state(step_results=_steps_text(steps=range(1, 6)),
                   completed_steps=[1, 2, 3, 4, 5], final_report="")
    state["score_disposition"] = {}

    report = quality.check_quality(state)

    assert [item["step"] for item in report["steps"]] == [1, 2, 3, 4, 5]
    assert "STEP_MISSING" not in _codes(report)


# ═══════════════════════════════════════════════════════
#  二、阈值按步定，不是一个数管 14 步
# ═══════════════════════════════════════════════════════

def test_threshold_follows_each_steps_own_median():
    """同样的 1,500 字：步骤 3（中位 12,765）算偏少，步骤 12（中位 2,063）算正常。"""
    ok = quality.check_quality(_state(
        step_results=_steps_text(chars={3: 1500, 12: 1500})))
    thin = _flag(ok, "STEP_THIN")

    assert thin is not None
    assert thin["steps"] == [3], thin


def test_empty_step_is_missing_not_thin():
    """空正文与「写得太短」是两件事，不能混成一条提示。"""
    report = quality.check_quality(_state(
        step_results=_steps_text(chars={9: 0})))

    assert _flag(report, "STEP_MISSING")["steps"] == [9]
    assert _flag(report, "STEP_THIN") is None


def test_thin_step_message_quotes_the_actual_ratio():
    report = quality.check_quality(_state(
        step_results=_steps_text(chars={7: 155})))

    thin = _flag(report, "STEP_THIN")
    assert "155" in thin["message"]
    assert str(int(quality.STEP_MEDIAN_CHARS[7])) in thin["message"].replace(",", "")


# ═══════════════════════════════════════════════════════
#  三、评分落地：双峰里的低那一半必须被标出来
# ═══════════════════════════════════════════════════════

def test_low_landing_ratio_is_flagged():
    report = quality.check_quality(_state(score_disposition={
        "step": 7, "total": 23, "counts": {"accepted": 5, "not_mentioned": 18},
        "accepted_score": 20.0, "no_evidence_scored": 0, "unmatched": [],
    }))

    flag = _flag(report, "SCORE_NOT_LANDED")
    assert flag["level"] == "warn"
    assert "5/23" in flag["message"]
    assert report["review_required"] is True


def test_tiny_sample_landing_is_not_judged():
    """只有 3 个指标时落地率没有统计意义，不能据此说报告不可信。"""
    report = quality.check_quality(_state(score_disposition={
        "step": 7, "total": 3, "counts": {"accepted": 1, "model_pending": 2},
        "accepted_score": 10.0, "no_evidence_scored": 0, "unmatched": [],
    }))

    assert "SCORE_NOT_LANDED" not in _codes(report)


def test_full_landing_with_no_evidence_is_disclosed_but_not_blocking():
    """新会话的真实形态：23/23 全部落地，但 15 项是「未取得佐证」计 0 分。"""
    report = quality.check_quality(_state(score_disposition={
        "step": 7, "total": 23, "counts": {"accepted": 23},
        "accepted_score": 41.0, "no_evidence_scored": 15, "unmatched": [],
    }))

    flag = _flag(report, "NO_EVIDENCE_SCORED")
    assert flag["level"] == "info"
    assert "15" in flag["message"]
    assert report["review_required"] is False


# ═══════════════════════════════════════════════════════
#  四、没数据 ≠ 通过
# ═══════════════════════════════════════════════════════

def test_missing_ledgers_are_unverified_not_passed():
    """台账缺失时那两项既不进 `verified`，也不许把结论说成 `pass`。

    回放 38 份存量时发现：`standard_purity` 0 份有值、`score_disposition` 只有 53%。
    若一律算 `unknown`，55% 的存量报告会变成一句「不知道」——可它们的 14 步产出
    其实都查过了。若算 `pass` 又是撒谎。所以单列 `partial`：
    **能查的通过了，没查的明确说出来**。
    """
    report = quality.check_quality(_state(score_disposition={}, standard_purity={}))

    assert report["verdict"] == "partial"
    assert report["review_required"] is False
    unavailable = {item["code"] for item in report["unavailable"]}
    assert {"SCORE_NOT_LANDED", "STANDARD_CARRY_VERDICT"} <= unavailable
    # 关键：没跑过的判据不能出现在「已验证」里
    assert "SCORE_NOT_LANDED" not in report["verified"]
    assert "STANDARD_CARRY_VERDICT" not in report["verified"]
    assert "STEP_CONTENT" in report["verified"]


def test_verified_lists_what_actually_ran():
    report = quality.check_quality(_state())

    assert report["verified"] == ["STEP_CONTENT", "STEP_TRUNCATED",
                                  "REPORT_BLOCKED", "SCORE_NOT_LANDED",
                                  "STANDARD_CARRY_VERDICT"]


@pytest.mark.parametrize("overrides", [
    {},                                              # 台账齐全
    {"score_disposition": {}, "standard_purity": {}},  # 两个台账都缺
    {"score_disposition": {}},                       # 只缺落地台账
    {"standard_purity": {}},                         # 只缺纯净度台账
    {"final_report": "", "completed_steps": [1, 2]},  # 还没生成报告
])
def test_verified_and_unavailable_never_overlap(overrides):
    """一条判据不能既算「验证过」又算「无从判断」—— 那是自相矛盾的结论。"""
    report = quality.check_quality(_state(**overrides))

    verified = set(report["verified"])
    unavailable = {item["code"] for item in report["unavailable"]}
    assert verified & unavailable == set(), (verified, unavailable)


def test_clean_state_with_all_ledgers_passes():
    report = quality.check_quality(_state())

    assert report["verdict"] == "pass"
    assert report["flags"] == []
    assert report["unavailable"] == []


def test_standard_purity_hits_are_flagged():
    report = quality.check_quality(_state(
        standard_purity={"leaves": 26, "with_verdict": 3, "ids": ["l3_a"]}))

    flag = _flag(report, "STANDARD_CARRY_VERDICT")
    assert flag["level"] == "warn"
    assert "3/26" in flag["message"]


def test_nothing_to_check_is_not_a_pass():
    """空会话（没跑过、没报告）不该被标成「通过」，也不该被标成「部分项未验证」。"""
    report = quality.check_quality({"session_id": "empty"})

    assert report["checked"] is False
    assert report["verdict"] == "unknown"
    assert report["verified"] == []


# ═══════════════════════════════════════════════════════
#  五、与一致性结论的关系：不重复一套口径
# ═══════════════════════════════════════════════════════

def test_blocking_consistency_issue_becomes_a_block_flag():
    """缺章这类结构性缺陷由 consistency 判，质量门禁只负责把它带上来。"""
    report = quality.check_quality(_state(
        final_report="# 报告\n\n## 五、综合评价分析及结论\n\n（本部分内容尚未生成，请检查AI模型配置后重新运行工作流）\n"))

    flag = _flag(report, "REPORT_BLOCKED")
    assert flag["level"] == "block"
    assert "REPORT_CHAPTER_MISSING" in flag["message"]
    assert report["verdict"] == "review"


def test_warning_level_consistency_issues_are_not_duplicated():
    """口径类提示（如分值分配偏差）不进质量门禁，否则两套结论会互相打架。"""
    report = quality.check_quality(_state())

    assert all(not item["code"].startswith("INDICATOR_ALLOCATION") for item in report["flags"])


# ═══════════════════════════════════════════════════════
#  六、落盘：standard_purity 不落盘，质量门禁就永远"无从判断"
# ═══════════════════════════════════════════════════════

def test_standard_purity_is_persisted_to_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(performance_agent, "STATE_BACKUP_DIR", tmp_path)
    state = _state()
    state["session_id"] = "persist-purity"

    persistence = performance_agent.PerformanceAgent.__new__(
        performance_agent.PerformanceAgent)
    persistence.session_id = "persist-purity"
    persistence._persist_state_backup(state)

    payload = json.loads((tmp_path / "persist-purity.json").read_text(encoding="utf-8"))
    assert payload["standard_purity"] == {"leaves": 26, "with_verdict": 0, "ids": []}


# ═══════════════════════════════════════════════════════
#  七、接口
# ═══════════════════════════════════════════════════════

@pytest.fixture
def api(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'quality.db').as_posix()}")
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(auth, "SECRET_KEY", "test-only-signing-key")
    monkeypatch.setattr(performance_agent, "STATE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(_shared_state, "active_workflows", {})
    monkeypatch.setattr(_shared_state, "active_workflow_configs", {})
    monkeypatch.setattr(_shared_state, "_WORKFLOW_PROGRESS", {})
    with factory.begin() as db:
        db.add(User(id=9, username="root", password_hash="unused",
                    role=UserRole.ADMIN, is_active=True))
    headers = {"Authorization": f"Bearer {auth.create_access_token({'user_id': 9})}"}
    yield TestClient(app), headers, tmp_path / "backups", factory
    engine.dispose()


def _seed(backup_dir, session_id, state):
    backup_dir.mkdir(parents=True, exist_ok=True)
    payload = dict(state)
    payload["session_id"] = session_id
    (backup_dir / f"{session_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_quality_endpoint_returns_the_same_verdict(api):
    client, headers, backup_dir, _ = api
    _seed(backup_dir, "q1", _state(score_disposition={
        "step": 7, "total": 23, "counts": {"accepted": 5, "not_mentioned": 18},
        "accepted_score": 20.0, "no_evidence_scored": 0, "unmatched": [],
    }))

    resp = client.get("/api/agent/workflow/q1/quality", headers=headers)

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["review_required"] is True
    assert data["verdict"] == "review"
    assert any(item["code"] == "SCORE_NOT_LANDED" for item in data["flags"])


def test_quality_endpoint_404_when_state_is_missing(api):
    client, headers, _, _ = api

    resp = client.get("/api/agent/workflow/nope/quality", headers=headers)

    assert resp.status_code == 404


def test_reports_list_carries_a_review_verdict(api):
    client, headers, backup_dir, factory = api
    _seed(backup_dir, "listed", _state(score_disposition={
        "step": 7, "total": 23, "counts": {"accepted": 5},
        "accepted_score": 20.0, "no_evidence_scored": 0, "unmatched": [],
    }))
    with factory.begin() as db:
        db.add(WorkflowSession(session_id="listed", user_id=9,
                               project_name=PROJECT, status="completed"))

    resp = client.get("/api/client/reports", headers=headers)

    assert resp.status_code == 200, resp.text
    row = resp.json()["data"][0]
    assert row["review"]["required"] is True
    assert row["review"]["verdict"] == "review"
    assert row["review"]["reasons"]


def test_reports_list_stays_usable_when_backup_is_gone(api):
    """备份被删/损坏时列表必须照常返回，不能因为算不出质量就 500。"""
    client, headers, _, factory = api
    with factory.begin() as db:
        db.add(WorkflowSession(session_id="orphan", user_id=9,
                               project_name=PROJECT, status="completed"))

    resp = client.get("/api/client/reports", headers=headers)

    assert resp.status_code == 200, resp.text
    row = resp.json()["data"][0]
    assert row["review"]["checked"] is False
    assert row["review"]["verdict"] == "unknown"


def test_reports_list_degrades_to_unknown_when_the_check_raises(api, monkeypatch):
    """结论算崩了也只能降级成「未验证」，绝不许降级成「通过」。

    这是列表侧 fail-open 的最后一道：`check_session` 本身已吞异常，但
    `review_brief` 或未来的改法仍可能抛 —— 那条兜底分支必须也是 unknown。
    """
    client, headers, backup_dir, factory = api
    monkeypatch.setattr(quality, "check_session",
                        lambda session_id: (_ for _ in ()).throw(RuntimeError("boom")))
    with factory.begin() as db:
        db.add(WorkflowSession(session_id="boom", user_id=9,
                               project_name=PROJECT, status="completed"))

    resp = client.get("/api/client/reports", headers=headers)

    assert resp.status_code == 200, resp.text
    row = resp.json()["data"][0]
    assert row["review"]["checked"] is False
    assert row["review"]["verdict"] == "unknown"
    assert row["review"]["required"] is False
