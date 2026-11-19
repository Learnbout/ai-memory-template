"""Dashboard 聚合数据（CONTRACT §5.8 结构 + §9.8 全部取真值）。

全部字段从真实 Vault / runs / mcp.json 派生，不再有示例定值；`stats[].sample` 一律 false。
"""

from __future__ import annotations

import getpass
import os
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from . import runs as runs_store
from . import tool_catalog
from . import vault
from .timeutil import local_date, local_hhmm, relative_time_label


def _local_user_name() -> str:
    """本地版「当前用户」显示名。

    为什么要这个函数：这里原来硬编码了一个真实姓名 —— 那是**作者自己**的机器账户，
    一份要开源给人用的代码里钉着某个人的名字，既泄露隐私又对别人毫无意义。
    现在的口径：`GRAPHITE_LOCAL_USER` 环境变量 > 操作系统当前用户名 > 「本地用户」。
    中心化版不读这个值（用户身份走登录态），只有本地版（零登录）用它填壳里的账户位。
    """
    name = (os.environ.get("GRAPHITE_LOCAL_USER") or "").strip()
    if name:
        return name
    try:
        return getpass.getuser() or "本地用户"
    except Exception:  # noqa: BLE001 —— 取不到用户名不该让 Dashboard 崩
        return "本地用户"


# 统计卡迷你折线图（sparkline）的窗口天数
TREND_DAYS = 14


# --------------------------------------------------------------------------- #
# 派生工具
# --------------------------------------------------------------------------- #
def _fmt_elapsed(elapsed_ms: int, status: str) -> str:
    """186 → "186ms"；1200 → "1.2s"；运行中 → "—"。"""
    if status == "运行中":
        return "—"
    if elapsed_ms >= 1000:
        return f"{elapsed_ms / 1000:.1f}s"
    return f"{elapsed_ms}ms"


def _updated_date(iso: str) -> Optional[date]:
    """ISO → **本地**日历日。

    不能用 `datetime.fromisoformat(iso).date()`：那取的是字符串自带时区的那一天。
    vault 里的 offset 不统一（`+08:00` 与 `+00:00` 混着写），按 UTC 分桶会把本地
    凌晨的文件错分到前一天。归一逻辑在 timeutil.to_local()。
    """
    return local_date(iso)


def _recent_notes(recs: List[Dict[str, Any]], limit: int = 5) -> List[Dict[str, Any]]:
    # 剔除自动生成页（记忆索引/路由索引/脚本索引）：sync 每次写入都会重建它们，
    # mtime 与 frontmatter updated 永远最新，不剔的话「最近笔记」榜首永远被索引页霸占，
    # 看起来像列表没在更新（主人 2026-10-16 反馈）。口径与活动日历的 is_generated 过滤一致：
    # 它们是生成物，不算「谁往库里加了/改了什么」。
    ordered = sorted(
        (r for r in recs if not vault.is_generated(r)),
        key=lambda r: (r["updatedAt"], r["id"]),
        reverse=True,
    )
    out: List[Dict[str, Any]] = []
    for r in ordered[:limit]:
        first_tag = r["tags"][0] if r["tags"] else ""
        out.append(
            {
                "id": r["id"],
                "title": r["title"],
                "relPath": r["relPath"],
                "timeLabel": r["updatedLabel"] or relative_time_label(r["updatedAt"]),
                "tag": f"#{first_tag}" if first_tag else "",
            }
        )
    return out


def _recent_calls(limit: int = 6) -> Dict[str, Any]:
    page = runs_store.list_runs(rtype="MCP 调用", page=1, page_size=limit)
    items: List[Dict[str, Any]] = []
    for r in page["items"]:
        status = r.get("status", "")
        items.append(
            {
                "time": r.get("timeLabel", ""),
                "tool": r.get("target", ""),
                "caller": r.get("caller", "手动"),
                "status": status,
                "elapsed": _fmt_elapsed(int(r.get("elapsedMs", 0)), status),
            }
        )
    return {"note": "真实记录 · 来自运行历史（/api/runs）", "items": items}


