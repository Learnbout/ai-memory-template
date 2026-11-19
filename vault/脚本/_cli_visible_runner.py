"""「弹窗显示」用的可见控制台包装器（queue_watch.py 的 --show-cli / 触发文件专用）。

为什么需要它
------------
监听器本体跑在 pythonw.exe（无控制台），CLI 输出被 PIPE 收走 —— 主人什么都看不见。
只给子进程加 CREATE_NEW_CONSOLE 也不够：stdout 一旦接回管道，窗口里就没有输出；
若让 CLI 直接继承窗口，输出就丢了，队列的 reason 判定（auth/env/silent）与 history 全瞎。

本包装器一次解决三件事（2026-10-13 二版）
----------------------------------------
1. **窗口摆位**：自己在**新控制台**里运行（调用方用 CREATE_NEW_CONSOLE 拉起），
   再按 `layout` 摆位。默认 **hacker**：窗口只占屏幕约 1/5～1/7，散落在九个
   「板块」（上/下/左/右/中/四角）上、每次随机微偏、允许适当重叠 —— 主人要的
   「桌面上漂着几个小终端」观感；`grid` 是旧的等分网格铺满，并发多路完全不重叠。
2. **真流式 + 人话**：CLI 以 `--output-format stream-json` 运行。该模式下 CLI
   **每产生一个事件就写一行**，所以是真流式（不再是「攒完最后一次性蹦出来」）。
   包装器逐行解析 JSON 事件并**实时渲染**成人类可读文本：会话就绪 / 思考 / 工具调用 /
   工具返回 / 正文 / 完成。屏幕带 ANSI 颜色（需启用 VT），tee 文件落**无颜色**纯文本。
   **思考（thinking）全量上屏**：不再截成一行 200 字，整块逐行刷出来（带每行极短
   延时制造滚动感）—— CLI 的 thinking 动辄上万字，这才是「黑客刷屏」的正体。
3. **队列不掉线**：队列仍从 tee 文件取回文本，reason 判定语义与管道路径一致；
   原始事件另存 `.jsonl`，排查时能看到机器真相。

**旧「黑屏」根因**（2026-10-13 实测）：默认 text 模式下 node 往管道写是块缓冲，
165 秒的任务里一个字都不输出，最后 5 秒停留时才整段蹦出来 —— 看上去就是黑窗。

若 cmd 里没有 `--output-format stream-json`，则退化为「原样透传」＝旧版行为。

spec.json 字段
--------------
    cmd          list[str]   CLI 命令行（必填）
    env          dict|None   环境变量
    cwd          str|None    工作目录
    tee          str        渲染后纯文本落盘路径（必填）
    status       str        运行状态 JSON 落盘路径（必填）
    hold_s       float      跑完停留秒数（默认 5）
    timeout_s    float      单任务超时秒数（默认 900）
    label        str        窗口标题/抬头用的任务标识（可选）
    slot         int        本窗口的槽位号（hacker 下决定取哪个板块；默认 0）
    total_slots  int        并发槽位总数（默认 1）
    layout       str        窗口布局：hacker（默认，小窗随机散布）| grid（等分网格）
    events       str        原始 stream-json 事件落盘路径（可选）
"""

from __future__ import annotations

import ctypes
import json
import math
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

