# -*- coding: utf-8 -*-
"""原生安装向导（.NET WinForms）—— **不是网页**。

## 为什么从网页换成 WinForms

前一版把界面做在 WebView2 里（HTML + GSAP）。鱼片的要求是「不要网页，要软件安装」——
用户对安装器的期待是**一个 Windows 窗口**：标题栏、系统按钮、下一步/上一步、进度条。
WebView2 那套要么依赖运行时（没装就白屏）、要么看起来就是浏览器。

选 WinForms 而不是 Tkinter 的原因很实际：**本机三个 Python 都没有 tkinter**
（managed 构建不带 Tcl/Tk），而 `pythonnet` 本来就在安装器环境里（旧版给 WebView2 用的），
所以 WinForms 零新增依赖，且控件是系统原生外观。

## 线程纪律（踩过就知道）

- **界面只在主线程碰**。后台线程只往队列里塞消息，Timer（100ms）在主线程取出来画。
- 主线程启动前先取好所有 Tk/WinForms 变量，不跨线程读控件。
- 进度条与日志在同一块区域：装的时候原地变日志，不跳页（借自参考文档第 5 节）。

## 与内核的分工

本文件只管「画」和「什么时候调」。真正的判定/复制/校验在 `core.py`：
预演与执行共用同一份 plan，所以界面上写的「写入 12 个、跳过 30 个」就是真跑时会发生的事。
"""
from __future__ import annotations

import os
import queue
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import agents, core, steps

try:
    import clr  # pythonnet
    clr.AddReference("System.Windows.Forms")
    clr.AddReference("System.Drawing")
    from System.Drawing import Color, Font, FontStyle, Point, Size
    from System.Windows.Forms import (
        Application, Button, CheckBox, DialogResult, FlatStyle, FolderBrowserDialog,
        Form, FormBorderStyle, FormStartPosition, Label, LinkLabel, Panel, ProgressBar,
        ProgressBarStyle, RadioButton, ScrollBars, TextBox, Timer, MessageBox,
        MessageBoxButtons, MessageBoxIcon,
    )
    HAVE_WINFORMS = True
except Exception:  # noqa: BLE001 —— 没 .NET 就退回命令行，别让安装器打不开
    HAVE_WINFORMS = False


BG = None
FG = None
MUTED = None
ACCENT = None


def _init_colors() -> None:
    global BG, FG, MUTED, ACCENT
    BG = Color.FromArgb(24, 26, 30)
    FG = Color.FromArgb(232, 234, 237)
    MUTED = Color.FromArgb(154, 163, 173)
    ACCENT = Color.FromArgb(122, 162, 255)


def package_version() -> str:
    """安装包版本：VERSION 文件 > 环境变量 > 未知。"""
    for cand in (Path(getattr(__import__("sys"), "_MEIPASS", "") or ".") / "VERSION",
                 Path(__file__).resolve().parent.parent / "VERSION"):
        try:
            if cand.is_file():
                return cand.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return os.environ.get("AI_MEMORY_VERSION", "") or "1.3.0"


