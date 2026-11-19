#!/usr/bin/env python3
"""自定义指令三处落点的一致性护栏（2026-10-20 补，之前只有文字约定、没有脚本）。

为什么必须有
------------
同一段「四条硬约束」薄壳存在**三个物理副本**：

    1. `~/.workbuddy/app/app-config.json` → `personalization.customPrompt`（WorkBuddy 注入用）
    2. `~/.codex/AGENTS.md`（Codex 注入用）
    3. 库内 `规则/规则_记忆写入格式规范.md` 的「可直接粘贴的自定义指令文本」代码块（真源）

三份手抄的东西，改一处忘两处就是**静默漂移**。本库已经吃过两次同型事故：
2026-10-13「WorkBuddy 侧只有三条、缺第 4 条」，2026-10-19「方向反过来：真源齐、镜像缺 1/2/3」。
规则文档从那时起就一直写着「由 check_custom_instruction.py 核对」，**而那个脚本根本不存在** ——
这也是漂移能发生三次的原因：约定写下来了，但没人执行。

判据（三条，全过才算过）
------------------------
- 四处文本（三处落点 + 本节）**逐字一致**（忽略末尾换行差异）；
- 长度 **≤ 1500 字符**（每轮注入都付费，这是预算不是美观）；
- 每条硬约束的小标题「1. 查 / 2. 写 / 3. 收尾 / 4. 汇报」都在（防「只剩三条」复发）。

跑法
----
    <venv>/python.exe 脚本/check_custom_instruction.py
    <venv>/python.exe 脚本/check_custom_instruction.py --show   # 顺带打印正文

退出码：0 全过；1 有不一致。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

VAULT = Path(__file__).resolve().parents[1]
LIMIT = 1500
HOME = Path.home()

APP_CONFIG = HOME / ".workbuddy" / "app" / "app-config.json"
AGENTS_MD = HOME / ".codex" / "AGENTS.md"
RULE_DOC = VAULT / "规则" / "规则_记忆写入格式规范.md"

REQUIRED_MARKS = ("1. 查", "2. 写", "3. 收尾", "4. 汇报")


def from_app_config() -> tuple[str | None, str]:
    if not APP_CONFIG.is_file():
        return None, f"文件不存在：{APP_CONFIG}"
    try:
        data = json.loads(APP_CONFIG.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return None, f"解析失败：{exc}"
    text = (data.get("personalization") or {}).get("customPrompt")
    if not isinstance(text, str):
        return None, "personalization.customPrompt 缺失或不是字符串"
    return text, ""


def from_agents_md() -> tuple[str | None, str]:
    if not AGENTS_MD.is_file():
        return None, f"文件不存在：{AGENTS_MD}"
    return AGENTS_MD.read_text(encoding="utf-8"), ""


def from_rule_doc() -> tuple[str | None, str]:
    if not RULE_DOC.is_file():
        return None, f"文件不存在：{RULE_DOC}"
    raw = RULE_DOC.read_text(encoding="utf-8")
    m = re.search(r"### 可直接粘贴的自定义指令文本.*?```text\n(.*?)\n```", raw, re.S)
    if not m:
        return None, "规则文档里找不到「可直接粘贴的自定义指令文本」代码块"
    return m.group(1), ""


def norm(text: str) -> str:
    r"""比对口径：**只比语义，不比空白**。

    为什么不比字节（2026-10-20 实测，四种差异全是编辑器加的不是人加的）：

      · WorkBuddy 输入框给每行末尾补 **2 个空格**（Markdown 硬换行），1422 字进去变 1448；
      · 粘贴时在第 3 行后**多插一个空行**；
      · 后半段几行被**多缩进 3 个空格**（疑似编辑器自动缩进）；
      · `项目_*` 里的下划线被**转义成 `项目\_*`**（Markdown 强调符转义）。

    这四样加起来让「逐字节一致」永远报红，而差异全是看不见的空白与转义 ——
    那种「永远红」的护栏最后一定被人关掉，比没有更糟。
    所以一致性按「每行 strip + 丢空行 + 还原 `\\_`」比：删词、改字、漏条、换序这些**实质漂移照样抓得住**。
    """
    text = text.replace("\\_", "_")
    lines = [ln.strip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return "\n".join(ln for ln in lines if ln)


def ui_len(text: str) -> int:
    """按「输入框会怎么存」估算长度：每行末尾补 2 个空格（实测 WorkBuddy 的行为）。"""
    lines = text.replace("\r\n", "\n").split("\n")
    return len("\n".join(ln.rstrip() + "  " for ln in lines).rstrip())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true", help="打印真源正文")
    args = ap.parse_args()

    sources = [
        ("app-config.json", from_app_config()),
        ("AGENTS.md", from_agents_md()),
        ("规则文档代码块", from_rule_doc()),
    ]

    ok = True
    texts: dict[str, str] = {}
    for name, (text, err) in sources:
        if text is None:
            print(f"[FAIL] {name}: {err}")
            ok = False
            continue
        texts[name] = norm(text)
        # 上限按**输入框的真实计数**卡（它在每行末尾补两个空格），
        # 一致性按 norm 后的语义比 —— 两个口径解决的是两件事，别混。
        n = max(len(texts[name]), ui_len(texts[name]))
        flag = "OK  " if n <= LIMIT else "FAIL"
        if n > LIMIT:
            ok = False
        print(f"[{flag}] {name}: {n} 字符（上限 {LIMIT}，余量 {LIMIT - n}）")

    if len(texts) >= 2:
        base_name, base = next(iter(texts.items()))
        for name, text in texts.items():
            if name == base_name:
                continue
            if text != base:
                ok = False
                # 指出第一处差异所在行，比「不一致」三个字有用得多
                a, b = base.split("\n"), text.split("\n")
                where = "长度不同"
                for i in range(min(len(a), len(b))):
                    if a[i] != b[i]:
                        where = f"第 {i + 1} 行起不同\n      真源: {a[i][:60]}\n      此处: {b[i][:60]}"
                        break
                print(f"[FAIL] {name} 与 {base_name} 不一致：{where}")
            else:
                print(f"[OK  ] {name} 与 {base_name} 逐字一致")

    if texts:
        base = next(iter(texts.values()))
        missing = [m for m in REQUIRED_MARKS if m not in base]
        if missing:
            ok = False
            print(f"[FAIL] 缺硬约束条目：{'、'.join(missing)}（「只剩三条」型漂移）")
        else:
            print(f"[OK  ] 四条硬约束齐全（{'、'.join(REQUIRED_MARKS)}）")

    print()
    if args.show and texts:
        print(next(iter(texts.values())))
        print()

    print("结果：PASS" if ok else "结果：FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
