#!/usr/bin/env python3
"""dnsq.py —— 不依赖 dig/nslookup 的 DNS 查询小工具（纯标准库，一个文件）。

为什么存在：服务器（AlmaLinux）没装 bind-utils，`dig` / `nslookup` 都不在，
而排障时经常需要「直接问某台权威服务器，看它到底返回什么」——
比如判断委派切没切、阿里云和 Cloudflare 对同一条记录各回了什么。

用法：
    python dnsq.py www.example.com @ns1.example.net @ns2.example.net
    python dnsq.py example.com NS @8.8.8.8
    python dnsq.py www.example.com A            # 不打 @ 就用系统 DNS

典型判据（2026-10-26 实战）：
    Cloudflare 当权威 → www 回 A 记录（橙云边缘 IP），能用
    阿里云当权威     → www 回 CNAME → *.cfargotunnel.com，解析到 fd10:: 私有地址，不可用
    同一份记录、两个权威，返回的东西完全不同 —— 所以必须逐台问，不能只看一家。
"""

from __future__ import annotations

import random
import socket
import struct
import sys

TYPES = {
    "A": 1, "NS": 2, "CNAME": 5, "SOA": 6, "PTR": 12, "MX": 15,
    "TXT": 16, "AAAA": 28, "SRV": 33, "DS": 43, "DNSKEY": 48,
}
TYPE_NAMES = {v: k for k, v in TYPES.items()}
RCODES = {0: "NOERROR", 1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN", 4: "NOTIMP", 5: "REFUSED"}


def encode_name(name: str) -> bytes:
    out = b""
    for label in name.rstrip(".").split("."):
        raw = label.encode("idna")
        if len(raw) > 63:
            raise ValueError(f"标签过长: {label}")
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def decode_name(data: bytes, off: int) -> tuple[str, int]:
    parts: list[str] = []
    seen = 0
    while True:
        if off >= len(data):
            raise ValueError("报文截断")
        length = data[off]
        if length == 0:
            off += 1
            break
        if length & 0xC0 == 0xC0:
            pointer = struct.unpack(">H", data[off:off + 2])[0] & 0x3FFF
            sub, _ = decode_name(data, pointer)
            parts.append(sub)
            off += 2
            return ".".join(parts), off
        parts.append(data[off + 1:off + 1 + length].decode("latin1"))
        off += 1 + length
        seen += 1
        if seen > 128:
            raise ValueError("名称链过深")
    return ".".join(parts), off


def build_query(name: str, qtype: int) -> tuple[int, bytes]:
    tid = random.randint(0, 0xFFFF)
    header = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)  # RD=1
    return tid, header + encode_name(name) + struct.pack(">HH", qtype, 1)


def query(server: str, name: str, qtype: int, timeout: float = 5.0) -> dict:
    tid, pkt = build_query(name, qtype)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(pkt, (server, 53))
        data, _ = sock.recvfrom(65535)
    finally:
        sock.close()

    if len(data) < 12:
        return {"error": "响应过短"}
    got_id = struct.unpack(">H", data[0:2])[0]
    if got_id != tid:
        return {"error": f"事务 ID 不匹配 ({tid} != {got_id})"}
    flags = struct.unpack(">H", data[2:4])[0]
    rcode = flags & 0xF
    qdcount, ancount = struct.unpack(">HH", data[4:8])

    off = 12
    for _ in range(qdcount):
        _, off = decode_name(data, off)
        off += 4

    answers = []
    for _ in range(ancount):
        owner, off = decode_name(data, off)
        if off + 10 > len(data):
            break
        rtype, rclass, ttl, rdlen = struct.unpack(">HHIH", data[off:off + 10])
        off += 10
        rdata_off = off
        rdata = data[off:off + rdlen]
        if rtype == 1 and rdlen == 4:
            value = socket.inet_ntoa(rdata)
        elif rtype == 28 and rdlen == 16:
            value = socket.inet_ntop(socket.AF_INET6, rdata)
        elif rtype in (2, 5, 12):
            value, _ = decode_name(data, rdata_off)
        elif rtype == 15:
            pref = struct.unpack(">H", rdata[:2])[0]
            host, _ = decode_name(data, rdata_off + 2)
            value = f"{pref} {host}"
        elif rtype == 6:
            mname, p = decode_name(data, rdata_off)
            rname, p = decode_name(data, p)
            value = f"{mname} {rname}"
        elif rtype == 16:
            parts = []
            i = 0
            while i < len(rdata):
                ln = rdata[i]
                parts.append(rdata[i + 1:i + 1 + ln].decode("utf-8", "replace"))
                i += 1 + ln
            value = "".join(parts)
        else:
            value = rdata.hex()
        answers.append({
            "owner": owner,
            "type": TYPE_NAMES.get(rtype, str(rtype)),
            "ttl": ttl,
            "value": value,
        })
        off += rdlen

    return {
        "rcode": rcode,
        "rcode_name": RCODES.get(rcode, str(rcode)),
        "ancount": ancount,
        "answers": answers,
        "flags_ad": bool(flags & 0x0020),
    }


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2

    name = argv[1]
    rest = argv[2:]
    qtype_name = "A"
    servers: list[str] = []
    for arg in rest:
        if arg.startswith("@"):
            servers.append(arg[1:])
        elif arg.upper() in TYPES:
            qtype_name = arg.upper()
        else:
            print(f"无法识别的参数: {arg}（服务器请写成 @1.1.1.1 的形式）", file=sys.stderr)
            return 2

    if not servers:
        # 没指定服务器就用系统配置的第一个 nameserver
        try:
            servers = [open("/etc/resolv.conf", encoding="utf-8").read()
                       .split("nameserver")[1].split()[0]]
        except Exception:
            servers = ["8.8.8.8"]

    qtype = TYPES[qtype_name]
    exit_code = 0
    for server in servers:
        try:
            ip = socket.gethostbyname(server)
        except OSError as exc:
            print(f"=== {server} ===  解析失败: {exc}")
            exit_code = 1
            continue
        print(f"=== {server} ({ip})  查询 {name} {qtype_name} ===")
        try:
            res = query(ip, name, qtype)
        except Exception as exc:
            print(f"  失败: {exc}")
            exit_code = 1
            continue
        if "error" in res:
            print(f"  失败: {res['error']}")
            exit_code = 1
            continue
        print(f"  rcode={res['rcode_name']}  应答数={res['ancount']}  AD(已验证)={res['flags_ad']}")
        for ans in res["answers"]:
            print(f"    {ans['owner']}  TTL={ans['ttl']}  {ans['type']}  {ans['value']}")
        if res["rcode"] != 0:
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