def _checkboxes(recs: List[Dict[str, Any]]) -> Tuple[int, int]:
    done = sum(len(r["done_items"]) for r in recs)
    open_ = sum(len(r["open_items"]) for r in recs)
    return done, open_


def _trend_from_recs(
    recs: List[Dict[str, Any]],
    weight: Any,
    days: int = TREND_DAYS,
) -> List[int]:
    """近 days 天（含今天）按 `updatedAt` 分桶的日度序列。

    `weight(rec) -> int` 决定该笔记当天贡献多少；返回索引 0 = 最旧一天，末位 = 今天。
    全部取自真实 Vault 数据，不做平滑/补值。
    """
    today = date.today()
    buckets = [0] * days
    for r in recs:
        d = _updated_date(r.get("updatedAt", ""))
        if d is None:
            continue
        delta = (today - d).days
        if 0 <= delta < days:
            w = weight(r)
            if w:
                buckets[days - 1 - delta] += w
    return buckets


def _trend_from_runs(rtype: str = "MCP 调用", days: int = TREND_DAYS) -> List[int]:
    """近 days 天每日运行记录条数（取自真实 runs 存储）。"""
    today = date.today()
    buckets = [0] * days
    try:
        page = runs_store.list_runs(rtype=rtype, page=1, page_size=2000)
    except Exception:  # noqa: BLE001 - 趋势是装饰性数据，取不到就退化成全 0，不影响主数据
        return buckets
    for r in page.get("items", []):
        d = _updated_date(r.get("at", ""))
        if d is None:
            continue
        delta = (today - d).days
        if 0 <= delta < days:
            buckets[days - 1 - delta] += 1
    return buckets


def _todos(recs: List[Dict[str, Any]], limit: int = 6) -> Dict[str, Any]:
    ordered = sorted(recs, key=lambda r: (r["updatedAt"], r["id"]), reverse=True)
    items: List[Dict[str, Any]] = []
    total = 0
    for r in ordered:
        for txt in r["open_items"]:
            total += 1
            if len(items) < limit:
                items.append(
                    {
                        # 携带来源笔记 id / 路径：前端据此把待办点进对应笔记
                        "id": r["id"],
                        "relPath": r["relPath"],
                        "title": txt,
                        "source": f"来自《{r['title']}》",
                        "timeLabel": r["updatedLabel"],
                        "done": False,
                    }
                )
    return {"totalLabel": f"{total} 项待完成", "items": items}


