"""
导出幂等缓存
同一用户对同一份内容、同一排版、同一种格式，在时间窗内只真正渲染一次。

为什么需要它：
  - 导出是重操作（Word 逐段落构造、PDF 走 reportlab 布局），前端连点两次按钮就会渲染两遍；
  - 渲染结果文件名由「项目名 + 步骤」决定，重复渲染只会覆盖同一个文件，
    用户看到的仍是 1 个文件，但 CPU/IO 白做，且并发覆盖时可能读到半个文件；
  - 幂等键（用户 + 会话 + 目标 + 格式 + 排版指纹）让第二次起直接复用文件，
    响应里带 reused=True，前端可以据此提示「已复用上次导出的文件」。

实现边界（诚实说明）：
  - 进程内内存字典，非分布式；多 worker 部署时每个 worker 各自一份，仍能挡住单 worker 的重复点击；
  - 只在文件仍然存在、且内容指纹一致时才算命中，内容改了就重新渲染。
"""
import hashlib
import os
import threading
import time
from typing import Any, Dict, Optional, Tuple

DEFAULT_TTL_SECONDS = 600
MAX_ENTRIES = 200

# key -> {"path": str, "content_hash": str, "created_at": float, "hits": int}
_entries: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
_lock = threading.Lock()
_stats = {"hit": 0, "miss": 0, "expired": 0, "evicted": 0, "stored": 0}


def content_fingerprint(content: str) -> str:
    """内容指纹：内容变了就必须重新渲染。"""
    return hashlib.sha1((content or "").encode("utf-8")).hexdigest()


def style_fingerprint(style) -> str:
    """排版指纹：只取影响产出的字段，顺序固定，避免 dict 序不同导致哈希漂移。"""
    attrs = (
        "font_name", "title_font_size", "heading1_font_size", "heading2_font_size",
        "body_font_size", "table_font_size", "line_spacing", "paragraph_spacing",
        "page_margin_top", "page_margin_bottom", "page_margin_left", "page_margin_right",
        "title_color", "heading_color", "table_header_color", "table_header_bg",
    )
    parts = []
    for name in attrs:
        value = getattr(style, name, None)
        if isinstance(value, (tuple, list)):
            value = ",".join(str(v) for v in value)
        parts.append(f"{name}={value}")
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def cache_key(user_id: Any, session_id: str, target: str, fmt: str, style_hash: str) -> Tuple[Any, ...]:
    """幂等键：用户 + 会话 + 导出目标（整份报告 / 某一步）+ 格式 + 排版指纹。"""
    return (str(user_id), str(session_id or ""), str(target or ""),
            str(fmt or "").lower(), str(style_hash or ""))


def _prune_locked(now: float, ttl: int) -> None:
    expired = [k for k, v in _entries.items() if now - v["created_at"] > ttl]
    for key in expired:
        _entries.pop(key, None)
        _stats["expired"] += 1
    while len(_entries) > MAX_ENTRIES:
        oldest = min(_entries, key=lambda k: _entries[k]["created_at"])
        _entries.pop(oldest, None)
        _stats["evicted"] += 1


def lookup(key: Tuple[Any, ...], content_hash: str, ttl: int = DEFAULT_TTL_SECONDS,
           now: Optional[float] = None) -> Optional[str]:
    """命中返回可复用的文件路径，未命中返回 None。

    三个条件同时满足才算命中：未超时、内容指纹一致、文件还在（可能被清理任务删掉）。
    """
    now = time.time() if now is None else now
    with _lock:
        _prune_locked(now, ttl)
        entry = _entries.get(key)
        if entry is None:
            _stats["miss"] += 1
            return None
        if entry["content_hash"] != content_hash or not os.path.exists(entry["path"]):
            _entries.pop(key, None)
            _stats["miss"] += 1
            return None
        entry["hits"] += 1
        _stats["hit"] += 1
        return entry["path"]


def store(key: Tuple[Any, ...], content_hash: str, path: str,
          now: Optional[float] = None, ttl: int = DEFAULT_TTL_SECONDS) -> None:
    now = time.time() if now is None else now
    with _lock:
        _prune_locked(now, ttl)
        _entries[key] = {
            "path": path,
            "content_hash": content_hash,
            "created_at": now,
            "hits": 0,
        }
        _stats["stored"] += 1


def clear() -> None:
    with _lock:
        _entries.clear()


def stats() -> Dict[str, int]:
    with _lock:
        return dict(_stats, entries=len(_entries))


def reset_stats() -> None:
    with _lock:
        for key in _stats:
            _stats[key] = 0
