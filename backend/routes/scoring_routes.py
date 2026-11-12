"""指标与评分域的路由（从 `agent_routes.py` 拆出，批次 41）。

拆分原则：**只搬代码，不改逻辑**。路径、方法、依赖、响应模型一字不动，
由 `tests/_route_snapshot.py` 前后比对来证明（153 条零差异）。

这个域覆盖步骤 6–7 的整条链路：
- 取指标（`/indicators-for-scoring`）、拖拽重排（`/reorder-indicators`）；
- 生成指标分析、保存逐指标得分、抽取/确认扣分点、逻辑校对；
- 评分汇总（`/score-summary`）。

跨域共享：依赖与状态读写从 `_workflow_deps` 拿（`_review_after` 也在那里，
因为它同时被步骤编辑与评分保存使用）；可变状态按模块访问 `_shared_state`。
本模块**不 import `agent_routes`**。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from langchain_openai import ChatOpenAI
from loguru import logger

from backend.agents import performance_agent
from backend.agents.tools.questionnaire_gen import indicator_leaves
from backend.agents.tools.score_validator import actual_indicator_score, score_number
from backend.config import settings
from backend.routes import _shared_state
from backend.routes._workflow_deps import (
    _get_workflow_values, _persist_state, _require_body_workflow, _require_path_workflow,
    _review_after,
)

router = APIRouter()


# ═══════════════════════════════════════════════════════
#  步骤7 - 逐指标评分分析接口
# ═══════════════════════════════════════════════════════

class IndicatorScoreInput(BaseModel):
    """单指标评分输入"""
    indicator_id: str
    indicator_name: str
    level1_name: str = ""
    level2_name: str = ""
    level3_name: str = ""
    full_score: float = 0
    actual_score: float = 0
    score_reason: str = ""


class BatchIndicatorScoreRequest(BaseModel):
    session_id: str
    scores: List[IndicatorScoreInput]


class IndicatorScoreItem(BaseModel):
    """单指标分析项"""
    indicator_id: str
    evaluation_point: str
    score: Optional[float] = None
    reason: Optional[str] = None


class GenerateIndicatorAnalysisRequest(BaseModel):
    session_id: str
    indicator_id: str
    level1_name: str = ""
    level2_name: str = ""
    level3_name: str = ""
    indicator_explanation: str = ""
    scoring_standard: str = ""
    score_inputs: List[IndicatorScoreItem] = []


@router.get("/workflow/{session_id}/indicators-for-scoring", dependencies=[Depends(_require_path_workflow)])
async def get_indicators_for_scoring(session_id: str):
    """
    步骤7：获取需要进行评分的指标体系结构（用于前端生成评分输入框）
    返回所有三级指标及其评分标准、指标解释，供逐指标填写得分和原因
    """
    try:
        values = _get_workflow_values(session_id)
        indicator_system = values.get("indicator_system", {})
        if not indicator_system or not indicator_system.get("level1_indicators"):
            raise HTTPException(status_code=400, detail="指标体系尚未生成，请先完成步骤3")

        items = []
        for l1 in indicator_system.get("level1_indicators", []):
            l1_name = l1.get("name", "")
            l1_score = l1.get("score", 0)
            for l2 in l1.get("children", []):
                l2_name = l2.get("name", "")
                l2_score = l2.get("score", 0)
                for l3 in l2.get("children", []):
                    l3_name = l3.get("name", "")
                    l3_score = l3.get("score", 0)
                    l3_id = l3.get("id", f"{l1_name}_{l2_name}_{l3_name}")
                    explanation = l3.get("explanation", "")
                    scoring_standard = l3.get("scoring_standard", "")

                    # 解析评分标准中的评价要点
                    eval_points = _parse_evaluation_points(scoring_standard)

                    items.append({
                        "indicator_id": l3_id,
                        "level1_name": l1_name,
                        "level1_score": l1_score,
                        "level2_name": l2_name,
                        "level2_score": l2_score,
                        "level3_name": l3_name,
                        "full_score": l3_score,
                        "indicator_explanation": explanation,
                        "scoring_standard": scoring_standard,
                        "evaluation_points": eval_points,
                    })

        return {"success": True, "data": {"indicators": items, "total_count": len(items)}}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取评分指标失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workflow/generate-indicator-analysis", dependencies=[Depends(_require_body_workflow)])
async def generate_indicator_analysis(req: GenerateIndicatorAnalysisRequest):
    """
    步骤7：为单个三级指标生成分析内容
    根据操作者输入的评分要点的得分和原因，由大模型撰写指标分析
    """
    try:
        values = _get_workflow_values(req.session_id)
        cfg = _shared_state.active_workflow_configs.get(req.session_id, {})
        model_name = cfg.get("model_name") or settings.DEFAULT_MODEL

        from backend.utils.llm_helper import get_model_credentials
        _creds = get_model_credentials(model_name)
        model = ChatOpenAI(model=model_name, api_key=_creds["api_key"], base_url=_creds["base_url"], temperature=0.7)

        project_core = values.get("effective_data", "")[:3000]
        score_inputs_text = "\n".join([
            f"- 评价要点「{s.evaluation_point}」: 得分={s.score}, 原因={s.reason or '无'}"
            for s in req.score_inputs
        ]) if req.score_inputs else "（无评分输入）"

        prompt = f"""## 项目核心内容
{project_core}

