"""相对时间口径 + 时区归一（Dashboard / 笔记列表 / 日历共用）。

**为什么需要时区归一**：vault 里的 ISO 时间戳offset不统一 —— 有的写
`+08:00`（手写 frontmatter），有的是 MCP 服务器写的 `+00:00`（UTC）。
`datetime.fromisoformat(s).strftime("%H:%M")` / `.date()` 直接取的是**字符串里那个
时区的钟点**，于是同一个「16:52 本地时间」在两份文件里会显示成 `16:52` 与 `08:52`
（实测：`记忆索引.md` 的 updated 是 `2026-10-15T08:52:03+00:00`，日历下钻显示 08:52，
而它真实发生在本地 16:52）。日期分桶同理：本地 10-15 04:00 的文件在 UTC 里是
10-14 20:00，会被错分到前一天。

所以凡是「显示时刻」或「按天分桶」的地方，一律先过 `to_local()`。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional


def parse_iso(iso: str) -> Optional[datetime]:
    """解析 ISO 8601；无时区的串按本地时区理解（`+00:00` 这类显式时区照它自己算）。"""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt


def to_local(iso: str) -> Optional[datetime]:
    """ISO → 本地时区的 aware datetime（解析不了返回 None）。"""
    dt = parse_iso(iso)
    return None if dt is None else dt.astimezone()


def local_date(iso: str) -> Optional[date]:
    """ISO → **本地日历日**。日历按天分桶必须用它，不能用 `iso[:10]`。"""
    dt = to_local(iso)
    return None if dt is None else dt.date()


def local_hhmm(iso: str) -> str:
    """ISO → 本地 `HH:MM`。

    只有日期没有时刻的串（如 `created: 2026-10-15`）返回**空串**：硬按 `00:00` 显示
    会让人读成「凌晨刚建」，而事实是「那天建的、没记几点」（实测主人看到 00:00 就在问
    「这个时间是不是也有问题」）。空串由前端渲染成占位符。
    解析不了的串退化为原串切片（保持旧行为，不抛）。
    """
    if not iso:
        return ""
    date_only = ("T" not in iso) and (" " not in iso)
    if date_only:
        return ""
    dt = to_local(iso)
    if dt is not None:
        return dt.strftime("%H:%M")
    return iso[11:16] if len(iso) >= 16 else ""


def relative_time_label(iso: str) -> str:
    """把 ISO 时间换算为「刚刚 / N 分钟前 / N 小时前 / 昨天 / N 天前」口径。"""
    dt = to_local(iso)
    if dt is None:
        return ""
    now = datetime.now().astimezone()
    secs = (now - dt).total_seconds()
    if secs < 0:
        secs = 0
    if secs < 60:
        return "刚刚"
    if secs < 3600:
        return f"{int(secs // 60)} 分钟前"
    if secs < 86400:
        return f"{int(secs // 3600)} 小时前"
    days = (now.date() - dt.date()).days
    if days <= 1:
        return "昨天"
    return f"{days} 天前"
