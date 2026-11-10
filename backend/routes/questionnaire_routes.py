"""问卷域的路由（从 `agent_routes.py` 拆出，批次 33）。

拆分原则：**只搬代码，不改逻辑**。路径、方法、依赖、响应模型一字不动，
由 `tests/_route_snapshot.py` 前后比对来证明。

跨域共享的东西（会话校验、路由级依赖、`active_workflows`）仍从 `agent_routes`
导入 —— 这一步只是把"问卷"这个最独立的域搬出去；把共享状态抽成独立模块是下一步。
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from loguru import logger

from backend.agents import performance_agent
from backend.agents.performance_agent import PerformanceAgent
from backend.agents.tools.questionnaire_gen import QuestionnaireGenerator
from backend.config import settings
from backend.database import get_db
from backend.middleware.auth_middleware import get_current_user
from backend.models.user_models import User, UserRole, WorkflowSession
from backend.routes import _shared_state
from backend.routes import agent_routes as agent_routes_module
from backend.routes._shared_state import (
    _published_questionnaire, _validate_session_id,
)
from backend.routes.agent_routes import (
    AgentResponse, _get_workflow_values, _owned_workflows, _require_body_workflow,
    _require_path_workflow, _require_workflow, active_workflow_configs, active_workflows,
)

router = APIRouter()


class QuestionnaireRequest(BaseModel):
    """问卷生成请求"""
    session_id: str
    max_questions: int = Field(default=30, ge=1, le=100)
    custom_indicator_ids: Optional[List[str]] = None


class QuestionnaireAnswerItem(BaseModel):
    question_id: str
    question_text: Optional[str] = None
    question_type: str = "likert"
    related_indicator_id: Optional[str] = None
    related_indicator_name: Optional[str] = None
    selected_level_index: Optional[int] = None
    text_answer: Optional[str] = None


class SubmitQuestionnaireRequest(BaseModel):
    session_id: str
    answers: List[QuestionnaireAnswerItem]
    questionnaire_version: str | None = None


class QuestionnaireAnswerItem(BaseModel):
    question_id: str
    question_text: Optional[str] = None
    question_type: str = "likert"
    related_indicator_id: Optional[str] = None
    related_indicator_name: Optional[str] = None
    selected_level_index: Optional[int] = None
    text_answer: Optional[str] = None

class SubmitQuestionnaireRequest(BaseModel):
    session_id: str
    answers: List[QuestionnaireAnswerItem]
    questionnaire_version: str | None = None

def _questionnaire_response(session_id, data):
    return AgentResponse(success=True, data={
        "session_id": session_id, "questionnaire": data["questions"],
        "display_markdown": data.get("display_md", ""),
        "total_questions": len(data["questions"]), "questionnaire_version": data.get("version"),
    })

@router.get("/workflow/{session_id}/questionnaire", response_model=AgentResponse)
async def get_public_questionnaire(session_id: str):
    return _questionnaire_response(session_id, _published_questionnaire(session_id))

@router.post("/workflow/questionnaire/submit")
async def submit_questionnaire_answers(req: SubmitQuestionnaireRequest, db: Session = Depends(get_db)):
    from backend.models.user_models import QuestionnaireAnswer

    current = _published_questionnaire(req.session_id)
    history = PerformanceAgent.load_state_backup(req.session_id).get("questionnaire_history", [])
    versions = [current, *reversed(history)]
    if req.questionnaire_version:
        versions = [item for item in versions if item.get("version") == req.questionnaire_version]
    questions = {}
    for version in versions:
        for question in version.get("questions", []):
            questions.setdefault(question.get("question_id") or question.get("id"), question)

    seen = set()
    rows = []
    for answer in req.answers:
        question = questions.get(answer.question_id)
        if not question or answer.question_id in seen:
            raise HTTPException(status_code=422, detail="答卷包含未知或重复题目，请重新加载问卷")
        seen.add(answer.question_id)
        kind = question.get("question_type") or answer.question_type
        index = answer.selected_level_index
        if kind == "likert":
            if index is None:
                continue
            if not 0 <= index < len(question.get("scale_levels") or range(5)):
                raise HTTPException(status_code=422, detail="量表选项超出范围")
        elif not (answer.text_answer or "").strip():
            continue
        rows.append(QuestionnaireAnswer(
            session_id=req.session_id, question_id=answer.question_id,
            question_text=question.get("question_text") or question.get("question") or answer.question_text,
            question_type=kind, related_indicator_id=question.get("related_indicator_id"),
            related_indicator_name=question.get("related_indicator_name"),
            selected_level_index=index if kind == "likert" else None,
            text_answer=answer.text_answer if kind != "likert" else None,
            submitted_at=datetime.now(timezone.utc),
        ))
    if not rows:
        raise HTTPException(status_code=422, detail="请至少回答一道题")
    db.add_all(rows)
    db.commit()
    return {"success": True, "data": {"saved": len(rows), "errors": []}, "message": "答卷已保存"}

