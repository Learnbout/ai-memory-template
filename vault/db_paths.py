"""数据库文件位置的**唯一解析入口**（2026-10-19 收口）。

为什么单独一个模块
------------------
「库为源」之后 `index.sqlite3` 是记忆库本体，不该再混在 `.workbuddy/` 里
（那个目录还放着日志、bench JSON、hook 状态、嵌入模型 —— 全是可丢的运行态）。
现在两个库统一放 `记忆库根/数据库/`。

单独成文件而不是塞进 `memory_store`，是为了让**校验脚本也能用同一套解析**：
`check_integrity.py` 刻意不 import `memory_store`（那个模块导入期会做路径检查、
未来还可能惰性建库，校验脚本必须零副作用）。本模块**纯计算、不建目录、不写盘**。

兼容策略（重要）
----------------
`resolve()` 先看新位置，再看旧位置 `.workbuddy/<name>`，都没有才返回新位置。
这条回落的唯一目的：**防止「代码改了、文件忘了搬」变成「写入走新库、读取走老库」**
—— 那会表现为「刚写的笔记搜不到」，是最难排查的一类故障。
真出现回落时打一条 WARNING，让人看得见。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("ai-memory")

DB_DIR_NAME = "数据库"
_LEGACY_DIR_NAME = ".workbuddy"


def vault_dir() -> Path:
    return Path(os.environ.get("AI_MEMORY_DIR") or Path(__file__).resolve().parent).resolve()


def db_dir() -> Path:
    """数据库目录（新位置）。不创建目录 —— 创建是写路径的责任。"""
    return vault_dir() / DB_DIR_NAME


def legacy_dir() -> Path:
    return vault_dir() / _LEGACY_DIR_NAME


def resolve(name: str) -> Path:
    """解析某个库文件的真实路径：优先新位置，其次旧位置（告警），否则新位置。"""
    new = db_dir() / name
    old = legacy_dir() / name
    if new.exists():
        return new
    if old.exists():
        logger.warning(
            "数据库文件仍在旧位置 %s，建议搬到 %s（本次按旧位置读，避免读写分裂）", old, new
        )
        return old
    return new


def index_db() -> Path:
    return resolve("index.sqlite3")


def versions_db() -> Path:
    return resolve("versions.sqlite3")


def audit_db() -> Path:
    """审计库（阶段 7）：**独立文件，不放进 index.sqlite3**。

    三个理由，缺一都不成立：

    1. `index.sqlite3` 会被 `sync_push.py` 整文件覆盖（服务器是只读镜像），
       审计记录混在里头，一次推送就全没了 —— 偏偏「事后追查」最需要的那份
       数据最容易被覆盖。独立文件天然对它免疫。
    2. 服务器 index 上有 9 个只读触发器（拦 notes/links/meta 写）。审计表
       也挂进去只会让「哪些写被允许」的判定变复杂；独立库不参与那套闸。
    3. 审计是**只追加**的，写入模式与业务库（有版本、会更新、会被覆盖）
       根本不同，同库会被业务库的备份/回滚语义带偏。

    不做旧位置回落：审计库是阶段 7 新建的，不存在历史文件需要兼容。
    """
    return db_dir() / "audit.sqlite3"


# ── 连接调优（2026-11-16 新增）────────────────────────────────────────────
# 这几个是**连接级** PRAGMA，不写进文件头，所以每次 connect 都要重设一遍。
# 单一实现放在这里，是因为 6 个热路径连接（memory_tools / vault_db×2 /
# web_store×2 / db_init / memory_versions）各写一份参数必然漂移，而参数漂移
# 不会报错、只会让「同一份代码在不同进程里表现不同」。
#
# 刻意坚持的：
#   · synchronous 保持 FULL，不降 NORMAL —— 记忆库是唯一事实源，
#     宁可每次写多一次 fsync，也不要在断电时丢掉刚写下的那条笔记。
#   · auto_vacuum 不在这里设（要设必须先 VACUUM 才生效，属一次性运维动作）。
_CACHE_KIB = -65536      # 64 MiB：全库（6.2MB）都能常驻
_MMAP_BYTES = 268435456  # 256 MiB


def tune(conn):
    """给连接设缓存 / mmap / 临时表参数。幂等，失败静默。

    为什么要它：改之前 cache_size 只有 2MB（默认 2000 页），而 notes.body_md
    加起来约 4MB —— 最热的读路径每轮都在翻磁盘。mmap_size=0 同理，白白放弃
    了内存映射读。
    """
    import sqlite3

    for sql in (
        f"PRAGMA cache_size={_CACHE_KIB}",
        f"PRAGMA mmap_size={_MMAP_BYTES}",
        "PRAGMA temp_store=MEMORY",
        # 2026-11-16：库里从来没有 sqlite_stat1（规划器等于闭眼挑索引）。
        # 0x10000 = 本连接长期存在，允许它做完整分析；0x2 = 只在「该分析了」时才动手，
        # 统计新鲜时这条语句几乎零开销（官方推荐的长连接写法）。
        "PRAGMA optimize=0x10002",
    ):
        try:
            conn.execute(sql)
        except sqlite3.Error:
            pass  # 老构建不支持 mmap / optimize 也不该拖垮业务
    return conn
