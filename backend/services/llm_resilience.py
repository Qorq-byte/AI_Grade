"""LLM 调用的重试与降级（批次 37）。

为什么单独抽出来：这套策略本来只活在 `PerformanceAgent._invoke_llm` 里（批次 31/32），
路由层（refine）要用就得 new 一个 agent —— 那会连库、建记忆会话。这里把它做成
**不依赖 state 的纯函数**：调用方给「模型链」和「怎么调一次」，其余（重试、降级、
留痕）由本模块按同一套规则处理。

规则与 agent 层保持一致（刻意复制文字，不改口径）：
- 只对 transient（429/超时/5xx/连接）换模型；400/401/403/404 认死理，直接抛；
- 换模型前**先重试同一模型**一次：429 常常 1–2 秒就好，换模型既改文风又更贵；
- 链最多 3 个（主 + 2 备选）：再降级，结果已经不可信；
- 每次尝试都记（成功也记），降级成功写一条 warning。

**已知取舍**：agent 层的 `_invoke_llm` 还没有切到本模块（那是最热的 14 步路径，
改它要单独一批 + 全量回归）。两份实现暂时并存，判定标准与文案在此对齐；
下次动 agent 时应把它切过来，只留一份。
"""
from __future__ import annotations

import os
import time
from typing import Callable, Dict, List, Optional, Sequence

#: 一次调用最多尝试多久（秒）由调用方控制；这里只管重试次数与退避。
MAX_CHAIN = 3
MAX_ATTEMPTS_KEPT = 200


class LLMInvocationError(RuntimeError):
    """调用失败。`kind` 用于决定 HTTP 状态：permanent → 502，transient → 503。"""

    def __init__(self, message: str, *, kind: str = "transient",
                 attempts: Optional[List[Dict]] = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.attempts = attempts or []


def model_chain(primary: str, configured, limit: int = MAX_CHAIN) -> List[str]:
    """主模型 + 备选链。`configured` 可以是 list，也可以是逗号分隔的字符串。"""
    chain: List[str] = []
    if primary:
        chain.append(primary)
    if isinstance(configured, (list, tuple)):
        items = configured
    else:
        items = str(configured or "").split(",")
    for item in items:
        name = str(item).strip()
        if name and name not in chain:
            chain.append(name)
    return chain[:limit]


def fallback_models_from(values: dict) -> List[str]:
    """从会话状态取备选链：`system_config.fallback_models` 优先，其次环境变量。"""
    raw = (values.get("system_config") or {}).get("fallback_models")
    if not raw:
        raw = os.getenv("LLM_FALLBACK_MODELS", "")
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw if str(item).strip()]
    return [item.strip() for item in str(raw).split(",") if item.strip()]


def invoke_with_fallback(*, chain: Sequence[str], call: Callable[[str], str], step: int,
                         attempts: List[Dict], warnings: Optional[List[str]] = None,
                         retries: Optional[int] = None,
                         sleep: Callable[[float], None] = time.sleep) -> str:
    """沿模型链调用，返回第一个成功的结果；全失败抛 `LLMInvocationError`。

    - `call(model)` 只负责「用这个模型调一次」，异常原样抛；
    - `attempts` 由调用方持有（便于预览不落盘、采用时再落盘）；
    - `warnings` 传入时，降级成功会追加一条提示（与 agent 层同一句话）。
    """
    from backend.agents.performance_agent import (  # 延迟导入：避免服务层反向拉起 agent
        LLM_RETRY_ATTEMPTS, classify_llm_error, retry_backoff,
    )

    retries = LLM_RETRY_ATTEMPTS if retries is None else retries
    if not chain:
        raise LLMInvocationError("没有可用模型：主模型为空且未配置备选", kind="permanent",
                                 attempts=attempts)
    last_error: Optional[BaseException] = None
    attempt_no = 0

    for index, model in enumerate(chain):
        for retry_index in range(1 + retries):
            attempt_no += 1
            started = time.perf_counter()
            try:
                content = call(model)
                attempts.append({
                    "step": int(step), "model": model, "attempt": attempt_no,
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                    "outcome": "ok", "detail": "",
                })
                if index and warnings is not None:
                    hint = (f"步骤 {step} 主模型 {chain[0]} 调用失败，"
                            f"已由备选模型 {model} 完成；"
                            f"该步文字风格与判断可能与其他步骤不一致")
                    if hint not in warnings:
                        warnings.append(hint)
                return content
            except Exception as exc:  # noqa: BLE001
                kind = classify_llm_error(exc)
                attempts.append({
                    "step": int(step), "model": model, "attempt": attempt_no,
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                    "outcome": "error", "detail": f"{kind}: {type(exc).__name__}",
                })
                last_error = exc
                if kind == "permanent":
                    raise LLMInvocationError(
                        f"模型 {model} 拒绝了这次请求：{exc}", kind="permanent",
                        attempts=attempts) from exc
                if retry_index < retries:
                    delay = retry_backoff(retry_index + 1)
                    if delay:
                        sleep(delay)
                    continue
                break

    raise LLMInvocationError(
        f"所有模型都调用失败（共 {attempt_no} 次）：{last_error}", kind="transient",
        attempts=attempts) from last_error


def merge_attempts(existing, new: List[Dict], limit: int = MAX_ATTEMPTS_KEPT) -> List[Dict]:
    """把本次尝试并进已有台账，只留最近 `limit` 条（与 agent 层一致）。"""
    merged = list(existing or []) + list(new or [])
    return merged[-limit:]
