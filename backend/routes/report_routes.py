"""报告域的路由（从 `agent_routes.py` 拆出，批次 35）。

拆分原则：**只搬代码，不改逻辑**。路径、方法、依赖、响应模型一字不动，
由 `tests/_route_snapshot.py` 前后比对来证明（148 条零差异）。

这个域是「报告成稿」的完整闭环：读报告 → 改报告 → 排章节 → 组装 → 格式校验，
外加步骤 12 的模块生成/增删改（模块是报告的组成部分，跟报告一起走）。

跨域共享的东西：依赖与状态读写从 `_workflow_deps` 拿，可变状态从 `_shared_state`
拿（按模块访问）。本模块**不 import `agent_routes`** —— 批次 34 之前那条
「export_routes → agent_routes」的反向依赖，这一批改成指向这里。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from langchain_openai import ChatOpenAI
from loguru import logger

from backend.agents.performance_agent import PerformanceAgent
from backend.config import settings
from backend.database import get_db
from backend.models.user_models import WorkflowSession
from backend.routes import _shared_state
from backend.routes._workflow_deps import (
    STEP_CONTENT_FIELDS, _get_workflow_values, _persist_state,
    _require_body_workflow, _require_path_workflow,
)
from backend.services.workflow_evidence import report_evidence_index

router = APIRouter()


# ═══════════════════════════════════════════════════════
#  工作流 - 报告查询 & 编辑存储
# ═══════════════════════════════════════════════════════

def _report_review_steps(session_id):
    return PerformanceAgent.load_state_backup(session_id).get("review_steps", [])


def _read_report(session_id: str, db: Session) -> tuple[str, str]:
    project_name = "绩效评价报告"
    record = db.query(WorkflowSession).filter(WorkflowSession.session_id == session_id).first()
    if record and record.final_report is not None:
        return record.final_report, record.project_name or project_name

    workflow = _shared_state.active_workflows.get(session_id)
    if workflow:
        config = {"configurable": {"thread_id": session_id}}
        snapshot = workflow.get_state(config)
        if snapshot and snapshot.values:
            values = snapshot.values
            content = values.get("final_report") or _assemble_report_from_steps(values)
            if content:
                return content, values.get("project_name") or project_name

    backup = PerformanceAgent.load_state_backup(session_id)
    if backup:
        project_name = backup.get("project_name") or project_name
        for key in ("final_report", "report_content", "report"):
            if isinstance(backup.get(key), str):
                return backup[key], project_name
        steps = backup.get("step_results", {})
        step14 = steps.get("14", {})
        content = (step14.get("content") if isinstance(step14, dict) else "")
        content = content or _assemble_report_from_backup_steps(backup)
        if content:
            return content, project_name

    raise HTTPException(status_code=404, detail="报告不存在，请先完成工作流")


@router.get("/workflow/{session_id}/report", dependencies=[Depends(_require_path_workflow)])
async def get_report_content(session_id: str, db: Session = Depends(get_db)):
    content, project_name = _read_report(session_id, db)
    review = _report_review_steps(session_id)
    return {
        "success": True,
        "data": {
            "session_id": session_id, "project_name": project_name,
            "content": content, "word_count": len(content), "review_steps": review,
        },
    }


class SaveReportRequest(BaseModel):
    session_id: str
    content: str


def _save_report(session_id: str, content: str, db: Session) -> None:
    if session_id in _shared_state._WORKFLOW_PROGRESS:
        raise HTTPException(status_code=409, detail="工作流正在执行")
    record = db.query(WorkflowSession).filter(WorkflowSession.session_id == session_id).first()
    if record:
        record.final_report = content
        record.updated_at = datetime.now(timezone.utc)
        db.commit()
    elif PerformanceAgent.load_state_backup(session_id):
        PerformanceAgent.update_state_backup(session_id, {"final_report": content})
    else:
        raise HTTPException(status_code=404, detail="工作流不存在，无法保存")

    workflow = _shared_state.active_workflows.get(session_id)
    if workflow:
        try:
            workflow.app.update_state({"configurable": {"thread_id": session_id}}, {"final_report": content})
        except Exception:
            logger.exception("同步报告检查点失败: {}", session_id)


@router.post("/workflow/save-report", dependencies=[Depends(_require_body_workflow)])
async def save_report_content(req: SaveReportRequest, db: Session = Depends(get_db)):
    _save_report(req.session_id, req.content, db)
    return {
        "success": True, "message": "报告已保存",
        "data": {"session_id": req.session_id, "word_count": len(req.content)},
    }


def _chapter_from_values(values: dict, key: str):
    """取某一章的正文。

    第四章（`indicator_analysis`）优先取步骤 7 的正文 `step7_prose`：整段输出是
    「评分 JSON + 正文」，直接拼进报告就是一段裸 JSON（批次 22）。老会话没有这个字段，
    回退原文，宁可在报告里留着也不清空章节。
    """
    if key == "indicator_analysis":
        prose = (values.get("step7_prose") or "").strip()
        if prose:
            return prose
    return values.get(key)


def _assemble_report_from_steps(values: dict) -> str:
    """从各步骤State值拼装完整报告Markdown"""
    sections = []
    project_name = values.get("project_name", "绩效评价报告")
    sections.append(f"# {project_name}\n")

    chapter_map = [
        ("一、绩效评价工作开展情况", values.get("work_introduction")),
        ("二、项目基本情况", values.get("basic_info")),
        ("三、评价指标体系", values.get("evaluation_table")),
        ("四、指标得分及分析", _chapter_from_values(values, "indicator_analysis")),
        ("五、综合评价分析及结论", values.get("comprehensive_evaluation")),
        ("六、主要经验做法", values.get("experience_summary")),
        ("七、存在问题及原因分析", values.get("problems_analysis")),
        ("八、改进建议", values.get("suggestions")),
    ]

    for title, content_section in chapter_map:
        clean = (content_section or "").strip()
        if "[调用失败" in clean or "[模型" in clean:
            clean = ""
        if not clean:
            clean = "（本部分内容尚未生成）"
        if not clean.startswith("#"):
            sections.append(f"## {title}\n\n{clean}\n")
        else:
            sections.append(f"{clean}\n")

    sections.append("\n---\n")
    sections.append("*报告由云海睿评AI生成系统自动生成*")
    return "\n".join(sections)


def _assemble_report_from_backup_steps(backup: dict) -> str:
    """从磁盘备份的 step_results 拼装报告（step name → chapter mapping）"""
    step_results = backup.get("step_results", {})
    project_name = backup.get("project_name", "绩效评价报告")

    # 步骤号 → 章节标题 映射
    step_chapter_map = {
        "13": "一、绩效评价工作开展情况",
        "12": "二、项目基本情况",
        "6": "三、评价指标体系",
        "7": "四、指标得分及分析",
        "11": "五、综合评价分析及结论",
        "8": "六、主要经验做法",
        "9": "七、存在问题及原因分析",
        "10": "八、改进建议",
    }

    sections = [f"# {project_name}\n"]
    for step_num, chapter_title in sorted(step_chapter_map.items()):
        step = step_results.get(step_num, {})
        content = ""
        if isinstance(step, dict):
            content = step.get("content", "")
        if step_num == "7":
            # 第四章只拼正文：不带这一步，报告里就会出现整段评分 JSON（批次 22）
            content = (backup.get("step7_prose") or "").strip() or content
        if not content:
            content = "（本部分内容尚未生成）"
        if not content.startswith("#"):
            sections.append(f"## {chapter_title}\n\n{content}\n")
        else:
            sections.append(f"{content}\n")

    sections.append("\n---\n")
    sections.append("*报告由云海睿评AI生成系统自动生成*")
    return "\n".join(sections)


# ═══════════════════════════════════════════════════════
#  步骤12 - 模块化内容生成
# ═══════════════════════════════════════════════════════

STEP12_MODULES = {
    "project_background": "项目背景",
    "project_content": "项目内容",
    "project_management": "项目组织管理情况",
    "project_funding": "项目资金投入情况",
    "project_targets": "项目绩效目标",
}


class ModuleGenerateRequest(BaseModel):
    session_id: str
    module_key: str
    module_name: Optional[str] = None


@router.post("/workflow/generate-module", dependencies=[Depends(_require_body_workflow)])
async def generate_module_content(req: ModuleGenerateRequest):
    """
    步骤12：单独生成某一个模块的内容
    支持：project_background, project_content, project_management, project_funding, project_targets
    """
    try:
        if req.module_key not in STEP12_MODULES and not req.module_name:
            raise HTTPException(status_code=400, detail=f"无效的模块: {req.module_key}，支持: {list(STEP12_MODULES.keys())}")

        module_name = req.module_name or STEP12_MODULES.get(req.module_key, req.module_key)

        values = _get_workflow_values(req.session_id)
        cfg = _shared_state.active_workflow_configs.get(req.session_id, {})
        model_name = cfg.get("model_name") or settings.DEFAULT_MODEL

        from backend.utils.llm_helper import get_model_credentials
        _creds = get_model_credentials(model_name)
        model = ChatOpenAI(model=model_name, api_key=_creds["api_key"], base_url=_creds["base_url"], temperature=0.7)

        project_name = values.get("project_name", "未命名项目")
        effective_data = values.get("effective_data", "") or values.get("raw_text", "") or ""

        module_prompts = {
            "project_background": "撰写项目背景部分，包括项目提出的背景、立项依据、项目必要性等。",
            "project_content": "撰写项目主要内容，包括建设内容/服务内容、实施周期、建设规模等。",
            "project_management": "撰写项目组织管理情况，包括组织机构设置、管理流程、制度建设等。",
            "project_funding": "撰写项目资金投入与使用情况，包括资金来源、到位情况、支出结构等。",
            "project_targets": "撰写项目绩效目标，包括原定的产出目标、效益目标、时效目标等。",
        }

        instruction = module_prompts.get(req.module_key, f"撰写「{module_name}」部分的详细内容。")

        prompt = f"""## 项目名称
{project_name}

