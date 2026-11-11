"""导出域的路由（从 `agent_routes.py` 拆出，批次 34；批次 35 并入导出样式）。

拆分原则：**只搬代码，不改逻辑**。路径、方法、依赖、响应模型一字不动，
由 `tests/_route_snapshot.py` 前后比对来证明（148 条零差异）。

与问卷域的区别：问卷域搬走时还反过来 import `agent_routes` 的六个符号；
这次先抽了 `_workflow_deps.py`，依赖与状态读写都从那里拿。
报告读取（`_read_report` / `_report_review_steps`）属于「报告」域（批次 35 已搬）。
这里**按模块访问** `report_routes`，不按名字导入 —— 按名字导入会把测试的 patch 点
搬到一个没人调用的副本上（批次 34 踩过）。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from loguru import logger

from backend.agents.performance_agent import PerformanceAgent
from backend.agents.prompts.default_prompts import STEP_META
from backend.agents.tools.export_service import ExportService, ExportStyle
from backend.config import settings
from backend.database import get_db
from backend.middleware.auth_middleware import get_current_user
from backend.models.user_models import User
from backend.routes import _shared_state
from backend.routes import report_routes as _report_routes
from backend.routes._workflow_deps import (
    AgentResponse, _get_workflow_values, _persist_state, _require_body_workflow,
    _require_path_workflow,
)
from backend.services import export_cache

router = APIRouter()


class ExportRequest(BaseModel):
    """导出请求"""
    session_id: str
    format: str = "word"
    style_mode: str = "classic"
    project_name: Optional[str] = None
    step_number: Optional[int] = None
    step_content: Optional[str] = None
    #: 用户已知报告有缺章仍要导出时置 True（导出闸门放行，但记录一条日志）
    force: bool = False


# ═══════════════════════════════════════════════════════
#  文件下载
# ═══════════════════════════════════════════════════════

def _export_base_dir(user: User) -> str:
    return os.path.join("backend", ExportService.EXPORT_DIR, str(user.id))


def _export_with_cache(*, user: User, session_id: str, target: str, content: str,
                       file_label: str, fmt: str, style: ExportStyle,
                       title: str) -> tuple[str, bool]:
    """带幂等的导出：同一内容 + 同一排版 + 同一格式在 10 分钟内只渲染一次。

    返回 (文件路径, 是否复用已有文件)。幂等键不含时间戳，所以「连点三次导出」
    只会真正渲染一次，后两次直接返回第一次的文件路径。
    """
    key = export_cache.cache_key(
        user.id, session_id, target, fmt, export_cache.style_fingerprint(style)
    )
    content_hash = export_cache.content_fingerprint(content)
    cached_path = export_cache.lookup(key, content_hash)
    if cached_path:
        logger.info(f"导出命中幂等缓存，复用文件: {os.path.basename(cached_path)}")
        return cached_path, True

    if fmt == "pdf":
        file_path = ExportService.export_to_pdf(
            content, file_label, style, base_dir=_export_base_dir(user), title=title
        )
    else:
        file_path = ExportService.export_to_word(
            content, file_label, style, base_dir=_export_base_dir(user), title=title
        )
    export_cache.store(key, content_hash, file_path)
    return file_path, False


@router.get("/download/{file_name}")
async def download_export_file(file_name: str, user: User = Depends(get_current_user)):
    """
    下载导出的Word/PDF文件
    前端导出成功后通过此接口下载文件
    """
    # 安全检查：防止路径穿越
    safe_name = os.path.basename(file_name)
    if safe_name != file_name or Path(safe_name).suffix.lower() not in {".pdf", ".docx"}:
        raise HTTPException(status_code=404, detail="文件不存在")
    export_dir = os.path.join(_export_base_dir(user), ExportService.EXPORT_DIR)
    file_path = os.path.join(export_dir, safe_name)

    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail=f"文件不存在: {safe_name}")

    # 根据扩展名设置content-type
    media_type = "application/octet-stream"
    if safe_name.endswith(".pdf"):
        media_type = "application/pdf"
    elif safe_name.endswith(".docx"):
        media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    return FileResponse(
        file_path,
        media_type=media_type,
        filename=safe_name,
    )


def _safe_fallback_note(session_id: str) -> str:
    """取降级标注；读不到状态就返回空串。

    脚注是「补充说明」，**绝不能因为它把导出拦掉** —— 历史会话只留了报告、
    没有状态快照时会抛 404；批次 32 第一版直接调 `_get_workflow_values`，
    把本来能导的报告变成了 404（被 `test_engineering_regressions` 抓到）。
    """
    try:
        return _fallback_note(_get_workflow_values(session_id))
    except HTTPException:
        return ""
    except Exception:  # noqa: BLE001
        logger.exception(f"读取降级标注失败，按无标注导出: {session_id}")
        return ""


def _fallback_note(values: dict) -> str:
    """导出件的降级标注（批次 32）。

    批次 31 只在 `warnings` 里留一句「已由备选模型 X 完成」，而 warnings 是要去翻的。
    导出件是给外部看的，**必须自己说清楚哪一步换了模型** —— 换个模型会改变措辞与判断，
    不标注等于让读者以为全程一个模型。

    从 `llm_attempts` 里取「非首个模型且成功」的记录（比解析 warning 文本可靠）。
    """
    attempts = values.get("llm_attempts") or []
    primary = ""
    for item in attempts:
        if isinstance(item, dict) and item.get("model"):
            primary = item["model"]
            break
    hits = []
    for item in attempts:
        if not isinstance(item, dict):
            continue
        if item.get("outcome") != "ok" or item.get("model") == primary:
            continue
        step = item.get("step")
        pair = (step, item.get("model"))
        if pair not in hits:
            hits.append(pair)
    if not hits:
        return ""
    steps = "、".join(f"步骤 {step}（{model}）" for step, model in sorted(hits))
    return (f"> 说明：本报告以下步骤由备选模型完成 —— {steps}。\n"
            f"> 主模型不可用时系统自动降级，该部分措辞与判断可能与其余章节存在差异。")


def _export_blockers(session_id: str) -> list:
    """导出前必须处理的问题（缺章、步骤没跑完、报告过短……）。

    读不到工作流状态时返回空 —— 闸门只对「能看见状态」的会话生效，
    不能因为状态丢了就谁都不许导出（那是比缺章更糟的故障）。
    """
    from backend.services import consistency

    try:
        values = _get_workflow_values(session_id)
    except HTTPException:
        return []
    except Exception:
        logger.exception(f"导出前一致性检查失败，放行: {session_id}")
        return []
    try:
        report = consistency.check_consistency(values)
    except Exception:
        logger.exception(f"一致性检查异常，放行: {session_id}")
        return []
    return [item for item in report.get("issues", []) if item.get("blocking")]


@router.post("/workflow/export", response_model=AgentResponse, dependencies=[Depends(_require_body_workflow)])
async def export_workflow_report(request: ExportRequest, user: User = Depends(get_current_user),
                                 db: Session = Depends(get_db)):
    """
    导出绩效评价报告（Word / PDF）
    支持：
      - 完整报告导出（需要工作流已完成）
      - 单步骤导出（step_number + step_content 直接导出）
    """
    try:
        session_id = request.session_id
        fmt = request.format or "word"
        style = ExportStyle.classic() if request.style_mode == "classic" else ExportStyle()
        project_name = request.project_name or "绩效评价报告"

        # ── 单步骤导出：使用前端传来的 step_content ──
        if request.step_number and request.step_content:
            step_name = STEP_META.get(request.step_number, {}).get("name", f"步骤{request.step_number}")
            file_label = f"{project_name}_步骤{request.step_number}_{step_name}"
            content = request.step_content

            file_path, reused = _export_with_cache(
                user=user, session_id=session_id, target=f"step:{request.step_number}",
                content=content, file_label=file_label, fmt=fmt, style=style,
                title=f"{project_name} - {step_name}",
            )

            file_name = os.path.basename(file_path)
            logger.info(f"步骤{request.step_number}导出成功: {file_name}（reused={reused}）")
            return AgentResponse(success=True, data={
                "session_id": session_id, "file_path": file_path,
                "file_name": file_name, "format": fmt,
                "step_number": request.step_number, "reused": reused,
            }, message=f"步骤{request.step_number}导出成功")

        if _report_review_steps(session_id):
            raise HTTPException(status_code=409, detail="报告关联的步骤尚待复核，请复核后重新组装报告")
        report_content, stored_project_name = _read_report(session_id, db)
        project_name = request.project_name or stored_project_name
        if not report_content:
            raise HTTPException(status_code=400, detail="报告内容为空，请确认工作流已完成")

        # 导出闸门：结构性缺陷（缺章/步骤没跑完/报告过短）先拦一次。
        # force 是明确的「我知道，仍然导出」—— 没有它，缺一章的会话就彻底导不出去了。
        if not request.force:
            blockers = _export_blockers(session_id)
            if blockers:
                detail = "报告还不能导出：" + "；".join(
                    f"{item['id']} {item['message']}" for item in blockers[:3]
                ) + "。确认要导出请选择「仍要导出」。"
                raise HTTPException(status_code=409, detail=detail,
                                    headers={"X-Export-Blocked": "1"})
        elif _export_blockers(session_id):
            logger.warning(f"报告存在结构性缺陷，用户强制导出: {session_id}")

        # 脚注是「补充说明」，读不到状态就不写 —— 绝不能因为它把导出拦掉
        # （历史会话只留了报告、没有状态快照时就会走到这里）
        try:
            note = _fallback_note(_get_workflow_values(session_id))
        except HTTPException:
            note = ""
        except Exception:
            logger.exception(f"读取降级标注失败，按无标注导出: {session_id}")
            note = ""
        if note:
            report_content = f"{report_content.rstrip()}\n\n---\n\n{note}\n"

        file_path, reused = _export_with_cache(
            user=user, session_id=session_id, target="report",
            content=report_content, file_label=project_name, fmt=fmt, style=style,
            title=project_name,
        )

        file_name = os.path.basename(file_path)
        logger.info(f"报告导出成功: {file_name}（reused={reused}）")
        return AgentResponse(success=True, data={
            "session_id": session_id, "file_path": file_path,
            "file_name": file_name, "format": fmt, "reused": reused,
        }, message="报告导出成功" if not reused else "已复用上次导出的文件")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"导出失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════
#  工作流 - 单步骤导出
# ═══════════════════════════════════════════════════════

class ExportStepRequest(BaseModel):
    session_id: str
    step_number: int
    format: str = "word"


@router.post("/workflow/export-step", response_model=AgentResponse, dependencies=[Depends(_require_body_workflow)])
async def export_step_content(request: ExportStepRequest, user: User = Depends(get_current_user)):
    """
    导出单个步骤的内容为Word或PDF
    从工作流State中获取步骤内容并导出
    """
    try:
        session_id = request.session_id
        step_number = request.step_number
        fmt = request.format or "word"
        style = ExportStyle.classic()

        # 从工作流state获取步骤内容
        step_content = None
        if session_id in _shared_state.active_workflows:
            workflow = _shared_state.active_workflows[session_id]
        elif session_id in _shared_state.active_workflow_configs:
            cfg = _shared_state.active_workflow_configs[session_id]
            workflow = PerformanceAgent(
                session_id=session_id, model_name=cfg.get("model_name") or settings.DEFAULT_MODEL,
                temperature=cfg.get("temperature", 0.7),
                top_p=cfg.get("top_p", 0.9),
                use_rag=cfg.get("use_rag", False),
                comparison_mode=cfg.get("comparison_mode", "single"),
                temperature_profile=cfg.get("temperature_profile", "normal"),
            )
            _shared_state.active_workflows[session_id] = workflow
        else:
            raise HTTPException(status_code=404, detail="工作流不存在")

        config = {"configurable": {"thread_id": session_id}}
        state_obj = workflow.get_state(config)

        if state_obj and getattr(state_obj, 'values', None):
            values = state_obj.values
            project_name = values.get("project_name", "绩效评价报告")
            step_results = values.get("step_results", {})
            step_data = step_results.get(step_number, {})
            if isinstance(step_data, dict):
                step_content = step_data.get("content", "")
            else:
                step_content = str(step_data) if step_data else ""

        if not step_content:
            raise HTTPException(status_code=400, detail=f"步骤{step_number}内容为空，请确认该步骤已执行完成")

        step_name = STEP_META.get(step_number, {}).get("name", f"步骤{step_number}")
        file_label = f"{project_name}_步骤{step_number}_{step_name}"

        file_path, reused = _export_with_cache(
            user=user, session_id=session_id, target=f"step:{step_number}",
            content=step_content, file_label=file_label, fmt=fmt, style=style,
            title=f"{project_name} - {step_name}",
        )

        file_name = os.path.basename(file_path)
        logger.info(f"步骤{step_number}导出成功: {file_name}（reused={reused}）")
        return AgentResponse(success=True, data={
            "session_id": session_id,
            "file_path": file_path,
            "file_name": file_name,
            "format": fmt,
            "step_number": step_number,
            "reused": reused,
        }, message=f"步骤{step_number} {step_name} 导出成功")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"步骤导出失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# 报告读取属于「报告」域（还没搬），转发一层只为保住两边的 patch 点：
# 按模块访问 `_agent_routes`（而不是 `from ... import _read_report`），
# 否则测试改的是 `agent_routes` 上的名字、跑的是这里的副本。
def _report_review_steps(session_id):
    return _report_routes._report_review_steps(session_id)


def _read_report(session_id: str, db: Session):
    return _report_routes._read_report(session_id, db)


# ═══════════════════════════════════════════════════════
#  导出样式自定义
# ═══════════════════════════════════════════════════════

class ExportStyleRequest(BaseModel):
    session_id: str
    font_family: str = "SimSun"
    font_size: int = 12
    line_spacing: float = 1.5
    page_margin: float = 2.5


@router.post("/workflow/export-style", dependencies=[Depends(_require_body_workflow)])
async def set_export_style(req: ExportStyleRequest):
    """
    设置导出PDF/Word文档的自定义样式
    支持的字体：SimSun(宋体), SimHei(黑体), KaiTi(楷体), FangSong(仿宋)
    """
    try:
        values = _get_workflow_values(req.session_id)
        style = {
            "font_family": req.font_family,
            "font_size": req.font_size,
            "line_spacing": req.line_spacing,
            "page_margin": req.page_margin,
        }
        _persist_state(req.session_id, {"export_style": style})
        return {"success": True, "data": style, "message": "导出样式已保存"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/workflow/{session_id}/export-style", dependencies=[Depends(_require_path_workflow)])
async def get_export_style(session_id: str):
    """获取当前的导出样式设置"""
    try:
        values = _get_workflow_values(session_id)
        style = values.get("export_style", {
            "font_family": "SimSun", "font_size": 12,
            "line_spacing": 1.5, "page_margin": 2.5,
        })
        return {"success": True, "data": style}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
