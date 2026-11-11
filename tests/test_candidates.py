"""批次 40（B6 第一步）：多模型候选的可比较与择优。

改造前的两个真问题（取证见 `progress_round3.md` B40）：
- 多模型分支 `return next(iter(outputs.values()))` —— 永远取第一个；
- `_record_step_result` 用 `{"default": content}` 覆盖 `raw_outputs`，候选当场丢失；
- `comparison_history` 不在备份白名单里，重启后候选全部消失。

这组用例钉住：评分可复算、并列不给推荐、候选落进 step_results、可回读、可人工改选。
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
from backend.agents.performance_agent import PerformanceAgent
from backend.main import app
from backend.middleware import auth_middleware as auth
from backend.models.user_models import User, UserRole, WorkflowSession
from backend.services import candidates


@compiles(MEDIUMTEXT, "sqlite")
def _mediumtext(element, compiler, **kwargs):
    return "TEXT"


def _system(*names):
    return {"level1_indicators": [{
        "id": "l1", "name": "产出与效益", "level": 1, "score": 100,
        "children": [{"id": "l2", "name": "产出", "level": 2, "score": 100,
                      "children": [{"id": f"leaf-{index}", "name": name, "level": 3, "score": 100}
                                   for index, name in enumerate(names)]}],
    }]}


GOOD = """一、完成情况

项目完成率 92%，满意度 90%，及时率 88%。[P1]

二、分析

三项指标均达到目标，其中完成率超出 2 个百分点。
"""

WEAK = "整体还行，基本完成。"


def _values():
    return {
        "indicator_system": _system("完成率", "满意度", "及时率"),
        "step_evidence": {"8": [{"id": "P1", "source": "x.pdf", "locator": "第 1 行",
                                 "kind": "project", "text": "完成率 92%"}]},
        "step_results": {8: {"content": GOOD, "version": 1}},
        "comparison_history": {8: {
            "step_number": 8, "models_used": ["a", "b"],
            "individual_outputs": {"a": GOOD, "b": WEAK},
            "selected_best": None,
        }},
    }


def test_rank_prefers_the_candidate_with_locatable_evidence():
    ranked = candidates.rank(_values(), 8)

    assert [item["model"] for item in ranked] == ["a", "b"]
    assert ranked[0]["score"] > ranked[1]["score"]
    assert ranked[0]["recommended"] is True
    assert ranked[1]["recommended"] is False
    assert any("可定位" in reason for reason in ranked[0]["reasons"])


def test_ties_have_no_recommendation():
    values = _values()
    values["comparison_history"][8]["individual_outputs"] = {"a": WEAK, "b": WEAK}

    ranked = candidates.rank(values, 8)

    assert not any(item["recommended"] for item in ranked)


def test_unlocatable_references_are_called_out():
    values = _values()
    bad = GOOD.replace("[P1]", "[X9]")
    values["comparison_history"][8]["individual_outputs"] = {"a": bad, "b": WEAK}

    ranked = candidates.rank(values, 8)

    assert any("0/1" in reason for reason in ranked[0]["reasons"])


def test_entry_lookup_accepts_string_keys_from_backup():
    values = {"comparison_history": {"8": {"individual_outputs": {"a": "x"}}}}

    assert candidates.entry_for(values, 8)["individual_outputs"] == {"a": "x"}


def test_candidates_fall_back_to_step_raw_outputs():
    values = {"step_results": {8: {"content": "x", "raw_outputs": {"a": "A", "b": "B"}}}}

    assert candidates.candidates_of(values, 8) == {"a": "A", "b": "B"}


def test_record_step_result_keeps_the_candidates(monkeypatch):
    """改造前 raw_outputs 会被覆盖成 {"default": content}，候选全丢。"""
    agent = PerformanceAgent.__new__(PerformanceAgent)
    agent.model_name = "a"
    state = _values()
    state["step_results"] = {}
    state["step_control"] = {}

    agent._record_step_result(state, 8, GOOD)

    result = state["step_results"][8]
    assert set(result["raw_outputs"]) == {"a", "b"}
    assert result["comparison_results"]["selected_best"] == "a"
    assert result["selected_model"] == "a"


def test_backup_whitelist_keeps_comparison_history(monkeypatch):
    """comparison_history 不在白名单里，重启后候选就没了（这次不能再漏）。"""
    agent = PerformanceAgent.__new__(PerformanceAgent)
    agent.session_id = "s1"
    captured = {}
    monkeypatch.setattr(PerformanceAgent, "update_state_backup",
                        staticmethod(lambda sid, updates: captured.update(updates)))
    state = {"session_id": "s1", "comparison_history": {8: {"models_used": ["a", "b"]}}}

    agent._persist_state_backup(state)

    assert captured["comparison_history"] == {8: {"models_used": ["a", "b"]}}


# ─── 接口 ───

@pytest.fixture
def api(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'cmp.db').as_posix()}")
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(auth, "SECRET_KEY", "test-only-signing-key")
    monkeypatch.setattr(performance_agent, "STATE_BACKUP_DIR", tmp_path / "backups")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "s1.json").write_text(json.dumps({
        "session_id": "s1", "project_name": "P", "completed_steps": [8],
        "indicator_system": _system("完成率", "满意度", "及时率"),
        "step_evidence": {"8": [{"id": "P1", "source": "x.pdf", "locator": "第 1 行",
                                 "kind": "project", "text": "完成率 92%"}]},
        "step_results": {"8": {"content": GOOD, "version": 1}},
        "comparison_history": {"8": {
            "step_number": 8, "models_used": ["a", "b"],
            "individual_outputs": {"a": GOOD, "b": WEAK}, "selected_best": None,
        }},
    }, ensure_ascii=False), encoding="utf-8")
    with factory.begin() as db:
        db.add(User(id=5, username="u5", password_hash="unused", role=UserRole.USER, is_active=True))
        db.add(WorkflowSession(session_id="s1", user_id=5, project_name="P", status="completed"))
    headers = {"Authorization": f"Bearer {auth.create_access_token({'user_id': 5})}"}
    yield TestClient(app), headers
    engine.dispose()


def _state():
    return PerformanceAgent.load_state_backup("s1")


def test_comparison_endpoint_lists_candidates_with_scores(api):
    client, headers = api

    body = client.get("/api/agent/workflow/s1/comparison/8", headers=headers).json()

    models = [item["model"] for item in body["data"]["candidates"]]
    assert models == ["a", "b"]
    assert body["data"]["recommended"] == "a"
    assert body["data"]["candidates"][0]["score"] > body["data"]["candidates"][1]["score"]


def test_select_candidate_writes_content_and_remembers_the_choice(api):
    client, headers = api

    resp = client.post("/api/agent/workflow/comparison/select", headers=headers,
                       json={"session_id": "s1", "step_number": 8, "model": "b"})

    assert resp.status_code == 200, resp.text
    state = _state()
    assert state["step_results"]["8"]["content"] == WEAK
    assert state["comparison_history"]["8"]["selected_best"] == "b"


def test_select_unknown_candidate_is_404(api):
    client, headers = api

    resp = client.post("/api/agent/workflow/comparison/select", headers=headers,
                       json={"session_id": "s1", "step_number": 8, "model": "nope"})

    assert resp.status_code == 404


def test_status_exposes_comparison_summary(api):
    client, headers = api

    data = client.get("/api/agent/workflow/s1/status", headers=headers).json()["data"]
    summaries = {item["step_number"]: item for item in data["step_summaries"]}

    assert summaries[8]["comparison"]["recommended"] == "a"
    assert summaries[8]["comparison"]["models"] == ["a", "b"]