## 项目核心资料
{effective_data[:5000]}

## 任务
{instruction}

请直接输出该模块的完整内容，要求客观、数据准确、语言规范。"""

        resp = model.invoke(prompt)
        content = resp.content if hasattr(resp, 'content') else str(resp)

        # 保存到模块字典（写回checkpoint）
        modules = values.get("basic_info_modules", {}) or {}
        modules[req.module_key] = content
        _persist_state(req.session_id, {"basic_info_modules": modules})

        return {
            "success": True,
            "data": {
                "module_key": req.module_key,
                "module_name": module_name,
                "content": content,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"生成模块内容失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/workflow/{session_id}/modules", dependencies=[Depends(_require_path_workflow)])
async def get_module_contents(session_id: str):
    """获取步骤12各模块内容"""
    try:
        values = _get_workflow_values(session_id)
        modules = values.get("basic_info_modules", {}) or {}
        return {
            "success": True,
            "data": {
                "modules": [
                    {"key": k, "name": STEP12_MODULES.get(k, k), "content": v}
                    for k, v in modules.items()
                ]
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取模块内容失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════
#  步骤14 - 最终报告自定义顺序组合 & 格式校验
# ═══════════════════════════════════════════════════════

# 报告的默认章节顺序
REPORT_SECTION_ORDER = {
    "step12_basic_info": "一、基本情况",
    "step13_work_intro": "二、绩效评价工作开展情况",
    "step6_indicator_system": "三、绩效评价指标体系",
    "step7_indicator_analysis": "四、指标体系分析情况",
    "step8_experience": "五、经验做法",
    "step9_problems": "六、问题及原因分析",
    "step10_suggestions": "七、建议",
    "step11_comprehensive": "八、综合评价分析情况及评价结论",
}

# 章节key → 工作流状态中的内容字段名
_SECTION_KEY_TO_STATE_FIELD = {
    "step12_basic_info": "basic_info",
    "step13_work_intro": "work_introduction",
    "step6_indicator_system": "evaluation_table",
    "step7_indicator_analysis": "indicator_analysis",
    "step8_experience": "experience_summary",
    "step9_problems": "problems_analysis",
    "step10_suggestions": "suggestions",
    "step11_comprehensive": "comprehensive_evaluation",
}


class ReportOrderRequest(BaseModel):
    session_id: str
    sections: List[Dict[str, Any]]


@router.get("/workflow/{session_id}/report-order", dependencies=[Depends(_require_path_workflow)])
async def get_report_order(session_id: str):
    """获取最终报告的章节顺序"""
    try:
        values = _get_workflow_values(session_id)
        saved_order = values.get("report_section_order")
        if saved_order:
            return {"success": True, "data": saved_order, "is_custom": True}
        # 返回默认顺序
        default = []
        for k, v in REPORT_SECTION_ORDER.items():
            state_field = _SECTION_KEY_TO_STATE_FIELD.get(k, "")
            has_content = bool(values.get(state_field) if state_field else values.get(k))
            default.append({"key": k, "name": v, "enabled": True, "has_content": has_content})
        return {"success": True, "data": default, "is_custom": False}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workflow/report-order", dependencies=[Depends(_require_body_workflow)])
async def set_report_order(req: ReportOrderRequest):
    """设置最终报告的自定义章节顺序"""
    try:
        values = _get_workflow_values(req.session_id)
        _persist_state(req.session_id, {"report_section_order": req.sections})
        return {"success": True, "message": "报告章节顺序已更新"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class AssembleReportRequest(BaseModel):
    session_id: str
    show_title: bool = True
    include_sections: Optional[List[str]] = None


@router.post("/workflow/assemble-report", dependencies=[Depends(_require_body_workflow)])
async def assemble_final_report(req: AssembleReportRequest, db: Session = Depends(get_db)):
    """
    步骤14：按自定义顺序组装最终报告
    支持选择性地包含某些章节
    """
    try:
        values = _get_workflow_values(req.session_id)
        project_name = values.get("project_name", "未命名项目")

        # 使用自定义顺序或默认顺序
        custom_order = values.get("report_section_order")
        if custom_order:
            section_map = {s["key"]: s for s in custom_order}
        else:
            section_map = {k: {"key": k, "name": v, "enabled": True} for k, v in REPORT_SECTION_ORDER.items()}

        # 按顺序组装
        review = set(values.get("review_steps", []))
        stale_fields = {STEP_CONTENT_FIELDS.get(step) for step in review}
        report_sections = []
        ordered_keys = custom_order and [s["key"] for s in custom_order] or list(REPORT_SECTION_ORDER.keys())
        if req.include_sections:
            ordered_keys = [k for k in ordered_keys if k in req.include_sections]

        for key in ordered_keys:
            sec = section_map.get(key, {"enabled": True})
            if not sec.get("enabled", True):
                continue
            title = sec.get("name", REPORT_SECTION_ORDER.get(key, key))
            # 通过映射获取正确的状态字段（basic_info / work_introduction 等）
            state_field = _SECTION_KEY_TO_STATE_FIELD.get(key, "")
            if state_field in stale_fields:
                report_sections.append(f"## {title}\n\n待复核：上游结果已修改。")
                continue
            content = values.get(state_field, "") if state_field else ""
            if not content:
                content = values.get(key, "") or ""
            if isinstance(content, dict):
                content = json.dumps(content, ensure_ascii=False, indent=2)
            if content:
                report_sections.append(f"## {title}\n\n{content}")

        full_report = "\n\n".join(report_sections)
        if req.show_title:
            full_report = f"# {project_name}绩效评价报告\n\n{full_report}"

        index = report_evidence_index(values.get("step_evidence", {}))
        if index:
            full_report += "\n\n" + index
        if review:
            return {"success": True, "data": {"report": full_report, "section_count": len(report_sections),
                                             "review_steps": sorted(review), "draft": True}}
        _save_report(req.session_id, full_report, db)
        return {"success": True, "data": {"report": full_report, "section_count": len(report_sections)}}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"组装报告失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workflow/{session_id}/format-check", dependencies=[Depends(_require_path_workflow)])
async def check_report_format(session_id: str):
    """
    步骤14：依据396号文要求进行格式校对
    检查最终报告的格式规范性和内容完整性
    """
    try:
        values = _get_workflow_values(session_id)
        final_report = values.get("final_report", "")
        if not final_report:
            raise HTTPException(status_code=400, detail="请先组装最终报告")

        cfg = _shared_state.active_workflow_configs.get(session_id, {})
        model_name = cfg.get("model_name") or settings.DEFAULT_MODEL
        from backend.utils.llm_helper import get_model_credentials
        _creds = get_model_credentials(model_name)
        model = ChatOpenAI(model=model_name, api_key=_creds["api_key"], base_url=_creds["base_url"], temperature=0.3)

        prompt = f"""你是绩效评价报告格式审查专家。请对以下报告进行格式和内容校对：

