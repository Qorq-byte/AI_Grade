"""
请求速率限制中间件
基于 IP + 路径的滑动窗口计数器
"""
import time
import threading
from collections import defaultdict
from fastapi import Request, HTTPException
from loguru import logger

# 内存存储（生产环境推荐 Redis）
_rate_limit_store: dict = defaultdict(lambda: {"hits": 0, "reset_at": 0})
_lock = threading.Lock()

# 默认限制
RATE_LIMITS = {
    "/api/auth/login":     (10, 60),    # 10次/分钟
    "/api/auth/register":  (3, 3600),   # 3次/小时
    "/api/agent/upload":   (30, 60),    # 30次/分钟
    "/api/agent/workflow/questionnaire/submit": (20, 60),
    "default":             (100, 60),   # 100次/分钟
}


def _find_matching_limit(path: str) -> tuple:
    """匹配最精确的限流规则"""
    best_key = ""
    best_limit = RATE_LIMITS["default"]
    for key, limit in RATE_LIMITS.items():
        if key != "default" and path.startswith(key):
            if len(key) > len(best_key):
                best_key = key
                best_limit = limit
    return best_limit


async def rate_limit_middleware(request: Request, call_next):
    """FastAPI 中间件：基于 IP 的滑动窗口限流"""
    ip = request.client.host if request.client else "unknown"
    path = request.url.path
    max_hits, window_sec = _find_matching_limit(path)
    key = f"{ip}:{path}"

    now = time.time()
    with _lock:
        entry = _rate_limit_store[key]
        if now > entry["reset_at"]:
            entry["hits"] = 0
            entry["reset_at"] = now + window_sec

        entry["hits"] += 1
        remaining = max(0, max_hits - entry["hits"])
        reset_ts = entry["reset_at"]

    if remaining <= 0:
        logger.warning(f"速率限制触发: ip={ip} path={path} hits={entry['hits']}/{max_hits}")
        raise HTTPException(
            status_code=429,
            detail="请求过于频繁，请稍后再试",
            headers={
                "X-RateLimit-Limit": str(max_hits),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(int(reset_ts)),
            }
        )

    response = await call_next(request)
    response.headers["X-RateLimit-Limit"] = str(max_hits)
    response.headers["X-RateLimit-Remaining"] = str(max(0, remaining - 1))
    response.headers["X-RateLimit-Reset"] = str(int(reset_ts))
    return response