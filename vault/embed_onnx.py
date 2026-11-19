"""嵌入模型兼容层 —— 业务代码只认 `encode()`，不直连 onnxruntime / tokenizers。

## 为什么要有这个文件（而不是在 memory_tools 里直接 import）

沿用 2026-10-10 定下的规范：**对某个第三方 SDK 的全部 import 与调用收进一个
`<sdk>_compat.py`，上层禁止直连**。理由在本轮又验证了一次：

- 一开始用的是 sentence-transformers（带 torch）：`import` 一项就要 **10.22s**。
- 改成 onnxruntime 后 `import` 只要 **0.65s**，编码 1 条 **13ms**，向量完全等价
  （8 条不同长度探测文本 cosine = 1.000000）。

也就是说「用哪个推理后端」这个决定，不该让 memory_tools 知道。这里收口之后，
以后要换模型、换后端、换量化精度，只改这个文件。

## 设计要点

1. **全懒加载**：模块 import 不碰 onnxruntime、不碰磁盘上的模型。MCP 进程里
   只要没人问语义，就一分钱不花。
2. **失败即降级**：模型文件缺失 / onnxruntime 没装 / 推理报错，`available()` 返回
   False、`encode()` 返回 None 并只告警一次 —— 语义通道消失，关键字检索照常工作。
   **绝不因为可选能力挂掉主链路。**
3. **BGE 口径**：`[CLS]` 池化 + L2 归一化。导出时已在 ONNX 里做了 CLS，这里只补
   归一化。用 mean pooling 会静默降低召回 —— 这类错误不报错，只会让效果变差。

## 用法

    import embed_onnx
    if embed_onnx.available():
        vecs = embed_onnx.encode(["怎么让 AI 不忘事"])   # -> list[list[float]] | None
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SEARCHED_DIRS: list[Path] = []


def _resolve_model_dir() -> Path:
    """模型目录：显式 env > 本体外一层（仓库壳的 模型/）> 本体内历史位置。

    为什么要搬出来（2026-10-19 目录收口后续）：bge-small-zh-v1.5 是 91 MB 的
    **可重建运行态**，却占了记忆库本体体积的 48%。「本体能整目录拷走」这句话里，
    91 MB 二进制不该跟着走 —— 拷到没有模型的地方，语义通道按设计降级为不可用
    （`available()` 返回 False），检索走关键字通道，功能不残缺、只是少一条腿。

    为什么默认放在 `本体/../模型/`（仓库壳层）而不是用户缓存目录：它和仓库同一个
    生命周期（一起备份、一起丢弃），路径又显式可见 —— 藏在 %LOCALAPPDATA% 里，
    换机时没人会想起来还有这么个东西。

    回落顺序保证**老位置仍然可用**：以前放在 `<vault>/.workbuddy/models/` 的，
    不改任何配置也能跑；两边都不存在时返回新位置（报错信息里给出该放哪）。
    """
    env = (os.environ.get("AI_MEMORY_MODEL_DIR") or "").strip()
    if env:
        SEARCHED_DIRS.append(Path(env))
        return Path(env)
    vault = Path(__file__).resolve().parent
    # 2026-11-18 二次结论（实测推翻前面的乐观判断）：bge-m3 本身更强
    # （语义覆盖 82%→93%、救援潜力 33%→78%），但**接进现有管线后 @5 持平 87%**，
    # 因为语义通道只是附加段、不参与主排序；而把主排序改成 RRF 融合后反而
    # 87% → 84%（p=0.26，延迟 33→58 ms）。结论：这套管线换嵌入拿不到收益，
    # 已回滚到 bge-small。bge-m3-int8 留在 `模型/` 里备用（想要时把下面这行
    # 注释掉、把 m3 那行放第一位，再跑一次 build_vectors.py --force 即可）。
    candidates = [
        vault.parent / "模型" / "bge-small-zh-v1.5",
        # vault.parent / "模型" / "bge-m3-int8",
        vault / ".workbuddy" / "models" / "bge-small-zh-v1.5",
    ]
    SEARCHED_DIRS.extend(candidates)
    for cand in candidates:
        if cand.is_dir():
            return cand
    return candidates[0]


MODEL_DIR = _resolve_model_dir()
MODEL_FILE = "model.onnx"
TOKENIZER_FILE = "tokenizer.json"
MAX_LEN = 512

# 环境开关：设 AI_MEMORY_SEMANTIC=0 可整体关掉语义通道（不改代码、不改版本）。
ENABLED = (os.environ.get("AI_MEMORY_SEMANTIC") or "1").strip().lower() not in (
    "0", "off", "false", "no",
)

_lock = threading.Lock()
_state: dict[str, Any] = {"session": None, "tokenizer": None, "dim": 0, "tried": False, "warned": False}


def _warn_once(msg: str, exc: BaseException | None = None) -> None:
    if _state["warned"]:
        return
    _state["warned"] = True
    if exc is not None:
        logger.warning("SEMANTIC-DISABLED %s: %s", msg, exc)
    else:
        logger.warning("SEMANTIC-DISABLED %s", msg)


def _load() -> bool:
    """懒加载一次；成功返回 True。任何异常都吞掉并降级，不让主链路受影响。"""
    if not ENABLED:
        _warn_once("AI_MEMORY_SEMANTIC=0")
        return False
    with _lock:
        if _state["tried"]:
            return _state["session"] is not None
        _state["tried"] = True

        model_path = MODEL_DIR / MODEL_FILE
        tok_path = MODEL_DIR / TOKENIZER_FILE
        if not model_path.exists() or not tok_path.exists():
            # 把**找过哪些位置**一并说清：换机/拷走本体时最常见的困惑是
            # 「模型到底该放哪」，只给一个路径等于让人猜。
            tried = " / ".join(str(p) for p in SEARCHED_DIRS) or str(MODEL_DIR)
            _warn_once(f"模型文件缺失，已找：{tried}（可设 AI_MEMORY_MODEL_DIR 指定）")
            return False
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer

            tok = Tokenizer.from_file(str(tok_path))
            tok.enable_truncation(max_length=MAX_LEN)
            tok.enable_padding()
            sess = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
            _state["tokenizer"] = tok
            _state["session"] = sess
            # 2026-11-18：输入/输出名不再写死。
            # bge-small 的图是 (input_ids, attention_mask, token_type_ids) → "cls"；
            # bge-m3 的图只有两路输入、输出叫 "vec"。写死就会报
            # "Invalid input name: token_type_ids" —— 换模型时最容易踩的一脚。
            _state["in_names"] = {i.name for i in sess.get_inputs()}
            _state["out_name"] = sess.get_outputs()[0].name
            _state["dim"] = int(sess.get_outputs()[0].shape[-1] or 0)
            logger.info("semantic-ready model=%s dim=%s in=%s out=%s",
                        MODEL_FILE, _state["dim"], sorted(_state["in_names"]), _state["out_name"])
            return True
        except Exception as exc:  # noqa: BLE001 —— 可选能力，绝不外抛
            _warn_once("加载失败", exc)
            _state["session"] = None
            return False


def available() -> bool:
    """语义通道是否可用。第一次调用会触发加载（约 0.7s），之后是内存判断。"""
    if _state["session"] is not None:
        return True
    return _load()


def dim() -> int:
    return int(_state["dim"]) if _state["session"] is not None else 0


def encode(texts: list[str]) -> list[list[float]] | None:
    """批量编码，返回**已 L2 归一化**的向量。语义通道不可用时返回 None（不是空列表）。

    返回 None 而不是 []，是为了让调用方能区分「没有语义能力」和「这批文本没有向量」——
    前者应该静默回退到关键字检索，后者是真异常。
    """
    if not texts:
        return []
    if not available():
        return None
    try:
        import numpy as np

        tok = _state["tokenizer"]
        sess = _state["session"]
        encs = tok.encode_batch([t or " " for t in texts])
        n = len(encs)
        # tokenizers 的 padding 默认补齐到批内最长；这里按实际最长长度建阵，避免
        # enable_padding 的固定长度设置与实际 ids 长度不一致导致 reshape 报错。
        seq = max(len(e.ids) for e in encs)
        ids = np.zeros((n, seq), dtype=np.int64)
        mask = np.zeros((n, seq), dtype=np.int64)
        types = np.zeros((n, seq), dtype=np.int64)
        for i, e in enumerate(encs):
            L = len(e.ids)
            ids[i, :L] = e.ids
            mask[i, :L] = e.attention_mask
            types[i, :L] = e.type_ids
        cls = sess.run([_state["out_name"]], {
            k: v for k, v in (
                ("input_ids", ids),
                ("attention_mask", mask),
                ("token_type_ids", types),
            ) if k in _state["in_names"]
        })[0]
        norm = np.linalg.norm(cls, axis=1, keepdims=True)
        norm[norm == 0] = 1.0
        return (cls / norm).astype(np.float32).tolist()
    except Exception as exc:  # noqa: BLE001
        _warn_once("推理失败", exc)
        return None


def status() -> dict[str, Any]:
    """给体检脚本 / 状态输出用，不触发加载。"""
    return {
        "enabled": ENABLED,
        "loaded": _state["session"] is not None,
        "modelDir": str(MODEL_DIR),
        "modelExists": (MODEL_DIR / MODEL_FILE).exists(),
        "tokenizerExists": (MODEL_DIR / TOKENIZER_FILE).exists(),
        "dim": int(_state["dim"]),
    }
