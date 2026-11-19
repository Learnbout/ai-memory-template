"""Dashboard 路由（§5.8）+ 新增日历（§9.11 → §16 语义改为「哪天加了哪些文件」）。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from .. import dashboard as dashboard_mod
from ..schemas import CalendarDayData, CalendarMonthData, DashboardData

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard", response_model=DashboardData)
def get_dashboard() -> DashboardData:
    return DashboardData(**dashboard_mod.get_dashboard())


@router.get("/calendar", response_model=CalendarMonthData)
def get_calendar(
    year: Optional[int] = Query(default=None, description="默认当年"),
    month: Optional[int] = Query(default=None, description="1-12，默认当月"),
) -> CalendarMonthData:
    """月视图新增热度：每天「新增文件数」（frontmatter created 落该日）+「仅更新数」。"""
    now = datetime.now()
    y = now.year if year is None else int(year)
    m = now.month if month is None else int(month)
    if not 1 <= m <= 12:
        raise HTTPException(status_code=400, detail=f"月份不合法（应为 1-12）：{m}")
    if not 1970 <= y <= 9999:
        raise HTTPException(status_code=400, detail=f"年份不合法：{y}")
    return CalendarMonthData(**dashboard_mod.activity_calendar(y, m))


@router.get("/calendar/day/{day_date}", response_model=CalendarDayData)
def get_calendar_day(day_date: str) -> CalendarDayData:
    """单日下钻：当天新增的文件 / 当天仅被更新的文件 / 当天工具调用次数。"""
    data = dashboard_mod.calendar_day(day_date)
    if data is None:
        raise HTTPException(status_code=400, detail=f"日期格式应为 YYYY-MM-DD：{day_date}")
    return CalendarDayData(**data)
