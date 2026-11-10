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

@router.get("/workflow/{session_id}/questionnaire/answers", dependencies=[Depends(_require_path_workflow)])
async def get_questionnaire_answers(session_id: str):
    """获取指定 session 的问卷答案（用于报告生成引用）"""
    from backend.database import SessionLocal
    from backend.models.user_models import QuestionnaireAnswer

    db = SessionLocal()
    try:
        rows = db.query(QuestionnaireAnswer).filter(
            QuestionnaireAnswer.session_id == session_id
        ).order_by(QuestionnaireAnswer.submitted_at).all()

        return {
            "success": True,
            "data": [{
                "id": r.id,
                "question_id": r.question_id,
                "question_text": r.question_text,
                "question_type": r.question_type,
                "related_indicator_name": r.related_indicator_name,
                "selected_level_index": r.selected_level_index,
                "text_answer": r.text_answer,
                "submitted_at": r.submitted_at.isoformat() if r.submitted_at else None,
            } for r in rows],
            "total": len(rows),
        }
    finally:
        db.close()


# ═══════════════════════════════════════════════════════
#  工作流 - 调查问卷生成
# ═══════════════════════════════════════════════════════

@router.post("/workflow/questionnaire", response_model=AgentResponse, dependencies=[Depends(_require_body_workflow)])
async def generate_questionnaire(request: QuestionnaireRequest):
    session_id = request.session_id
    values = _get_workflow_values(session_id)
    indicators = values.get("indicator_system") or {}
    if not indicators.get("level1_indicators"):
        raise HTTPException(status_code=400, detail="指标体系尚未生成，请先完成步骤 3")
    signature = hashlib.sha256(json.dumps(
        [indicators, sorted(set(request.custom_indicator_ids)) if request.custom_indicator_ids is not None else None,
         request.max_questions], ensure_ascii=False, sort_keys=True,
    ).encode()).hexdigest()
    backup = PerformanceAgent.load_state_backup(session_id)
    previous = _shared_state._QUESTIONNAIRE_CACHE.get(session_id) or backup.get("questionnaire", {})
    if previous.get("indicator_version") == signature and previous.get("questions"):
        return _questionnaire_response(session_id, previous)

    generator = QuestionnaireGenerator()
    questions = generator.generate_questions(
        indicators, custom_indicators=request.custom_indicator_ids, max_questions=request.max_questions,
    )
    if not questions:
        return AgentResponse(success=True, data={
            "session_id": session_id, "questionnaire": [], "display_markdown": "", "total_questions": 0,
        }, message="资料不足，当前指标中没有符合条件的问卷题目")
    for item in previous.get("questions", []):
        if item.get("created_by") == "user" and item.get("question_type") == "open_ended":
            questions.append({**item, "index": len(questions) + 1})

    published = {
        "project_name": values.get("project_name") or "未命名项目", "questions": questions,
        "display_md": generator.format_questionnaire_for_display(questions),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "indicator_version": signature, "version": uuid.uuid4().hex,
    }
    history = backup.get("questionnaire_history", [])
    if previous.get("questions"):
        history = [*history, previous]
    PerformanceAgent.update_state_backup(session_id, {"questionnaire": published, "questionnaire_history": history})
    _shared_state._QUESTIONNAIRE_CACHE[session_id] = published
    return _questionnaire_response(session_id, published)