def _projects_pool(recs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """项目笔记按最近更新排序（卡片只展示前 3 条，但总数要能报出来）。"""
    projs = [r for r in recs if r["type"] == "project"]
    projs.sort(key=lambda r: (r["updatedAt"], r["id"]), reverse=True)
    return projs


def _project_progress(rec: Dict[str, Any]) -> Optional[int]:
    done = len(rec["done_items"])
    open_ = len(rec["open_items"])
    tot = done + open_
    if tot == 0:
        return None
    return int(round(done / tot * 100))


def _projects(recs: List[Dict[str, Any]], limit: int = 3) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in _projects_pool(recs)[:limit]:
        progress = _project_progress(r)
        blocked = "受阻" in r["title"] or "受阻" in r["summary"]
        if progress == 100:
            status, kind = "已完成", "done"
        elif blocked:
            status, kind = "受阻", "blocked"
        elif progress is not None:
            status, kind = "进行中", "in_progress"
        else:
            status, kind = "未设置", "none"
        out.append(
            {
                # id / relPath：点项目名能落到那篇项目笔记（原来只给名字，点了没反应）
                "id": r["id"],
                "relPath": r["relPath"],
                "name": r["title"],
                "status": status,
                "statusKind": kind,
                "progress": progress,
            }
        )
    return out


def _search_summary() -> Dict[str, Any]:
    page = runs_store.list_runs(rtype="Agent 检索", page=1, page_size=20)
    items = [r for r in page["items"] if r.get("status") in ("成功", "警告")]
    if not items:
        return {
            "badge": "暂无 Agent Search 记录",
            "query": "—",
            "conclusion": "还没有 Agent 检索记录。发起一次检索后，这里会显示最近一次的查询、引用与耗时摘要。",
            "meta": [],
            "action": {"label": "去发起检索", "to": "/agent-search"},
        }
    r = items[0]
    detail = r.get("detail") or {}
    cited = int(detail.get("citedNotes", 0) or 0)
    elapsed = int(r.get("elapsedMs", 0))
    secs = f"{elapsed / 1000:.1f}s" if elapsed >= 1000 else f"{elapsed}ms"
    steps_raw = r.get("stepLabel", "7 / 7") or "7 / 7"
    steps_num = steps_raw.split("/")[0].strip().split(" ")[0]
    conclusion = (
        f"本次 Agent 检索最终引用 {cited} 篇笔记证据，耗时 {secs}，"
        f"共 {steps_num} 个执行步骤。结论由命中正文抽取拼接，可打开轨迹查看逐篇引文。"
    )
    return {
        "badge": f"最近一次 Agent Search · {r.get('dateLabel', '')}".strip(),
        "query": r.get("target", ""),
        "conclusion": conclusion,
        "meta": [
            {"icon": "clock", "label": f"耗时 {secs}"},
            {"icon": "cite", "label": f"引用 {cited} 篇笔记"},
            {"icon": "steps", "label": f"{steps_num} 个执行步骤"},
        ],
        "action": {"label": "查看完整轨迹", "to": "/agent-search"},
    }


def _calendar(recs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """降级打点：本月「有新增文件」的日期（口径与 /api/calendar 的 added 一致）。"""
    now = datetime.now()
    year, month, today = now.year, now.month, now.day
    markers: set = set()
    for r in recs:
        if vault.is_generated(r):
            continue
        d = _created_date(r)
        if d and d.year == year and d.month == month:
            markers.add(d.day)
    return {
        "title": "本月新增",
        "monthLabel": f"{month}月",
        "year": year,
        "month": month,
        "today": today,
        "markers": sorted(markers),
        "weekdays": WEEKDAY_MIN,
    }


# --------------------------------------------------------------------------- #
# 活动日历（§9.11）：月视图热度 + 单日下钻
# --------------------------------------------------------------------------- #
WEEK_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
# 月视图星期表头：周一起（与设计稿 shot-v2.png 的「一 二 三 四 五 六 日」
# 及 dayjs zh-cn 语言的 weekStart=1 一致）
WEEKDAY_MIN = ["一", "二", "三", "四", "五", "六", "日"]


def _hhmm(iso: str) -> str:
    """ISO → 本地 "HH:MM"（必须换算时区，理由见 timeutil 模块 docstring）。"""
    return local_hhmm(iso)


def _month_last_day(year: int, month: int) -> date:
    first = date(year, month, 1)
    return (first + timedelta(days=31)).replace(day=1) - timedelta(days=1)


def _heat_level(count: int, max_count: int) -> int:
    """0..4 热度分档（按当月峰值等分）。"""
    if count <= 0 or max_count <= 0:
        return 0
    ratio = count / max_count
    if ratio <= 0.25:
        return 1
    if ratio <= 0.5:
        return 2
    if ratio <= 0.75:
        return 3
    return 4


def _runs_on_date(date_str: str, page_size: int = 200) -> List[Dict[str, Any]]:
    """某一天的运行记录（真数据，来自 runs 存储）；取不到时退化为空表，不影响主数据。"""
    try:
        page = runs_store.list_runs(
            date_from=date_str, date_to=date_str, page=1, page_size=page_size
        )
    except Exception:  # noqa: BLE001
        return []
    return list(page.get("items", []))


def _created_date(rec: Dict[str, Any]) -> Optional[date]:
    """文件的「入库日期」：只认 frontmatter 里**显式**写的 `created`。

    ⚠ 这里曾经写的是「vault 解析时已保证 created 缺失会回落到 updated，所以不会拿到空值」
    —— 那个回落对「显示创建时间」是合理的兜底，对「哪天加了哪些文件」却是**伪造事实**：
    自动生成页（记忆索引 / 路由索引）没有 created，回落成 updated 之后，每天重建一次
    就等于每天「新增」一次（实测主人发现「记忆索引」挂在 10-15 的新增清单里）。
    所以现在按 `hasCreated` 判定：没有显式 created 的文件不进「新增」桶。

    判据写 `is False` 而不是 `not ...`：字段来自 `_parse_file`，正常一定存在；
    「键不存在」只可能出现在**进程没重启、内存里还是旧结构的索引缓存**那种情况，
    此时应该退回旧行为（用回落的 created）而不是把整库都判成「没有 created」
    —— 后者会让日历当天新增直接变成 0，是个看起来像数据丢失的假故障。
    """
    if rec.get("hasCreated") is False:
        return None
    return _updated_date(rec.get("created", ""))


def _added_updated_by_day(
    recs: List[Dict[str, Any]], year: int, month: int
) -> Tuple[Dict[str, int], Dict[str, int]]:
    """把全库按「created 落在该月」与「updated 落在该月」分两个桶。

    两桶按**天**互斥，不是按文件：同一天创建又更新的文件只算「新增」，
    不重复计入当天的「更新」；但一个文件完全可以既进 created 的桶（9/10），
    又进 updated 的桶（9/19）—— 那本来就是两件事、两天。

    两个桶都跳过自动生成页（见 `vault.is_generated`），并按**本地日历日**做键
    （`iso[:10]` 取的是字符串自带时区那一天，offset 混写时会错分到前一天）。
    """
    added: Dict[str, int] = {}
    updated: Dict[str, int] = {}
    for r in recs:
        if vault.is_generated(r):
            continue
        c_d = _created_date(r)
        if c_d is not None and c_d.year == year and c_d.month == month:
            key = c_d.isoformat()
            added[key] = added.get(key, 0) + 1
        u_d = _updated_date(r.get("updatedAt") or "")
        if u_d is None or u_d.year != year or u_d.month != month:
            continue
        if c_d == u_d:
            continue
        ukey = u_d.isoformat()
        updated[ukey] = updated.get(ukey, 0) + 1
    return added, updated


def activity_calendar(year: int, month: int) -> Dict[str, Any]:
    """月视图：每天「新增文件数」（created 落该日）+「仅更新数」，热度按新增分档。"""
    vault.ensure_index()
    recs = vault._records()
    today = datetime.now().date()
    first = date(year, month, 1)
    last = _month_last_day(year, month)

    added_by_day, updated_by_day = _added_updated_by_day(recs, year, month)

    days: List[Dict[str, Any]] = []
    for i in range(last.day):
        d = first + timedelta(days=i)
        key = d.isoformat()
        add = added_by_day.get(key, 0)
        upd = updated_by_day.get(key, 0)
        days.append({"date": key, "day": d.day, "added": add, "updated": upd, "level": 0})

    max_added = max((x["added"] for x in days), default=0)
    for x in days:
        x["level"] = _heat_level(x["added"], max_added)

    return {
        "year": year,
        "month": month,
        "monthLabel": f"{year}年{month}月",
        "today": today.day if (today.year == year and today.month == month) else 0,
        "weekdays": WEEKDAY_MIN,
        "days": days,
        "maxAdded": max_added,
        "totalAdded": sum(x["added"] for x in days),
        "totalUpdated": sum(x["updated"] for x in days),
        "addedDays": sum(1 for x in days if x["added"] > 0),
    }


def _day_file(rec: Dict[str, Any], event: str) -> Dict[str, Any]:
    """下钻列表里的一行文件。

    `event` 决定 `timeLabel` 取哪个时刻（`"created"` = 新增时刻 / `"updated"` = 更新时刻）；
    两个 ISO 时间戳都照样给出，前端不需要再猜。

    `timeSource` 说明 `timeLabel` 到底是哪个时刻，前端据此决定要不要加「非创建时刻」的提示：
      - `"created"` —— 就是 `created` 里的时刻，正常情况。
      - `"updated"` —— 这一行本来就是「当天更新」事件，取 `updated` 天经地义。
      - `"updated-fallback"` —— `created` 只写到「日」（如 `created: 2026-10-15`），
        根本没有钟点。**既不能编**（写 `00:00` 会被读成「凌晨刚建」，主人质疑过这一点），
        **也不能空着**（时间列空一格，主人同样会问「为什么没有时间」）。
        于是回落到该文件 `updated` 的本地时刻，并如实标注来源，让前端能标出「这是推断的」。
        实测受影响 3 篇：规则_协同线程 / 规则_任务队列使用规范 / 规则_记忆写入格式规范。
    """
    iso = (rec.get("created") if event == "created" else rec.get("updatedAt")) or ""
    label = _hhmm(iso)
    source = "created" if event == "created" else "updated"
    if not label:
        alt = _hhmm(rec.get("updatedAt") or "")
        if alt:
            label = alt
            source = "updated-fallback"
    return {
        "id": rec["id"],
        "title": rec["title"],
        "relPath": rec["relPath"],
        "folder": rec["folder"],
        "type": rec["type"],
        "typeLabel": rec["typeLabel"],
        "tags": rec["tags"],
        "excerpt": rec["excerpt"],
        "event": event,
        "timeLabel": label,
        "timeSource": source,
        "createdAt": rec.get("created", ""),
        "updatedAt": rec.get("updatedAt", ""),
        "wordCount": rec["wordCount"],
        "sizeKB": rec["sizeKB"],
        "degree": rec["degree"],
    }


def calendar_day(date_str: str) -> Optional[Dict[str, Any]]:
    """单日下钻：当天「新增的文件」+「仅被更新的文件」+ 当天运行记录条数。

    `date_str` 必须是 `YYYY-MM-DD`，否则返回 None（路由层转 400）。
    """
    try:
        d = date.fromisoformat(date_str)
    except (ValueError, TypeError):
        return None

    vault.ensure_index()
    recs = vault._records()

    added = [
        r
        for r in recs
        if not vault.is_generated(r) and _created_date(r) == d
    ]
    updated = [
        r
        for r in recs
        if not vault.is_generated(r)
        and _updated_date(r.get("updatedAt") or "") == d
        and _created_date(r) != d
    ]
    added.sort(key=lambda r: (r.get("created") or "", r["id"]), reverse=True)
    updated.sort(key=lambda r: (r.get("updatedAt") or "", r["id"]), reverse=True)

    files_added = [_day_file(r, "created") for r in added]
    files_updated = [_day_file(r, "updated") for r in updated]

    # 类型 / 标签分布只描述「新增文件」—— 它们是这一天「加了什么」的画像
    type_counter: Dict[str, int] = {}
    tag_counter: Dict[str, int] = {}
    for r in added:
        type_counter[r["type"]] = type_counter.get(r["type"], 0) + 1
        for t in r["tags"]:
            tag_counter[t] = tag_counter.get(t, 0) + 1
    types = sorted(
        ({"key": k, "label": vault.TYPE_LABEL.get(k, k), "count": v} for k, v in type_counter.items()),
        key=lambda x: (-x["count"], x["key"]),
    )
    tags = sorted(
        ({"tag": k, "count": v} for k, v in tag_counter.items()),
        key=lambda x: (-x["count"], x["tag"]),
    )[:8]

    return {
        "date": date_str,
        "label": f"{d.month}月{d.day}日 {WEEK_CN[d.weekday()]}",
        "isToday": d == datetime.now().date(),
        "addedCount": len(files_added),
        "updatedCount": len(files_updated),
        "runCount": len(_runs_on_date(date_str)),
        "added": files_added,
        "updated": files_updated,
        "types": types,
        "tags": tags,
    }


def _dashboard_graph(recs: List[Dict[str, Any]], limit: int = 60) -> Dict[str, Any]:
    full = vault.graph()
    stats = full["stats"]
    top = sorted(full["nodes"], key=lambda n: (-n["degree"], n["id"]))[:limit]
    keep = {n["id"] for n in top}
    links = [l for l in full["links"] if l["source"] in keep and l["target"] in keep]
    return {
        "title": "知识图谱",
        # 2026-10-20：这句此前写「此处展示 degree 最高的前 60 个节点」，而卡片实际
        # 渲染的是 `/api/graph` 的**全部** 119 个节点 —— 页面上写着 60、画着 119，
        # 是典型的「说明与实现各说各话」。
        # 数量已经在上方标题栏（`{nodes} 节点 / {uniqueLinks} 连线 / {danglingTitles} 悬空`），
        # 这里只说**口径**，不再重复数字：卡片窄（约 230px），重复数字会挤成 4 行把卡撑破。
        "note": "按 Obsidian [[wikilink]] 关系链构建：连线按唯一对计（重复引用合并），悬空按标题计；空心环为索引页。",
        "stats": {
            "nodes": stats["nodes"],
            "links": stats["links"],
            "dangling": stats["dangling"],
            "orphans": stats["orphans"],
        },
        "nodes": top,
        "links": links,
    }


def _hot_tags(tags: List[Dict[str, Any]], limit: int = 8) -> Dict[str, Any]:
    """热门标签。

    `tag` 是带井号的展示文案，`key` 是**不带井号的原标签值** —— 点标签跳
    `/memory-explorer?tag=<key>` 必须用后者：笔记 frontmatter 里的 tags 没有井号，
    拿 `#mcp` 去匹配会一篇都筛不到（子串匹配也不命中）。
    """
    return {
        "title": "热门标签",
        "items": [
            {"tag": f"#{t['tag']}", "key": t["tag"], "count": t["count"]}
            for t in tags[:limit]
        ],
    }


# --------------------------------------------------------------------------- #
# 两张横向条形卡（热度分层 / 记忆类型，CONTRACT §17）
# --------------------------------------------------------------------------- #
def _dist(title: str, note: str, raw: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把 [{key,label,count}] 归一成条形卡数据：补 share（占全库比例）。

    `share` 用「占全库比例」而不是「占最大条比例」—— 前者是描述性统计（能直接说
    「常温占 98%」），后者的相对长度交给前端用 count/max(count) 现算，两者互不混淆。
    """
    total = sum(int(x["count"]) for x in raw)
    items = [
        {
            "key": x["key"],
            "label": x["label"],
            "count": int(x["count"]),
            "share": round(int(x["count"]) / total, 4) if total else 0.0,
        }
        for x in raw
    ]
    return {"title": title, "note": note, "total": total, "items": items}


def _tier_dist() -> Dict[str, Any]:
    """热度分层：hot / warm / cold / 未分层（口径见 vault.tier_counts）。

    未分层在 vault 里是空串 key；这里归一成 `"unknown"` —— 空串没法当查询参数用
    （`?tier=` 会被前端当成「没筛」，点「未分层」那一行就会跳回全部），
    `unknown` 是能落进 URL 且能精确匹配的写法。
    """
    raw = [
        {"key": (x["key"] or "unknown"), "label": x["label"], "count": x["count"]}
        for x in vault.tier_counts()
    ]
    return _dist("热度分层", "tier 分布", raw)


def _type_dist(recs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """记忆类型：按 frontmatter type 计数，数量倒序。"""
    counter: Dict[str, int] = {}
    for r in recs:
        counter[r["type"]] = counter.get(r["type"], 0) + 1
    raw = [{"key": k, "label": vault.TYPE_LABEL.get(k, k), "count": v} for k, v in counter.items()]
    raw.sort(key=lambda x: (-x["count"], x["key"]))
    return _dist("记忆类型", "type 分布", raw)


def _mcp_connections() -> Dict[str, Any]:
    """活跃 MCP 连接：直接复用 `tool_catalog.get_sources()` 的**真实连接态**。

    2026-10-20 修：此处原先自己读 mcp.json，把 `ai-memory` 硬编码成「已挂载」、
    其余名字一律「未连接」——而实测工作台已直连 headroom / playwright（14:43 的
    /api/tools/sources 三条 connected=true），卡片却报它们未连接。
    同一件事存在两套判定就是「两个事实源」，所以改成只认 tool_catalog 那套：
    它连得上就报工具数，连不上才报未连接（figma 是 URL 型需 OAuth，如实报未连接）。

    工作台自身（key=workspace，内置 7 个工具）不是「MCP 连接」，不列进来。
    """
    title = "活跃 MCP 连接"
    try:
        sources = tool_catalog.get_sources()
    except Exception as e:  # noqa: BLE001 - 卡片是装饰性数据，取不到不该拖垮整个看板
        return {"title": title, "items": [], "note": f"读取 MCP 来源失败：{e}"}
    items: List[Dict[str, Any]] = []
    for s in sources:
        key = str(s.get("key") or "")
        if key in ("", "workspace"):
            continue
        connected = bool(s.get("connected"))
        count = int(s.get("toolCount") or 0)
        items.append(
            {
                "key": key,
                "name": str(s.get("label") or key),
                "connected": connected,
                # 连上就报真实工具数（数字本身就是「连着」的证据），没连上才只写状态
                "badge": (f"{count} 个工具" if count else "已连接") if connected else "未连接",
                "toolCount": count,
            }
        )
    return {
        "title": title,
        "items": items,
        "note": "状态取自工作台直连结果（/api/tools/sources），非 mcp.json 配置推断",
    }


# --------------------------------------------------------------------------- #
# 聚合
# --------------------------------------------------------------------------- #
def _greeting(now: Optional[datetime] = None) -> str:
    """按时段问候。2026-10-20 前这里是写死的「早上好」—— 下午三点打开还是「早上好」，
    一眼就是假数据。分档按本地钟点，深夜单独一档（不说「晚上好」，那不符合事实）。
    """
    hour = (now or datetime.now()).hour
    if 5 <= hour < 11:
        word = "早上好"
    elif 11 <= hour < 13:
        word = "中午好"
    elif 13 <= hour < 18:
        word = "下午好"
    elif 18 <= hour < 23:
        word = "晚上好"
    else:
        word = "夜深了，还在忙"
    return f"{word}，欢迎回到 Graphite Workspace"


def get_dashboard() -> Dict[str, Any]:
    vault.ensure_index()
    recs = vault._records()
    fac = vault.facets()
    totals = fac["totals"]

    today = datetime.now().date()
    n7 = 0
    n30 = 0
    for r in recs:
        d = _updated_date(r["updatedAt"])
        if not d:
            continue
        delta = (today - d).days
        if 0 <= delta <= 6:
            n7 += 1
        if 0 <= delta <= 30:
            n30 += 1

    active_projects = 0
    for r in recs:
        if r["type"] != "project":
            continue
        d = _updated_date(r["updatedAt"])
        if d is not None and 0 <= (today - d).days <= 30:
            active_projects += 1

    done_cb, open_cb = _checkboxes(recs)
    cb_total = done_cb + open_cb
    ratio = f"{int(round(open_cb / cb_total * 100))}%" if cb_total else "0%"

    # 工具口径：与 MCP Lab 同源，取真实目录（内置 + ai-memory + 外部占位）
    catalog = tool_catalog.get_full_catalog()
    tool_total = len(catalog)
    tool_invokable = sum(1 for t in catalog if t.get("invokable"))

    # 4 条迷你折线图序列：各自是真数据、语义互不相同
    trend_notes = _trend_from_recs(recs, lambda _r: 1)
    trend_projects = _trend_from_recs(recs, lambda r: 1 if r["type"] == "project" else 0)
    trend_tasks = _trend_from_recs(recs, lambda r: len(r.get("open_items") or []))
    trend_tools = _trend_from_runs()

    return {
        "user": {"name": _local_user_name(), "role": "本地账户", "initial": _local_user_name()[:1]},
        "hero": {
            "greeting": _greeting(),
            "description": (
                f"你的知识库目前共有 {totals['notes']} 篇笔记、{active_projects} 个活跃项目。"
                "可以直接发起一次 AI 检索，或进入 MCP Lab 调试工具调用。"
            ),
            "actions": [
                {"label": "发起新的 Agent 检索", "to": "/agent-search", "variant": "primary"},
                {"label": "打开 MCP Lab", "to": "/mcp-lab", "variant": "ghost"},
            ],
        },
        "stats": [
            {
                "key": "notes",
                "icon": "notes",
                "value": f"{totals['notes']:,}",
                "label": "笔记总数",
                "delta": f"↑ {n7} 近 7 天" if n7 > 0 else "近 7 天无更新",
                "deltaDir": "up" if n7 > 0 else "flat",
                "trend": trend_notes,
                "trendLabel": f"近 {TREND_DAYS} 天每日有更新的笔记数",
                "sample": False,
            },
            {
                "key": "projects",
                "icon": "projects",
                "value": str(active_projects),
                "label": "活跃项目",
                "delta": f"↑ {active_projects} 近 30 天" if active_projects > 0 else "近 30 天无更新",
                "deltaDir": "up" if active_projects > 0 else "flat",
                "trend": trend_projects,
                "trendLabel": f"近 {TREND_DAYS} 天每日有更新的项目笔记数",
                "sample": False,
            },
            {
                "key": "tasks",
                "icon": "tasks",
                "value": str(open_cb),
                "label": "待办任务",
                "delta": f"占全部任务 {ratio}",
                "deltaDir": "flat",
                "trend": trend_tasks,
                "trendLabel": f"近 {TREND_DAYS} 天每日涉及的未完成待办条数",
                "sample": False,
            },
            {
                "key": "tools",
                "icon": "tools",
                "value": f"{tool_total} 个",
                "label": "MCP 工具",
                "delta": f"{tool_invokable} 个可直接调用",
                "deltaDir": "flat",
                "trend": trend_tools,
                "trendLabel": f"近 {TREND_DAYS} 天每日 MCP 调用次数",
                "sample": False,
            },
        ],
        "recentNotes": _recent_notes(recs, 5),
        "todos": _todos(recs, 6),
        # 卡片只放最近 3 个项目笔记，但「共几个」必须报真数：
        # 原先卡头写死 `projects.length`（永远是 3），看着像全库只有 3 个项目
        "projects": _projects(recs, 3),
        "projectsTotal": active_projects,
        "searchSummary": _search_summary(),
        "recentCalls": _recent_calls(6),
        "calendar": _calendar(recs),
        "graph": _dashboard_graph(recs, 60),
        "tierDist": _tier_dist(),
        "typeDist": _type_dist(recs),
        "hotTags": _hot_tags(fac["tags"], 8),
        "mcpConnections": _mcp_connections(),
    }
