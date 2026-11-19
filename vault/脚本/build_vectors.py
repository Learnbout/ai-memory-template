"""把全库笔记的句向量写进 index.sqlite3 的 note_vecs 表（可反复跑、只补变化）。

## 为什么存在这个脚本

语义检索需要「文档侧向量」与「查询侧向量」。文档侧是固定的 119 篇，没必要时每次
检索都重算 —— 预计算一次、按 updated 增量刷新即可。查询侧才用 embed_onnx 现算。

## 为什么用普通表 + 暴力余弦，而不是 sqlite-vec 的 vec0

`vec0` 是 ANN 索引，收益在万级以上向量时出现；本库 119 篇、512 维，
暴力余弦一次约 2ms（实测同量级），用 vec0 只会增加一层虚拟表依赖和调试成本。
**能算得完就不要建索引** —— 这也是 09-17 定「只允许 sqlite-vec 一个扩展」时
没把它当成必选项的原因。等库涨到几千篇再换。

## 为什么落 index.sqlite3 而不是新库

`index.sqlite3` 的定位是「可从 md 一条命令重建的投影」，向量属于同类东西：
丢了可以重算。放同一个库，重建流程天然带着它，不用再维护第二套备份策略。

跑法：
  <venv>/python.exe 脚本/build_vectors.py            # 增量：只算新增/变化的
  <venv>/python.exe 脚本/build_vectors.py --force    # 全量重算
"""

from __future__ import annotations

import argparse
import sqlite3
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import db_paths  # 数据库位置唯一入口（2026-10-19 收口到「数据库/」）

DB = db_paths.resolve("index.sqlite3")

# 2026-11-18 修：模型名原来是写死的 "bge-small-zh-v1.5"。换 bge-m3-int8 后这个常量没变，
# 于是 `prev[0] != MODEL_TAG` 永远为假 —— 脚本打印「已是最新，无需重算」，
# 而 note_vecs 里躺着 244 条 512 维的旧向量。**换模型时最危险的一脚是静默跳过。**
# 现在从实际加载的模型目录名取 tag，换目录就自动全量重算。
def _model_tag() -> str:
    try:
        import embed_onnx
        return Path(embed_onnx.MODEL_DIR).name or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


MODEL_TAG = _model_tag()

DDL = """
CREATE TABLE IF NOT EXISTS note_vecs (
    rel_path TEXT PRIMARY KEY,
    model    TEXT NOT NULL,
    dim      INTEGER NOT NULL,
    updated  TEXT,
    vec      BLOB NOT NULL
)
"""


def pack(vec: list[float]) -> bytes:
    return struct.pack("<%df" % len(vec), *vec)


def unpack(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack("<%df" % n, blob))


def doc_text(title: str, summary: str) -> str:
    """文档侧文本口径：**只用标题**。

    这是刻意的：t7c 量过「只嵌标题 / 标题+摘要 / 标题+摘要+正文前400字」三种，
    20 条 query 上 top5 都是 15/20，没有一组更好。既然没量出差异，就选最省、
    最不会被正文噪声稀释的一种 —— **不发布没量过的配置**。
    """
    return title.strip() or " "


def connect(db: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db or DB))
    conn.execute(DDL)
    conn.commit()
    return conn


def refresh_one(rel_path: str, title: str, updated: str | None, db: Path | None = None) -> bool:
    """刷新单篇笔记的向量（写入路径调用）。永不抛异常 —— 语义是可选能力。

    为什么需要它：`memory_write` 写完若不同步向量，新笔记在语义通道里会「消失」，
    直到下一次全量重建。这与 2026-10-19 修掉的「写入不刷全文索引」是**同一类故障**
    （写入成功但搜不到），不能再犯第二次。
    """
    try:
        import embed_onnx

        if not embed_onnx.available():
            return False
        vecs = embed_onnx.encode([doc_text(title or "", "")])
        if not vecs:
            return False
        v = vecs[0]
        conn = connect(db)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO note_vecs(rel_path, model, dim, updated, vec) VALUES (?,?,?,?,?)",
                (rel_path, MODEL_TAG, len(v), updated, pack(v)),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="全量重算，忽略已有向量")
    args = ap.parse_args()

    import embed_onnx

    if not embed_onnx.available():
        st = embed_onnx.status()
        missing = not st.get("modelExists") or not st.get("tokenizerExists")
        if missing:
            # 模型是**可选依赖**（2026-10-19 外置到仓库壳层 `模型/` 之后尤其如此）：
            # 缺失时语义通道按设计降级，检索仍走关键字通道，功能不残缺。
            # 所以这里必须返回 0 —— 否则「把本体拷到没有模型的机器上」会让
            # `sync_all` 永远 rc=1，一条固定警报会把真正的失败淹没掉。
            # 出声但不失败：详情里给出它找过的目录，换机时能直接知道该往哪放。
            print("语义通道不可用（模型缺失，按设计跳过建向量；关键字通道不受影响）："
                  f"{st.get('modelDir')}")
            return 0
        # 模型在、却加载不起来（onnxruntime 缺失 / 文件损坏）—— 这才是异常
        print("语义通道不可用，放弃建向量：", st)
        return 2

    conn = sqlite3.connect(str(DB))
    conn.execute(DDL)
    conn.commit()

    rows = conn.execute("SELECT rel_path, title, summary, updated FROM notes ORDER BY rel_path").fetchall()
    have = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT rel_path, model, updated FROM note_vecs")}

    todo = []
    for rel, title, summary, updated in rows:
        prev = have.get(rel)
        if args.force or prev is None or prev[0] != MODEL_TAG or prev[1] != updated:
            todo.append((rel, doc_text(title or "", summary or ""), updated))

    print(f"库内 {len(rows)} 篇 / 已有向量 {len(have)} 条 / 待更新 {len(todo)} 条")
    if not todo:
        print("已是最新，无需重算。")
        conn.close()
        return 0

    t0 = time.perf_counter()
    vecs = embed_onnx.encode([t for _, t, _ in todo])
    if vecs is None:
        print("编码失败，放弃（不改动已有向量）")
        conn.close()
        return 2
    dt = time.perf_counter() - t0

    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    for (rel, _, updated), v in zip(todo, vecs):
        conn.execute(
            "INSERT OR REPLACE INTO note_vecs(rel_path, model, dim, updated, vec) VALUES (?,?,?,?,?)",
            (rel, MODEL_TAG, len(v), updated, pack(v)),
        )
    conn.commit()

    # 清掉已从库里消失的笔记的残留向量（否则会检索到不存在的笔记）
    alive = {r[0] for r in rows}
    stale = [k for k in have if k not in alive]
    for rel in stale:
        conn.execute("DELETE FROM note_vecs WHERE rel_path = ?", (rel,))
    conn.commit()

    total = conn.execute("SELECT count(*) FROM note_vecs").fetchone()[0]
    dim = conn.execute("SELECT dim FROM note_vecs LIMIT 1").fetchone()
    conn.close()

    print(f"编码 {len(todo)} 条，耗时 {dt:.2f}s（{len(todo)/max(dt,1e-9):.1f} 条/秒）")
    print(f"清理失效向量 {len(stale)} 条")
    print(f"note_vecs 现有 {total} 条 / dim={dim[0] if dim else 0} / {now}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
