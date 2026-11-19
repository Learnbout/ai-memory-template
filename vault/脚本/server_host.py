"""服务器地址解析（2026-10-27）。

## 为什么需要这个文件

服务器 IP 走 DHCP，在作者的机器上已漂过三次（局域网段换一次就换一个地址）。
`sync_push.py` / `deploy_graphite.py` 都靠这个地址，写死在代码里就必然有一天连不上，
而表现只是 paramiko 的一句 `No existing session` / `Connection timed out`，
看不出真因（2026-10-27 晚上就这样浪费了一轮排查）。

一个事实源：所有需要连服务器的脚本都从这里取地址，不许各自再写一遍默认值。

## 解析顺序（先便宜后贵）

 1. 环境变量 `AIM_SSH_HOST` —— 显式指定，永远优先，不缓存
 2. 缓存文件 `<vault>/.workbuddy/server_host` —— 上次探到的地址，先用一次 TCP 探活确认
 3. 扫描本机所在网段 —— 并发探 254 个地址的 8766 端口 + `/healthz`
 4. 都不通 —— 抛 `ServerHostNotFound`，把「怎么手工指定」写进报错

## 判据为什么是 8766 + /healthz，不是 ping

- ping 会被 firewalld 拦，也可能被本机的代理软件（Clash TUN 之类）伪造；
- 小坑实测：本机 `bash` 的 `/dev/tcp` 在同一台机器上会把大量不存在的 IP 报成「通」，
  所以「连得上」这件事只能由真实协议握手确认，不能靠裸 TCP。
- 8766 是 ai-memory 的门禁端口，`/healthz` 返回 200 基本只有我们的服务会回。

## 网段怎么取

`getaddrinfo(hostname)` 而不是 UDP connect 到外部地址那种小技巧 —— 实测本机后者
返回 `198.18.0.1`（Clash TUN 的地址），是代理出口不是局域网地址，拿它扫等于扫空气。
"""
from __future__ import annotations

import os
import socket
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

VAULT = Path(__file__).resolve().parent.parent
CACHE = VAULT / ".workbuddy" / "server_host"

WEB_PORT = 8766          # ai-memory 门禁（L3 Web）端口，探测判据
SSH_PORT = 22
PROBE_TIMEOUT = 0.45     # 单个地址的 TCP 探活超时（秒）
HTTP_TIMEOUT = 1.5       # healthz 请求超时（秒）
WORKERS = 96             # 并发数


class ServerHostNotFound(RuntimeError):
    """扫完所有候选网段仍没找到服务器。"""


def _is_usable(ip: str) -> bool:
    if ip.startswith(("127.", "0.", "255.")):
        return False
    if ip.startswith("169.254."):          # 链路本地，没配 DHCP 时的自留地
        return False
    if ip.startswith(("198.18.", "198.19.")):  # Clash / TUN 类软件的 fake 网段
        return False
    return ip.count(".") == 3


def candidate_subnets() -> list[str]:
    """本机所在的所有 /24（去重、去回环、去 fake 网段）。"""
    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted({ip.rsplit(".", 1)[0] for ip in ips if _is_usable(ip)})


def _tcp_open(ip: str, port: int, timeout: float = PROBE_TIMEOUT) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def _healthz(ip: str) -> bool:
    url = f"http://{ip}:{WEB_PORT}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as r:
            return r.status == 200
    except Exception:
        return False


def probe(ip: str) -> bool:
    """这个地址像不像我们的服务器：8766 通、healthz 200、22 也开。"""
    return _tcp_open(ip, WEB_PORT) and _healthz(ip)


def _read_cache() -> str | None:
    try:
        for line in CACHE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    except OSError:
        return None
    return None


def _write_cache(ip: str) -> None:
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(
            "# ai-memory 服务器地址缓存（脚本/server_host.py 自动写入，可随时删除重探）\n"
            f"{ip}\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def scan(verbose: bool = True) -> str:
    """并发扫所有候选网段，返回命中的地址。"""
    hits: list[str] = []
    for subnet in candidate_subnets():
        hosts = [f"{subnet}.{i}" for i in range(1, 255)]
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for ip, ok in zip(hosts, pool.map(probe, hosts)):
                if ok:
                    hits.append(ip)
        if verbose:
            print(f"[server_host] 扫 {subnet}.0/24 -> {hits or '无命中'}", file=sys.stderr)
        if hits:
            break
    if not hits:
        raise ServerHostNotFound(
            "扫遍 " + ", ".join(s + ".0/24" for s in candidate_subnets()) + " 都没找到 ai-memory 服务。\n"
            "  三种可能：① 服务器换了网段（不在本机所在的 /24 里）；② 服务没起；③ 防火墙拦了 8766。\n"
            "  手工指定：export AIM_SSH_HOST=<服务器IP> 后再跑。"
        )
    if len(hits) > 1:
        print(f"[server_host] 命中多个：{hits} —— 取第一个，若不对请用 AIM_SSH_HOST 指定", file=sys.stderr)
    return hits[0]


def resolve(verbose: bool = True) -> str:
    """env → 缓存 → 扫描。返回可用的服务器地址。"""
    env = os.environ.get("AIM_SSH_HOST", "").strip()
    if env:
        return env

    cached = _read_cache()
    if cached and probe(cached):
        if verbose:
            print(f"[server_host] 用缓存地址 {cached}", file=sys.stderr)
        return cached

    if verbose:
        print("[server_host] 缓存无效，开始扫描本机网段 ...", file=sys.stderr)
    ip = scan(verbose=verbose)
    _write_cache(ip)
    if verbose:
        print(f"[server_host] 找到服务器 {ip}（已写入缓存 {CACHE}）", file=sys.stderr)
    return ip


if __name__ == "__main__":
    print(resolve())
