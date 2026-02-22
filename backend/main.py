"""
FastAPI应用主入口
提供后端API服务，支持前端交互
"""
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pathlib import Path
from loguru import logger
from datetime import datetime, timedelta
import sys
import asyncio

from backend.config import settings

# 前端目录（模块级常量）
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

# 配置日志
logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    level="DEBUG" if settings.DEBUG else "INFO"
)

# 创建FastAPI应用实例
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="基于LangGraph和RAG的智能绩效评价报告生成系统",
    debug=settings.DEBUG
)

# ── 前端页面路由注册表 ──
_PAGE_ROUTES: dict[str, tuple[str, str]] = {
    "/": ("index.html", "平台首页"),
    "/login": ("login/index.html", "登录注册"),
    "/workflow": ("workflow/index.html", "工作报告生成"),
    "/admin": ("admin/index.html", "管理员面板"),
    "/questionnaire": ("questionnaire/index.html", "调查问卷"),
    "/report": ("report/index.html", "报告编辑"),
    "/client": ("client/index.html", "客户端仪表盘"),
}

def _serve_page(path: str, name: str) -> FileResponse:
    """统一的静态页面响应，包含缓存策略"""
    full_path = FRONTEND_DIR / path
    if not full_path.exists():
        raise HTTPException(status_code=404, detail=f"{name}页面不存在")

