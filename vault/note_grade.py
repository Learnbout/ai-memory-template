"""笔记分级（阶段 6，2026-10-23）。

为什么单独一个文件：分级要在**三个地方**用同一套口径 —— 写入时给新笔记定级、
迁移时给存量笔记补级、查询时判可见性。规则写在三个地方必然漂移，所以收在这里，
纯函数、不 import 任何本项目模块（谁都能 import 它，不会成环）。

## 四档

| 级别 | 含义 | 典型内容 |
|---|---|---|
| S | 机密 / 危险 | 明文凭据、令牌、私钥、账号口令表 |
| A | 重要 | 规则、决策、索引、项目状态、路线图、人设与偏好 |
| B | 普通 | 项目进展、知识沉淀、日常记录 |
| C | 草稿 | tags 含 draft、标题以「草稿_」开头 |

## 为什么凭据判定要写这么窄

第一版用宽正则（命中「密码」「token」就算 S），实测 142 篇里有 2 篇被误判 ——
它们是在**讨论**密码规范（《规则_Git版本控制规范》：“口令：明文密码、设备 root 口令”），
正文里一个真凭据都没有。判级的目的是**防泄漏**，不是防提词，所以只认「值」的形态：
冒号后面必须紧跟一段像凭据的字符（≥6 位字母数字混排）、PEM 私钥头、JWT 三段、
或 GitHub / OpenAI 风格的固定前缀。宁漏勿滥 —— 漏掉的靠人工复核补。
"""
from __future__ import annotations

import re

# 级别 → 敏感度排序（数值越大越敏感）。可见性判断只有这一条规则：
# 笔记等级 <= 成员被授予的最高等级，才看得到。
GRADE_RANK = {"C": 1, "B": 2, "A": 3, "S": 4}
GRADE_DESC = {
    "S": "机密/危险（含明文凭据材料）",
    "A": "重要（规则/决策/索引/项目状态）",
    "B": "普通（项目与知识笔记）",
    "C": "草稿（draft）",
}
DEFAULT_GRADE = "B"

# 只认「像凭据的值」，不认「提到凭据的词」—— 见模块文档里的误判记录。
_CRED_PATTERNS = (
    re.compile(r"(?:密码|口令|密钥)\s*[:：=]\s*[A-Za-z0-9!@#$%^&*_\-]{6,}"),
    re.compile(r"(?:password|passwd|pwd|secret|token)\s*[:=]\s*[\"']?[A-Za-z0-9!@#$%^&*_\-]{6,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\."),
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
)

# type 落在这些值上的笔记一律算重要
_IMPORTANT_TYPES = {"rule", "decision", "index", "preference"}
# 标题里出现这些词也算重要（历史笔记的 type 不一定可靠，标题更稳）
_IMPORTANT_TITLE_WORDS = ("路线图", "项目状态", "记忆索引", "路由索引", "脚本索引", "用户画像", "AI人格")


def rank(grade: str | None) -> int:
    return GRADE_RANK.get(str(grade or DEFAULT_GRADE).upper(), GRADE_RANK[DEFAULT_GRADE])


def has_credential(text: str) -> bool:
    if not text:
        return False
    return any(p.search(text) for p in _CRED_PATTERNS)


def grade_of(title: str, body: str = "", note_type: str | None = None,
             tags: list[str] | None = None) -> str:
    """给一篇笔记定级。顺序有意为之：S 最高优先，草稿次之（草稿里含凭据仍是 S）。"""
    head = str(title or "")
    tags = [str(t).strip().lower() for t in (tags or [])]

    if has_credential(head) or has_credential(body or ""):
        return "S"

    if "draft" in tags or head.startswith("草稿_"):
        return "C"

    if str(note_type or "").lower() in _IMPORTANT_TYPES:
        return "A"
    if any(w in head for w in _IMPORTANT_TITLE_WORDS):
        return "A"

    return DEFAULT_GRADE
