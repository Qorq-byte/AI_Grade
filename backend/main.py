"""
FastAPI应用主入口
提供后端API服务，支持前端交互
"""
from fastapi import FastAPI, Request, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from loguru import logger
from datetime import datetime, timedelta
import sys
import asyncio

from backend.config import settings
from backend.middleware.request_context import (
    RequestContextMiddleware,
    get_request_id,
    install_log_context,
)

# 前端目录（模块级常量）
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

# 配置日志（rid= 前缀使同一次请求的前后端日志可串联）
install_log_context()
logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{extra[request_id]}</cyan> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    level="DEBUG" if settings.DEBUG else "INFO"
)

# ── 启动 / 关闭（lifespan）──────────────────────────────
# 为什么不用 @app.on_event：FastAPI 已弃用（启动日志和测试里一直报 deprecation），
# 而且 on_event 的 shutdown 只能打日志、没法 await 被取消的后台任务。
# lifespan 允许「先 yield 再 await 收尾」，清理定时器才有地方等它真正停下（批次 23）。
@asynccontextmanager
async def lifespan(app: FastAPI):
    await _on_startup()
    try:
        yield
    finally:
        # 用 finally：正常退出、异常退出、被取消，都要把后台任务收干净
        await _on_shutdown()


async def _on_startup():
    """应用启动：配置自检 → 建库建表 → 默认管理员/模板 → 清理定时器。"""
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
        from backend.services.auth_service import create_admin_if_not_exists, mark_default_password_users
        db = SessionLocal()
        try:
            create_admin_if_not_exists(db)
            # 存量库里可能已有用初始口令跑着的账号（升级前创建的），逐个试算并打标
            mark_default_password_users(db)
        finally:
            db.close()
        logger.info("管理员账号检查完成")

        # 密钥存储状态（未配置 SECRET_ENCRYPTION_KEY 时这里会明确提示）
        from backend.services.secret_box import describe_storage
        logger.info(f"数据库密钥加密: {describe_storage()}")

        # 自动填充默认Prompt模板到数据库（仅插入缺失步骤）
        from backend.agents.prompts.default_prompts import PromptTemplateFactory, STEP_META
        db2 = SessionLocal()
        try:
            for step_number in range(1, 15):
                existing = db2.query(PromptTemplate).filter_by(
                    step_number=step_number, is_system=True
                ).first()
                if existing is None:
                    meta = STEP_META.get(step_number, {"name": f"Step {step_number}"})
                    system_prompt = PromptTemplateFactory.build_raw_system_prompt(step_number)
                    human_prompt = PromptTemplateFactory.build_raw_human_prompt(step_number)
                    full_prompt = f"{system_prompt}\n\n---\n\n{human_prompt}"
                    pt = PromptTemplate(
                        step_number=step_number,
                        step_name=meta.get("name", f"Step {step_number}"),
                        prompt_content=full_prompt,
                        is_system=True,
                        version=1,
                        change_log="系统初始化默认模板",
                    )
                    db2.add(pt)
            db2.commit()
            logger.info("默认Prompt模板已同步到数据库")
        except Exception as e:
            logger.warning(f"默认Prompt模板同步失败（非致命）: {e}")
        finally:
            db2.close()

        # ── 启动文件保留清理任务 ──
        # 沿用改前的行为：DB 起不来就不起清理任务（它一样连不上库）
        _start_cleanup_scheduler()

    except Exception as e:
        logger.error(f"数据库初始化失败: {str(e)}")


async def _on_shutdown():
    """应用关闭：停掉清理定时器（取消 + 等它收尾）。"""
    logger.info("应用关闭...")
    try:
        await _stop_cleanup_scheduler()
    except Exception as e:  # noqa: BLE001 关闭阶段不能再把进程卡住
        logger.error(f"关闭清理定时器失败: {e}")


# 创建FastAPI应用实例
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="基于LangGraph和RAG的智能绩效评价报告生成系统",
    debug=settings.DEBUG,
    lifespan=lifespan,
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
    # 前端需要读取 request_id 以在报错时告知用户/支持人员；
    # X-Password-Change-Required 用于区分「首登要改密」与「登录失效」两种 403
    expose_headers=["X-Request-ID", "X-Password-Change-Required"],
)

# 请求上下文（最外层）：为每个请求生成/透传 X-Request-ID 并注入日志。
# 放在 CORS 之后注册 = 更靠外层，保证 CORS 生成的响应同样带上该头。
app.add_middleware(RequestContextMiddleware)