# --------------------------------------------------------------------------
# ANSI 颜色（Windows 10+ 控制台需先启用 VT 处理才会解释）
# --------------------------------------------------------------------------
RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[90m"
RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
BLUE = "\x1b[34m"
MAGENTA = "\x1b[35m"
CYAN = "\x1b[36m"
WHITE = "\x1b[97m"

_KIND_COLOR = {
    "head": BOLD + WHITE,
    "dim": DIM,
    "think": MAGENTA,
    "tool": CYAN,
    "toolret": DIM,
    "text": WHITE,
    "ok": GREEN,
    "warn": YELLOW,
    "err": RED,
    "info": BLUE,
}


# --------------------------------------------------------------------------
# 控制台直写（三版修复；这是「窗口全黑」的根治点）
# --------------------------------------------------------------------------
# 为什么不能用 print / sys.stdout：
#   queue_watch._spawn_visible 用 CREATE_NEW_CONSOLE 起本进程，同时把
#   stdout / stderr 设成 subprocess.DEVNULL —— 它不要 runner 的输出，
#   输出改从 tee 文件读回。于是 print 全部落进黑洞：**窗口是开的、进程在跑、
#   屏幕却一片纯黑**（2026-10-13 三版实测：队列日志写着「输出 1471 字」，
#   主人那边却「完全黑什么都不显示」）。
# 修法：自己 CreateFileW("CONOUT$") 拿到本进程那个控制台的输出句柄，
#   用 WriteConsoleW 直写 —— 与标准流无关，DEVNULL 也拦不住。
_CONOUT: int | None = None


def _open_conout() -> bool:
    global _CONOUT
    try:
        k = ctypes.windll.kernel32
        k.CreateFileW.restype = ctypes.c_void_p
        k.CreateFileW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ]
        # 必须带 GENERIC_READ：GetConsoleMode / SetConsoleMode（开 VT 用）要求读权限，
        # 只用 GENERIC_WRITE 开出来的句柄会让 GetConsoleMode 直接失败（2026-10-13 自证实测）。
        h = k.CreateFileW("CONOUT$", 0xC0000000, 0x00000003, None, 3, 0, None)
        if not h or h == ctypes.c_void_p(-1).value:
            h = k.CreateFileW("CONOUT$", 0x40000000, 0x00000003, None, 3, 0, None)
        if h and h != ctypes.c_void_p(-1).value:
            _CONOUT = h
            return True
    except Exception:
        pass
    return False


def _console_write(text: str) -> None:
    """写进可见窗口；拿不到控制台句柄时才退回 sys.stdout（DEVNULL，等于丢）。"""
    if _CONOUT:
        try:
            n = ctypes.c_uint32(0)
            ctypes.windll.kernel32.WriteConsoleW(
                ctypes.c_void_p(_CONOUT), text, len(text), ctypes.byref(n), None
            )
            return
        except Exception:
            pass
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except Exception:
        pass


def _say(text: str = "") -> None:
    _console_write(text + "\n")


def _enable_utf8_console() -> None:
    """把控制台切到 UTF-8，否则中文输出在 cp936 控制台下是乱码或直接报错。"""
    try:
        subprocess.run("chcp 65001 >nul", shell=True, check=False)
    except Exception:
        pass
    for stream in ("stdout",):
        try:
            getattr(sys, stream).reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _enable_vt() -> bool:
    """启用 ENABLE_VIRTUAL_TERMINAL_PROCESSING，否则 ANSI 转义码会原样显示成一堆怪符号。

    必须对本进程自己的 **CONOUT$ 句柄**设置：标准 STD_OUTPUT_HANDLE 已被调用方
    接成 DEVNULL，对它 GetConsoleMode 必然失败（三版修复的连带修正）。
    """
    if not _CONOUT:
        return False
    try:
        k = ctypes.windll.kernel32
        h = ctypes.c_void_p(_CONOUT)
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        # 0x0004 = ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return bool(k.SetConsoleMode(h, mode.value | 0x0004))
    except Exception:
        return False


def _set_title(text: str) -> None:
    try:
        ctypes.windll.kernel32.SetConsoleTitleW(text)
    except Exception:
        pass


def _find_own_window(title: str) -> int:
    """按标题找**本任务那个可见的顶层窗口**。

    默认终端是 Windows Terminal 时（本机实测就是），`GetConsoleWindow()` 返回的是
    隐藏的 ConPTY 伪窗口 —— 摆它是白摆：诊断里明明写着「实际=(0,0) 960x1032」，
    屏幕上两个窗口却纹丝不动地重叠着。真身是 WindowsTerminal.exe 的窗口
    （类名 `CASCADIA_HOSTING_WINDOW_CLASS`），标题与 SetConsoleTitleW 设的一致，
    只能按标题从所有顶层窗口里捞。
    """
    try:
        import win32gui
    except Exception:
        return 0

    hits: list[int] = []

    def cb(hwnd, _):
        try:
            if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd) == title:
                hits.append(hwnd)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        return 0
    return hits[-1] if hits else 0  # 多个命中取最后（最新）那个


