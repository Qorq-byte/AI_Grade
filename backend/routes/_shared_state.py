"""跨域共享的会话级状态与校验（批次 33 拆分的第一个抽口）。

为什么要有这个文件：`agent_routes.py` 里有些东西是**多个域共用**的 ——
会话 ID 校验、问卷内存缓存、问卷读取口径。把问卷端点搬走时，如果新模块反过来
`from backend.routes.agent_routes import ...`，就形成了"子域依赖大文件"；
而如果 `agent_routes` 再 import 子域，就是循环导入。

所以把这些**不依赖任何路由模块**的小东西放在这里：
`agent_routes` 和 `questionnaire_routes` 都从这里拿，谁也不用 import 谁。

刻意只放"纯状态/纯校验"：真正的业务依赖（`active_workflows`、`_require_*`）
仍留在 `agent_routes`，等下一批再抽。
"""
from __future__ import annotations

import re
from typing import Dict

from fastapi import HTTPException

from backend.agents.performance_agent import PerformanceAgent


def _validate_session_id(session_id: str) -> None:
    """会话 ID 的格式校验（路径安全 + 防注入）。"""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", session_id):
        raise HTTPException(status_code=400, detail="无效的会话 ID")


#: 问卷内存缓存（用于 list 端点和跨请求访问）。
#: 进程内缓存，重启后从磁盘备份恢复 —— 所以它是"加速器"而不是"唯一数据源"。
_QUESTIONNAIRE_CACHE: Dict[str, Dict] = {}