# 全局异常处理 — 确保所有异常返回JSON而非HTML traceback
# 三类错误体都带 request_id：前端报错时把请求号显示出来，即可用一行日志定位。
def _rid(request: Request) -> str:
    return getattr(request.state, "request_id", "") or get_request_id()


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "detail": exc.detail, "request_id": _rid(request)},
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"success": False, "detail": jsonable_encoder(exc.errors()),
                 "request_id": _rid(request)},
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    request_id = _rid(request)
    # 必须用 opt(exception=True)：loguru 不认 exc_info，会把它当格式化参数；
    # 而异常文本里一旦出现花括号（JSON / dict repr），format 会抛 IndexError，
    # 兜底处理器自己崩掉、客户端只拿到空响应体（批次 35 踩过）。
    logger.opt(exception=True).error(f"未处理的异常 [{request.method} {request.url.path}]: {exc}")
    # 异常原文可能带 SQL、路径、内部字段名，只在调试模式下回给前端；
    # 生产环境让用户报 request_id 即可，日志里能查到完整堆栈。
    detail = f"服务器内部错误: {exc}" if settings.DEBUG else "服务器内部错误，请稍后重试"
    return JSONResponse(
        status_code=500,
        content={"success": False, "detail": detail,
                 "request_id": request_id},
    )

# 导入路由
from backend.routes import agent_routes
from backend.routes.client_routes import router as client_router


# 后台清理任务的引用：`asyncio.create_task` 返回的任务只被事件循环弱引用，
# 不自己拿住就有被 GC 的风险；而且进程关闭时没人 cancel 它（批次 23）。
_CLEANUP_TASK: Optional[asyncio.Task] = None


def _start_cleanup_scheduler() -> Optional[asyncio.Task]:
    """启动后台定时清理任务（每天凌晨3点检查）；重复调用返回同一个任务。"""
    global _CLEANUP_TASK
    if _CLEANUP_TASK is not None and not _CLEANUP_TASK.done():
        return _CLEANUP_TASK

    from backend.services.cleanup_service import CleanupService
    from backend.database import SessionLocal

    async def _cleanup_loop():
        while True:
            now = datetime.now()
            next_run = now.replace(hour=3, minute=0, second=0, microsecond=0)
            if next_run <= now:
                next_run = next_run + timedelta(days=1)
            wait_seconds = (next_run - now).total_seconds()

            logger.info(f"文件清理任务将在 {next_run.strftime('%Y-%m-%d %H:%M:%S')} 执行（等待 {wait_seconds/3600:.1f} 小时）")
            await asyncio.sleep(wait_seconds)

            try:
                db = SessionLocal()
                try:
                    stuck_count = CleanupService.cleanup_failed_workflows(db, max_age_hours=24)
                    if stuck_count > 0:
                        logger.info(f"自动清理了 {stuck_count} 个卡住的工作流")

                    stats = CleanupService.cleanup_expired_files(db)
                    if stats["deleted_files"] > 0:
                        logger.info(
                            f"自动清理完成: 删除 {stats['deleted_files']} 文件, "
                            f"释放 {stats['freed_disk_bytes']/1024/1024:.2f} MB"
                        )
                finally:
                    db.close()
            except Exception as e:
                logger.error(f"自动清理任务执行失败: {e}")

    _CLEANUP_TASK = asyncio.create_task(_cleanup_loop(), name="file-cleanup")
    logger.info("文件保留清理定时器已启动（每日凌晨3点）")
    return _CLEANUP_TASK


async def _stop_cleanup_scheduler() -> bool:
    """取消清理任务并等它真正结束；返回是否真的停了一个在跑的任务。

    只 cancel 不 await 等于把清理逻辑留在半路（它在写文件/写库时被砍断），
    所以这里等任务退出后再返回。
    """
    global _CLEANUP_TASK
    task, _CLEANUP_TASK = _CLEANUP_TASK, None
    if task is None or task.done():
        return False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as e:  # noqa: BLE001 任务自身异常不应阻断关闭
        logger.error(f"清理任务退出时异常: {e}")
    logger.info("文件保留清理定时器已停止")
    return True


@app.get("/health")
async def health_check():
    """健康检查接口"""
    return {
        "status": "healthy",
        "service": settings.APP_NAME,
        "version": settings.APP_VERSION
    }


# 注册路由
app.include_router(agent_routes.router, prefix="/api/agent", tags=["Agent"])
# 批次 33：问卷域单独一个模块（从 agent_routes 拆出，路径与依赖不变）
from backend.routes.questionnaire_routes import router as questionnaire_router
app.include_router(questionnaire_router, prefix="/api/agent", tags=["问卷"])
# 批次 41：指标与评分域单独一个模块（同上）
from backend.routes.scoring_routes import router as scoring_router
app.include_router(scoring_router, prefix="/api/agent", tags=["指标与评分"])
# 批次 35：报告域单独一个模块（同上）
from backend.routes.report_routes import router as report_router
app.include_router(report_router, prefix="/api/agent", tags=["报告"])
# 批次 34：导出域单独一个模块（同上，只搬代码不改逻辑）
from backend.routes.export_routes import router as export_router
app.include_router(export_router, prefix="/api/agent", tags=["导出"])
from backend.routes.auth_routes import router as auth_router
app.include_router(auth_router)

from backend.routes.admin_routes import router as admin_router
app.include_router(admin_router)

app.include_router(client_router)

# 挂载前端静态文件（CSS/JS等）
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="frontend")
    logger.info(f"前端静态文件已挂载: {FRONTEND_DIR}")


if __name__ == "__main__":
    import uvicorn
    logger.info(f"启动服务器: http://{settings.HOST}:{settings.PORT}")
    uvicorn.run(
        "backend.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG
    )