# 「黑客布局」参数（四版，2026-10-13）。主人的原话：不要全屏、约 1/5 还要小一点、
# 位置不固定（上下左右中间板块都可以）、可以适当重叠。做法：
#   ① 尺寸取工作区的 32%~38% 宽 × 34%~42% 高（面积约 1/7，比 1/5 更小）；
#   ② 九个「板块」锚点按**分散优先**排序，slot 顺次取 —— 一批并发必然落在不同板块；
#   ③ 每次再加 ±5% 的随机抖动 —— 同一 slot 每次位置都不同（"位置不固定"）；
#   ④ 锚点间距与窗口尺寸留了交叠余量 —— 相邻板块的窗口边缘会"适当重叠"。
HACKER_ANCHORS = (
    (0.32, 0.28),  # 中
    (0.03, 0.03),  # 左上
    (0.62, 0.60),  # 右下
    (0.62, 0.03),  # 右上
    (0.03, 0.60),  # 左下
    (0.32, 0.02),  # 上
    (0.02, 0.28),  # 左
    (0.63, 0.28),  # 右
    (0.32, 0.58),  # 下
)
HACKER_W_FRAC = (0.32, 0.38)   # 窗口宽 / 工作区宽
HACKER_H_FRAC = (0.34, 0.42)   # 窗口高 / 工作区高
HACKER_JITTER = 0.05           # 锚点随机抖动幅度（占工作区比例）


