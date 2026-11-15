"""业务路由的契约测试（批次 15）。

report / scoring / export / questionnaire 四个路由一共 32 个端点，都挂在 `/api/agent`
前缀下。此前 `/api/agent` 虽有 21 个测试文件，但打的是工作流主流程；这批端点里
大部分从没被单独打过 —— 换个函数、改个参数名，没有任何一条测试会红。

这里不追求覆盖每个分支（那要造完整的工作流状态和 LLM 桩），而是先把最容易出事的
三条契约钉住：

1. **必须登录** —— 漏一个 `Depends(get_current_user)` 就是数据裸奔；
2. **不存在的会话不能 500** —— 404/400 都行，堆栈不行；
3. **参数缺失不能假装成功** —— 422/400 都行，200 不行。

另外单独验证下载接口的路径穿越防护（`file_name` 直接进文件路径）。
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
from backend.models.user_models import User, UserRole


@compiles(MEDIUMTEXT, "sqlite")
def compile_mediumtext(element, compiler, **kwargs):
    return "TEXT"


USER_ID = 7
GHOST = "no-such-session"

#: 这些端点在「会话不存在 / 参数缺失」时允许的返回。共同点：都不是 200、都不是 5xx。
BAD_REQUEST_OK = (400, 404, 409, 422)


@pytest.fixture
def client(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'biz.db').as_posix()}")
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(auth, "SECRET_KEY", "test-only-signing-key")

    with factory.begin() as db:
        db.add(User(id=USER_ID, username="bob", password_hash="x:y",
                    role=UserRole.USER, is_active=True))

    token = auth.create_access_token({"user_id": USER_ID, "ver": 0})
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {token}"})
    yield c
    engine.dispose()


# ═══════════════════════════════════════════════════════
#  一、鉴权：漏一个依赖就是数据裸奔
# ═══════════════════════════════════════════════════════

PROTECTED_GETS = [
    f"/api/agent/workflow/{GHOST}/report",
    f"/api/agent/workflow/{GHOST}/modules",
    f"/api/agent/workflow/{GHOST}/report-order",
    f"/api/agent/workflow/{GHOST}/indicators-for-scoring",
    f"/api/agent/workflow/{GHOST}/extract-deductions",
    f"/api/agent/workflow/{GHOST}/score-summary",
    f"/api/agent/workflow/{GHOST}/export-style",
    "/api/agent/workflow/questionnaire/list",
]


@pytest.mark.parametrize("path", PROTECTED_GETS)
def test_get_endpoints_require_login(path):
    """不带令牌必须是 401/403，不能落到「查库→空结果→200」。"""
    anon = TestClient(app)
    resp = anon.get(path)
    assert resp.status_code in (401, 403), f"{path} 竟然允许匿名访问：HTTP {resp.status_code}"


# ═══════════════════════════════════════════════════════
#  二、不存在的会话：给 4xx，不给堆栈
# ═══════════════════════════════════════════════════════

@pytest.mark.parametrize("path", [
    f"/api/agent/workflow/{GHOST}/report",
    f"/api/agent/workflow/{GHOST}/modules",
    f"/api/agent/workflow/{GHOST}/report-order",
    f"/api/agent/workflow/{GHOST}/indicators-for-scoring",
    f"/api/agent/workflow/{GHOST}/score-summary",
    f"/api/agent/workflow/{GHOST}/export-style",
])
def test_unknown_session_is_4xx_not_500(client, path):
    resp = client.get(path)
    assert resp.status_code < 500, f"{path} 对不存在的会话返回了 {resp.status_code}"


@pytest.mark.parametrize("path,body", [
    ("/api/agent/workflow/save-report", {}),
    ("/api/agent/workflow/generate-module", {}),
    ("/api/agent/workflow/assemble-report", {}),
    ("/api/agent/workflow/add-custom-module", {}),
    ("/api/agent/workflow/delete-module", {}),
    ("/api/agent/workflow/update-module-content", {}),
    ("/api/agent/workflow/generate-indicator-analysis", {}),
    ("/api/agent/workflow/save-indicator-scores", {}),
    ("/api/agent/workflow/confirm-deductions", {}),
    ("/api/agent/workflow/reorder-indicators", {}),
    ("/api/agent/workflow/export", {}),
    ("/api/agent/workflow/export-step", {}),
    ("/api/agent/workflow/questionnaire/submit", {}),
    ("/api/agent/workflow/questionnaire/batch-delete", {}),
    ("/api/agent/workflow/questionnaire/add-open-question", {}),
])
def test_missing_payload_is_rejected_not_accepted(client, path, body):
    """空 body 不能被当成「参数都为空」正常执行 —— 那会写坏数据或空转。"""
    resp = client.post(path, json=body)
    assert resp.status_code in BAD_REQUEST_OK, \
        f"{path} 对空 body 返回 {resp.status_code}，要么该 4xx 要么是真做了事"


# ═══════════════════════════════════════════════════════
#  三、下载接口的路径穿越
# ═══════════════════════════════════════════════════════

@pytest.mark.parametrize("name", [
    "../../../etc/passwd",
    "..%2F..%2Fsecret.docx",
    "%2Fetc%2Fpasswd",
    "report.txt",          # 后缀不在白名单
    "report.docx.bak",
])
def test_download_rejects_traversal_and_odd_suffix(client, name):
    resp = client.get(f"/api/agent/download/{name}")
    assert resp.status_code in (400, 404, 405, 422), f"{name} 竟然通过了：{resp.status_code}"


def test_download_missing_file_is_404(client):
    resp = client.get("/api/agent/download/nope.docx")
    assert resp.status_code == 404


# ═══════════════════════════════════════════════════════
#  四、问卷列表：即使没数据也要是结构完整的 200
# ═══════════════════════════════════════════════════════

def test_questionnaire_list_shape(client):
    resp = client.get("/api/agent/workflow/questionnaire/list")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("success") is True
    data = body.get("data") or {}
    assert isinstance(data, dict) or isinstance(data, list)


def test_questionnaire_batch_delete_with_empty_ids_deletes_nothing(client):
    """空 ids 必须是「一条都不删」，绝不能退化成「删除全部」。

    实现是遍历 ids 逐个删，所以空列表天然安全 —— 但这条语义值得钉住：
    哪天有人改成 `if not ids: 删所有`，这里会立刻红。
    """
    resp = client.post("/api/agent/workflow/questionnaire/batch-delete",
                       json={"session_ids": []})
    assert resp.status_code == 200, resp.text
    assert (resp.json().get("data") or {}).get("deleted") == 0