@router.get("/workflow/questionnaire/list", response_model=AgentResponse)
async def list_questionnaires(user: User = Depends(get_current_user),
                              db: Session = Depends(get_db)):
    """
    列出所有已有问卷的 WorkflowSession 记录
    前端「问卷填写」Tab 调用此接口获取问卷列表
    """
    try:
        allowed_ids = None
        if user.role != UserRole.ADMIN:
            allowed_ids = {sid for (sid,) in _owned_workflows(db, user).with_entities(
                WorkflowSession.session_id
            )}
        results = []
        for sid, data in _shared_state._QUESTIONNAIRE_CACHE.items():
            if data.get("deleted") or (allowed_ids is not None and sid not in allowed_ids):
                continue
            results.append({
                "session_id": sid,
                "project_name": data.get("project_name", "未命名项目"),
                "question_count": len(data.get("questions", [])),
                "updated_at": data.get("updated_at", ""),
            })

        # 也检查 active_workflows 中有 indicator_system 的session（可生成问卷但尚未生成）
        for sid, wf in active_workflows.items():
            if (allowed_ids is not None and sid not in allowed_ids) or sid in _shared_state._QUESTIONNAIRE_CACHE:
                continue
            try:
                config = {"configurable": {"thread_id": sid}}
                state_obj = wf.get_state(config)
                if state_obj and getattr(state_obj, 'values', None):
                    indicator_system = state_obj.values.get("indicator_system", {})
                    if indicator_system and indicator_system.get("level1_indicators"):
                        results.append({
                            "session_id": sid,
                            "project_name": state_obj.values.get("project_name", "未命名项目"),
                            "question_count": 0,
                            "updated_at": str(datetime.now(timezone.utc)),
                            "needs_generation": True,
                        })
            except Exception:
                pass

        # 也检查 active_workflow_configs 中的session（内存evict后从配置重建）
        for sid in active_workflow_configs:
            if allowed_ids is not None and sid not in allowed_ids:
                continue
            if sid in _shared_state._QUESTIONNAIRE_CACHE or any(r.get("session_id") == sid for r in results):
                continue
            try:
                results.append({
                    "session_id": sid,
                    "project_name": active_workflow_configs[sid].get("project_name", "未命名项目"),
                    "question_count": 0,
                    "updated_at": str(datetime.now(timezone.utc)),
                    "needs_generation": True,
                })
            except Exception:
                pass

        # 始终尝试从文件备份恢复问卷列表（服务器重启后内存数据丢失，磁盘备份是唯一数据源）
        try:
            backup_dir = performance_agent.STATE_BACKUP_DIR
            if os.path.exists(backup_dir):
                for fname in os.listdir(backup_dir):
                    if not fname.endswith(".json"):
                        continue
                    sid = fname[:-5]
                    if allowed_ids is not None and sid not in allowed_ids:
                        continue
                    # 跳过已在内存中的 session
                    if any(r.get("session_id") == sid for r in results):
                        continue
                    try:
                        # 必须走持锁的读入口：本循环可能与 update_state_backup 的
                        # 原子替换并发，直接 open() 会让对方拿到 WinError 5
                        backup = PerformanceAgent.load_state_backup(sid)
                        if not backup:
                            continue
                        qdata = backup.get("questionnaire", {})
                        if qdata.get("deleted"):
                            continue
                        indicator_system = backup.get("indicator_system", {})
                        has_indicator = bool(indicator_system and indicator_system.get("level1_indicators"))
                        results.append({
                            "session_id": sid,
                            "project_name": qdata.get("project_name", "") or backup.get("project_name", "未命名项目"),
                            "question_count": len(qdata.get("questions", [])),
                            "updated_at": qdata.get("updated_at", str(datetime.now(timezone.utc))),
                            "needs_generation": not bool(qdata.get("questions")) and has_indicator,
                        })
                    except Exception:
                        pass
        except Exception:
            pass

        return AgentResponse(success=True, data=results,
                             message=f"找到 {len(results)} 条问卷记录")
    except Exception as e:
        logger.error(f"获取问卷列表失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))

def _delete_questionnaire_data(session_id: str, db: Session) -> None:
    from backend.models.user_models import QuestionnaireAnswer

    deleted = {"questions": [], "deleted": True}
    PerformanceAgent.update_state_backup(session_id, {"questionnaire": deleted, "questionnaire_history": []})
    _shared_state._QUESTIONNAIRE_CACHE[session_id] = deleted
    db.query(QuestionnaireAnswer).filter(QuestionnaireAnswer.session_id == session_id).delete()

@router.delete("/workflow/questionnaire/{session_id}", response_model=AgentResponse,
               dependencies=[Depends(_require_path_workflow)])
async def delete_questionnaire(session_id: str, db: Session = Depends(get_db)):
    _delete_questionnaire_data(session_id, db)
    db.commit()
    return AgentResponse(success=True, data={"session_id": session_id}, message="问卷已删除")

class BatchDeleteQRequest(BaseModel):
    session_ids: list[str]

@router.post("/workflow/questionnaire/batch-delete", response_model=AgentResponse)
async def batch_delete_questionnaires(req: BatchDeleteQRequest, user: User = Depends(get_current_user),
                                      db: Session = Depends(get_db)):
    session_ids = list(dict.fromkeys(req.session_ids))
    for session_id in session_ids:
        _require_workflow(session_id, user, db)
    for session_id in session_ids:
        _delete_questionnaire_data(session_id, db)
    db.commit()
    return AgentResponse(success=True, data={"deleted": len(session_ids), "session_ids": session_ids},
                         message=f"成功删除 {len(session_ids)} 条问卷")

class AddOpenQuestionRequest(BaseModel):
    session_id: str
    question_text: str
    related_indicator_id: Optional[str] = None
    related_indicator_name: Optional[str] = None

@router.post("/workflow/questionnaire/add-open-question", dependencies=[Depends(_require_body_workflow)])
async def add_open_ended_question(req: AddOpenQuestionRequest):
    """
    向已有问卷手动添加开放性问题（填空题）
    操作者可以根据需要在里克特量表问卷基础上添加自定义开放性问题
    """
    try:
        session_id = req.session_id

        qcache = _published_questionnaire(session_id)
        questions = list(qcache["questions"])
        new_question = QuestionnaireGenerator().add_open_ended_question(questions, req.question_text)
        new_question["related_indicator_id"] = req.related_indicator_id or ""
        new_question["related_indicator_name"] = req.related_indicator_name or ""
        updated = {**qcache, "questions": questions,
                   "display_md": QuestionnaireGenerator().format_questionnaire_for_display(questions),
                   "updated_at": datetime.now(timezone.utc).isoformat()}
        PerformanceAgent.update_state_backup(session_id, {"questionnaire": updated})
        _shared_state._QUESTIONNAIRE_CACHE[session_id] = updated

        return {
            "success": True,
            "data": {"question": new_question, "total_questions": len(questions)},
            "message": "开放性问题已添加"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"添加开放性问题失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))

