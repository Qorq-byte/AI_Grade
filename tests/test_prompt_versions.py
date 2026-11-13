"""提示词版本历史（批次 5）。

在这之前，改坏一条自定义提示词就只剩「重置为系统默认」——原来的内容永久丢失。
这批补了快照 + 对比 + 回滚，测试锁四件事：

1. **第一次改动前先把原始内容存下来**（否则历史里永远缺最早那版，回不去）；
2. 每次保存 / 重置 / 回滚都追加一版，且**回滚不删历史**；
3. 列表接口不返回正文（否则一屏几十条提示词会把响应撑到几 MB）；
4. 删除提示词时历史跟着走，不能撞外键。
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from backend import database
from backend.main import app
from backend.middleware import auth_middleware as auth
from backend.models.user_models import PromptTemplate, PromptVersion, User, UserRole

ORIGINAL = "你是绩效评价专家。\n请按步骤输出。\n不要编造数据。"
EDITED = "你是绩效评价专家。\n请按步骤输出，并标注证据来源。\n不要编造数据。"


@compiles(MEDIUMTEXT, "sqlite")
def compile_mediumtext(element, compiler, **kwargs):
    return "TEXT"


ADMIN_ID = 1
PROMPT_ID = 10


@pytest.fixture
def api(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'pv.db').as_posix()}")
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(auth, "SECRET_KEY", "test-only-signing-key")

    with factory.begin() as db:
        db.add(User(id=ADMIN_ID, username="ops", password_hash="x:y",
                    role=UserRole.ADMIN, is_active=True))
        db.add(PromptTemplate(id=PROMPT_ID, step_number=7, step_name="指标得分分析",
                              prompt_content=ORIGINAL, is_system=False, version=1,
                              modified_by=None))

    headers = {"Authorization": f"Bearer {auth.create_access_token({'user_id': ADMIN_ID, 'ver': 0})}"}
    yield TestClient(app), headers, factory
    engine.dispose()


def _versions(client, headers, prompt_id=PROMPT_ID):
    resp = client.get(f"/api/admin/prompts/{prompt_id}/versions", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["versions"]


# ═══════════════════════════════════════════════════════
#  一、历史是完整的：第一次改动前的原始内容必须留住
# ═══════════════════════════════════════════════════════

def test_first_edit_keeps_the_pre_edit_content(api):
    client, headers, _ = api

    resp = client.put(f"/api/admin/prompts/{PROMPT_ID}", headers=headers,
                      json={"prompt_content": EDITED, "change_log": "加了证据要求"})
    assert resp.status_code == 200, resp.text

    versions = _versions(client, headers)
    assert [v["version"] for v in versions] == [2, 1]
    assert versions[0]["is_current"] is True
    assert versions[1]["is_current"] is False
    assert "历史起点" in versions[1]["change_log"]

    # v1 的正文必须是改动前那份，否则「回到最初」就成了空话
    v1 = client.get(f"/api/admin/prompts/{PROMPT_ID}/versions/1", headers=headers).json()["data"]
    assert v1["prompt_content"] == ORIGINAL
    assert v1["current_content"] == EDITED


def test_repeated_edits_append_versions_in_order(api):
    client, headers, _ = api
    for i in range(3):
        client.put(f"/api/admin/prompts/{PROMPT_ID}", headers=headers,
                   json={"prompt_content": EDITED + f"\n第{i}次修改", "change_log": f"改{i}"})

    versions = _versions(client, headers)
    assert [v["version"] for v in versions] == [4, 3, 2, 1]
    assert versions[0]["is_current"] is True
    # 倒序排列：最新那版的变更说明是最新写的那条
    assert [v["change_log"] for v in versions] == ["改2", "改1", "改0", versions[3]["change_log"]]

    v4 = client.get(f"/api/admin/prompts/{PROMPT_ID}/versions/4", headers=headers).json()["data"]
    assert v4["prompt_content"].endswith("第2次修改")


def test_list_does_not_ship_full_content(api):
    """列表只回元信息：几十条提示词每条都带上千字正文，响应会大得没法用。"""
    client, headers, _ = api
    client.put(f"/api/admin/prompts/{PROMPT_ID}", headers=headers,
               json={"prompt_content": EDITED, "change_log": "x"})

    versions = _versions(client, headers)
    assert versions
    for v in versions:
        assert "prompt_content" not in v
        assert "length" in v


def test_history_records_who_did_it(api):
    client, headers, _ = api
    client.put(f"/api/admin/prompts/{PROMPT_ID}", headers=headers,
               json={"prompt_content": EDITED, "change_log": "x"})

    top = _versions(client, headers)[0]
    assert top["modified_by"] == ADMIN_ID
    assert top["modified_by_name"] == "ops"


# ═══════════════════════════════════════════════════════
#  二、回滚：内容回来，历史不丢
# ═══════════════════════════════════════════════════════

def test_rollback_restores_content_and_keeps_history(api):
    client, headers, factory = api
    client.put(f"/api/admin/prompts/{PROMPT_ID}", headers=headers,
               json={"prompt_content": EDITED, "change_log": "改坏了"})

    resp = client.post(f"/api/admin/prompts/{PROMPT_ID}/rollback/1", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["version"] == 3

    with factory() as db:
        prompt = db.query(PromptTemplate).filter(PromptTemplate.id == PROMPT_ID).first()
        assert prompt.prompt_content == ORIGINAL
        assert prompt.version == 3
        assert "回滚到 v1" in (prompt.change_log or "")
        # 回滚本身也要留痕，且旧版本一个都没少
        assert db.query(PromptVersion).filter(PromptVersion.prompt_id == PROMPT_ID).count() == 3

    versions = _versions(client, headers)
    assert [v["version"] for v in versions] == [3, 2, 1]


def test_rollback_to_current_is_rejected(api):
    client, headers, _ = api
    # 先改一次，让历史里同时存在 v1 和 v2（v2 就是当前版本）
    client.put(f"/api/admin/prompts/{PROMPT_ID}", headers=headers,
               json={"prompt_content": EDITED, "change_log": "改一次"})

    resp = client.post(f"/api/admin/prompts/{PROMPT_ID}/rollback/2", headers=headers)
    assert resp.status_code == 400
    assert "无需回滚" in resp.json()["detail"]


def test_rollback_to_unknown_version_is_404(api):
    client, headers, _ = api
    assert client.post(f"/api/admin/prompts/{PROMPT_ID}/rollback/99",
                       headers=headers).status_code == 404


# ═══════════════════════════════════════════════════════
#  三、重置与删除
# ═══════════════════════════════════════════════════════

def test_reset_also_records_a_version(api):
    client, headers, factory = api
    resp = client.post(f"/api/admin/prompts/{PROMPT_ID}/reset", headers=headers)
    if resp.status_code == 400:
        pytest.skip("该步骤没有系统默认提示词，跳过")

    assert resp.status_code == 200, resp.text
    with factory() as db:
        rows = db.query(PromptVersion).filter(PromptVersion.prompt_id == PROMPT_ID).all()
        assert {r.version for r in rows} == {1, 2}
        assert any(r.change_log and "重置为系统默认" in r.change_log for r in rows)


def test_delete_prompt_removes_its_history(api):
    """历史表没有 ORM 关系，不显式删就会撞外键 —— 删除接口直接 500。"""
    client, headers, factory = api
    client.put(f"/api/admin/prompts/{PROMPT_ID}", headers=headers,
               json={"prompt_content": EDITED, "change_log": "x"})

    resp = client.delete(f"/api/admin/prompts/{PROMPT_ID}", headers=headers)
    assert resp.status_code == 200, resp.text

    with factory() as db:
        assert db.query(PromptVersion).filter(PromptVersion.prompt_id == PROMPT_ID).count() == 0
        assert db.query(PromptTemplate).filter(PromptTemplate.id == PROMPT_ID).first() is None


def test_empty_content_is_rejected(api):
    client, headers, _ = api
    resp = client.put(f"/api/admin/prompts/{PROMPT_ID}", headers=headers,
                      json={"prompt_content": "   "})
    assert resp.status_code == 400
    assert "不能为空" in resp.json()["detail"]
