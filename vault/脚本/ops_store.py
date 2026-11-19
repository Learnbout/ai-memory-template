"""运维数据层（阶段 11，2026-10-25）：备份 / 恢复 / 审计轮转 / 库健康 / 令牌回收。

## 为什么单独一个模块，不塞进 admin_api

`admin_api` 管的是**人**（账号、角色、令牌），这里管的是**物**（库文件、备份、
归档）。两者失败代价完全不同：把某个人的角色改错，改回来就完了；把 `index.sqlite3`
恢复成三天前的样子，这三天写的东西就没了。代价不同，所以审核与护栏也该分开。

现实原因还有一个：这里要 import `backup_db`（备份策略常量 `TARGETS` / `KEEP_DAYS`
都在那儿）。`admin_api` 没必要知道备份保留几天。

## 三条硬边界

1. **备份文件名只认白名单。** 所有接受文件名的入口先过 `_safe_name()`：
   必须是 `数据库/备份/` 下的 `.sqlite3`、不带路径分隔符、不带 `..`。
   恢复接口一旦能被 `../../index.sqlite3` 指到别处，等于给了「任意文件覆盖」。
2. **恢复前先给现在的库再备份一次。** 顺序是「安全网备份 → 校验备份文件 →
   反向 backup → 恢复后自查」，任何一步失败都停在原地。恢复是唯一能把数据
   变少的操作，不允许「先试一下」。
3. **只恢复 index / versions。** 审计库有只追加铁律（恢复一份旧审计 = 擦掉
   记录）；聊天与计划是过程数据，恢复它们只会让人困惑「我发的话去哪了」。

## 为什么恢复用 `Connection.backup()` 而不是复制文件

同 `backup_db.py` 的理由：WAL 模式下已提交未 checkpoint 的页在 `-wal` 里，
直接覆盖 `.sqlite3` 会得到半新半旧的文件。`backup()` 走 SQLite 自己的页级
复制，两边都能正确处理 WAL，且不用停服务。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CODE_ROOT = Path(__file__).resolve().parents[1]
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

import audit  # noqa: E402
import db_paths  # noqa: E402

# `mcp_auth` 刻意**不在顶层导入**：它一进来就会拉 `mcp_compat`，而后者模块级
# `importlib.metadata.version("mcp")` 在没装 MCP SDK 的环境里直接抛
# PackageNotFoundError。Graphite 的独立 venv 就是这种环境（它用 HTTP 连 MCP，
# 不需要 SDK）—— 顶层导入等于让整个运维页起不来。令牌回收改走 admin_api
# 的权威链路（subprocess 调 issue_token.py），与页面上的单条吊销同一条路。

# backup_db 放在 脚本/ 里，得先让那个目录进 path
_SCRIPT_DIR = CODE_ROOT / "脚本"
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
import backup_db  # noqa: E402

DEST = backup_db.DEST
MANIFEST = backup_db.MANIFEST

# 只允许恢复这两份库（见文件头第 3 条）
RESTORABLE = {label: (src, table) for label, src, table in backup_db.TARGETS}

# 审计轮转记录（audit_rotate.py 写的）
ROTATE_LOG = db_paths.vault_dir() / "日志" / "审计轮转记录.jsonl"


# ── 小工具 ─────────────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_name(name: str) -> Path:
    """备份文件名 → 绝对路径。任何越界都抛 ValueError。"""
    raw = str(name or "").strip()
    if not raw or raw != Path(raw).name or raw.startswith(".") or not raw.endswith(".sqlite3"):
        raise ValueError(f"备份文件名不合法：{raw or '（空）'}")
    root = DEST.resolve()
    p = (root / raw).resolve()
    if p.parent != root:
        raise ValueError("备份文件不在备份目录内")
    if not p.is_file():
        raise ValueError(f"备份不存在：{raw}")
    return p


def _label_of(name: str) -> str:
    for label in RESTORABLE:
        if name.startswith(label + "-"):
            return label
    raise ValueError(f"认不出这份备份属于哪个库：{name}（只支持 {sorted(RESTORABLE)}）")


def _beyond_keep_window(iso: str) -> bool:
    """这条备份时间是否已经超出保留窗口（按 backup_db.KEEP_DAYS 天算）。

    用于区分「被策略清掉的旧备份」和「意外丢失的新备份」。解析不出来就当 False
    —— 宁可按警报显示，也不要静默放过一份本该在的新备份。
    """
    if not iso:
        return False
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return False
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).days > int(backup_db.KEEP_DAYS)


def _count(path: Path, table: str) -> int | None:
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10.0)
        try:
            return int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] or 0)
        finally:
            con.close()
    except sqlite3.Error:
        return None


def _quick_check(path: Path) -> str:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10.0)
    try:
        return str(con.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        con.close()


# ── 备份 ───────────────────────────────────────────────────────────────────
def list_backups(limit: int = 60) -> dict[str, Any]:
    """读 manifest.jsonl（最后写的那条为准），并核对文件是否还在。

    为什么以 manifest 为准而不是扫目录：manifest 里有行数、sha256、源文件路径，
    扫目录只能拿到文件名和大小。但**文件可能被手工删掉**，所以两边都要看：
    manifest 里有、盘上没有 → `missing=true`，页面上标红（说明有人动过备份目录）。

    两种「盘上没有」必须分开（2026-10-25 修）：轮转（`event=rotate`）正常清掉的
    文件，和**意外丢失**的文件长得一模一样。混在一起的话，页面上永远挂着几条
    「文件缺失」的红标 —— 红标天天有，真丢了谁也注意不到。所以先收集 manifest
    里的 `removed` 集合，被它记录过的直接不列（那是设计内的清理）。
    """
    items: list[dict[str, Any]] = []
    by_file: dict[str, dict[str, Any]] = {}
    pruned: set[str] = set()
    if MANIFEST.is_file():
        try:
            for line in MANIFEST.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("event") == "rotate":
                    for name in rec.get("removed") or []:
                        pruned.add(str(name))
                    continue
                name = str(rec.get("file") or "")
                if name:
                    by_file[name] = rec          # 同名后写覆盖前写（manifest 是追加流水）
        except OSError:
            pass

    for name, rec in sorted(by_file.items(), key=lambda kv: str(kv[1].get("time") or ""), reverse=True):
        p = DEST / name
        exists = p.is_file()
        if not exists and name in pruned:
            continue                             # 轮转清掉的，不列也不报红
        if not exists and _beyond_keep_window(str(rec.get("time") or "")):
            # 早于保留窗口的、盘上又没有的 —— 不管是被谁清掉的，都不该继续当警报。
            # 这一条兜住「清理留痕上线之前」的历史删除（那时 manifest 只记建不记删）。
            continue
        items.append(
            {
                "file": name,
                "label": rec.get("label") or name.split("-")[0],
                "time": rec.get("time") or "",
                "rows": rec.get("rows"),
                "bytes": (p.stat().st_size if exists else rec.get("bytes")),
                "sha256": rec.get("sha256") or "",
                "exists": exists,
                "missing": not exists,
            }
        )
        if len(items) >= limit:
            break

    # 盘上有、manifest 里没有的文件（手工拷进来的备份）：也列出来，标 unregistered。
    #
    # 判「有没有登记」必须拿**全量** by_file 比，不能拿上面截断后的 items ——
    # 那是 2026-10-25 实测逮到的 bug：limit=12 时，第 13 条之后那些其实登记过的
    # 备份被当成「未登记」又追加了一遍，页面上凭空多出两个「未登记」标签。
    if DEST.is_dir():
        known = set(by_file) | {it["file"] for it in items}
        for p in sorted(DEST.glob("*.sqlite3"), key=lambda x: x.stat().st_mtime, reverse=True):
            if len(items) >= limit:
                break
            if p.name in known:
                continue
            items.append(
                {
                    "file": p.name,
                    "label": p.stem.split("-")[0],
                    "time": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
                    "rows": _count(p, RESTORABLE.get(p.stem.split("-")[0], ("", ""))[1])
                    if p.stem.split("-")[0] in RESTORABLE
                    else None,
                    "bytes": p.stat().st_size,
                    "sha256": "",
                    "exists": True,
                    "missing": False,
                    "unregistered": True,
                }
            )

    return {
        "items": items,
        "total": len(items),
        "dest": str(DEST),
        "keepDays": backup_db.KEEP_DAYS,
        "keepMonths": backup_db.KEEP_MONTHS,
        "manifestExists": MANIFEST.is_file(),
    }


def create_backup() -> dict[str, Any]:
    """立刻备份 index + versions，并按策略轮转。"""
    DEST.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    made: list[dict[str, Any]] = []
    for label, src, table in backup_db.TARGETS:
        rec = backup_db.backup_one(label, Path(src), table, stamp)
        made.append(rec)
    rotated = backup_db.rotate()
    ok = [r for r in made if not r.get("skipped")]
    if not ok:
        raise RuntimeError("备份未产生任何文件（源库可能不存在）")
    return {
        "items": made,
        "rotated": rotated,
        "stamp": stamp,
        "rows": {r.get("label"): r.get("rows") for r in ok},
    }


def verify_backup(name: str) -> dict[str, Any]:
    """恢复演练：不开替代品，只把这份备份当真实库打开自检。"""
    p = _safe_name(name)
    label = _label_of(p.name)
    table = RESTORABLE[label][1]
    check = _quick_check(p)
    rows = _count(p, table)
    rec = next((it for it in list_backups(500)["items"] if it["file"] == p.name), {})
    return {
        "file": p.name,
        "label": label,
        "quickCheck": check,
        "rows": rows,
        "bytes": p.stat().st_size,
        "sha256": _sha256(p),
        "sha256Match": (not rec.get("sha256")) or (rec.get("sha256") == _sha256(p)),
        "ok": check.lower() == "ok",
    }


def restore_backup(name: str) -> dict[str, Any]:
    """把某个备份恢复回去。顺序见文件头第 2 条，任何一步不过就停。"""
    p = _safe_name(name)
    label = _label_of(p.name)
    target, table = RESTORABLE[label]
    target = Path(target)

    # ① 备份文件自己必须是好的（拿坏文件去覆盖，等于主动毁库）
    if _quick_check(p).lower() != "ok":
        raise RuntimeError(f"备份文件本身 quick_check 未通过：{p.name}")
    backup_rows = _count(p, table)

    # ② 安全网：现库先备份一份（这一步失败就不往下走）
    DEST.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safeguard = backup_db.backup_one(label, target, table, stamp)
    if safeguard.get("skipped"):
        raise RuntimeError("恢复前备份当前库失败，已中止（不覆盖任何文件）")

    # ③ 反向 backup：备份文件 → 现库
    before_rows = _count(target, table)
    src = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=20.0)
    dst = sqlite3.connect(target.as_posix(), timeout=30.0)
    try:
        src.backup(dst)
        check = str(dst.execute("PRAGMA quick_check").fetchone()[0])
        after = int(dst.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] or 0)
    finally:
        dst.close()
        src.close()

    if check.lower() != "ok":
        raise RuntimeError(
            f"恢复后 quick_check 未通过：{check}。当前库状态不可信，"
            f"请用安全网备份 {safeguard.get('file')} 再恢复一次。"
        )
    if backup_rows is not None and after != backup_rows:
        raise RuntimeError(
            f"恢复后行数不一致：备份 {backup_rows} / 现库 {after}。"
            f"安全网备份 {safeguard.get('file')} 可用于回滚。"
        )
    return {
        "restored": p.name,
        "label": label,
        "target": str(target),
        "rowsBefore": before_rows,
        "rowsAfter": after,
        "quickCheck": check,
        "safeguard": safeguard.get("file"),
        "at": _now(),
    }


# ── 审计状态与轮转 ──────────────────────────────────────────────────────────
def audit_state() -> dict[str, Any]:
    """审计库文件清单 + 是否该轮转了。

    「该轮转」的判据只有一条可执行口径：**最早一条记录不在当年**，或者主库行数
    超过 `ROTATE_ROWS` 阈值（默认 5 万，约等于日更 140 条跑一年）。
    不按文件大小判 —— 5 万条定长文本行也就是十几 MB，拿 MB 当阈值对这个小库没意义。
    """
    files = []
    total = 0
    earliest = ""
    latest = ""
    for p in audit.archive_files():
        n = _count(p, "audit_log")
        total += int(n or 0)
        lo, hi = "", ""
        try:
            con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=5.0)
            try:
                row = con.execute("SELECT MIN(ts), MAX(ts) FROM audit_log").fetchone()
                lo, hi = str(row[0] or ""), str(row[1] or "")
            finally:
                con.close()
        except sqlite3.Error:
            pass
        if lo and (not earliest or lo < earliest):
            earliest = lo
        if hi and hi > latest:
            latest = hi
        files.append(
            {
                "file": p.name,
                "rows": n,
                "bytes": p.stat().st_size if p.is_file() else 0,
                "from": lo,
                "to": hi,
                "archive": p.name != db_paths.audit_db().name,
            }
        )

    files.sort(key=lambda r: (r["archive"], r["file"]))
    last_rotate = ""
    if ROTATE_LOG.is_file():
        try:
            for line in ROTATE_LOG.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("action") == "rotate":
                    last_rotate = str(rec.get("ts") or "")
        except OSError:
            pass

    this_year = datetime.now().year
    suggest = False
    reason = ""
    if earliest and earliest[:4] and int(earliest[:4]) < this_year:
        suggest = True
        reason = f"库内还有 {earliest[:4]} 年的记录，可以按年归档"
    if total > 50000:
        suggest = True
        reason = f"主库 {total} 条已超过 5 万条，建议归档以保持查询速度"

    return {
        "files": files,
        "total": total,
        "earliest": earliest,
        "latest": latest,
        "lastRotate": last_rotate,
        "suggestRotate": suggest,
        "reason": reason,
        "rotateThreshold": 50000,
        "rotateLog": str(ROTATE_LOG),
    }


# ── 库健康 ─────────────────────────────────────────────────────────────────
def health() -> dict[str, Any]:
    """`数据库/` 下每个 .sqlite3 的体检：能不能打开、quick_check、行数、大小。

    行数只报能数出来的表（`notes` / `audit_log` / `messages` / `plans`），
    数不出来就报 None —— 不同库的表结构不一样，硬编一张表名对照表会在加库时失准。
    """
    d = db_paths.db_dir()
    rows_table = {
        "index.sqlite3": "notes",
        "versions.sqlite3": "note_versions",
        "audit.sqlite3": "audit_log",
        "chat.sqlite3": "messages",
        "plan.sqlite3": "plans",
    }
    out = []
    total_bytes = 0
    for p in sorted(d.glob("*.sqlite3")) if d.is_dir() else []:
        size = p.stat().st_size
        total_bytes += size
        item: dict[str, Any] = {"file": p.name, "bytes": size}
        try:
            item["quickCheck"] = _quick_check(p)
            table = rows_table.get(p.name)
            item["rows"] = _count(p, table) if table else None
            item["rowsTable"] = table
        except sqlite3.Error as e:
            item["quickCheck"] = f"打不开：{e}"
            item["rows"] = None
        out.append(item)
    return {
        "items": out,
        "totalBytes": total_bytes,
        "dbDir": str(d),
        "at": _now(),
    }


# ── 令牌回收 ───────────────────────────────────────────────────────────────
def sweep_tokens(actor: str) -> dict[str, Any]:
    """把**已过期但没标吊销**的令牌在台账里标掉。

    为什么要这一步：JWT 过期后验签本身会拒（`exp` 已经过了），所以它们不是
    安全漏洞；但台账里留着一堆「看起来还有效」的票，管理员在页面上分不清哪些
    能用 —— 而「分不清」的直接后果是会重复签发，票越滚越多。

    为什么不删台账行：台账是吊销名单，删掉等于「这张票从没存在过」，
    而审计里又有它的签发记录，两边对不上。

    吊销走 `admin_api.revoke_token`，不自己改 JSON：那条链路同时改
    `account_tokens` 表、台账、写审计，三处一致。自己写 JSON 会让页面上的
    「已吊销」标记和 MCP 侧的真实判据分裂。
    """
    import admin_api  # 惰性：admin_api 会读 web_auth 配置，运维页不该被它拖累

    now = _now()
    try:
        rows = admin_api._token_rows()  # noqa: SLF001 —— 同仓内部表读取
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"读不到令牌台账：{type(e).__name__}: {e}") from e

    swept: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("revoked_at") or "").strip():
            continue
        exp = str(row.get("expires_at") or "").strip()
        if not exp or exp.startswith("9999"):     # 永久票不扫
            continue
        if exp > now:                              # ISO 同格式，字符串比较即可
            continue
        jti = str(row.get("jti") or "")
        try:
            admin_api.revoke_token(actor, jti)
            swept.append({"jti": jti, "username": row.get("username") or "", "expires_at": exp})
        except Exception as e:  # noqa: BLE001 —— 一张失败不该中断整批
            failed.append({"jti": jti, "error": f"{type(e).__name__}: {e}"})
    return {"swept": swept, "failed": failed, "count": len(swept), "at": now}