class Wizard:
    """多步向导。步骤序列按形态不同而不同。"""

    def __init__(self, package_mode: str = "local") -> None:
        self.pkg = package_mode if package_mode in ("local", "central") else "local"
        self.state: Dict[str, Any] = {}
        self.ui_q: "queue.Queue[tuple]" = queue.Queue()
        self.installing = False
        self.path_editing = False
        self.agent_rows: List[Dict[str, Any]] = []
        self.targets: set[str] = set()
        self.role = "server"
        self.runtime_mode = "portable"
        self._build()

    # ------------------------------------------------------------ 构建界面
    def _build(self) -> None:
        _init_colors()
        self.form = Form()
        self.form.Text = "ai-memory 安装"
        self.form.ClientSize = Size(760, 520)
        self.form.StartPosition = FormStartPosition.CenterScreen
        self.form.BackColor = BG
        self.form.ForeColor = FG
        self.form.MaximizeBox = False
        self.form.MinimizeBox = True
        self.form.FormBorderStyle = FormBorderStyle.FixedSingle
        self.form.Font = Font("Microsoft YaHei UI", 9.5)
        self.form.SuspendLayout()

        self.head = Label()
        self.head.Text = "ai-memory 安装"
        self.head.Font = Font("Microsoft YaHei UI", 10.5, FontStyle.Bold)
        self.head.Location = Point(24, 16)
        self.head.AutoSize = True
        self.head.ForeColor = MUTED

        self.body = Panel()
        self.body.Location = Point(24, 52)
        self.body.Size = Size(712, 372)
        self.body.AutoScroll = True

        self.back = Button()
        self.back.Text = "上一步"
        self.back.Size = Size(104, 36)
        self.back.Location = Point(24, 470)
        self.back.FlatStyle = FlatStyle.System
        self.back.Visible = False
        self.back.Click += lambda s, e: self.go_back()

        self.alt = Button()
        self.alt.Text = ""
        self.alt.Size = Size(140, 36)
        self.alt.Location = Point(452, 470)
        self.alt.FlatStyle = FlatStyle.System
        self.alt.Visible = False
        self.alt.Click += lambda s_, e_: self.on_alt()

        self.main = Button()
        self.main.Text = "开始安装"
        self.main.Size = Size(144, 36)
        self.main.Location = Point(600, 470)
        self.main.FlatStyle = FlatStyle.System
        self.main.Click += lambda s, e: self.on_main()

        self.form.Controls.Add(self.head)
        self.form.Controls.Add(self.body)
        self.form.Controls.Add(self.back)
        self.form.Controls.Add(self.alt)
        self.form.Controls.Add(self.main)

        self.pump = Timer()
        self.pump.Interval = 100
        self.pump.Tick += lambda s, e: self._drain()
        self.pump.Start()

        self.panels: Dict[str, Panel] = {}
        for key in ("welcome", "probe", "where", "env", "role", "admin", "agents",
                    "doing", "done", "doneServer", "doneClient", "exist", "j1", "j2"):
            p = Panel()
            p.Location = Point(0, 0)
            p.Size = Size(700, 368)
            p.Visible = False
            p.BackColor = BG
            self.body.Controls.Add(p)
            self.panels[key] = p

        self._build_welcome()
        self._build_probe()
        self._build_where()
        self._build_env()
        self._build_role()
        self._build_admin()
        self._build_agents()
        self._build_doing()
        self._build_done()
        self._build_exist()
        self._build_connect()
        self.form.ResumeLayout()

    # ---- 小工具
    def _label(self, parent, text, x, y, size=9.5, bold=False, color=None, width=660):
        lb = Label()
        lb.Text = text
        lb.Location = Point(x, y)
        lb.Size = Size(width, 22)
        lb.Font = Font("Microsoft YaHei UI", size, FontStyle.Bold if bold else FontStyle.Regular)
        lb.ForeColor = color if color is not None else FG
        parent.Controls.Add(lb)
        return lb

    def _wrap(self, parent, text, x, y, width=660, height=44, color=None):
        lb = Label()
        lb.Text = text
        lb.Location = Point(x, y)
        lb.Size = Size(width, height)
        lb.ForeColor = color if color is not None else MUTED
        parent.Controls.Add(lb)
        return lb

    # ------------------------------------------------------------ 各屏
    def _build_welcome(self) -> None:
        p = self.panels["welcome"]
        self._label(p, "让你的 AI 记住你", 0, 0, size=17, bold=True, color=Color.White)
        self.wl_lead = self._wrap(
            p, "装好之后，你对 AI 说过的偏好和定下的规矩，下次它会自己想起来，不用你再说一遍。",
            0, 44, height=26, color=FG)
        self._wrap(p, "· 记忆存在你自己电脑的一个文件夹里，用记事本也能打开看，随时能整个拷走。",
                   0, 84, height=24)
        self._wrap(p, "· 你电脑上的 AI 工具我们帮你接上，改它们的设置文件之前会先备份。",
                   0, 112, height=24)
        self._wrap(p, "· 不需要注册账号，也只需要连着网装这一次。", 0, 140, height=24)
        self.wl_wrong = self._wrap(p, "", 0, 186, height=40, color=MUTED)

    def _build_probe(self) -> None:
        p = self.panels["probe"]
        self._label(p, "先看看这台电脑", 0, 0, size=17, bold=True, color=Color.White)
        self._wrap(p, "几秒钟就好，检查完会自动往下走，不用你操作。", 0, 36, height=24)
        self.probe_rows: Dict[str, Label] = {}
        y = 76
        for key, name in (("env", "运行环境"), ("disk", "存放位置"),
                          ("ports", "程序要用的号码"), ("agents", "你电脑上的 AI 工具")):
            self._label(p, name, 0, y, color=MUTED, width=170)
            lb = self._label(p, "检查中…", 180, y, width=480)
            self.probe_rows[key] = lb
            y += 30
        self.probe_note = self._wrap(p, "", 0, y + 8, height=40, color=Color.FromArgb(224, 100, 100))

    def _build_where(self) -> None:
        p = self.panels["where"]
        self._label(p, "你的记忆要放在哪儿", 0, 0, size=17, bold=True, color=Color.White)
        self._wrap(p, "就像一个笔记本放在哪个抽屉里。以后想备份，整个文件夹拷走就行。",
                   0, 36, height=24)
        self.where_path = TextBox()
        self.where_path.Location = Point(0, 74)
        self.where_path.Size = Size(660, 26)
        self.where_path.ReadOnly = True
        self.where_path.BackColor = Color.FromArgb(38, 41, 46)
        self.where_path.ForeColor = FG
        p.Controls.Add(self.where_path)
        self._wrap(p, "这个文件夹里是普通的文本文件，用记事本就能打开，我们不会把它加密锁起来。",
                   0, 108, height=24)
        self.where_edit = TextBox()
        self.where_edit.Location = Point(0, 150)
        self.where_edit.Size = Size(660, 26)
        self.where_edit.Visible = False
        p.Controls.Add(self.where_edit)
        self.where_hint = self._wrap(p, "换了之后，以后所有笔记都存这里。", 0, 182, height=24,
                                     color=Color.FromArgb(224, 179, 65))
        self.where_hint.Visible = False

    def _build_env(self) -> None:
        p = self.panels["env"]
        self._label(p, "运行环境放哪儿", 0, 0, size=17, bold=True, color=Color.White)
        self._wrap(p, "ai-memory 要有个「引擎」才跑得起来。它自己的引擎可以一起装进这个文件夹，"
                      "也可以借你电脑上已有的。", 0, 36, height=44)
        self.rb_portable = RadioButton()
        self.rb_portable.Text = "一起装进来（推荐，便携）：不用联网、不用先装 Python、不碰系统"
        self.rb_portable.Location = Point(0, 92)
        self.rb_portable.Size = Size(660, 24)
        self.rb_portable.Checked = True
        self.rb_portable.ForeColor = FG
        p.Controls.Add(self.rb_portable)
        self._wrap(p, "整个文件夹拷到别的电脑也能用；把文件夹删掉就等于卸干净。", 24, 118, width=620,
                   height=24)
        self.rb_system = RadioButton()
        self.rb_system.Text = "用我电脑上已有的 Python：省点磁盘，但要联网装依赖"
        self.rb_system.Location = Point(0, 156)
        self.rb_system.Size = Size(660, 24)
        self.rb_system.ForeColor = FG
        p.Controls.Add(self.rb_system)
        self.env_hint = self._wrap(p, "", 0, 196, height=24, color=MUTED)

    def _build_role(self) -> None:
        p = self.panels["role"]
        self._label(p, "先确认一下这台电脑", 0, 0, size=17, bold=True, color=Color.White)
        self._wrap(p, "这个安装包是「大家一起用」的那种装法。", 0, 36, height=24)
        self.rb_server = RadioButton()
        self.rb_server.Text = "就是这台：我打算让这台电脑一直开着，同事以后连它。"
        self.rb_server.Location = Point(0, 80)
        self.rb_server.Size = Size(660, 24)
        self.rb_server.Checked = True
        self.rb_server.ForeColor = FG
        p.Controls.Add(self.rb_server)
        self.rb_client = RadioButton()
        self.rb_client.Text = "不是这台：我只是想让我电脑上的 AI 接上别人已经装好的服务器。"
        self.rb_client.Location = Point(0, 120)
        self.rb_client.Size = Size(660, 24)
        self.rb_client.ForeColor = FG
        p.Controls.Add(self.rb_client)

    def _build_admin(self) -> None:
        p = self.panels["admin"]
        self._label(p, "谁能进来", 0, 0, size=17, bold=True, color=Color.White)
        self._wrap(p, "建一个你自己的管理员账号。以后同事的账号由你来开。", 0, 36, height=24)
        self._label(p, "你的账号名", 0, 76, color=MUTED, width=120)
        self.adm_user = TextBox()
        self.adm_user.Text = "admin"
        self.adm_user.Location = Point(130, 74)
        self.adm_user.Size = Size(240, 26)
        p.Controls.Add(self.adm_user)
        self._label(p, "密码", 0, 116, color=MUTED, width=120)
        self.adm_pass = TextBox()
        self.adm_pass.Location = Point(130, 114)
        self.adm_pass.Size = Size(240, 26)
        self.adm_pass.UseSystemPasswordChar = True
        p.Controls.Add(self.adm_pass)
        self._label(p, "再输一遍", 0, 156, color=MUTED, width=120)
        self.adm_pass2 = TextBox()
        self.adm_pass2.Location = Point(130, 154)
        self.adm_pass2.Size = Size(240, 26)
        self.adm_pass2.UseSystemPasswordChar = True
        p.Controls.Add(self.adm_pass2)
        self._wrap(p, "管理员就是「钥匙保管员」，只有你能开新账号、看操作记录。", 0, 196, height=24)
        self._wrap(p, "密码至少 12 个字符，可以是一整句话，比如「我家猫叫豆豆2026」。",
                   0, 222, height=24, color=Color.FromArgb(224, 179, 65))
        self.adm_err = self._wrap(p, "", 0, 254, height=44, color=Color.FromArgb(224, 100, 100))

    def _build_agents(self) -> None:
        p = self.panels["agents"]
        self._label(p, "让哪些 AI 用上它", 0, 0, size=17, bold=True, color=Color.White)
        self.ag_lead = self._wrap(p, "下面这些是你电脑上已经装的 AI 工具。勾上的，我们会帮它接上你的记忆库。",
                                  0, 36, height=26)
        self.ag_box = Panel()
        self.ag_box.Location = Point(0, 72)
        self.ag_box.Size = Size(660, 190)
        self.ag_box.AutoScroll = True
        p.Controls.Add(self.ag_box)
        self._wrap(p, "接上之后，你在这些工具里说「查一下我的记忆库」，它就能自己去翻了。",
                   0, 272, height=24)
        self._wrap(p, "改它们的设置文件之前，我们会先存一份原样的备份。", 0, 296, height=24)
        self.ag_err = self._wrap(p, "", 0, 320, height=30, color=Color.FromArgb(224, 100, 100))

    def _build_doing(self) -> None:
        p = self.panels["doing"]
        self._label(p, "正在装", 0, 0, size=17, bold=True, color=Color.White)
        self._wrap(p, "一共四件事，做完这个窗口会自己告诉你下一步。", 0, 36, height=24)
        self.do_labels: Dict[str, Label] = {}
        y = 74
        for key, name in (("files", "放好记忆库文件"), ("app", "启动工作台"),
                          ("agents", "接上 AI 工具"), ("selfcheck", "做个自检")):
            lb = self._label(p, "·  " + name, 0, y, width=660)
            self.do_labels[key] = lb
            y += 30
        self.do_bar = ProgressBar()
        self.do_bar.Location = Point(0, 208)
        self.do_bar.Size = Size(660, 16)
        self.do_bar.Style = ProgressBarStyle.Continuous
        p.Controls.Add(self.do_bar)
        self.do_now = self._wrap(p, "", 0, 232, height=22, color=FG)
        self.do_log = TextBox()
        self.do_log.Location = Point(0, 258)
        self.do_log.Size = Size(660, 96)
        self.do_log.Multiline = True
        self.do_log.ReadOnly = True
        self.do_log.ScrollBars = ScrollBars.Vertical
        self.do_log.BackColor = Color.FromArgb(18, 20, 23)
        self.do_log.ForeColor = MUTED
        p.Controls.Add(self.do_log)
        self.do_err = self._wrap(p, "", 0, 358, height=10, color=Color.FromArgb(224, 100, 100))

    def _build_done(self) -> None:
        p = self.panels["done"]
        self._label(p, "装好了", 0, 0, size=17, bold=True, color=Color.White)
        self._wrap(p, "你的记忆库已经在这台电脑上跑起来了。这里是接下来要做的三件事，"
                      "不做完也不影响使用。", 0, 36, height=44)
        self.done_url = TextBox()
        self.done_url.Location = Point(0, 92)
        self.done_url.Size = Size(660, 26)
        self.done_url.ReadOnly = True
        self.done_url.BackColor = Color.FromArgb(38, 41, 46)
        self.done_url.ForeColor = Color.FromArgb(207, 224, 255)
        p.Controls.Add(self.done_url)
        self._wrap(p, "一、看看里面有什么：点下面的「打开我的记忆库」，浏览器会打开工作台。",
                   0, 128, height=24)
        self._wrap(p, "二、让 AI 认一下门：打开你勾选的 AI 工具，发一句「用记忆库查一下我之前的偏好」。",
                   0, 154, height=24)
        self._wrap(p, "三、以后想加新的 AI 工具：桌面上有「让新的 AI 连上记忆库」，双击即可。",
                   0, 180, height=24)
        self.done_note = self._wrap(p, "", 0, 220, height=44, color=MUTED)

        q = self.panels["doneServer"]
        self._label(q, "装好了，现在可以让同事进来了", 0, 0, size=17, bold=True, color=Color.White)
        self._wrap(q, "你的同事不用装任何东西，用浏览器打开下面这个网址就行。", 0, 36, height=24)
        self.srv_url = TextBox()
        self.srv_url.Location = Point(0, 76)
        self.srv_url.Size = Size(660, 26)
        self.srv_url.ReadOnly = True
        self.srv_url.BackColor = Color.FromArgb(38, 41, 46)
        self.srv_url.ForeColor = Color.FromArgb(207, 224, 255)
        q.Controls.Add(self.srv_url)
        self.srv_hint = self._wrap(q, "", 0, 112, height=44, color=MUTED)

        r = self.panels["doneClient"]
        self._label(r, "接上了", 0, 0, size=17, bold=True, color=Color.White)
        self._wrap(r, "现在打开你的 AI 工具，试试这句话：", 0, 36, height=24)
        self._wrap(r, "用记忆库查一下我之前的偏好", 0, 68, height=24, color=Color.FromArgb(207, 224, 255))
        self._wrap(r, "这台电脑上没有装工作台本体，你只是连上了团队的库。", 0, 104, height=24)

    def _build_exist(self) -> None:
        p = self.panels["exist"]
        self._label(p, "这台电脑上已经装过了", 0, 0, size=17, bold=True, color=Color.White)
        self._wrap(p, "你的记忆库现在就开着，笔记都在。你想做什么？", 0, 36, height=24)
        self.exist_box = Panel()
        self.exist_box.Location = Point(0, 74)
        self.exist_box.Size = Size(660, 200)
        p.Controls.Add(self.exist_box)
        self.exist_radios: List[RadioButton] = []

    def _build_connect(self) -> None:
        a = self.panels["j1"]
        self._label(a, "你的服务器在哪儿", 0, 0, size=17, bold=True, color=Color.White)
        self._wrap(a, "把给你开账号的人发给你的网址粘进来，原样粘，不用改。", 0, 36, height=24)
        self._label(a, "服务器网址", 0, 78, color=MUTED, width=110)
        self.j_url = TextBox()
        self.j_url.Text = ""
        self.j_url.Location = Point(120, 76)
        self.j_url.Size = Size(380, 26)
        a.Controls.Add(self.j_url)
        self._wrap(a, "不知道填什么？找给你开账号的人要，他发你的那段话第一行就是这个网址。",
                   0, 112, height=24)
        self.j_err = self._wrap(a, "", 0, 144, height=44, color=Color.FromArgb(224, 100, 100))

        b = self.panels["j2"]
        self._label(b, "你是谁", 0, 0, size=17, bold=True, color=Color.White)
        self._wrap(b, "用给你开账号的人发你的账号和密码登录。", 0, 36, height=24)
        self._label(b, "账号", 0, 78, color=MUTED, width=110)
        self.j_user = TextBox()
        self.j_user.Location = Point(120, 76)
        self.j_user.Size = Size(300, 26)
        b.Controls.Add(self.j_user)
        self._label(b, "密码", 0, 114, color=MUTED, width=110)
        self.j_pass = TextBox()
        self.j_pass.Location = Point(120, 112)
        self.j_pass.Size = Size(300, 26)
        self.j_pass.UseSystemPasswordChar = True
        b.Controls.Add(self.j_pass)
        self.j_ok = self._wrap(b, "", 0, 152, height=24, color=Color.FromArgb(62, 207, 142))

    # ------------------------------------------------------------ 状态机
    def order(self) -> List[str]:
        if self.pkg == "central":
            return ["welcome", "probe", "where", "env", "role", "admin", "agents"]
        return ["welcome", "probe", "where", "env", "agents"]

    def is_page(self, key: str) -> bool:
        """当前是不是这一屏。**判显隐用它，别用 Control.Visible**：
        祖先控件不可见（比如窗体还没 Run）时，Visible 恒为 False。"""
        return getattr(self, "page", "") == key

    def show(self, key: str) -> None:
        for name, panel in self.panels.items():
            panel.Visible = (name == key)
        self.page = key
        # 进「AI 工具」屏时如果还没填过清单，就地填 —— 否则用户看到的是一片空白
        # （上一轮实测反馈：等 15 秒一个名字都没有）
        if key == "agents" and not self.agent_rows:
            self.fill_agents()
        self.back.Visible = key in ("probe", "where", "env", "role", "admin", "agents", "j1", "j2")
        self._sync_foot()

    def _sync_foot(self) -> None:
        k = getattr(self, "page", "welcome")
        main_text = {
            "welcome": "开始安装", "probe": "继续", "where": "就用这里", "env": "这样就行，继续",
            "role": "继续", "admin": "建好，继续", "agents": "接上这几个，继续",
            "j1": "连上它", "j2": "登录", "doing": "", "exist": "",
        }.get(k, "打开我的记忆库")
        self.main.Text = main_text
        self.main.Visible = bool(main_text)
        if k == "where":
            self.main.Text = "用这个文件夹" if self.path_editing else "就用这里"
            self.alt.Text = "用默认位置" if self.path_editing else "换一个文件夹"
            self.alt.Visible = True
        elif k == "agents":
            self.alt.Text = "先不接"
            self.alt.Visible = True
        else:
            self.alt.Visible = False

    def go_back(self) -> None:
        seq = self.order()
        k = getattr(self, "page", "welcome")
        if k == "j2":
            return self.show("j1")
        if k == "j1":
            return self.show("role")
        if k in seq:
            i = seq.index(k)
            if i > 0:
                self.show(seq[i - 1])

    def on_alt(self) -> None:
        k = getattr(self, "page", "welcome")
        if k == "where":
            return self.toggle_path()
        if k == "agents":
            return self.on_skip_agents()

    def _on_exist(self) -> None:
        act = None
        for rb in self.exist_radios:
            if rb.Checked:
                act = rb.Tag
                break
        exist = self.state.get("existing") or {}
        if act in (None, "open"):
            return self._open(exist.get("web_url") or "")
        if act == "agents":
            self.state["agents"] = [a for a in agents.scan() if a.found]
            self.pkg = exist.get("mode") or self.pkg
            self.show("agents")
            self.fill_agents()
            return
        if act == "repair":
            self.state["path"] = exist.get("target") or str(steps.default_target())
            return self.start_install()
        if act == "uninstall":
            return self.do_uninstall()

    def do_uninstall(self) -> None:
        exist = self.state.get("existing") or {}
        target = Path(exist.get("target") or steps.default_target())
        ans = MessageBox.Show(
            "只删程序，你的笔记默认保留。" + chr(13) + chr(10) + chr(13) + chr(10) +
            "点「是」= 只删程序，笔记留着" + chr(13) + chr(10) +
            "点「否」= 连笔记一起删",
            "卸掉 ai-memory", MessageBoxButtons.YesNoCancel, MessageBoxIcon.Warning)
        if ans == DialogResult.Cancel:
            return
        from .server import _do_uninstall
        res = _do_uninstall(target, keep_notes=(ans == DialogResult.Yes))
        if res.get("ok"):
            MessageBox.Show("卸干净了。" + (res.get("note") or ""), "完成")
            self.form.Close()
        else:
            MessageBox.Show("没卸成：" + (res.get("error_text") or ""), "没卸载成功",
                            MessageBoxButtons.OK, MessageBoxIcon.Error)

    def on_main(self) -> None:
        k = getattr(self, "page", "welcome")
        if k == "welcome":
            return self.start_probe()
        if k == "probe":
            return self.goto_where()
        if k == "where":
            if self.path_editing:
                return self.toggle_path()
            return self.show("env")
        if k == "env":
            self.runtime_mode = "system" if self.rb_system.Checked else "portable"
            return self.show("role" if self.pkg == "central" else "agents")
        if k == "role":
            self.role = "client" if self.rb_client.Checked else "server"
            return self.show("j1" if self.role == "client" else "admin")
        if k == "admin":
            return self.validate_admin()
        if k == "agents":
            return self.on_agents_continue()
        if k == "j1":
            return self.connect_probe()
        if k == "j2":
            return self.connect_login()
        if k == "done":
            return self.open_workbench()

    # ------------------------------------------------------------ 各步逻辑
    def start_probe(self) -> None:
        self.show("probe")
        for lb in self.probe_rows.values():
            lb.Text = "检查中…"
        threading.Thread(target=self._probe_worker, daemon=True).start()

    def _probe_worker(self) -> None:
        try:
            found = agents.scan()
            portable = steps.bundle_runtime() is not None
            py = steps.system_python()
            env_ok = portable or py is not None
            env_msg = ("安装包自带运行环境，不用另装 Python" if portable
                       else ("会用你电脑上已有的 Python" if py else "没找到 Python 3.10+"))
            target = steps.default_target()
            disk_ok, disk_msg = steps.disk_status(target)
            self.ui_q.put(("probe", {"env_ok": env_ok, "env_msg": env_msg,
                                     "disk_ok": disk_ok, "disk_msg": disk_msg,
                                     "port_ok": True, "port_msg": "空着，能用",
                                     "agents": [a for a in found if a.found],
                                     "path": str(target), "portable": portable}))
        except Exception as exc:  # noqa: BLE001
            self.ui_q.put(("probe_error", {"message": str(exc)}))

    def goto_where(self) -> None:
        self.where_path.Text = self.state.get("path") or str(steps.default_target())
        self.where_edit.Text = self.where_path.Text
        self.show("where")

    def toggle_path(self) -> None:
        if self.path_editing:
            v = (self.where_edit.Text or "").strip()
            if v:
                self.where_path.Text = v
                self.state["path"] = v
            self.path_editing = False
        else:
            self.path_editing = True
            self.where_edit.Text = self.where_path.Text
        self.where_edit.Visible = self.path_editing
        self.where_hint.Visible = self.path_editing
        self._sync_foot()

    def validate_admin(self) -> None:
        pw = self.adm_pass.Text or ""
        pw2 = self.adm_pass2.Text or ""
        self.adm_err.Text = ""
        if not (self.adm_user.Text or "").strip():
            self.adm_err.Text = "账号名不能空。"
            return
        if len(pw) < 12:
            self.adm_err.Text = ("这个密码太短，别人试几次就进去了。至少 12 个字符，"
                                 "可以是一整句话，比如「我家猫叫豆豆2026」。")
            return
        if pw != pw2:
            self.adm_err.Text = "两次输的不一样，再输一次。"
            return
        self.state["admin"] = {"username": self.adm_user.Text.strip(), "password": pw}
        self.show("agents")
        self.fill_agents()

    def fill_agents(self) -> None:
        for c in list(self.ag_box.Controls):
            self.ag_box.Controls.Remove(c)
        self.agent_rows = []
        found = self.state.get("agents") or []
        if not found:
            self.ag_lead.Text = ("没找到可以连的 AI 工具。这不影响安装 —— 记忆库本身现在就能用，"
                                 "工作台能看能搜。")
            return
        y = 0
        for a in found:
            info = agents.as_dict(a) if hasattr(agents, "as_dict") else a
            cb = CheckBox()
            label = info.get("name") or info.get("key")
            if info.get("manual"):
                label += "（需要手动配置）"
                cb.Enabled = False
            elif info.get("configured"):
                label += "（已经接过了）"
            cb.Text = label
            cb.Location = Point(0, y)
            cb.Size = Size(620, 24)
            cb.ForeColor = FG
            # 「已经接过了」也要打勾：不打勾用户会以为「继续会不会把它断掉」（实测反馈）。
            cb.Checked = bool(info.get("found")) and not info.get("manual")
            cb.Tag = info.get("key")
            self.ag_box.Controls.Add(cb)
            self.agent_rows.append({"key": info.get("key"), "cb": cb})
            y += 28

    def on_agents_continue(self) -> None:
        picked = {r["key"] for r in self.agent_rows if r["cb"].Checked}
        pickable = [r for r in self.agent_rows if r["cb"].Enabled]
        if not picked and pickable and not getattr(self, "_skip_confirmed", False):
            self._skip_confirmed = True
            self.ag_err.Text = ("一个都没勾也行，但这样 AI 读不到你的笔记。确定要这样？"
                                "再点一次就继续。")
            return
        self.targets = picked
        self.start_install()

    def on_skip_agents(self) -> None:
        self.targets = set()
        self.start_install()

    def connect_probe(self) -> None:
        self.j_err.Text = "正在连…"
        threading.Thread(target=self._connect_worker, daemon=True).start()

    def _connect_worker(self) -> None:
        try:
            from .server import _connect_probe
        except Exception:  # noqa: BLE001
            self.ui_q.put(("j1_error", {"message": "这一版没带联网模块"}))
            return
        url = self.j_url.Text or ""
        self.ui_q.put(("j1", _connect_probe(url)))

    def connect_login(self) -> None:
        self.j_ok.Text = "正在登录…"
        threading.Thread(target=self._login_worker, daemon=True).start()

    def _login_worker(self) -> None:
        try:
            from .server import _connect_login
        except Exception:  # noqa: BLE001
            self.ui_q.put(("j2", {"ok": False, "error": "这一版没带联网模块"}))
            return
        self.ui_q.put(("j2", _connect_login(self.j_url.Text or "",
                                            self.j_user.Text or "",
                                            self.j_pass.Text or "")))

    # ------------------------------------------------------------ 安装
    def start_install(self) -> None:
        if self.installing:
            return
        self.installing = True
        self.show("doing")
        self.main.Visible = False
        self.back.Visible = False
        for key, lb in self.do_labels.items():
            lb.Text = "·  " + {"files": "放好记忆库文件", "app": "启动工作台",
                               "agents": "接上 AI 工具", "selfcheck": "做个自检"}[key]
            lb.ForeColor = FG
        self.do_bar.Value = 0
        self.do_log.Text = ""
        self.do_err.Text = ""
        threading.Thread(target=self._install_worker, daemon=True).start()

    def _install_worker(self) -> None:
        target = Path(self.state.get("path") or steps.default_target())
        version = package_version()
        try:
            self.ui_q.put(("step", {"key": "files", "state": "run"}))
            plan_obj = core.plan(target, mode=self.pkg, runtime_mode=self.runtime_mode,
                                 package_version=version)
            self.ui_q.put(("log", "计划：" + core.preview(plan_obj)))
            res = core.execute(plan_obj, progress=lambda m, p: self.ui_q.put(("progress", {"msg": m, "pct": p})),
                               package_version=version, mode=self.pkg,
                               runtime_mode=self.runtime_mode,
                               admin=self.state.get("admin") if self.role == "server" else None)
            if not res.get("ok"):
                self.ui_q.put(("install_failed", {"errors": res.get("errors")}))
                return
            self.ui_q.put(("step", {"key": "files", "state": "done"}))
            self.ui_q.put(("progress", {"msg": "文件放好了", "pct": 0.82}))

            # 起工作台（网页界面）
            self.ui_q.put(("step", {"key": "app", "state": "run"}))
            launched = steps.start_app(target, log=lambda m: self.ui_q.put(("log", m)))
            # 起 MCP 服务（AI 客户端连的那扇门）。少了这一步，配置写得再对也是「连不上」。
            mcp = steps.start_mcp(target, log=lambda m: self.ui_q.put(("log", m)))
            self.ui_q.put(("log", "MCP 服务：%s" % ("已就绪 " + mcp.get("url", "")
                                                   if mcp.get("ok") else mcp.get("reason", ""))))
            self.ui_q.put(("step", {"key": "app",
                                    "state": "done" if (launched.get("ok") and mcp.get("ok")) else "bad"}))

            self.ui_q.put(("step", {"key": "agents", "state": "run"}))
            url = "http://127.0.0.1:8770/mcp"
            found = [a for a in agents.scan() if a.found]
            chosen = [a for a in found if not self.targets or a.key in self.targets]
            results = agents.apply(chosen, url, "")
            ok_agents = all(not r.get("error") for r in results)
            for r in results:
                self.ui_q.put(("log", "AI %s → %s" % (r.get("agent"),
                                                      r.get("action") or r.get("error") or r.get("reason"))))
            self.ui_q.put(("step", {"key": "agents", "state": "done" if ok_agents else "bad"}))

            # 自检
            self.ui_q.put(("step", {"key": "selfcheck", "state": "run"}))
            need = [target / "backend" / "app" / "main.py", target / "frontend" / "dist" / "index.html",
                    target / "vault" / "db_paths.py"]
            ok_self = all(p.is_file() for p in need) and steps.runtime_ready(target)
            self.ui_q.put(("step", {"key": "selfcheck", "state": "done" if ok_self else "bad"}))
            steps.create_shortcuts(target, mode=self.pkg)

            self.ui_q.put(("install_done", {
                "ok": ok_self, "web_ok": bool(launched.get("ok")),
                "web_url": launched.get("url") or "",
                "web_reason": launched.get("reason") or "",
                "target": str(target),
            }))
        except Exception as exc:  # noqa: BLE001
            import traceback
            self.ui_q.put(("install_failed", {"errors": [type(exc).__name__ + ": " + str(exc)
                                                         + "\n" + traceback.format_exc()[-800:]]}))
        finally:
            self.installing = False

    # ------------------------------------------------------------ 队列泵
    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.ui_q.get_nowait()
                self._handle(kind, payload)
        except queue.Empty:
            return

    def _handle(self, kind: str, payload: Dict[str, Any]) -> None:
        if kind == "probe":
            d = payload
            self.state.update(d)
            self.probe_rows["env"].Text = ("可以" if d.get("env_ok")
                                           else (d.get("env_msg") or "缺少运行环境"))
            self.probe_rows["disk"].Text = d.get("disk_msg") or "可以"
            self.probe_rows["ports"].Text = d.get("port_msg") or "空着，能用"
            n = len(d.get("agents") or [])
            self.probe_rows["agents"].Text = ("找到 %d 个" % n) if n else "一个都没找到（不影响安装）"
            for key in ("env", "disk", "ports", "agents"):
                self.probe_rows[key].ForeColor = FG
            if not d.get("env_ok"):
                self.probe_note.Text = d.get("env_msg") or ""
            else:
                self.probe_note.Text = ""
                self.goto_where()
        elif kind == "probe_error":
            self.probe_note.Text = "检查时出错：" + str(payload.get("message"))
        elif kind == "progress":
            self.do_bar.Value = max(0, min(100, int(payload.get("pct", 0) * 100)))
            self.do_now.Text = str(payload.get("msg") or "")
        elif kind == "log":
            self.do_log.AppendText(str(payload) + "\r\n")
        elif kind == "step":
            key, st = payload.get("key"), payload.get("state")
            mark = {"run": "→", "done": "✓", "bad": "×"}.get(st, "·")
            names = {"files": "放好记忆库文件", "app": "启动工作台",
                     "agents": "接上 AI 工具", "selfcheck": "做个自检"}
            lb = self.do_labels[key]
            lb.Text = "%s  %s" % (mark, names[key])
            lb.ForeColor = (Color.FromArgb(62, 207, 142) if st == "done"
                            else (Color.FromArgb(224, 100, 100) if st == "bad" else FG))
        elif kind == "install_done":
            self.do_bar.Value = 100
            self.finish_local(payload)
        elif kind == "install_failed":
            self.do_err.Text = "装到一半出错了：" + "；".join(payload.get("errors") or [])
            self.main.Text = "重试"
            self.main.Visible = True
            self.main.Click += lambda s, e: self.start_install()
        elif kind == "j1":
            if payload.get("ok"):
                self.j_err.ForeColor = Color.FromArgb(62, 207, 142)
                self.j_err.Text = "连上了：%s%s" % (
                    payload.get("title") or "对方的记忆库",
                    ("，目前 %s 篇笔记" % payload.get("notes")) if payload.get("notes") is not None else "")
                self.show("j2")
            else:
                self.j_err.ForeColor = Color.FromArgb(224, 100, 100)
                self.j_err.Text = ("这个地址连不上。可能是：这台电脑和服务器不在同一个网络；"
                                   "服务器没开机；或者网址少了一截。先用浏览器打开试试？")
        elif kind == "j2":
            if payload.get("ok"):
                self.j_ok.Text = "登录成功，接入码已自动取到，你不用管。"
                self.state["server_url"] = self.j_url.Text
                self.state["agents"] = [a for a in agents.scan() if a.found]
                self.show("agents")
                self.fill_agents()
            else:
                self.j_ok.Text = payload.get("error") or "账号或密码不对。"

    # ------------------------------------------------------------ 收尾
    def finish_local(self, payload: Dict[str, Any]) -> None:
        self.state["result"] = payload
        if self.role == "client":
            self.show("doneClient")
            self.main.Text = "用浏览器打开记忆库"
            self.main.Visible = True
            self.main.Click += lambda s, e: self._open(self.state.get("server_url") or "")
            self.back.Visible = False
            return
        if payload.get("web_ok"):
            self.done_url.Text = payload.get("web_url") or ""
            self.done_note.Text = "关掉这个窗口不会关掉记忆库，它已经在后台跑着了。"
            self.main.Text = "打开我的记忆库"
            self.main.Click += lambda s, e: self._open(payload.get("web_url") or "")
        else:
            self.done_url.Text = "（工作台没起来）"
            self.done_note.Text = ("工作台没起来：" + (payload.get("web_reason") or "原因不明")
                                   + "　可以点「打开我的记忆库」再试，或者关掉窗口稍后从桌面图标打开。")
            self.main.Text = "重试启动工作台"
            self.main.Click += lambda s, e: self.start_install()
        self.show("done")
        self.main.Visible = True
        self.back.Visible = False

    def open_workbench(self) -> None:
        url = (self.state.get("result") or {}).get("web_url") or ""
        self._open(url)

    def _open(self, url: str) -> None:
        if not url:
            return
        try:
            os.startfile(url)          # noqa: S606 —— 用系统默认浏览器打开
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ 启动
    def run(self) -> None:
        self.show("welcome")
        self.wl_wrong.Text = ("这个安装包是「大家一起用」的装法。如果你只是想自己用，"
                              "去下载「只有我自己用」的那个安装包。") if self.pkg == "central" else \
                             ("如果你其实是想和同事一起用，请去下载「我和同事一起用」的那个安装包。")
        exist = steps.detect_existing()
        if exist.get("installed") and (exist.get("mode") or "local") == self.pkg:
            self.state["existing"] = exist
            self.fill_exist(exist)
            self.show("exist")
        Application.Run(self.form)

    def fill_exist(self, exist: Dict[str, Any]) -> None:
        for c in list(self.exist_box.Controls):
            self.exist_box.Controls.Remove(c)
        self.exist_radios = []
        opts = [("打开我的记忆库", "open"),
                ("只重新接一下 AI 工具", "agents"),
                ("修一下装坏的地方（重装本体，笔记不动）", "repair"),
                ("卸掉它（会问你要不要保留笔记）", "uninstall")]
        y = 0
        for text, act in opts:
            rb = RadioButton()
            rb.Text = text
            rb.Location = Point(0, y)
            rb.Size = Size(620, 24)
            rb.ForeColor = FG
            rb.Tag = act
            if act == "open":
                rb.Checked = True
            self.exist_box.Controls.Add(rb)
            self.exist_radios.append(rb)
            y += 30
        self.main.Text = "打开我的记忆库"
        self.main.Visible = True
        self.main.Click += lambda s, e: self._on_exist()


def run_wizard(package_mode: str = "local") -> int:
    """给 entry.py 用：没有 WinForms 就返回 2，让调用方退回命令行。"""
    if not HAVE_WINFORMS:
        return 2
    w = Wizard(package_mode)
    w.run()
    return 0