## 格式要求（参照396号文）：
1. 报告结构是否完整（包含基本情况、工作开展、指标体系、分析、经验、问题、建议、结论）
2. 各级标题编号是否规范（"一、" → "(一)" → "1." 层级）
3. 表格格式是否规范（表头、边框、数据对齐）
4. 措辞是否正式、客观
5. 数据前后是否一致

## 待校对报告：
{final_report[:8000]}

请按以下格式输出校对结果：
### 通过的检查项
（列出符合要求的项目）

### 需要修改的问题
| 序号 | 问题类型 | 位置 | 问题描述 | 修改建议 |
|------|----------|------|----------|----------|

### 总结评价
是否建议通过/需修改后提交"""
        resp = model.invoke(prompt)
        result = resp.content if hasattr(resp, 'content') else str(resp)
        _persist_state(session_id, {"format_check_result": result})

        return {"success": True, "data": {"check_result": result}}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"格式校对失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════
#  步骤12 - 动态模块添加/删除
# ═══════════════════════════════════════════════════════

class AddCustomModuleRequest(BaseModel):
    session_id: str
    module_key: str
    module_name: str
    content: Optional[str] = None


@router.post("/workflow/add-custom-module", dependencies=[Depends(_require_body_workflow)])
async def add_custom_module(req: AddCustomModuleRequest):
    """
    步骤12：添加自定义模块（除默认5个模块外）
    操作者可根据项目需要新增模块内容
    """
    try:
        values = _get_workflow_values(req.session_id)
        modules = values.get("basic_info_modules", {}) or {}

        if req.module_key in modules:
            raise HTTPException(status_code=400, detail="该模块key已存在")

        modules[req.module_key] = req.content or ""
        # 也更新模块名映射
        module_names = values.get("module_names", {}) or {}
        module_names[req.module_key] = req.module_name
        _persist_state(req.session_id, {
            "basic_info_modules": modules,
            "module_names": module_names,
        })

        return {"success": True, "data": {"key": req.module_key, "name": req.module_name},
                "message": f"模块「{req.module_name}」已添加"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"添加模块失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class DeleteModuleRequest(BaseModel):
    session_id: str
    module_key: str


@router.post("/workflow/delete-module", dependencies=[Depends(_require_body_workflow)])
async def delete_module(req: DeleteModuleRequest):
    """步骤12：删除自定义模块"""
    try:
        values = _get_workflow_values(req.session_id)
        modules = values.get("basic_info_modules", {}) or {}

        if req.module_key not in modules:
            raise HTTPException(status_code=404, detail="模块不存在")

        modules.pop(req.module_key, "")
        _persist_state(req.session_id, {"basic_info_modules": modules})

        return {"success": True, "message": f"模块已删除"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workflow/update-module-content", dependencies=[Depends(_require_body_workflow)])
async def update_module_content(req: AddCustomModuleRequest):
    """
    步骤12：更新已有模块内容（支持默认5模块+自定义模块）
    """
    try:
        values = _get_workflow_values(req.session_id)
        modules = values.get("basic_info_modules", {}) or {}
        modules[req.module_key] = req.content or ""
        _persist_state(req.session_id, {"basic_info_modules": modules})
        return {"success": True, "message": f"模块「{req.module_name or req.module_key}」内容已更新"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
