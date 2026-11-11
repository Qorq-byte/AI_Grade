"""工作流域的共享依赖：路由级依赖 + 状态读写（批次 34 从 `agent_routes.py` 抽出）。

为什么要有这个文件：批次 33 拆出问卷域之后，新模块还得从 `agent_routes`
按名字导入 AgentResponse、_get_workflow_values、_require_* 等六个符号。
拆到第三个域时 `agent_routes` 就会变成"所有域的公共依赖库" —— 越拆越乱。
所以先把这些**端点无关**的东西提到这里：

    请求/响应模型   AgentResponse
    路由级依赖      _require_workflow / _require_path_workflow / _require_body_workflow
    所有权过滤      _owned_workflows
    状态读写        _get_workflow_values / _persist_state

**只搬代码，不改逻辑**：函数体逐行照抄，唯一的改动是可变状态改成按模块访问
（`_shared_state.active_workflows`），理由见 `_shared_state.py` 的模块文档。

本模块**不 import 任何路由模块**（有用例守着）：
`agent_routes` / `export_routes` / `questionnaire_routes` 都从这里拿，谁也不用 import 谁。
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.agents.performance_agent import PerformanceAgent
from backend.database import get_db
from backend.middleware.auth_middleware import get_current_user
from backend.models.user_models import User, UserRole, WorkflowSession
from backend.routes import _shared_state
from backend.routes._shared_state import _validate_session_id


#: 每一步的正文落在 state 的哪个字段里。
#: 步骤编辑（`_apply_step_edit`）与报告组装（`assemble-report`）都要用 ——
#: 两处各写一份的话，新增一步就会出现"改得了、拼不进去"。
STEP_CONTENT_FIELDS = {
    1: "cost_list", 2: "effective_data", 5: "scoring_standards", 6: "evaluation_table",
    7: "indicator_analysis", 8: "experience_summary", 9: "problems_analysis", 10: "suggestions",
    11: "comprehensive_evaluation", 12: "basic_info", 13: "work_introduction",
}


class AgentResponse(BaseModel):
    """Agent响应基础模型"""
    success: bool
    data: Optional[Any] = None
    message: str = ""


def _owned_workflows(db: Session, user: User):
    query = db.query(WorkflowSession)
    if user.role != UserRole.ADMIN:
        query = query.filter(WorkflowSession.user_id == user.id)
    return query


def _require_workflow(session_id: str, user: User, db: Session) -> None:
    _validate_session_id(session_id)
    row = _owned_workflows(db, user).with_entities(WorkflowSession.session_id).filter(
        WorkflowSession.session_id == session_id
    ).first()
    if row is not None and row.session_id == session_id:
        return
    if user.role == UserRole.ADMIN and row is None and PerformanceAgent.load_state_backup(session_id):
        return
    raise HTTPException(status_code=404, detail="工作流不存在")


def _require_path_workflow(session_id: str, user: User = Depends(get_current_user),
                           db: Session = Depends(get_db)):
    _require_workflow(session_id, user, db)


async def _require_body_workflow(request: Request, user: User = Depends(get_current_user),
                                 db: Session = Depends(get_db)):
    # FastAPI validates malformed request bodies against the endpoint's model.
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return
    session_id = body.get("session_id") if isinstance(body, dict) else None
    if isinstance(session_id, str):
        _require_workflow(session_id, user, db)


def _get_workflow_values(session_id: str) -> dict:
    """Read the checkpoint or disk projection, using legacy name-based indicator IDs."""
    workflow = _shared_state.active_workflows.get(session_id)
    snapshot = workflow.get_state({"configurable": {"thread_id": session_id}}) if workflow else None
    values = dict(getattr(snapshot, "values", {}) or PerformanceAgent.load_state_backup(session_id))
    if not values:
        raise HTTPException(status_code=404, detail="工作流状态不存在")
    if "step_results" in values:
        values["step_results"] = {int(key): value for key, value in values["step_results"].items()}

    def with_ids(items, parents=()):
        nodes = []
        for item in items:
            names = (*parents, item.get("name") or "")
            node = {**item, "id": item.get("id") or "_".join(names)}
            if item.get("children"):
                node["children"] = with_ids(item["children"], names)
            nodes.append(node)
        return nodes

    system = values.get("indicator_system")
    if system and system.get("level1_indicators"):
        values["indicator_system"] = {**system, "level1_indicators": with_ids(system["level1_indicators"])}
    return values


def _persist_state(session_id: str, updates: dict) -> None:
    if session_id in _shared_state._WORKFLOW_PROGRESS and updates.keys() & {"indicator_system", "indicator_score_map", "step_results"}:
        raise HTTPException(status_code=409, detail="工作流执行中，暂不能修改指标或步骤")
    workflow = _shared_state.active_workflows.get(session_id)
    if workflow:
        config = {"configurable": {"thread_id": session_id}}
        workflow.app.update_state(config, updates)
    persisted = dict(updates)
    if "step_results" in updates:
        persisted["step_results"] = {
            key: {**item, "created_at": str(item.get("created_at", "")),
                  "updated_at": str(item.get("updated_at", ""))}
            for key, item in updates["step_results"].items()
        }
    PerformanceAgent.update_state_backup(session_id, persisted)