## 当前分析指标
- 一级指标：{req.level1_name}
- 二级指标：{req.level2_name}
- 三级指标：{req.level3_name}
- 指标解释：{req.indicator_explanation}
- 评分标准：{req.scoring_standard}
- 满分分值：{sum(s.score or 0 for s in req.score_inputs) if req.score_inputs else 0}

## 操作者评分输入
{score_inputs_text}

## 任务
请根据上述信息，撰写该三级指标的详细分析内容。分析应：
1. 逐条对照评分标准中的评价要点
2. 说明得分/扣分原因
3. 引用项目核心内容中的具体事实和数据
4. 语言专业、客观

请直接输出分析内容。"""
        resp = await run_in_threadpool(model.invoke, prompt)
        analysis = resp.content if hasattr(resp, 'content') else str(resp)
        return {"success": True, "data": {"analysis": analysis, "indicator_id": req.indicator_id}}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"生成指标分析失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workflow/save-indicator-scores", dependencies=[Depends(_require_body_workflow)])
async def save_indicator_scores(req: BatchIndicatorScoreRequest):
    """
    步骤7：保存逐指标评分结果并生成评分表
    """
    try:
        values = _get_workflow_values(req.session_id)
        if req.session_id in _shared_state._WORKFLOW_PROGRESS:
            raise HTTPException(status_code=409, detail="工作流执行中，暂不能修改评分")
        indicators = {item.get("id"): item for item in indicator_leaves(
            values.get("indicator_system", {}).get("level1_indicators", [])
        )}
        score_map = dict(values.get("indicator_score_map", {}))
        seen = set()
        for entry in req.scores:
            indicator = indicators.get(entry.indicator_id)
            full = score_number(indicator.get("score")) if indicator else None
            actual = score_number(entry.actual_score)
            if (entry.indicator_id in seen or full is None or actual is None
                    or actual > full or score_number(entry.full_score) != full):
                raise HTTPException(status_code=422, detail=f"指标 {entry.indicator_id} 的分值、满分或 ID 无效")
            seen.add(entry.indicator_id)
            score_map[entry.indicator_id] = {
                **entry.model_dump(), "indicator_name": indicator["name"],
                "full_score": full, "actual_score": actual,
            }

        # 生成评分表
        score_table = _generate_score_table(values.get("indicator_system", {}), score_map)
        _persist_state(req.session_id, {
            "indicator_score_map": score_map,
            "score_table": score_table,
            # 人工改过分值后台账必须跟着变，否则界面继续显示旧的「7/23 已落地」
            "score_disposition": performance_agent.refresh_disposition(
                values.get("score_disposition") or {}, score_map, list(indicators)
            ),
            "review_steps": _review_after(values, 7),
        })

        return {"success": True, "data": {"score_table": score_table, "saved_count": len(seen)}}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"保存评分结果失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════
#  步骤9 - 扣分点一键提取
# ═══════════════════════════════════════════════════════

@router.get("/workflow/{session_id}/extract-deductions", dependencies=[Depends(_require_path_workflow)])
async def extract_deduction_points(session_id: str):
    """
    步骤9：从步骤7指标分析中提取扣分点
    返回扣分指标列表，供操作者选择哪些作为问题提出
    """
    try:
        values = _get_workflow_values(session_id)
        indicator_analysis = values.get("indicator_analysis", "")
        score_map = values.get("indicator_score_map", {})

        if not indicator_analysis and not score_map:
            raise HTTPException(status_code=400, detail="步骤7尚未完成，请先生成指标分析")

        deduction_points = []
        if score_map:
            for ind_id, info in score_map.items():
                actual = score_number(info.get("actual_score"))
                full = score_number(info.get("full_score"))
                if actual is not None and full is not None and actual < full:
                    deduction_points.append({
                        "indicator_id": ind_id,
                        "indicator_name": info.get("indicator_name", ""),
                        "level1_name": info.get("level1_name", ""),
                        "level2_name": info.get("level2_name", ""),
                        "full_score": full,
                        "actual_score": actual,
                        "deduction": round(full - actual, 2),
                        "score_reason": info.get("score_reason", ""),
                        "selected": True,
                        "source": "score_map",
                    })

        return {
            "success": True,
            "data": {
                "deduction_points": deduction_points,
                "total_count": len(deduction_points),
                "total_deduction": sum(p.get("deduction", 0) for p in deduction_points),
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"提取扣分点失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class DeductionSelectionRequest(BaseModel):
    session_id: str
    selected_points: List[Dict[str, Any]]
    manual_problems: Optional[List[Dict[str, str]]] = None


@router.post("/workflow/confirm-deductions", dependencies=[Depends(_require_body_workflow)])
async def confirm_deduction_points(req: DeductionSelectionRequest):
    """
    步骤9：确认扣分点选择（含手动添加的问题）
    操作者选择哪些扣分点作为问题提出，也可以手动添加问题
    """
    try:
        values = _get_workflow_values(req.session_id)
        _persist_state(req.session_id, {
            "confirmed_deduction_points": req.selected_points,
            "manual_problems": req.manual_problems or [],
        })
        return {
            "success": True,
            "data": {
                "confirmed_count": len(req.selected_points),
                "manual_count": len(req.manual_problems or []),
            },
            "message": "扣分点已确认"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"确认扣分点失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════
#  步骤10后 - 逻辑校对
# ═══════════════════════════════════════════════════════

@router.post("/workflow/{session_id}/verify-logic", dependencies=[Depends(_require_path_workflow)])
async def verify_logic_consistency(session_id: str):
    """
    步骤10后：对指标体系、指标分析、问题、建议进行逻辑校对
    确保通篇逻辑合理、数据一致
    """
    try:
        values = _get_workflow_values(session_id)
        cfg = _shared_state.active_workflow_configs.get(session_id, {})

        indicator_system = values.get("indicator_system", {})
        indicator_analysis = values.get("indicator_analysis", "")
        problems_analysis = values.get("problems_analysis", "")
        suggestions = values.get("suggestions", "")
        score_table = values.get("score_table", "")

        if not all([indicator_analysis, problems_analysis, suggestions]):
            raise HTTPException(status_code=400, detail="请先完成步骤7-10后再进行逻辑校对")

        model_name = cfg.get("model_name") or settings.DEFAULT_MODEL
        from backend.utils.llm_helper import get_model_credentials
        _creds = get_model_credentials(model_name)
        model = ChatOpenAI(model=model_name, api_key=_creds["api_key"], base_url=_creds["base_url"], temperature=0.3)

        prompt = f"""你是绩效评价报告逻辑校对专家。请对以下内容进行逻辑一致性校对：

