"""时间口径工具（统一 UTC 存储 / UTC 输出 / 北京时区算「今天」）。

背景
----
模型层所有时间列都是 `default=datetime.utcnow` —— 存进 MySQL `DATETIME` 的是
**不带时区的 UTC**。但序列化时直接 `dt.isoformat()` 出来的字符串形如
`2026-11-13T11:00:00`，没有时区后缀。浏览器 `new Date()` 按 ES 规范会把
「无偏移的 date-time」当成**本地时间**解析，于是管理端每个时间都比真实时间早 8 小时，
而且不报错、没人发现。

这个模块提供三件事，避免各处再各写一遍：

1. `utcnow()`       —— 写入数据库时统一用它（naive UTC，与列类型口径一致）
2. `utc_iso(dt)`    —— 输出给前端时统一用它，永远带 `Z`，浏览器不会再猜错
3. `local_day_start_utc(days_ago)` —— 「今天/本周/近30天」的边界按北京时间算，
   再换算成 naive UTC 去和库里的值比大小。原来直接用 UTC 零点，等于把北京时间
   早上 8 点当成一天的开始。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

try:  # Python 3.9+
    from zoneinfo import ZoneInfo

    LOCAL_TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover - 极端环境没有 tzdata 时退化为固定偏移
    LOCAL_TZ = timezone(timedelta(hours=8))

UTC = timezone.utc


def utcnow() -> datetime:
    """当前时间（naive UTC），与数据库 DATETIME 列的存储口径一致。"""
    return datetime.now(UTC).replace(tzinfo=None)


def attach_utc(dt: datetime) -> datetime:
    """把库里读出来的 naive datetime 标注成 UTC（不改数值）。"""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def utc_iso(dt: Optional[datetime]) -> Optional[str]:
    """序列化为带 `Z` 的 ISO 8601 串；None 原样返回。

    带 `Z` 之后 `new Date()` 不再按本地时间猜，前端也不会再差 8 小时。
    已经是 aware 的值先归一到 UTC 再输出，避免同一份数据出现两种偏移写法。
    """
    if dt is None:
        return None
    return attach_utc(dt).astimezone(UTC).isoformat().replace("+00:00", "Z")


def local_now() -> datetime:
    """当前北京时间（aware）。"""
    return datetime.now(LOCAL_TZ)


def local_day_start_utc(days_ago: int = 0) -> datetime:
    """北京时间「今天 0 点」（可回退 N 天），换算成 naive UTC 用于查询。"""
    start = local_now().replace(hour=0, minute=0, second=0, microsecond=0)
    if days_ago:
        start = start - timedelta(days=days_ago)
    return start.astimezone(UTC).replace(tzinfo=None)
