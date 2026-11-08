"""
进程内进度总线（progress bus）
================================
职责：
1. 承载 LLM token 级增量（token_delta），供 SSE 生成器实时取走；
2. 保存最近 N 条已发送的 SSE 帧，支持断线后按 Last-Event-ID 补发；
3. 提供「当前是否有流消费者」的判定，避免无人收听时做无用功。

设计约束：
- agent 层只依赖本模块，不再反向 import routes（解除循环依赖）；
- 纯进程内实现，单 worker 语义；多 worker 需替换为 Redis 发布订阅（接口保持不变）；
- 所有操作加锁，允许 executor 线程写入、事件循环线程读取。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque, Dict, List, Optional, Tuple

# 每个会话最多缓存的增量字符数与 SSE 帧数
MAX_DELTA_CHARS = 200_000
MAX_FRAMES = 400
# 增量在队列里停留超过该秒数仍未取走则丢弃，避免慢消费者拖垮内存
DELTA_TTL_SECONDS = 120.0

_lock = threading.RLock()
_deltas: Dict[str, Deque[Tuple[float, int, str]]] = {}
_frames: Dict[str, Deque[Tuple[int, str]]] = {}
_seq: Dict[str, int] = {}
_active_streams: Dict[str, int] = {}

# 工作流进度字典（与 routes 内的 _WORKFLOW_PROGRESS 共享同一份数据）
# 迁移后 agent 与 routes 都只依赖本模块，不再互相 import
PROGRESS: Dict[str, dict] = {}


class ProgressProxy:
    """转发到模块级 PROGRESS 的只读视图。

    为什么需要代理：调用方如果在模块级缓存 `PROGRESS` 引用，模块被 reload 后
    会指向旧字典，导致两侧看到不同数据。代理每次操作都重新解析 PROGRESS，
    因此始终指向当前实例。
    """

    def __getitem__(self, key):
        return PROGRESS[key]

    def __setitem__(self, key, value):
        PROGRESS[key] = value

    def __delitem__(self, key):
        del PROGRESS[key]

    def __contains__(self, key):
        return key in PROGRESS

    def __iter__(self):
        return iter(PROGRESS)

    def __len__(self):
        return len(PROGRESS)

    def get(self, key, default=None):
        return PROGRESS.get(key, default)

    def pop(self, key, default=None):
        return PROGRESS.pop(key, default)

    def setdefault(self, key, default):
        return PROGRESS.setdefault(key, default)

    def keys(self):
        return PROGRESS.keys()

    def values(self):
        return PROGRESS.values()

    def items(self):
        return PROGRESS.items()


PROXY = ProgressProxy()


def set_progress(session_id: str, value: dict) -> dict:
    with _lock:
        PROGRESS[session_id] = value
        return value


def get_progress(session_id: str) -> Optional[dict]:
    with _lock:
        return PROGRESS.get(session_id)


def pop_progress(session_id: str) -> Optional[dict]:
    with _lock:
        return PROGRESS.pop(session_id, None)


def begin(session_id: str) -> int:
    """开始一次新的流：清空增量与帧，返回起始序号。"""
    with _lock:
        _deltas[session_id] = deque()
        _frames[session_id] = deque(maxlen=MAX_FRAMES)
        _seq[session_id] = 0
        _active_streams[session_id] = 0
        return 0


def next_seq(session_id: str) -> int:
    """分配下一个帧序号（从 1 开始）。"""
    with _lock:
        current = _seq.get(session_id, 0) + 1
        _seq[session_id] = current
        return current


def record_frame(session_id: str, seq: int, frame: str) -> None:
    """记录已发送的帧，用于断线补发。"""
    with _lock:
        buf = _frames.get(session_id)
        if buf is None:
            buf = deque(maxlen=MAX_FRAMES)
            _frames[session_id] = buf
        buf.append((seq, frame))


def frames_since(session_id: str, last_event_id: int) -> List[Tuple[int, str]]:
    """返回序号大于 last_event_id 的帧。"""
    with _lock:
        buf = _frames.get(session_id) or ()
        return [(seq, frame) for seq, frame in buf if seq > last_event_id]


def last_seq(session_id: str) -> int:
    with _lock:
        return _seq.get(session_id, 0)


def push_delta(session_id: str, step_number: int, text: str) -> None:
    """写入一段 token 增量。没有活跃消费者时直接丢弃，避免做无用功。"""
    if not text:
        return
    with _lock:
        if _active_streams.get(session_id, 0) <= 0:
            return
        buf = _deltas.setdefault(session_id, deque())
        buf.append((time.monotonic(), int(step_number or 0), text))
        # 控制总字符量
        total = sum(len(item[2]) for item in buf)
        while buf and total > MAX_DELTA_CHARS:
            _, _, dropped = buf.popleft()
            total -= len(dropped)


def drain_deltas(session_id: str) -> List[Tuple[int, str]]:
    """取走并清空待发送增量，返回 [(step_number, text)]。"""
    now = time.monotonic()
    with _lock:
        buf = _deltas.get(session_id)
        if not buf:
            return []
        out: List[Tuple[int, str]] = []
        while buf:
            ts, step, text = buf.popleft()
            if now - ts > DELTA_TTL_SECONDS:
                continue
            out.append((step, text))
        return out


def attach_consumer(session_id: str) -> int:
    """登记一个流消费者（SSE 连接），返回当前消费者数量。"""
    with _lock:
        _active_streams[session_id] = _active_streams.get(session_id, 0) + 1
        return _active_streams[session_id]


def detach_consumer(session_id: str) -> int:
    with _lock:
        count = max(_active_streams.get(session_id, 1) - 1, 0)
        _active_streams[session_id] = count
        return count


def has_consumer(session_id: str) -> bool:
    with _lock:
        return _active_streams.get(session_id, 0) > 0


def clear(session_id: str) -> None:
    with _lock:
        _deltas.pop(session_id, None)
        _frames.pop(session_id, None)
        _seq.pop(session_id, None)
        _active_streams.pop(session_id, None)
