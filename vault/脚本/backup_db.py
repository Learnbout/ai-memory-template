#!/usr/bin/env python3
"""索引库与版本库的在线备份（2026-10-19，写路径切「库为源」之后的安全底线）。

为什么必须补这一环
------------------
切库为源之前，`index.sqlite3` 是从 md 重建出来的投影 —— 删了不心疼。
切成库为源之后，它是**唯一记忆库本体**：
· `db_dump.py` 只 dump `versions.sqlite3`（不可重建）；
· `.gitignore` 排除了数据库目录，所以 `index.sqlite3` 根本不在 git 里；
· 一旦 md 出现「导出失败/还没导出」，那些行在世界上只有一份，就在这个文件里。

**直接复制文件是错的**：WAL 模式下已提交但未 checkpoint 的页在 `-wal` 里，
只拷 `.sqlite3` 会得到旧快照或损坏文件。必须用 SQLite 的在线备份 API
（`Connection.backup()`），它会自动处理 WAL，且不阻塞写入。

每次备份后做什么
----------------
1. `PRAGMA quick_check` —— 备份文件自己必须是好的；
2. 核心表行数比对 —— 备份的行数必须与源库一致（少一行就说明备份不完整）；
3. 记 SHA-256 到 `manifest.jsonl` —— 以后能验证「这份备份没被改过」。

轮转
----
按文件名里的日期分组，保留最近 `KEEP_DAYS` 个不同日期各一份；每月首日那份额外保留
（`KEEP_MONTHS` 个月）。其余删除。**先写新备份、校验通过后才删旧的** —— 顺序反了，
磁盘满或备份失败时会进入「旧的删了新的没成」。

用法：
    <venv>/python.exe 脚本/backup_db.py              # 备份并轮转
    <venv>/python.exe 脚本/backup_db.py --list       # 看现有备份
    <venv>/python.exe 脚本/backup_db.py --verify X   # 恢复演练：校验某份备份
    <venv>/python.exe 脚本/backup_db.py --no-rotate  # 只备份，不删旧

退出码：0 成功 / 1 备份失败 / 2 参数或环境问题。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

# 代码根 vs 数据根（2026-10-25 修）：`AI_MEMORY_DIR` 指「数据放哪」，脚本住在
# 代码树里。原来把 VAULT 直接塞 sys.path，一旦 AI_MEMORY_DIR 指向临时目录
# （演练 / 测试），下一行 `import db_paths` 就 ModuleNotFoundError —— 备份脚本
# 恰恰是最需要在临时目录里演练的那一个。现在拆成两个根。
CODE_ROOT = Path(__file__).resolve().parents[1]
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))
import db_paths  # noqa: E402  数据库位置唯一入口（2026-10-19 收口到「数据库/」）

VAULT = db_paths.vault_dir()

# 备份就放在本体旁边：`数据库/备份/`。理由是「一个文件夹 = 整个记忆库」——
# 拷走这个目录就等于拷走了库和它的历史备份，不需要再记住别处还有一份。
DEST = VAULT / "数据库" / "备份"
MANIFEST = DEST / "manifest.jsonl"

# (标签, 源文件, 用来比行数的表)
TARGETS = [
    ("index", db_paths.resolve("index.sqlite3"), "notes"),
    ("versions", db_paths.resolve("versions.sqlite3"), "note_versions"),
]

# 保留口径（2026-11-18 收紧：7 天/3 月 → 3 天/1 月）
#
# 为什么要收：库是事实源、md 只是快照，所以旧库 dump 是**唯一完整的历史回滚点** ——
# 不能为了省磁盘乱删。但另一方面，11-16 瘦身前每个 dump 有 26 MB（index 26.2 +
# versions 20.6 ≈ 47 MB/对），按 7 天 + 3 个月首日攒下来是 25 个文件 294.8 MB。
# 折中：日常留 3 天（覆盖「昨天改坏了」这种最常见回滚），月度首日只留 1 份
# （跨月对账用）。实测这样 294.8 → 107.5 MB，仍保留月首与最近两天。
KEEP_DAYS = 3
KEEP_MONTHS = 1


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def count_rows(conn: sqlite3.Connection, table: str) -> int | None:
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.Error:
        return None  # 表不存在（新库/不同版本）—— 不阻断，只记 None


def backup_one(label: str, src_path: Path, table: str, stamp: str) -> dict:
    """在线备份一份并自检。返回 manifest 记录；失败抛异常。"""
    if not src_path.is_file():
        return {"label": label, "skipped": "源文件不存在", "src": str(src_path)}

    tmp_dir = Path(tempfile.mkdtemp(prefix="dbbak_", dir=str(DEST)))
    tmp = tmp_dir / f"{label}.sqlite3"
    try:
        # ① 在线备份（自动处理 WAL，不阻塞写入）
        src = sqlite3.connect(f"file:{src_path.as_posix()}?mode=ro", uri=True, timeout=15.0)
        dst = sqlite3.connect(str(tmp))
        try:
            src.backup(dst)
            # ② 自检：备份文件必须能过 quick_check，且行数与源库一致
            ok = dst.execute("PRAGMA quick_check").fetchone()[0]
            if str(ok).lower() != "ok":
                raise RuntimeError(f"quick_check 未通过：{ok}")
            n_dst = count_rows(dst, table)
            n_src = count_rows(src, table)
            if n_src is not None and n_dst != n_src:
                raise RuntimeError(f"行数不一致：源 {n_src} / 备份 {n_dst}")
        finally:
            dst.close()
            src.close()

        # ③ 原子落盘（同卷 os.replace）
        #
        # 文件名碰撞必须挡掉（2026-10-25 实测踩出来的）：stamp 精度是秒，而恢复
        # 流程要在同一秒里做两次备份（「安全网备份现库」+ 之前那次手动备份）。
        # 撞名时 os.replace 会把先落的那份**静默覆盖** —— 表现是「恢复成功了，
        # 但数据没变」：回滚点被当成垃圾扔了，而这一步报的是成功。
        # 所以撞名就加序号，绝不覆盖已有备份。
        final = DEST / f"{label}-{stamp}.sqlite3"
        seq = 2
        while final.exists():
            final = DEST / f"{label}-{stamp}-{seq}.sqlite3"
            seq += 1
        os.replace(tmp, final)
        rec = {
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "label": label,
            "file": final.name,
            "src": str(src_path),
            "table": table,
            "rows": n_dst,
            "bytes": final.stat().st_size,
            "sha256": sha256(final),
        }
        with MANIFEST.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec
    finally:
        # 失败时把临时目录清掉；成功时 tmp 已被 os.replace 移走，目录为空
        for p in tmp_dir.glob("*"):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            tmp_dir.rmdir()
        except OSError:
            pass


def rotate() -> list[str]:
    """按「最近 KEEP_DAYS 个不同日期各留每个库最新一份 + 每月首日额外保留」清理旧备份。"""
    files = sorted(DEST.glob("*.sqlite3"))
    if not files:
        return []

    def day_of(p: Path) -> str:
        # 文件名形如 index-20261019-204500.sqlite3
        return p.stem.split("-")[1]

    by_day: dict[str, list[Path]] = {}
    for p in files:
        by_day.setdefault(day_of(p), []).append(p)

    days = sorted(by_day, reverse=True)

    def latest_per_label(day: str) -> set[Path]:
        picked: set[Path] = set()
        for label, _src, _t in TARGETS:
            mine = sorted(p for p in by_day[day] if p.name.startswith(label + "-"))
            if mine:
                picked.add(mine[-1])
        return picked

    keep: set[Path] = set()
    for d in days[:KEEP_DAYS]:
        keep |= latest_per_label(d)
    for d in [x for x in days if x.endswith("01")][:KEEP_MONTHS]:   # 每月首日额外保留
        keep |= latest_per_label(d)

    removed = []
    for p in files:
        if p not in keep:
            try:
                p.unlink()
                removed.append(p.name)
            except OSError:
                pass

    # 清理也要留痕（2026-10-25 补）：manifest 是「这个目录发生过什么」的流水，
    # 只记「建了哪些」不记「删了哪些」，运维页就会把被轮转正常清掉的文件
    # 标成「文件缺失」—— 一堆红标天天挂着，真正的丢失就没人看了。
    if removed:
        try:
            with MANIFEST.open("a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                            "event": "rotate",
                            "removed": removed,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except OSError:
            pass
    return removed


def verify(path: Path) -> int:
    """恢复演练：把备份当真实库打开，跑自检并报行数。"""
    if not path.is_file():
        print(f"文件不存在：{path}")
        return 2
    conn = sqlite3.connect(str(path))
    try:
        ok = conn.execute("PRAGMA quick_check").fetchone()[0]
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        print(f"文件   : {path}")
        print(f"大小   : {path.stat().st_size/1024/1024:.1f} MB")
        print(f"quick_check: {ok}")
        print(f"表     : {', '.join(tables) or '（空）'}")
        if "notes" in tables:
            n = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
            print(f"notes  : {n} 行")
        if "note_versions" in tables:
            nv = conn.execute("SELECT COUNT(*) FROM note_versions").fetchone()[0]
            print(f"versions: {nv} 行")
        digest = sha256(path)
        rec = None
        if MANIFEST.is_file():
            for line in MANIFEST.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("file") == path.name:
                    rec = r
        if rec:
            same = rec.get("sha256") == digest
            print(f"manifest: {'一致' if same else '不一致（文件被改过）'}")
            return 0 if (same and str(ok).lower() == "ok") else 1
        print("manifest: 无记录（手工放入的备份？）")
        return 0 if str(ok).lower() == "ok" else 1
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="索引库 / 版本库在线备份")
    ap.add_argument("--list", action="store_true", help="只列现有备份")
    ap.add_argument("--verify", metavar="FILE", help="恢复演练：校验指定备份")
    ap.add_argument("--no-rotate", action="store_true", help="只备份不轮转")
    args = ap.parse_args()

    if not VAULT.is_dir():
        print(f"记忆库目录不存在：{VAULT}（检查 AI_MEMORY_DIR）")
        return 2

    DEST.mkdir(parents=True, exist_ok=True)

    if args.list:
        files = sorted(DEST.glob("*.sqlite3"))
        if not files:
            print("暂无备份")
            return 0
        for p in files:
            print(f"{p.name:34s} {p.stat().st_size/1024/1024:6.1f} MB  "
                  f"{datetime.fromtimestamp(p.stat().st_mtime):%Y-%m-%d %H:%M}")
        return 0

    if args.verify:
        return verify((DEST / args.verify) if not Path(args.verify).is_file() else Path(args.verify))

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    ok_all = True
    for label, src, table in TARGETS:
        try:
            rec = backup_one(label, src, table, stamp)
            if rec.get("skipped"):
                print(f"[SKIP] {label}: {rec['skipped']}")
            else:
                print(f"[OK  ] {label}: {rec['file']}  {rec['rows']} 行  "
                      f"{rec['bytes']/1024/1024:.1f} MB  sha256={rec['sha256'][:12]}")
        except Exception as exc:  # noqa: BLE001
            ok_all = False
            print(f"[FAIL] {label}: {type(exc).__name__}: {exc}")

    if ok_all and not args.no_rotate:
        removed = rotate()
        if removed:
            print(f"轮转：删除 {len(removed)} 个过期备份（保留最近 {KEEP_DAYS} 天 + 每月首日）")
        else:
            print("轮转：无需删除")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
