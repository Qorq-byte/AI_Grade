"""状态备份的编码层（批次 25 · C3 写放大治理）

`state_backups/<session>.json` 是工作流的磁盘兜底：每完成一步就把关键状态写一遍。
实测（36 个真实会话）有三个毛病：

1. **同一段正文存了两份**：`_record_step_result` 同时写 `content` 和
   `raw_outputs["default"]`，两者逐字相同；`raw_outputs` 占 step_results 的 47%，
   而全项目没有任何一处读它。
2. **步骤正文与独立状态字段重复**：步骤 8 的 `content` 与 `experience_summary`
   本来就是同一个字符串，步骤 14 的 `content` 与 `final_report` 同理。
   这类重复约 5371 KB，占 step_results 的 37%。
3. `json.dump(..., indent=2)`：纯排版开销。

合起来：`step_results` 占备份体积 64%，其中 83% 是可证明的重复。

本模块只做两件互逆的事：

- `compact(backup)`：写盘前把可证明的重复换成引用（`content_ref` + 200 字预览）。
  只在**逐字相同**时替换，不做模糊匹配 —— 一旦不等就原样保留，保证无损。
- `hydrate(backup)`：读盘后把引用还原成 `content`。

读侧一律走 `hydrate`，所以 `load_state_backup()` 的返回值形状与改造前完全一致，
routes 里的 `backup["step_results"][n]["content"]` 一个都不用改。写侧一律走
`compact`，于是「读 → 改 → 写」这个循环天然幂等：hydrate 填回来的 content 会在
下一次 compact 时重新被判成重复。

保底：`content_ref` 指向的字段可能为空（比如用户把报告清空了），这时退回
`content_preview`。预览只在引用链断了的时候才会被读到，正常路径永远读全文。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

#: 写进引用的预览长度。正常路径读不到它，只在引用字段丢失时兜底。
CONTENT_PREVIEW_CHARS = 200

#: 引用键：值为「同内容的独立状态字段名」
CONTENT_REF_KEY = "content_ref"

#: 预览键
PREVIEW_KEY = "content_preview"

#: 步骤号 -> 承载同一段正文的独立状态字段。
#: 只收录实测逐字相同的步骤；步骤 3/4/5 的对应字段是 dict（指标体系/得分/评分标准），
#: 与正文永远不相等，留着让「不相等就原样保留」这条规则处理。
STEP_CONTENT_FIELDS: Dict[int, str] = {
    1: "cost_list",
    2: "effective_data",
    6: "evaluation_table",
    7: "indicator_analysis",
    8: "experience_summary",
    9: "problems_analysis",
    10: "suggestions",
    11: "comprehensive_evaluation",
    12: "basic_info",
    13: "work_introduction",
    14: "final_report",
}


def _step_number(key: Any, item: Dict[str, Any]) -> Optional[int]:
    """从步骤项里取出步骤号；取不到就返回 None（调用方按「不做引用替换」处理）。"""
    number = item.get("step_number")
    if isinstance(number, int):
        return number
    try:
        return int(key)
    except (TypeError, ValueError):
        return None


def _compact_step(key: Any, item: Any, backup: Dict[str, Any]) -> Any:
    if not isinstance(item, dict):
        return item
    out = dict(item)
    content = out.get("content")
    if not isinstance(content, str) or not content:
        return out

    # 1) raw_outputs 与 content 逐字相同时丢掉：content 就在同一个对象里，
    #    留着只是把正文再写一遍。多模型对比时 raw_outputs 会有别的键，那时不动它。
    if out.get("raw_outputs") == {"default": content}:
        out.pop("raw_outputs")

    # 2) content 与独立字段逐字相同时改成引用。必须逐字相等才换 —— 只要有一个
    #    字符不同就保留原文，无损是这里的硬约束。
    field = STEP_CONTENT_FIELDS.get(_step_number(key, out) or 0)
    if field and backup.get(field) == content:
        out.pop("content")
        out[CONTENT_REF_KEY] = field
        out[PREVIEW_KEY] = content[:CONTENT_PREVIEW_CHARS]
    return out


def compact(backup: Dict[str, Any]) -> Dict[str, Any]:
    """返回可写盘的紧凑版本（不修改入参）。"""
    steps = backup.get("step_results")
    if not isinstance(steps, dict):
        return dict(backup)
    out = dict(backup)
    out["step_results"] = {key: _compact_step(key, item, backup) for key, item in steps.items()}
    return out


def _hydrate_step(key: Any, item: Any, backup: Dict[str, Any]) -> Any:
    if not isinstance(item, dict):
        return item
    out = dict(item)
    content = out.get("content")
    if not isinstance(content, str) or not content:
        ref = out.get(CONTENT_REF_KEY)
        value = backup.get(ref) if isinstance(ref, str) else None
        if isinstance(value, str) and value:
            out["content"] = value
        else:
            preview = out.get(PREVIEW_KEY)
            if isinstance(preview, str):
                out["content"] = preview
    # 引用与预览是编码细节：还原之后就不该再出现在调用方看到的形状里，
    # 否则「读 → 改 → 写」会把它们当成业务字段一路传下去。
    out.pop(CONTENT_REF_KEY, None)
    out.pop(PREVIEW_KEY, None)
    if "raw_outputs" not in out and isinstance(out.get("content"), str):
        out["raw_outputs"] = {"default": out["content"]}
    return out


def hydrate(backup: Dict[str, Any]) -> Dict[str, Any]:
    """把引用还原成 `content`，返回形状与改造前的备份一致（不修改入参）。"""
    steps = backup.get("step_results")
    if not isinstance(steps, dict):
        return dict(backup)
    out = dict(backup)
    out["step_results"] = {key: _hydrate_step(key, item, backup) for key, item in steps.items()}
    return out


def dump(backup: Dict[str, Any], target) -> None:
    """把备份紧凑地写进已打开的文件对象。"""
    # separators 去掉 json.dump 默认的 ", " 与 ": "，纯排版节省
    json.dump(compact(backup), target, ensure_ascii=False, separators=(",", ":"))


def dumps(backup: Dict[str, Any]) -> str:
    return json.dumps(compact(backup), ensure_ascii=False, separators=(",", ":"))


def redundancy(backup: Dict[str, Any]) -> Dict[str, Any]:
    """量化一份备份里还剩多少可压缩的重复，供探针与回归用例断言。

    返回：
    - `raw_outputs_bytes`：`raw_outputs` 与 `content` 逐字相同时的体积
    - `content_ref_bytes`：`content` 与独立字段逐字相同时的体积
    - `naive_bytes` / `compacted_bytes`：老写法（全量 + indent=2）与新写法的字节数
    """
    steps = backup.get("step_results") or {}
    raw_dup = 0
    ref_dup = 0
    for key, item in steps.items():
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, str) or not content:
            continue
        size = len(content.encode("utf-8"))
        if item.get("raw_outputs") == {"default": content}:
            raw_dup += size
        field = STEP_CONTENT_FIELDS.get(_step_number(key, item) or 0)
        if field and backup.get(field) == content:
            ref_dup += size
    naive = len(json.dumps(backup, ensure_ascii=False, indent=2).encode("utf-8"))
    compacted = len(dumps(backup).encode("utf-8"))
    return {
        "raw_outputs_bytes": raw_dup,
        "content_ref_bytes": ref_dup,
        "observable_duplicate_bytes": raw_dup + ref_dup,
        "naive_bytes": naive,
        "compacted_bytes": compacted,
        "ratio": (compacted / naive) if naive else 1.0,
    }
