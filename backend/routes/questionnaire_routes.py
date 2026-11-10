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


def _questionnaire_response(session_id, data):
    return AgentResponse(success=True, data={
        "session_id": session_id, "questionnaire": data["questions"],
        "display_markdown": data.get("display_md", ""),
        "total_questions": len(data["questions"]), "questionnaire_version": data.get("version"),
    })

@router.get("/workflow/{session_id}/questionnaire", response_model=AgentResponse)
async def get_public_questionnaire(session_id: str):
    return _questionnaire_response(session_id, _published_questionnaire(session_id))