## 一、指标体系（Step6）
{json.dumps(indicator_system, ensure_ascii=False, indent=2)[:2000]}

## 二、指标分析（Step7）
{indicator_analysis[:2000]}

## 三、问题及原因分析（Step9）
{problems_analysis[:2000]}

## 四、改进建议（Step10）
{suggestions[:2000]}

## 校对要求
请检查以下维度：
1. **总分一致性**：各部分得分汇总是否等于100分
2. **问题-指标对应**：问题分析中提到的扣分项是否与指标分析中的扣分一致
3. **建议-问题对应**：每条建议是否都有对应的问题
4. **数据一致性**：全文提及的金额、比例、数字是否前后矛盾
5. **逻辑连贯性**：从指标体系到分析到问题到建议的逻辑链条是否完整

请按以下格式输出：
### 校对通过项
（列出通过校对的维度）

### 发现的问题
| 序号 | 类型 | 问题描述 | 涉及内容 | 修改建议 |
|------|------|----------|----------|----------|

### 总结
（是否需要修改，关键问题概述）"""
        resp = await run_in_threadpool(model.invoke, prompt)
        result_text = resp.content if hasattr(resp, 'content') else str(resp)

        # 保存到state（写回checkpoint）
        _persist_state(session_id, {"logic_verification": result_text})

        return {"success": True, "data": {"verification_result": result_text}}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"逻辑校对失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════
#  步骤11 - 自动评分汇总与等级判定
# ═══════════════════════════════════════════════════════

class GradeThresholdsRequest(BaseModel):
    excellent: float = 90
    good: float = 80
    medium: float = 60
    poor: float = 0


@router.get("/workflow/{session_id}/score-summary", dependencies=[Depends(_require_path_workflow)])
async def get_score_summary(
    session_id: str,
    excellent: float = Query(90, description="优秀线"),
    good: float = Query(80, description="良好线"),
    medium: float = Query(60, description="合格线"),
):
    """
    步骤11：自动评分汇总与等级判定
    从指标体系+评分结果计算汇总得分、判定等级、生成图表数据
    """
    try:
        values = _get_workflow_values(session_id)
        indicator_system = values.get("indicator_system", {})
        score_map = values.get("indicator_score_map", {})

        if not indicator_system:
            raise HTTPException(status_code=400, detail="指标体系尚未生成")

        # 计算各一级指标得分
        level1_scores = []
        total_score = 0
        total_full = 0
        missing = 0
        assessed = 0

        for l1 in indicator_system.get("level1_indicators", []):
            l1_name = l1.get("name", "")
            l1_score = 0
            l1_full = 0
            level2_data = []

            for l2 in l1.get("children", []):
                l2_name = l2.get("name", "")
                l2_score = 0
                l2_full = 0
                level3_data = []

                for l3 in l2.get("children", []):
                    l3_name = l3.get("name", "")
                    l3_full_score = score_number(l3.get("score")) or 0
                    l3_id = l3.get("id", f"{l1_name}_{l2_name}_{l3_name}")
                    l3_score = actual_indicator_score(l3, score_map, l3_id)
                    missing += l3_score is None
                    assessed += l3_score is not None
                    l2_score += l3_score if l3_score is not None else 0
                    l2_full += l3_full_score
                    level3_data.append({
                        "name": l3_name,
                        "full_score": l3_full_score,
                        "actual_score": l3_score,
                        "score_rate": round(l3_score / l3_full_score * 100, 1) if l3_score is not None and l3_full_score > 0 else None,
                    })

                l1_score += l2_score
                l1_full += l2_full
                level2_data.append({
                    "name": l2_name,
                    "full_score": l2_full,
                    "actual_score": l2_score,
                    "score_rate": round(l2_score / l2_full * 100, 1) if l2_full > 0 else 0,
                    "items": level3_data,
                })

            total_score += l1_score
            total_full += l1_full
            level1_scores.append({
                "name": l1_name,
                "full_score": l1_full,
                "actual_score": l1_score,
                "score_rate": round(l1_score / l1_full * 100, 1) if l1_full > 0 else 0,
                "items": level2_data,
            })

        # 等级判定
        score_100 = round(total_score / total_full * 100, 2) if total_full > 0 else 0
        if missing or not assessed or abs(total_full - 100) > 0.01:
            grade = "待核实"
            score_100 = None
        elif score_100 >= excellent:
            grade = "优秀"
        elif score_100 >= good:
            grade = "良好"
        elif score_100 >= medium:
            grade = "合格"
        else:
            grade = "不合格"

        # 图表数据（用于前端柱状图）
        chart_data = {
            "categories": [item["name"] for item in level1_scores],
            "full_scores": [item["full_score"] for item in level1_scores],
            "actual_scores": [item["actual_score"] for item in level1_scores],
            "score_rates": [item["score_rate"] for item in level1_scores],
        }

        result = {
            "total_score": round(total_score, 2),
            "total_full": total_full,
            "score_100": score_100,
            "grade": grade,
            "missing_count": missing,
            # 缺失原因的分项口径（新增字段，向后兼容）：只有 missing_count 无法回答
            # 「是模型看过资料说没法评，还是系统把评分丢了」——实测这两种差 16 倍
            "disposition": values.get("score_disposition", {}) or {},
            "thresholds": {"excellent": excellent, "good": good, "medium": medium},
            "level1_scores": level1_scores,
            "chart_data": chart_data,
        }

        return {"success": True, "data": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"评分汇总失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════
#  评分标准解析
# ═══════════════════════════════════════════════════════

def _parse_evaluation_points(scoring_standard: str) -> list:
    """从评分标准文本中解析评价要点"""
    points = []
    if not scoring_standard:
        return points
    # 按常见序号分割
    lines = re.split(r'[\n;；]', scoring_standard)
    for line in lines:
        line = re.sub(r'^\s*[\d一二三四五六七八九十]+[、.)）]\s*', '', line).strip()
        if line and len(line) > 2:
            points.append(line)
    if not points:
        points = [scoring_standard]
    return points


def _generate_score_table(indicator_system: dict, score_map: dict) -> str:
    """从指标体系和评分映射生成评分表Markdown"""
    rows = []
    rows.append("| 一级指标 | 二级指标 | 三级指标 | 分值 | 得分 | 得分率 |")
    rows.append("|----------|----------|----------|------|------|--------|")

    for l1 in indicator_system.get("level1_indicators", []):
        l1_name = l1.get("name", "")
        for l2 in l1.get("children", []):
            l2_name = l2.get("name", "")
            l2_full, l2_score = 0, 0
            sub_rows = []
            for l3 in l2.get("children", []):
                l3_name = l3.get("name", "")
                l3_full = score_number(l3.get("score")) or 0
                l3_id = l3.get("id", f"{l1_name}_{l2_name}_{l3_name}")
                actual = actual_indicator_score(l3, score_map, l3_id)
                rate = f"{actual / l3_full * 100:.1f}%" if actual is not None and l3_full else "—"
                l2_full += l3_full
                l2_score += actual if actual is not None else 0
                score_text = actual if actual is not None else "待核实"
                sub_rows.append(f"| {l1_name} | {l2_name} | {l3_name} | {l3_full} | {score_text} | {rate} |")
            for sr in sub_rows:
                rows.append(sr)
            rows.append(f"| | **{l2_name} 已核实小计** | | **{l2_full}** | **{l2_score}** | — |")

    return "\n".join(rows)


# ═══════════════════════════════════════════════════════
#  步骤6 - 指标拖拽重排序
# ═══════════════════════════════════════════════════════

class ReorderRequest(BaseModel):
    session_id: str
    level: str  # "level1" / "level2" / "level3"
    from_index: int
    to_index: int
    parent_l1_idx: Optional[int] = None  # level2时需指定属于哪个一级指标
    parent_l2_idx: Optional[int] = None  # level3时需指定属于哪个二级指标


@router.post("/workflow/reorder-indicators", dependencies=[Depends(_require_body_workflow)])
async def reorder_indicators(req: ReorderRequest):
    """
    步骤6：拖拽重排序指标体系
    level1: 一级指标顺序拖动（其下所有二级、三级指标跟随移动）
    level2: 二级指标在某一级指标内拖动（其下三级指标跟随）
    level3: 三级指标在某二级指标内拖动（不能跨二级指标）
    """
    try:
        values = _get_workflow_values(req.session_id)
        indicator_system = values.get("indicator_system", {})
        if not indicator_system or not indicator_system.get("level1_indicators"):
            raise HTTPException(status_code=400, detail="指标体系尚未生成")

        l1_list = indicator_system.get("level1_indicators", [])

        if req.level == "level1":
            if 0 <= req.from_index < len(l1_list) and 0 <= req.to_index < len(l1_list):
                item = l1_list.pop(req.from_index)
                l1_list.insert(req.to_index, item)
                indicator_system["level1_indicators"] = l1_list

        elif req.level == "level2":
            if req.parent_l1_idx is None:
                raise HTTPException(status_code=400, detail="level2重排需要指定parent_l1_idx")
            if req.parent_l1_idx >= len(l1_list):
                raise HTTPException(status_code=400, detail="一级指标索引越界")
            l2_list = l1_list[req.parent_l1_idx].get("children", [])
            if 0 <= req.from_index < len(l2_list) and 0 <= req.to_index < len(l2_list):
                item = l2_list.pop(req.from_index)
                l2_list.insert(req.to_index, item)
                l1_list[req.parent_l1_idx]["children"] = l2_list

        elif req.level == "level3":
            if req.parent_l1_idx is None or req.parent_l2_idx is None:
                raise HTTPException(status_code=400, detail="level3重排需要指定parent_l1_idx和parent_l2_idx")
            if req.parent_l1_idx >= len(l1_list):
                raise HTTPException(status_code=400, detail="一级指标索引越界")
            l2_list = l1_list[req.parent_l1_idx].get("children", [])
            if req.parent_l2_idx >= len(l2_list):
                raise HTTPException(status_code=400, detail="二级指标索引越界")
            l3_list = l2_list[req.parent_l2_idx].get("children", [])
            if 0 <= req.from_index < len(l3_list) and 0 <= req.to_index < len(l3_list):
                item = l3_list.pop(req.from_index)
                l3_list.insert(req.to_index, item)
                l2_list[req.parent_l2_idx]["children"] = l3_list
                l1_list[req.parent_l1_idx]["children"] = l2_list
        else:
            raise HTTPException(status_code=400, detail="level参数无效，支持: level1/level2/level3")

        # 写回checkpoint（workflow.app 是编译后的图，才有update_state方法）
        _persist_state(req.session_id, {"indicator_system": indicator_system,
                                       "review_steps": _review_after(values, 3)})

        return {"success": True, "message": f"{req.level} 指标重排序完成"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"重排序失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))
