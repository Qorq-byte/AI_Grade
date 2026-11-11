"""Batch 36（B4 第一步）：refine 预览、证据注入、版本回滚。

改造前的问题（取证见 `progress_round3.md` B36）：
- 提示词只塞当前正文（`[:8000]` 硬截），**不注入本步证据**，模型只能凭要求改写；
- 截断是静默的：8000 字以后的正文直接消失，模型和用户都不知道；
- 改完立刻落库，前端没有「先看看再决定」的机会；
- `_apply_step_edit` 有 version 与 revisions，但没有任何接口把它们暴露出来。

这组用例先钉住四件事：
1. 截断保留首尾并留标记（不静默丢内容）；
2. 本步证据与上一步摘要真的进了提示词；
3. preview 只出草稿，**不改**任何状态；
4. 回滚把正文恢复成旧版本、版本号继续递增（不倒退，避免与前端缓存的 version 撞车）。
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
from backend.routes import agent_routes
from backend.services import refine


@compiles(MEDIUMTEXT, "sqlite")
def _mediumtext(element, compiler, **kwargs):
    return "TEXT"


# ─── 纯函数：截断 / 提示词 / 差异 ───

def test_clip_keeps_head_and_tail_with_a_marker():
    text = "开头" + "中间" * 1000 + "结尾"

    clipped, truncated = refine.clip(text, 100)

    assert truncated is True
    assert clipped.startswith("开头")
    assert clipped.endswith("结尾")
    assert "省略" in clipped
    assert len(clipped) <= 100 + 40  # 标记本身占几十字


def test_clip_leaves_short_text_alone():
    assert refine.clip("短正文", 100) == ("短正文", False)


def _values(**overrides):
    values = {
        "project_name": "某某项目",
        "step_results": {
            7: {"content": "上一步的正文", "version": 1},
            8: {"content": "这一步的旧正文", "version": 2},
        },
        "step_evidence": {
            "8": [
                {"id": "P1", "source": "可研报告.pdf", "locator": "第 12 行",
                 "kind": "project", "text": "项目实际完成 80%"},
                {"id": "K1", "source": "绩效评价规程", "locator": "第三部分",
                 "kind": "knowledge", "text": "经验做法应可复制"},
            ]
        },
    }
    values.update(overrides)
    return values


def test_prompt_injects_step_evidence_and_neighbour_context():
    built = refine.build_prompt(_values(), 8, "这一步的旧正文", "多写具体做法")

    assert built["evidence_count"] == 2
    assert "[P1]" in built["prompt"] and "[K1]" in built["prompt"]
    assert "可研报告.pdf" in built["prompt"]
    assert "上一步的正文" in built["prompt"]
    assert "某某项目" in built["prompt"]
    assert "多写具体做法" in built["prompt"]


def test_prompt_states_when_no_evidence_instead_of_inventing_ids():
    built = refine.build_prompt(_values(step_evidence={}), 8, "正文", "改一改")

    assert built["evidence_count"] == 0
    assert "资料不足" in built["prompt"]
    assert "[P1]" not in built["prompt"]


def test_prompt_marks_truncated_content_instead_of_dropping_it():
    long_text = "开头" + "正文" * 5000 + "结尾"

    built = refine.build_prompt(_values(), 8, long_text, "压缩一下", content_limit=200)

    assert built["truncated"] is True
    assert "省略" in built["prompt"]
    assert "结尾" in built["prompt"]


def test_diff_summary_counts_added_and_removed_lines():
    diff = refine.diff_summary("甲\n乙\n丙", "甲\n乙改\n丙\n丁")

    assert diff["added"] >= 2
    assert diff["removed"] >= 1
    assert any(row["op"] == "add" and "丁" in row["text"] for row in diff["lines"])


def test_diff_summary_is_empty_for_identical_text():
    diff = refine.diff_summary("一样\n的内容", "一样\n的内容")

    assert diff["added"] == 0 and diff["removed"] == 0 and diff["lines"] == []


def test_diff_summary_caps_the_number_of_rows():
    before = "\n".join(f"第{i}行" for i in range(300))
    after = "\n".join(f"第{i}行改" for i in range(300))

    diff = refine.diff_summary(before, after, max_lines=20)

    assert len(diff["lines"]) == 20
    assert diff["truncated"] is True


# ─── 接口：preview 不落盘 / 回滚 ───

class _FakeModel:
    def __init__(self, *args, **kwargs):
        pass

    def invoke(self, prompt):
        _FakeModel.last_prompt = prompt
        return type("R", (), {"content": "改写后的正文\n第二行"})()


@pytest.fixture
def api(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'refine.db').as_posix()}")
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(auth, "SECRET_KEY", "test-only-signing-key")
    monkeypatch.setattr(performance_agent, "STATE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(agent_routes, "ChatOpenAI", _FakeModel)
    monkeypatch.setattr("backend.utils.llm_helper.get_model_credentials",
                        lambda model_name: {"api_key": "k", "base_url": ""})
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "s1.json").write_text(json.dumps({
        "session_id": "s1", "project_name": "某某项目",
        "completed_steps": [1, 2, 3, 4, 5, 6, 7, 8],
        "step_results": {
            "7": {"content": "上一步的正文", "version": 1, "edited_by": "workflow"},
            "8": {"content": "这一步的旧正文\n第二行", "version": 2, "edited_by": "workflow",
                  "revisions": [{"version": 1, "content": "最初正文", "edited_by": "workflow"}]},
        },
        "step_evidence": {"8": [{"id": "P1", "source": "可研报告.pdf", "locator": "第 12 行",
                                 "kind": "project", "text": "项目实际完成 80%"}]},
        "review_steps": [],
    }, ensure_ascii=False), encoding="utf-8")
    with factory.begin() as db:
        db.add(User(id=5, username="u5", password_hash="unused", role=UserRole.USER, is_active=True))
        db.add(WorkflowSession(session_id="s1", user_id=5, project_name="某某项目", status="completed"))
    headers = {"Authorization": f"Bearer {auth.create_access_token({'user_id': 5})}"}
    yield TestClient(app), headers
    engine.dispose()


def _state():
    """走 hydrate 读入口，和路由看到的一致。

    直接读 JSON 会看到 compact 后的 `content_ref`/`content_preview`
    （步骤 8 的正文与 `experience_summary` 逐字相同时会被换成引用）。
    """
    return performance_agent.PerformanceAgent.load_state_backup("s1")


def test_preview_returns_draft_without_touching_the_step(api):
    client, headers = api

    resp = client.post("/api/agent/workflow/refine", headers=headers,
                       json={"session_id": "s1", "step_number": 8,
                             "message": "多写具体做法", "preview": True})

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["draft"] == "改写后的正文\n第二行"
    assert data["version"] == 2
    assert data["evidence_count"] == 1
    assert data["diff"]["added"] >= 1

    stored = _state()["step_results"]["8"]
    assert stored["content"] == "这一步的旧正文\n第二行"
    assert stored["version"] == 2


def test_preview_prompt_really_carries_the_evidence(api):
    client, headers = api

    client.post("/api/agent/workflow/refine", headers=headers,
                json={"session_id": "s1", "step_number": 8,
                      "message": "多写具体做法", "preview": True})

    assert "[P1]" in _FakeModel.last_prompt
    assert "可研报告.pdf" in _FakeModel.last_prompt


def test_apply_path_still_writes_and_bumps_version(api):
    client, headers = api

    resp = client.post("/api/agent/workflow/refine", headers=headers,
                       json={"session_id": "s1", "step_number": 8, "message": "改"})

    assert resp.status_code == 200, resp.text
    stored = _state()["step_results"]["8"]
    assert stored["content"] == "改写后的正文\n第二行"
    assert stored["version"] == 3
    assert stored["edited_by"] == "model_refine"


def test_history_lists_versions_newest_first(api):
    client, headers = api

    rows = client.get("/api/agent/workflow/s1/steps/8/history", headers=headers).json()["data"]

    versions = [row["version"] for row in rows]
    assert versions == [2, 1]
    assert rows[0]["is_current"] is True
    assert rows[1]["edited_by"] == "workflow"


def test_rollback_restores_content_and_keeps_version_monotonic(api):
    client, headers = api

    resp = client.post("/api/agent/workflow/s1/steps/8/rollback", headers=headers,
                       json={"to_version": 1})

    assert resp.status_code == 200, resp.text
    stored = _state()["step_results"]["8"]
    assert stored["content"] == "最初正文"
    # 版本继续往上走：回滚也是一次修改，不能让前端缓存的 version 撞车
    assert stored["version"] == 3
    assert stored["edited_by"] == "rollback"


def test_rollback_to_unknown_version_is_404(api):
    client, headers = api

    resp = client.post("/api/agent/workflow/s1/steps/8/rollback", headers=headers,
                       json={"to_version": 99})

    assert resp.status_code == 404


def test_preview_is_rejected_while_the_workflow_is_running(api, monkeypatch):
    from backend.routes import _shared_state

    client, headers = api
    monkeypatch.setattr(_shared_state, "_WORKFLOW_PROGRESS", {"s1": {"status": "running"}})

    resp = client.post("/api/agent/workflow/refine", headers=headers,
                       json={"session_id": "s1", "step_number": 8,
                             "message": "改", "preview": True})

    assert resp.status_code == 409
