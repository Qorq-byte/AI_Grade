"""跨域共享的会话级状态与校验（批次 33 拆分起，批次 34 扩到工作流状态）。

为什么要有这个文件：`agent_routes.py` 里有些东西是**多个域共用**的 —— 会话 ID 校验、
问卷内存缓存、问卷读取口径、运行中的工作流与进度。把端点搬走时，如果新模块反过来
从 `agent_routes` 按名字导入，就形成了"子域依赖大文件"；
而如果 `agent_routes` 再 import 子域，就是循环导入。

这里刻意只放**可变状态与纯校验**：函数/类可以按名字导入，**dict/list/set 必须按模块访问**
（`_shared_state.active_workflows`）。按名字导入绑定的是导入那一刻的对象，
测试 `monkeypatch.setattr(_shared_state, ...)` 重绑后，读方还拿着旧 dict ——
批次 33 的表现是"缓存里有数据、接口却 404"，批次 34 把这条写进了验收用例。
"""
from __future__ import annotations

import re
from typing import Dict

from fastapi import HTTPException

from backend.agents.performance_agent import PerformanceAgent
from backend.services import progress_bus


def _validate_session_id(session_id: str) -> None:
    """会话 ID 的格式校验（路径安全 + 防注入）。"""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", session_id):
        raise HTTPException(status_code=400, detail="无效的会话 ID")


#: 问卷内存缓存（用于 list 端点和跨请求访问）。
#: 进程内缓存，重启后从磁盘备份恢复 —— 所以它是"加速器"而不是"唯一数据源"。
_QUESTIONNAIRE_CACHE: Dict[str, Dict] = {}

#: 运行中的工作流实例。进程内状态，多 worker 各存各的（见 C2）。
active_workflows: Dict[str, PerformanceAgent] = {}

#: 每个 session 的初始化参数，用于 approve 后重建 / 内存 evict 后恢复。
active_workflow_configs: Dict[str, dict] = {}

#: 工作流进度（与 `progress_bus.PROGRESS` 是同一份数据，代理每次操作重新解析）。
_WORKFLOW_PROGRESS = progress_bus.PROXY


def _published_questionnaire(session_id: str) -> dict:
    """取已发布问卷：先看内存缓存，再看磁盘备份；都没有则 404。"""
    _validate_session_id(session_id)
    data = _QUESTIONNAIRE_CACHE.get(session_id) or (
        PerformanceAgent.load_state_backup(session_id).get("questionnaire") or {}
    )
    if not data.get("questions"):
        raise HTTPException(status_code=404, detail="问卷不存在或尚未生成")
    return data
