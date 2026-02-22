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

    resp = FileResponse(str(full_path))
    # HTML 页面禁用缓存以确保用户看到最新版本
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

def _register_page_routes():
    """动态注册所有前端页面路由"""
    for route, (path, name) in _PAGE_ROUTES.items():
        def _handler(p=path, n=name):
            return _serve_page(p, n)
        _handler.__name__ = f"page_{name}"
        app.get(route)(_handler)

_register_page_routes()

# 配置CORS（允许前端跨域访问）
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.get_allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 全局异常处理 — 确保所有异常返回JSON而非HTML traceback
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "detail": exc.detail},
        headers=getattr(exc, "headers", None),
    )

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"未处理的异常 [{request.method} {request.url.path}]: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"success": False, "detail": f"服务器内部错误: {str(exc)}"},
    )

# 导入路由
from backend.routes import agent_routes
from backend.routes.client_routes import router as client_router


@app.on_event("startup")
async def startup_event():
    """应用启动事件"""
    logger.info(f"{settings.APP_NAME} v{settings.APP_VERSION} 启动中...")
    
    # ── 环境配置校验 ──
    config_issues = settings.validate_required_settings()
    if config_issues:
        logger.warning("=" * 50)
        logger.warning("环境配置缺失，请检查以下配置项（可参考 .env.example）：")
        for issue in config_issues:
            logger.warning(f"  ⚠ {issue}")
        logger.warning("=" * 50)
    
    logger.info(f"LangSmith追踪: {'启用' if settings.LANGCHAIN_TRACING_V2 else '禁用'}")
    logger.info(f"默认模型: {settings.DEFAULT_MODEL}")
    
    # 初始化数据库
    try:
        from backend.database import init_db
        # 先导入所有模型，确保Base.metadata包含所有表定义
        from backend.models.user_models import (
            User, Project, ProjectFile, WorkflowSession, Report,
            TokenUsage, PromptTemplate, AiConversation, AiMessage,
            OperationLog, Notification, ReportModuleSequence,
        )
        init_db()
        logger.info("数据库初始化成功")
        
        # 自动创建默认管理员账号
        from backend.database import SessionLocal
        from backend.services.auth_service import create_admin_if_not_exists
        db = SessionLocal()
        try:
            create_admin_if_not_exists(db)
        finally:
            db.close()
        logger.info("管理员账号检查完成")
        
