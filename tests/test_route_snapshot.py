"""路由快照工具的「守卫的守卫」（批次 33）。

拆 `agent_routes.py` 时靠这个工具证明"路径/依赖/响应模型一点没变"。
但它自己也得被验证 —— 如果 `diff()` 永远返回空，所有拆分都会"零差异"通过。
"""
from tests._route_snapshot import diff


def _route(path, methods=("GET",), name="n", deps=(), body="", response="", status=200):
    return {"path": path, "methods": list(methods), "name": name, "deps": list(deps),
            "body": body, "response_model": response, "status_code": status}


def test_identical_snapshots_have_no_diff():
    rows = [_route("/a"), _route("/b", ("POST",))]

    assert diff(rows, rows) == []


def test_missing_route_is_detected():
    issues = diff([_route("/a"), _route("/b")], [_route("/a")])

    assert any("少了路由" in item and "/b" in item for item in issues)


def test_added_route_is_detected():
    issues = diff([_route("/a")], [_route("/a"), _route("/b")])

    assert any("多了路由" in item and "/b" in item for item in issues)


def test_changed_dependency_is_detected():
    """依赖被悄悄摘掉，是拆分里最危险的一种变化（鉴权可能没了）。"""
    issues = diff([_route("/a", deps=["auth.require"])], [_route("/a")])

    assert any("deps" in item for item in issues)


def test_dependency_moving_between_modules_is_not_a_diff():
    """批次 34：`_require_*` 搬到 `_workflow_deps` 后模块名变了，但契约没变。

    把模块名算进契约，每次搬依赖都会假红 —— 快照一旦假红，就会被人为无视。
    """
    before = [_route("/a", deps=["backend.routes.agent_routes._require_path_workflow"])]
    after = [_route("/a", deps=["backend.routes._workflow_deps._require_path_workflow"])]

    assert diff(before, after) == []


def test_dependency_replaced_by_another_function_is_detected():
    """同模块换函数（鉴权换成另一个）仍要抓 —— 只放宽了模块名。"""
    before = [_route("/a", deps=["backend.routes.agent_routes._require_path_workflow"])]
    after = [_route("/a", deps=["backend.routes._workflow_deps._require_body_workflow"])]

    assert any("deps" in item for item in diff(before, after))


def test_changed_response_model_and_body_are_detected():
    before = [_route("/a", body="OldReq", response="OldResp")]
    after = [_route("/a", body="NewReq", response="NewResp")]

    fields = [item for item in diff(before, after)]
    assert any("body" in item for item in fields)
    assert any("response_model" in item for item in fields)


def test_method_change_counts_as_a_different_route():
    issues = diff([_route("/a", ("GET",))], [_route("/a", ("POST",))])

    assert any("少了路由" in item for item in issues)
    assert any("多了路由" in item for item in issues)
