"""贴纸清单（阶段 8）—— 后端是唯一事实源，前端只按 id 画。

## 为什么不让前端自带一套

贴纸 id 会写进消息 payload（存库、要长期可读）。如果清单只在前端，前端一改版
就会出现「历史消息里的贴纸画不出来」。清单放后端：id 由后端校验，前端拿到的是
同一份列表，加贴纸只改这一个文件。

## 为什么不用图片文件

贴纸是纯 SVG 图形，前端内联渲染：零请求、零体积、深浅色随主题走，也不怕
「服务器上的图片目录被 sync 覆盖」。真要加位图贴纸再开上传通道。
"""
from __future__ import annotations

from typing import Any

# id → (名字, 分类)。分类用于面板分组。
STICKERS: list[tuple[str, str, str]] = [
    ("whale-hi", "嗨", "表情"),
    ("whale-ok", "好的", "表情"),
    ("whale-thanks", "谢谢", "表情"),
    ("whale-love", "比心", "表情"),
    ("whale-cry", "哭了", "表情"),
    ("whale-angry", "生气", "表情"),
    ("whale-doubt", "疑惑", "表情"),
    ("whale-think", "思考", "状态"),
    ("whale-sleep", "睡了", "状态"),
    ("whale-eat", "干饭", "状态"),
    ("whale-fire", "燃起来了", "状态"),
    ("whale-wave", "再见", "状态"),
]

STICKER_IDS = {s[0] for s in STICKERS}

# 消息里可以点回应的快捷表情（前端面板第一排）
QUICK_EMOJI = ["👍", "😂", "🎉", "👀", "🔥", "🙏"]


def catalog() -> dict[str, Any]:
    return {
        "stickers": [{"id": i, "name": n, "group": g} for i, n, g in STICKERS],
        "quickEmoji": QUICK_EMOJI,
    }


def is_sticker(sticker_id: str) -> bool:
    return (sticker_id or "") in STICKER_IDS
