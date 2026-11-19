"""数据版本探针（阶段 21 补强，2026-10-25）。

── 主人问的那个关键问题 ────────────────────────────────────
「预先加载了笔记，别人突然加了一篇，怎么更新？」

答案不是「别预取」，而是**给缓存配一个几十字节的版本戳**：
这个接口返回 `{count, latest, rev}`，rev = 笔记数 + 最新更新时间。
前端每隔十几秒（且页面可见时）探一次，rev 变了就清缓存重拉。

── 为什么用「数量 + 最新更新时间」而不是自增版本号 ──────────
· 自增号要额外维护：落盘、防多进程竞争、重启恢复 —— 三处都能出错。
· 库是唯一事实源：任何人用任何通道写入都会改这两个值 —— 网页里的编辑、
  服务器上的 `sync_push`、MCP 的 memory_write —— 谁写的都能被发现。
· payload 极小（< 100 字节），比重新拉全量列表便宜两个数量级。

── 代价（诚实写出来）──────────────────────────────────────
· 一篇笔记被改但 `updatedAt` 恰好没变时探不到（正常写入一定会变）。
· 删一篇 + 增一篇让 count 不变时，靠 latest 兜底：新增的必定更新，所以 latest 会变。
· 探测有最长一个间隔的延迟 —— 这个延迟是「别人改完到你看见」的上限，20 秒。
  自己改的不用等：写操作成功后前端直接清缓存（`invalidateApiCache`）。
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter

from .. import vault

router = APIRouter(tags=["data"])


@router.get("/data/rev")
def data_rev() -> Dict[str, Any]:
    """返回库的「版本戳」。故意做得极轻：不排序、不筛、只扫两列。"""
    notes = vault.list_notes()
    latest = ""
    for n in notes:
        v = n.get("updatedAt") or ""
        if v > latest:
            latest = v
    return {"ok": True, "count": len(notes), "latest": latest, "rev": f"{len(notes)}:{latest}"}