def _place_window(
    title: str,
    slot: int,
    total: int,
    debug: Path | None = None,
    layout: str = "hacker",
) -> None:
    """把本任务的控制台窗口摆到合适位置。

    layout="hacker"（默认）：小窗随机散布在九个板块上，允许适当重叠。
    layout="grid"：按 slot/total 等分网格铺满，并发多路完全不重叠（旧行为，备用）。

    两件必须做对的事（2026-10-13 两次实测换来的教训）：
    1. **找对窗口** —— Windows Terminal 下 `GetConsoleWindow()` 是隐藏伪窗口，
       摆它无效；必须按标题找 WT 的真窗口（见 `_find_own_window`）。
    2. **等标题同步** —— WT 的窗口标题不是瞬间就绪的，要边轮询边等，出现即摆位。
    """
    def _note(msg: str) -> None:
        if debug is None:
            return
        try:
            with debug.open("a", encoding="utf-8") as fh:
                fh.write(msg + "\n")
        except Exception:
            pass

    # place.txt 是追加写 —— 同一 stem 反复跑（排障时常见）会累积多轮记录，
    # 一眼看不出"这次"摆到哪。开跑前先清掉，保证文件里只有本轮。
    if debug is not None:
        try:
            debug.unlink()
        except OSError:
            pass

    try:
        import win32api
        import win32con
        import win32console
        import win32gui
    except Exception as exc:
        _note(f"[place] pywin32 不可用，放弃摆位：{exc}")
        return  # pywin32 不在就放弃摆位，不影响功能

    hwnd = 0
    waited = 0.0
    for _ in range(12):  # 最多等约 4.8 秒，等 WT 把窗口标题同步上来
        hwnd = _find_own_window(title)
        if hwnd:
            break
        time.sleep(0.4)
        waited += 0.4
    if hwnd:
        _note(f"[place] 按标题命中窗口 hwnd={hwnd}（等待 {waited:.1f}s）")
    else:
        try:
            hwnd = win32console.GetConsoleWindow()  # 传统 conhost 场景的退回路径
            _note(f"[place] 按标题未命中，退回 GetConsoleWindow={hwnd}")
        except Exception as exc:
            _note(f"[place] GetConsoleWindow 失败：{exc}")
            return
    if not hwnd:
        _note("[place] 没拿到可用窗口句柄，放弃")
        return

    try:
        info = win32api.GetMonitorInfo(win32api.MonitorFromWindow(hwnd))
        left, top, right, bottom = info["Work"]  # 已排除任务栏
    except Exception as exc:
        _note(f"[place] 取工作区失败，退回全屏：{exc}")
        left, top = 0, 0
        right, bottom = win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)

    ww, hh = right - left, bottom - top
    total = max(1, int(total))
    if str(layout).lower() == "grid":
        cols = 1 if total == 1 else (2 if total <= 4 else 3)
        rows = math.ceil(total / cols)
        w = max(320, ww // cols)
        h = max(240, hh // rows)
        x = left + (slot % cols) * w
        y = top + (slot // cols) * h
        _note(f"[place] layout=grid slot={slot}/{total} cols={cols} rows={rows}")
    else:
        # hacker：小窗 + 板块锚点 + 随机抖动。随机种子混入纳秒时间 —— 同一 slot
        # 每次跑都换位置（"位置不固定"）；同一批不同 slot 因锚点不同而自然错开。
        rnd = random.Random(f"{slot}:{total}:{time.time_ns()}")
        w = max(400, int(ww * rnd.uniform(*HACKER_W_FRAC)))
        h = max(240, int(hh * rnd.uniform(*HACKER_H_FRAC)))
        ax, ay = HACKER_ANCHORS[slot % len(HACKER_ANCHORS)]
        jx = rnd.uniform(-HACKER_JITTER, HACKER_JITTER)
        jy = rnd.uniform(-HACKER_JITTER, HACKER_JITTER)
        x = int(left + ww * (ax + jx))
        y = int(top + hh * (ay + jy))
        # 夹进工作区，别让窗口跑到屏幕外
        x = max(left, min(x, right - w))
        y = max(top, min(y, bottom - h))
        _note(
            f"[place] layout=hacker slot={slot}/{total} 取板块#{slot % len(HACKER_ANCHORS)}"
            f" 抖动=({jx:+.3f},{jy:+.3f}) 尺寸={w}x{h}（占工作区 {w / ww:.2f}x{h / hh:.2f}）"
        )

    def _apply() -> None:
        try:
            win32gui.SetWindowPos(
                hwnd, 0, x, y, w, h,
                win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW,
            )
            _note(f"[place] 目标=({x},{y}) {w}x{h}  实际={win32gui.GetWindowRect(hwnd)}")
        except Exception as exc:
            _note(f"[place] SetWindowPos 失败：{exc}")

    _note(f"[place] slot={slot}/{total} 工作区=({left},{top},{right},{bottom})")
    for delay in (0.0, 0.5, 1.2):  # 首次摆位 + 系统级联 / WT 重排之后再各钉一次
        if delay:
            time.sleep(delay)
        _apply()


# --------------------------------------------------------------------------
# stream-json 事件 → 人话
# --------------------------------------------------------------------------
def _clip(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _brief_tool(name: str, inp: dict) -> str:
    """把工具入参压成一行摘要，屏幕上只看关键定位信息。"""
    if not isinstance(inp, dict):
        return _clip(inp, 90)
    for key in ("file_path", "notebook_path", "pattern", "query", "command", "url", "prompt", "path"):
        if key in inp:
            return f"{key}={_clip(inp[key], 80)}"
    keys = ", ".join(list(inp)[:4])
    return f"({keys})" if keys else ""


# 思考块渲染上限（四版）：CLI 的 thinking 动辄上万字（实测单任务 4 万+字 / 14 块），
# 全刷正是主人要的"黑客感"，但得有兜底 —— 单块超过这个行数就截断并注明，
# 防止某个病态长块把窗口刷成几万行。
_THINK_MAX_LINES = 800
# 思考滚动动画（2026-10-13 v2，自适应）：2ms 固定延时肉眼等于"啪一下全出来"，
# 没有黑客那种逐行滚动感。改成整个任务共用一份动画预算，每块先数行数再摊：
#   短思考（≤60 行）→ 单行吃满上限，打字机感拉满；
#   长思考（几百行）→ 预算均摊，行越密滚得越快，观感像终端疯狂输出；
#   无论多少行，动画总时长不超预算，不吃任务超时（延时在 CLI 之外换算）。
_THINK_LINE_DELAY_MAX = 0.09   # 单行上限：短思考时的打字机节奏
_THINK_ANIM_BUDGET_S = 6.0     # 整个任务思考动画总预算（秒）
# 思考标签的显示宽度：'[思考] ' = 6 个全角宽 + 1 空格，续行用等宽空白对齐。
_THINK_TAG = "[思考] "
_THINK_PAD = " " * 7


def _render(ev: dict) -> list[tuple[str, str, bool]]:
    """把一个事件渲染成若干 (文本, 类别, 是否带时间戳)。

    类别决定屏幕颜色，文本本身不带颜色码。第三位 show_ts 让"某一块的续行"能免掉
    每行一个 [HH:MM:SS] 前缀 —— 思考块动辄几十行，每行都顶时间戳会把窗口挤爆。
    """
    if not isinstance(ev, dict):
        return []
    etype = ev.get("type")

    if etype == "system":
        if ev.get("subtype") != "init":
            return []
        model = ev.get("model") or "?"
        mcps = ev.get("mcp_servers") or []
        ok = [m.get("name") for m in mcps if isinstance(m, dict) and m.get("status") == "connected"]
        bad = [m.get("name") for m in mcps if isinstance(m, dict) and m.get("status") != "connected"]
        line = f"会话就绪 · 模型 {model} · MCP 已连接 {len(ok)} 个"
        if ok:
            line += f"（{', '.join(str(x) for x in ok)}）"
        if bad:
            line += f" · 未就绪 {', '.join(str(x) for x in bad)}"
        return [(line, "info", True)]

    if etype == "assistant":
        out: list[tuple[str, str, bool]] = []
        content = (ev.get("message") or {}).get("content") or []
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for blk in content:
            if not isinstance(blk, dict):
                continue
            btype = blk.get("type")
            if btype == "text":
                txt = (blk.get("text") or "").rstrip()
                if txt.strip():
                    for ln in txt.splitlines():
                        out.append((ln, "text", True))
            elif btype == "thinking":
                txt = (blk.get("thinking") or "").strip()
                if txt:
                    lines = [ln.rstrip() for ln in txt.splitlines()]
                    lines = [ln for ln in lines if ln.strip()]
                    dropped = max(0, len(lines) - _THINK_MAX_LINES)
                    lines = lines[:_THINK_MAX_LINES]
                    for i, ln in enumerate(lines):
                        # 首行挂 [思考] 标签，续行用等宽空白对齐
                        out.append((f"{_THINK_TAG if i == 0 else _THINK_PAD}{ln}", "think", i == 0))
                    if dropped:
                        out.append((f"   …（思考块过长，省略后续 {dropped} 行）", "dim", False))
            elif btype == "tool_use":
                out.append((f"[工具] {blk.get('name')}  {_brief_tool(blk.get('name') or '', blk.get('input'))}", "tool", True))
        return out

    if etype == "user":
        out = []
        content = (ev.get("message") or {}).get("content") or []
        if isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "tool_result":
                    body = blk.get("content")
                    n = len(json.dumps(body, ensure_ascii=False)) if not isinstance(body, str) else len(body)
                    flag = "  （错误）" if blk.get("is_error") else ""
                    out.append((f"[返回] {n} 字{flag}", "toolret", True))
        return out

    if etype == "result":
        dur = float(ev.get("duration_ms") or 0) / 1000.0
        cost = ev.get("total_cost_usd")
        errs = [str(e) for e in (ev.get("errors") or [])]
        if ev.get("is_error") or errs:
            msg = errs[0] if errs else str(ev.get("subtype") or "unknown")
            return [(f"[失败] {msg}", "err", True), (f"耗时 {dur:.1f}s", "dim", True)]
        tail = f"花费 ${cost}" if cost not in (None, 0) else ""
        return [(f"[完成] 耗时 {dur:.1f}s  {tail}".rstrip(), "ok", True)]

    return []


def main() -> int:
    _enable_utf8_console()
    has_console = _open_conout()
    vt_ok = _enable_vt()

    if len(sys.argv) < 2:
        _say("[runner] 缺少 spec.json 参数")
        return 2

    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    cmd = list(spec["cmd"])
    env = spec.get("env") or None
    cwd = spec.get("cwd") or None
    tee = Path(spec["tee"])
    status_path = Path(spec["status"])
    hold = float(spec.get("hold_s", 5.0))
    tout = float(spec.get("timeout_s", 900.0))
    label = str(spec.get("label") or "")
    slot = int(spec.get("slot") or 0)
    total_slots = int(spec.get("total_slots") or 1)
    layout = str(spec.get("layout") or "hacker")
    events_path = Path(spec["events"]) if spec.get("events") else None

    tee.parent.mkdir(parents=True, exist_ok=True)
    status_path.parent.mkdir(parents=True, exist_ok=True)

    # 窗口标题 + 网格摆位
    title = (f"ai-memory CLI [{slot + 1}/{total_slots}] " + (label or "")).replace("\n", " ").strip()[:120]
    _set_title(title)
    _place_window(
        title, slot, total_slots,
        debug=status_path.with_name(status_path.stem + ".place.txt"),
        layout=layout,
    )

    # 控制台自证：写一行诊断，跑完能查「屏上那条通道到底通没通」
    try:
        status_path.with_name(status_path.stem + ".console.txt").write_text(
            f"CONOUT$={'0x%X' % _CONOUT if _CONOUT else 'NONE'}  vt={vt_ok}  has_console={has_console}\n",
            encoding="utf-8",
        )
    except Exception:
        pass

    # CLI 是否走 stream-json（决定是渲染还是原样透传）
    stream_mode = False
    for i, tok in enumerate(cmd):
        if tok == "--output-format" and i + 1 < len(cmd) and cmd[i + 1] == "stream-json":
            stream_mode = True
            break

    def emit(text: str, kind: str = "dim", *, show_ts: bool = True, delay: float = 0.0) -> None:
        if delay > 0:
            time.sleep(delay)  # 思考行之间的极短延时 —— 把"整块"拆成逐行滚动的观感
        color = _KIND_COLOR.get(kind, "")
        prefix = ""
        if show_ts:
            prefix = f"{DIM}[{time.strftime('%H:%M:%S')}]{RESET} "
        _console_write(f"{prefix}{color}{text}{RESET}\n")

    # ---- 抬头 ----
    _say("=" * 78)
    _say(f"任务    : {label or '(未命名)'}      窗口 {slot + 1}/{total_slots}")
    _say(f"命令    : {subprocess.list2cmdline(cmd[:2])} ... --model {cmd[cmd.index('--model') + 1] if '--model' in cmd else '?'}")
    _say(f"工作目录: {cwd or '(继承)'}")
    _say(f"输出模式: {'stream-json（实时渲染）' if stream_mode else 'text（原样透传）'}"
         + ("" if vt_ok else "   [注意] 本控制台未启用 VT，颜色可能不显示"))
    _say("=" * 78)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=cwd,
    )

    state = {"killed": False, "think_left": _THINK_ANIM_BUDGET_S}

    def _kill() -> None:
        state["killed"] = True
        try:
            proc.kill()
        except Exception:
            pass

    killer = threading.Timer(tout, _kill)
    killer.start()

    read_error = ""
    ev_fh = events_path.open("w", encoding="utf-8") if events_path else None
    try:
        with tee.open("w", encoding="utf-8", newline="") as f:
            assert proc.stdout is not None
            last_text: str | None = None
            for raw in proc.stdout:
                line = raw.rstrip("\n")
                if not line.strip():
                    continue
                if ev_fh:
                    ev_fh.write(line + "\n")
                    ev_fh.flush()

                if not stream_mode:
                    # 旧行为：原样打屏 + 落盘
                    _console_write(line + "\n")
                    f.write(line + "\n")
                    f.flush()
                    continue

                try:
                    ev = json.loads(line)
                except Exception:
                    # 非 JSON 行 = CLI 自身的告警/堆栈。折一行提示，堆栈不刷屏。
                    if line.startswith("fetch config failed"):
                        text, kind = "CLI 配置拉取告警（不影响本次执行）", "warn"
                    elif line.lstrip().startswith("at "):
                        continue
                    else:
                        text, kind = _clip(line, 200), "warn"
                    emit(text, kind)
                    f.write(text + "\n")
                    f.flush()
                    continue

                outs = _render(ev)
                # 自适应滚动节奏：先数本块思考行数，把剩余动画预算均摊下去。
                # 行少的块（短思考）滚得从容，行海（长思考）滚得飞快但依然连续。
                n_think = sum(1 for _t, k, _s in outs if k == "think")
                step = 0.0
                if n_think:
                    step = min(
                        _THINK_LINE_DELAY_MAX,
                        state["think_left"] / n_think,
                    )
                for text, kind, show_ts in outs:
                    if kind == "info" and text == last_text:
                        continue  # CLI 偶发重复发同一 init 事件，去重免得刷屏
                    if kind == "info":
                        last_text = text
                    d = 0.0
                    if kind == "think" and step > 0:
                        d = step
                        state["think_left"] = max(0.0, state["think_left"] - step)
                    emit(text, kind, show_ts=show_ts, delay=d)
                    f.write(text + "\n")
                    f.flush()
    except Exception as exc:  # noqa: BLE001 —— 读流失败也必须留下退出码
        read_error = f"{type(exc).__name__}: {exc}"
        _say(f"\n[runner] 读取 CLI 输出异常：{read_error}")
    finally:
        killer.cancel()
        if ev_fh:
            ev_fh.close()

    rc = proc.wait()
    timed_out = bool(state["killed"])

    status_path.write_text(
        json.dumps({"rc": rc, "timed_out": timed_out, "read_error": read_error}, ensure_ascii=False),
        encoding="utf-8",
    )

    _say("-" * 78)
    _say(f"[退出码 {rc}]" + ("  （超时被杀）" if timed_out else ""))
    _say(f"本窗口将在 {hold:g} 秒后自动关闭。")
    time.sleep(max(0.0, hold))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
