"""进程级路径解析：所有「本机绝对路径」的唯一出口（2026-10-23 中心化改造）。

## 为什么单开一个文件

原先后端有 **6 处** 写死的作者机器绝对路径，散在 4 个文件里：

| 文件 | 行 | 常量 |
|---|---|---|
| `mcp_client.py` | 31 | `MCP_JSON_PATH` |
| `tool_catalog.py` | 28 / 29 | `MCP_JSON_PATH`、`AI_MEMORY_DIR_DEFAULT` |
| `settings_store.py` | 80 / 163 | `_default()` 里的 vaultPath、`_VAULT_DEFAULT` |
| `vault.py` | 31 | `DEFAULT_VAULT_PATH` |

搬到 Linux 时只要漏一处，症状**不是报错**，而是静默回落：

- vault 根指错 → `vault_db.db_path_for()` 四个候选位置全落空 → 工作台整个改扫盘，
  看到的是过期 md，主人感知为「刚写的东西在 web 上看不见」；
- mcp.json 指错 → `_read_servers()` 吞掉 `OSError` 返回 `{}` → 工具页全空，
  不报错、也不提示配置缺失。

6 处散在 4 个文件里，靠人肉保证不漏不现实。收成一个出口后：将来加新路径只加在这里，
新增平台只改 `_DEFAULTS` 一处。

## 覆盖顺序（每个键都一样）

    环境变量  >  <backend>/data/backend.json  >  内置默认（Windows 本机路径）

- **环境变量**给 systemd 单元用（一个单元一个实例，最直观）；
- **backend.json** 给「同一台机器跑多个不同库的实例」用，免得为每个实例写一套
  `Environment=`（Windows 上由 vbs 拉起时也没地方塞环境变量）；
- 内置默认保持原值，所以**本机行为零变化** —— 不设任何变量时，6 处解析结果
  与改造前逐字节相同。

## 变量名为什么是 GRAPHITE_ 前缀而不是 AI_MEMORY_

`AI_MEMORY_DIR` 是 ai-memory 自己认的变量，本机 shell / MCP 客户端配置里可能已经
设了值。工作台若直接读它，一次 `export` 就能把工作台的库指到别处。
所以主变量用 `GRAPHITE_VAULT_ROOT`，`AI_MEMORY_DIR` 只作为**次选兜底**
（服务器单元里本就设了它，能少写一行）。优先级高的在前。

## 两类路径，别混（R2，2026-10-27）

上面那 8 个键是「配置项」，走 环境变量 > backend.json > 内置默认 三层。
**程序资源目录与用户数据目录不属于配置项**，它们由 `resource_dir()` / `data_dir()`
解析 —— 冻结态下两者必须分开，详见下面 R2 那一节。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

# backend/app/paths.py → parents[0]=app, parents[1]=backend
BACKEND_DIR = Path(__file__).resolve().parents[1]

# 阶段 12：代码根 = backend 的上一级（本地是 graphite-workspace/，服务器是
# /opt/ai-memory/graphite/）。一键更新只往这棵树里写，不碰树外任何东西。
CODE_ROOT = BACKEND_DIR.parent


# ---------------------------------------------------------------------------
# R2（2026-10-27）：程序资源 / 用户数据 / 配置文件三个路径的解析
#
# ## 为什么必须分成两个函数
#
# 冻结成 exe 后 `__file__` 落在解包临时目录（`sys._MEIPASS`），原来的
# `DATA_DIR = BACKEND_DIR / "data"` 会把**用户数据写进临时目录** —— 退出即蒸发，
# 而且不报错。所以：
#
#   resource_dir()  只读资源（frontend/dist、模板、内置默认）→ 冻结态取 _MEIPASS
#   data_dir()      持久化数据（runs/settings/tasks/plugins/日志）→ 冻结态取用户目录
#
# 两者**不得互相回落**：data_dir() 绝不能因为 resource_dir() 存在就跟着落到
# _MEIPASS；反之资源目录也不拿数据目录顶上。解析不出来就抛错，不猜。
#
# ## data_dir() 的解析顺序（决策 D-004）
#
#   1. GRAPHITE_DATA_DIR（**只从环境变量读**）
#   2. 便携模式标记        ← R2 只预留位置，未实现（见 _PORTABLE_RESERVED）
#   3. 非冻结态 → <CODE_ROOT>/backend/data（保持零行为变化）
#   4. 冻结态 → 用户应用数据目录（按平台）
#   5. 拿不到用户目录 → 抛 PathResolutionError
#
# 第 1 步为什么不能来自 backend.json：配置文件本身住在数据目录里
# （`config_file()` = data_dir()/backend.json）。若让它指定数据目录，就是
# 「先要知道 A 在哪，才能找到 A 的说明书」—— 循环无解。数据目录只能由进程
# 外部（环境变量 / 启动器 / systemd 单元）决定。
#
# ## 为什么第 3 步必须排在冻结分支之前
#
# 本机常驻实例与服务器部署都是**源码态**，读的是
# backend/data。一旦让它们默认落到用户目录，重启那一刻 runs.json（361 条）、
# settings.json（15 条目）、tasks.json（2 条）全部「消失」—— 文件还在，但服务
# 读的是空目录，且**不会报错**。改默认路径的失败方式不是「不能用」，是「数据
# 看起来没了」，所以源码态必须钉死在老路径上。
# ---------------------------------------------------------------------------

APP_DIR_NAME = "Graphite"
DATA_DIR_ENV = "GRAPHITE_DATA_DIR"
CONFIG_ENV = "GRAPHITE_CONFIG"

# 便携模式：EXE 同级 `data/` 的开关文件名。**R2 不实现检测**（主人裁决：预留不实现），
# 这里只占住优先级第 2 的位置，将来加进来不需要重排顺序、不必改已有测试期望。
_PORTABLE_RESERVED = "portable.flag"


class PathResolutionError(RuntimeError):
    """路径无法确定。**必须让调用方看见** —— 静默回落到另一个目录比直接崩更糟：
    数据会写到一个谁也想不到的地方，而且没有任何报错。"""


def is_frozen() -> bool:
    """是否运行在 PyInstaller 冻结产物里。

    用 `sys.frozen`（PyInstaller / cx_Freeze 都会设），**不用** `sys.executable`
    是否以 `.exe` 结尾来判断 —— 那条在 `python.exe` 上会误判成冻结态。
    """
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> Path:
    """只读资源根。冻结态 = `_MEIPASS`；源码态 = 代码根（`backend/` 的上一级）。"""
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if not meipass:
            raise PathResolutionError(
                "检测到冻结态（sys.frozen）但没有 sys._MEIPASS，无法定位程序资源。"
            )
        return Path(str(meipass))
    return CODE_ROOT


def resource_path(*parts: str) -> Path:
    """资源目录下的子路径。前端静态托管用它，避免各文件再自算一遍 `parents[n]`。"""
    return resource_dir().joinpath(*parts)


def _frozen_app_data_dir(platform: str, env: dict) -> Path:
    """冻结态的默认用户数据目录（按平台）。**纯函数**：平台与 env 都从参数进来，
    不读全局 —— 否则一台机器上无法验证另外两个平台的选择逻辑。

    `platform` 取 `sys.platform` 的值：`win32` / `darwin` / 其他（按 XDG 处理）。
    """
    if platform.startswith("win"):
        base = (env.get("LOCALAPPDATA") or "").strip()
        if not base:
            raise PathResolutionError(
                "冻结态下 %LOCALAPPDATA% 缺失或为空，无法确定用户数据目录。"
                f"请设置 {DATA_DIR_ENV} 显式指定。"
            )
        return Path(base) / APP_DIR_NAME / "data"

    if platform == "darwin":
        home = (env.get("HOME") or "").strip()
        if not home:
            raise PathResolutionError(
                "冻结态下 $HOME 缺失，无法确定用户数据目录。"
                f"请设置 {DATA_DIR_ENV} 显式指定。"
            )
        return Path(home) / "Library" / "Application Support" / APP_DIR_NAME / "data"

    xdg = (env.get("XDG_DATA_HOME") or "").strip()
    if xdg:
        return Path(xdg) / APP_DIR_NAME / "data"
    home = (env.get("HOME") or "").strip()
    if not home:
        raise PathResolutionError(
            "冻结态下 $XDG_DATA_HOME 与 $HOME 都缺失，无法确定用户数据目录。"
            f"请设置 {DATA_DIR_ENV} 显式指定。"
        )
    return Path(home) / ".local" / "share" / APP_DIR_NAME / "data"


def data_dir_source() -> str:
    """`data_dir()` 的结果凭什么来的，供 `/api/health/paths` 自述。"""
    if (os.environ.get(DATA_DIR_ENV) or "").strip():
        return "env"
    if not is_frozen():
        return "source-default"
    return "frozen-default"


def data_dir() -> Path:
    """持久化数据目录（决策 D-004）。

    **纯函数**：不 mkdir、不写文件、不迁移。这一条是刻意的 —— 只有保持纯净，
    「目录是否存在」才能被当作「是否全新安装」的唯一可靠信号，后续迁移逻辑才
    有依据区分「新装」与「用户数据被清空」。
    """
    raw = (os.environ.get(DATA_DIR_ENV) or "").strip()
    if raw:
        p = Path(os.path.expanduser(raw))
        if not p.is_absolute():
            # 相对路径按当前工作目录解析：显式给了就照给的来，但不留悬空的相对值
            p = Path.cwd() / p
        return p.resolve()

    if not is_frozen():
        return BACKEND_DIR / "data"

    return _frozen_app_data_dir(sys.platform, dict(os.environ))


def ensure_data_dir() -> Path:
    """创建数据目录并**实际探测可写**，失败大声抛错（不回落）。

    与 `data_dir()` 分开的原因：Getter 保持纯净，创建/探测这种有副作用的动作只在
    启动路径上发生一次。只在需要写盘时调用，不要放进 import。
    """
    d = data_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise PathResolutionError(
            f"数据目录无法创建：{d}（{e}）。请检查 {DATA_DIR_ENV} 或目录权限。"
        ) from e

    probe = d / f".write-probe-{os.getpid()}"
    try:
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        raise PathResolutionError(
            f"数据目录不可写：{d}（{e}）。请检查 {DATA_DIR_ENV} 或目录权限。"
        ) from e
    return d


def readonly_mode() -> bool:
    """只读副本模式（阶段 10，2026-10-29）。

    与 ai-memory 侧 `web_store.readonly_mode()` 读**同一个**环境变量
    `AI_MEMORY_READONLY` —— 两个包是独立部署单元，不共享 import，所以只共享
    「变量名」这一个契约。服务器副本上设 `1` 后：

    - 本进程对 `index.sqlite3` 一律 `mode=ro` 打开（哪怕调用方想写）；
    - 配合文件权限 440 + 服务降权到 `aim`，三层一起构成「真只读」。

    为什么必须有这一层：`ai-memory` 副本上的 `index` 由 `sync_push` 整文件覆盖，
    服务只是在读它。可写连接不仅没用，还会在建连阶段就因 440 权限抛
    `PermissionError`（OSError 子类），被宽 `except` 吞掉后表现为「看不到数据」。
    """
    return os.environ.get("AI_MEMORY_READONLY", "").strip().lower() in {"1", "true", "yes", "on"}


def config_file() -> Path:
    """实例配置文件（`backend.json`）。

    `GRAPHITE_CONFIG` 显式指定优先级最高（多实例各一份）；否则跟着数据目录走。
    注意它是 data_dir() 的**下游**，绝不能反过来决定 data_dir()。
    """
    raw = (os.environ.get(CONFIG_ENV) or "").strip()
    if raw:
        return Path(os.path.expanduser(raw))
    return data_dir() / "backend.json"


def __getattr__(name: str):
    """兼容垫（PEP 562）：老代码 `paths.DATA_DIR` / `paths.CONFIG_FILE` 仍然可用。

    存在的理由：这两个名字曾是模块级常量，仓库外可能有引用（脚本、临时探针）。
    直接删掉会让它们 ImportError。这里让它们**返回当前解析结果**而不是导入期
    快照 —— 冻结态下仍然是正确值。

    但**不要**在新代码里用它们：`from .paths import DATA_DIR` 会在导入那一刻
    取值，等于把路径重新冻成常量，R2 的改造就白做了。
    """
    if name == "DATA_DIR":
        return data_dir()
    if name == "CONFIG_FILE":
        return config_file()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# 键 → 基线默认值（改造前的硬编码原值，一个字节都不改）
#
# 2026-11-18（阶段 14 开源发布）：`vault_root` / `mcp_json` 两条**不再写死作者的机器路径**。
# 之前钉着作者机器的库根与 mcp.json 绝对路径 ——
# 一份要开源给人用的代码里钉着某个人的家目录，等于把「照文档装不起来」写进默认值。
# 现在的口径：
#   · vault_root：留空 → `_detect_vault_root()` 就近探测（库就在源码树旁边时零配置）；
#     探不到就抛 `PathResolutionError` 并告诉用户该设哪个变量 —— 不猜、不静默。
#   · mcp_json：`Path.home()` 拼出来，跨机器成立（Windows 是 %USERPROFILE%）。
_DEFAULTS: Dict[str, str] = {
    "vault_root": "",
    "mcp_json": str(Path.home() / ".workbuddy" / "mcp.json").replace(os.sep, "/"),
    # 阶段 12（一键更新）：三个键都有意做成可配置 ——
    # 主人 2026-10-25 明确说过「以后仓库里的东西会被换成打包后的 exe」，
    # 那时仓库名、包内目录、tag 前缀都可能变，写死在代码里等于每次都要改代码。
    # enabled 默认 "0"：**只有主人自己部署的实例才该出现更新按钮**，
    # 别人照文档装一份，默认不该能点「覆盖本机代码」。
    "update_enabled": "0",
    "update_repo": "Learnbout/ai-memory",
    "update_package_root": "graphite",
    "update_tag_prefix": "graphite-v",
    # 更新完要重启哪个 systemd 单元。**默认空 = 不自动重启**：
    # 猜错单元名比不重启危险得多（可能重启的是网关、把整站弄挂）。服务器上在
    # 单元里写 Environment=GRAPHITE_UPDATE_SERVICE=ai-memory-graphite 即可开启。
    "update_service": "",
    # 代码根覆盖。默认空 = 用 CODE_ROOT（backend 的上一级）。
    # 存在的唯一理由：让「更新」这件事**能在隔离副本上被验收** —— 否则验一次
    # 就得真覆盖正在跑的代码。顺便也覆盖「以后包结构变了、代码根不在这个位置」。
    "update_code_root": "",
}

# 键 → 环境变量候选（按优先级降序）
_ENV_KEYS: Dict[str, tuple] = {
    "vault_root": ("GRAPHITE_VAULT_ROOT", "AI_MEMORY_DIR"),
    "mcp_json": ("GRAPHITE_MCP_JSON",),
    "update_enabled": ("GRAPHITE_UPDATE_ENABLED",),
    "update_repo": ("GRAPHITE_UPDATE_REPO",),
    "update_package_root": ("GRAPHITE_UPDATE_PACKAGE_ROOT",),
    "update_tag_prefix": ("GRAPHITE_UPDATE_TAG_PREFIX",),
    "update_service": ("GRAPHITE_UPDATE_SERVICE",),
    "update_code_root": ("GRAPHITE_CODE_ROOT",),
}

_file_cache: Dict[str, Any] | None = None


def _file_conf() -> Dict[str, Any]:
    """读 backend.json；读不到就当空配置（不抛，让默认值兜住）。"""
    global _file_cache
    if _file_cache is not None:
        return _file_cache
    try:
        with open(config_file(), "r", encoding="utf-8") as f:
            data = json.load(f)
        _file_cache = data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        _file_cache = {}
    return _file_cache


def reset_cache() -> None:
    """丢掉配置文件缓存（测试与热重载用）。"""
    global _file_cache
    _file_cache = None


def _resolve(key: str) -> str:
    for env_key in _ENV_KEYS.get(key, ()):
        raw = os.environ.get(env_key)
        if raw and raw.strip():
            return raw.strip()
    conf = _file_conf()
    for conf_key in (key, key.replace("_", "")):
        raw = conf.get(conf_key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    default = _DEFAULTS[key]
    if default:
        return default
    # vault_root 的默认值留空 = 交给「就近探测」。探不到就抛，绝不返回空串 ——
    # 空串会一路传到 `vault_db.db_path_for("")` 变成相对路径，最后报一句
    # 「索引库不存在」，把「你还没配库根」伪装成「库文件没了」。
    if key == "vault_root":
        return _detect_vault_root()
    return ""


def _detect_vault_root() -> str:
    """就近找库根（阶段 14 开源后的零配置路径）。

    开源用户最常见的布局是把库放在代码旁边，所以按「离源码树由近到远」探：

      1. `<CODE_ROOT>/记忆库`        —— 库和 graphite 同级（本仓推荐的目录布局）
      2. `<CODE_ROOT>/../记忆库`     —— 库在上一层（作者本机就是这种：ai-memory/记忆库）
      3. `<CODE_ROOT>` 本身          —— 直接把 graphite 放进库根里

    判据取「有 `数据库/` 或 `脚本/` 目录」——这是库根的两个标志物，比猜文件名可靠。
    三条都不成立就抛 `PathResolutionError`，把「该设哪个变量」写进错误里。
    """
    candidates = [
        CODE_ROOT / "记忆库",
        CODE_ROOT / "vault",          # 安装器的布局：<目标>/vault（2026-11-19 补）
        CODE_ROOT.parent / "记忆库",
        CODE_ROOT,
    ]
    for cand in candidates:
        try:
            if (cand / "数据库").is_dir() or (cand / "脚本").is_dir():
                return str(cand).replace(os.sep, "/")
        except OSError:
            continue
    raise PathResolutionError(
        "找不到记忆库根（vault_root）。三条就近探测都不成立：%s\n"
        "请用下面任意一种方式指定：\n"
        "  · 环境变量 GRAPHITE_VAULT_ROOT=/path/to/记忆库\n"
        "  · %s 里写 {\"vault_root\": \"/path/to/记忆库\"}\n"
        % (" / ".join(str(c) for c in candidates), config_file())
    )


def vault_root() -> str:
    """记忆库**根**（不是 md 扫描目录）。

    合法值有两种（`vault_db.DB_REL_CANDIDATES` 都认）：
    - `.../ai-memory`（库在 `<根>/记忆库/数据库/index.sqlite3`）
    - `.../ai-memory/记忆库`（库在 `<根>/数据库/index.sqlite3`）

    服务器现状是第三种：`AI_MEMORY_DIR=/opt/ai-memory/vault`，库在
    `/opt/ai-memory/vault/数据库/index.sqlite3` —— 正好等于第二种，无需特殊处理。

    2026-11-18：没配任何东西时不再回落到作者的 `C:/Users/...`，改为就近探测
    （见 `_detect_vault_root`），探不到直接抛错。
    """
    return _resolve("vault_root")


def mcp_json() -> str:
    """MCP 服务清单（`mcpServers` 那棵树）。

    服务器上没有 `~/.workbuddy/mcp.json`，由部署脚本写一份到
    `/opt/ai-memory/graphite/mcp.json`，再用环境变量指过来。
    """
    return _resolve("mcp_json")


# ── 运行形态（阶段 26，2026-11-18）────────────────────────────────────────
# 两个发行形态共用一套代码，靠这个开关决定「注册哪些路由 / 给哪些能力」：
#
#   local   —— 本地版：单人、零登录、只监听 127.0.0.1。
#              admin / 聊天 / 审计 / 运维台这些「多人协作才需要」的路由**根本不注册**，
#              访问它们是 404（不是 403）—— 403 等于告诉别人「这儿有个门，只是没钥匙」。
#   central —— 中心化版：登录、账号、聊天、审计、8766 对公。
#
# **默认值刻意是 local（fail-safe）**：忘了配也不会把一个未登录的实例暴露到公网。
# 真正的默认由「装的是哪个包」决定，安装器会把显式值写进 `backend.json` 或单元环境变量
# （鱼片 2026-11-18 定稿：本地包→local，中心化包→central，不让用户自己去选）。
MODE_ENV = "GRAPHITE_MODE"
MODE_LOCAL = "local"
MODE_CENTRAL = "central"
MODES = (MODE_LOCAL, MODE_CENTRAL)


def mode() -> str:
    """当前运行形态：`local` | `central`。

    优先级：`GRAPHITE_MODE` 环境变量 > `backend.json` 的 `mode` > 默认 `local`。
    非法值**显式抛错**，不静默回落 —— 拼错一个词就把中心化版降级成本地版
    （或反过来）这种事故，比启动失败难查一百倍。
    """
    raw = (os.environ.get(MODE_ENV) or "").strip().lower()
    if not raw:
        conf = _file_conf()
        raw = str(conf.get("mode") or conf.get("MODE") or "").strip().lower()
    if not raw:
        return MODE_LOCAL
    if raw not in MODES:
        raise PathResolutionError(
            "GRAPHITE_MODE=%r 不是合法形态，只接受 %s（本地版）或 %s（中心化版）"
            % (raw, MODE_LOCAL, MODE_CENTRAL)
        )
    return raw


def is_local() -> bool:
    return mode() == MODE_LOCAL


def update_settings() -> Dict[str, Any]:
    """一键更新的进程级配置（阶段 12，2026-10-25）。

    **token 刻意不在这里解析。** 它只从环境变量 `GRAPHITE_GH_TOKEN` 读，
    绝不进本函数的返回值 —— `describe()` 的结果会经 `/api/health/paths`
    直接吐给前端，任何「配置自述」都不该把凭据念出来。

    返回：
        enabled       是否允许在本实例上执行更新（默认 False）
        repo          owner/name
        package_root  仓库里装着这套代码的目录（默认 graphite）
        tag_prefix    tag 前缀（`graphite-v1.2.0`）；留空 = tag 就是版本号
        service       更新后要重启的 systemd 单元；空 = 不自动重启
        code_root     本机代码根
        backup_root   更新前的回滚点目录
    """
    enabled = str(_resolve("update_enabled")).strip().lower() in ("1", "true", "yes", "on")
    return {
        "enabled": enabled,
        "repo": str(_resolve("update_repo")).strip(),
        "package_root": str(_resolve("update_package_root")).strip().strip("/"),
        "tag_prefix": str(_resolve("update_tag_prefix")).strip(),
        "service": str(_resolve("update_service")).strip(),
        # 空串 → 退回真实代码根；给了就按给的来（隔离验收 / 特殊部署结构用）
        "code_root": str(_resolve("update_code_root")).strip() or str(CODE_ROOT),
        # 服务器上 CODE_ROOT=/opt/ai-memory/graphite → 备份落在 /opt/ai-memory/backup，
        # 与阶段 0/11 的备份同一个院子，运维时不用记第二个地方
        "backup_root": str(
            (Path(str(_resolve("update_code_root")).strip() or CODE_ROOT)).parent / "backup"
        ),
    }


def ensure_import_path() -> str:
    """把**真实库目录**与 `<库>/脚本` 插进 sys.path，返回库目录。

    为什么需要：`acl.py` / `audit.py` / `db_paths.py` / `admin_api.py` 都是
    **库的一部分**，不是 backend 包的一部分。backend 进程默认的 sys.path 里
    没有它，import 它们会 ModuleNotFoundError。

    为什么不能直接用 `vault_root()` 拼：`vault_root()` 有两种合法值 ——
    记忆库本身，或者它的**上一级**（本机现状就是上一级 `.../Desktop/ai-memory`，
    库在 `.../Desktop/ai-memory/记忆库/`）。直接 `root / "脚本"` 在本机会得到
    `ai-memory/脚本`（不存在），于是 `import admin_api` 当场 ModuleNotFoundError。
    判据取「谁里面有 db_paths.py」—— 那是库根唯一可靠的标志物，不靠猜层级。

    为什么集中成一个函数（2026-10-23 连踩两次）：`chat_api.py` 最初写成
    「先 `from .. import chat_db`、后面才插 sys.path」—— Python 按行执行 import，
    chat_db 顶部的 `import db_paths` 在 sys.path 还没插时就跑了，进程起不来。
    五处各写一遍 sys.path.insert，顺序和路径各错一次就炸一次。
    """
    root = Path(vault_root()).resolve()
    candidates = [root, root / "记忆库"]
    vault = next((c for c in candidates if (c / "db_paths.py").is_file()), root)
    for p in (str(vault), str(vault / "脚本")):
        if p not in sys.path:
            sys.path.insert(0, p)
    return str(vault)


def describe() -> Dict[str, Any]:
    """/api/admin 与排障用的自述：每个键的解析结果 + 它从哪来的。

    2026-10-27（R2）新增四项：`dataDir` / `resourceDir` / `frozen` / `meipass`。
    这四个是冻结版排障的唯一线上窗口 —— 「数据到底写进哪个目录」光看日志看不出来，
    而这里的 `dataDir.source` 会直接告诉你它是 env 给的、还是源码态默认、还是用户目录。
    """
    out: Dict[str, Any] = {"configFile": str(config_file()), "keys": {}}

    d = data_dir()
    meipass = getattr(sys, "_MEIPASS", None)
    r = resource_dir()
    out["dataDir"] = {
        "value": str(d),
        "source": data_dir_source(),
        "exists": d.exists(),
        "isDir": d.is_dir(),
    }
    out["resourceDir"] = {"value": str(r), "exists": r.is_dir()}
    out["frozen"] = is_frozen()
    out["meipass"] = str(meipass) if meipass else None
    # 不变量 I1 的自证：数据目录不得等于、也不得位于解包临时目录之内
    out["dataDirInsideMeipass"] = bool(meipass) and str(d).startswith(str(meipass))

    for key in _DEFAULTS:
        src = "default"
        for env_key in _ENV_KEYS.get(key, ()):
            raw = os.environ.get(env_key)
            if raw and raw.strip():
                src = f"env:{env_key}"
                break
        else:
            conf = _file_conf()
            if any(isinstance(conf.get(k), str) and conf.get(k, "").strip() for k in (key, key.replace("_", ""))):
                src = "file"
        value = _resolve(key)
        p = Path(value)
        out["keys"][key] = {
            "value": value,
            "source": src,
            "exists": p.exists(),
            "isDir": p.is_dir(),
        }
    return out
