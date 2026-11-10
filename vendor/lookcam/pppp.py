"""PPPP/PPCS: LAN discovery of cameras without the vendor SDK.

The protocol was recovered by disassembling libPPCS_API.so:
  * packet header    — magic 0xF1, cmd, length BE16  (cs2p2p_PPPP_Proto_Write_Header)
  * LAN search       — 4 bytes F1 30 00 00 in broadcast  (Proto_Send_LanSerch)
  * search port      — 32108 (0x7D6C)  (cs2p2p_PPPP_thread_recv_LanSearch)
  * camera reply     — PunchPkt 0x41: 7-byte id + BE32 + 7-byte field

Only discovery is implemented (no session): this is enough to find a camera,
obtain its identifier and address without bringing up the SDK.
"""
from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass, field

MAGIC = 0xF1
LAN_SEARCH_PORT = 32108          # from cs2p2p_setup_listen_port(0x7d6c)

CMD_NAMES = {
    0x00: "Hello",
    0x01: "HelloAck",
    0x02: "HelloTo",
    0x03: "HelloToAck",
    0x08: "QueryDID",
    0x09: "QueryDIDAck",
    0x10: "DevLogin",
    0x11: "DevLoginAck",
    0x20: "P2PReq",
    0x21: "P2PReqAck",
    0x30: "LanSearch",
    0x40: "PunchTo",
    0x41: "PunchPkt",
    0x42: "P2PRdy",
    0x43: "TryLanTcp",
    0x44: "P2PRdyLanTcp",
    0x60: "RSLgn",
    0x61: "RSLgnAck",
    0x69: "ListReqAck",
    0x70: "RlyHello",
    0x71: "RlyHelloAck",
    0x80: "RlyReq",
    0x81: "RlyReqAck",
    0x82: "RlyTo",
    0x83: "RlyPkt",
    0x84: "RlyRdy",
    0xE0: "Alive",
    0xE1: "AliveAck",
    0xF0: "Close",
}


@dataclass
class Reply:
    addr: tuple[str, int]
    cmd: int
    payload: bytes
    raw: bytes
    decoded: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        return CMD_NAMES.get(self.cmd, f"cmd0x{self.cmd:02x}")


def build(cmd: int, payload: bytes = b"") -> bytes:
    """Build a packet: magic | cmd | len(BE16) | payload."""
    return struct.pack(">BBH", MAGIC, cmd & 0xFF, len(payload)) + payload


def parse(data: bytes) -> tuple[int, bytes] | None:
    """Parse the header; return (cmd, payload) or None."""
    if len(data) < 4 or data[0] != MAGIC:
        return None
    cmd = data[1]
    length = struct.unpack(">H", data[2:4])[0]
    payload = data[4:4 + length] if length else data[4:]
    return cmd, payload


# ----------------------------------------------------------------------- decoders

def _cstr(data: bytes) -> str:
    return data.split(b"\x00", 1)[0].decode("utf-8", "replace")


def _ip_be(raw: bytes) -> str:
    return ".".join(str(b) for b in raw[:4])


def decode(cmd: int, payload: bytes) -> dict:
    """Parse the payload of known messages (according to layouts from the Write_* functions)."""
    p = payload
    if cmd == 0x41 and len(p) >= 0x14:            # PunchPkt
        return {
            "id": _cstr(p[0:8]),
            "value": struct.unpack(">I", p[8:12])[0],
            "field2": _cstr(p[0x0C:0x14]),
        }
    if cmd == 0x01 and len(p) >= 8:               # HelloAck
        family, port = struct.unpack(">HH", p[0:4])
        return {"family": family, "port": port, "ip": _ip_be(p[4:8])}
    if cmd == 0x11 and len(p) >= 1:               # DevLoginAck
        return {"result": p[0]}
    if cmd == 0x69 and len(p) >= 4:               # ListReqAck: list of addresses
        count = p[0]
        items = []
        offset = 4
        for _ in range(count):
            if offset + 8 > len(p):
                break
            family, port = struct.unpack(">HH", p[offset:offset + 4])
            items.append({"family": family, "port": port,
                          "ip": _ip_be(p[offset + 4:offset + 8])})
            offset += 16
        return {"count": count, "items": items}
    if cmd == 0x09:                               # QueryDIDAck (payload not parsed)
        return {"raw": p[:32].hex(" ")}
    return {}


# -------------------------------------------------------------------- discovery

def lan_search(target: str | None = None, port: int = LAN_SEARCH_PORT,
               timeout: float = 2.5, broadcast: bool = True,
               extra_packets: bool = True, bind_port: int | None = None,
               source_ip: str | None = None) -> list[Reply]:
    """Send F1 30 00 00 and collect camera replies.

    Verified on a live camera: it answers only to **broadcast** sent
    from source port 32108 (from an ephemeral port and to unicast it stays silent).
    That is why the socket is bound to 32108 by default.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    want_bind = bind_port if bind_port is not None else LAN_SEARCH_PORT
    bound = None
    candidates = [("0.0.0.0", want_bind), ("0.0.0.0", 0)]
    if source_ip:
        candidates = [(source_ip, want_bind), (source_ip, 0)] + candidates
    for candidate in candidates:
        try:
            s.bind(candidate)
            bound = s.getsockname()
            break
        except OSError:
            continue
    if bound is None:
        s.close()
        raise OSError("не удалось открыть UDP-сокет для LAN-поиска")
    s.settimeout(0.3)

    packets = [build(0x30)]                       # LanSearch, 4 bytes
    if extra_packets:
        packets.append(build(0x00))               # Hello
        packets.append(build(0x68))               # ListReq

    for pkt in packets:
        for dst in ([("255.255.255.255", port)] if broadcast else []) + \
                   ([(target, port)] if target else []):
            try:
                s.sendto(pkt, dst)
            except OSError:
                pass

    replies: dict[tuple[str, int, bytes], Reply] = {}
    own = set()
    try:
        import socket as _s
        own = {info[4][0] for info in _s.getaddrinfo(_s.gethostname(), None, _s.AF_INET)}
    except Exception:
        pass
    sent_bytes = set(packets)
    end = time.time() + timeout
    while time.time() < end:
        try:
            data, addr = s.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        if addr[0] in own or data in sent_bytes:
            continue                              # our own broadcast came back
        parsed = parse(data)
        if not parsed:
            continue
        cmd, payload = parsed
        key = (addr[0], addr[1], data)
        if key in replies:
            continue
        replies[key] = Reply(addr, cmd, payload, data, decode(cmd, payload))
    s.close()
    return sorted(replies.values(), key=lambda r: (r.addr[0], r.addr[1]))


# DID prefixes that the application attributes to the "MyKJ" platform
# (PpcsIdConverter.isMykjPlatform in the APK).
MYKJ_PREFIXES = ("FHBB", "GHBB", "HHBB", "IHBB", "JHBB", "KHBB", "THBB", "XHBB")


# "Virtual prefix <-> real" mapping from PpcsIdConverter (APK).
VIRTUAL_TO_REAL = {
    "9": "AHCC", "B": "BHCC", "C": "CHCC", "F": "FHBB", "G": "GHBB",
    "H": "HHBB", "I": "IHBB", "J": "JHBB", "K": "KHBB", "T": "THBB", "X": "XHBB",
}


def normalize_did(did: str) -> str:
    """Bring a DID to the form the SDK expects (cs2p2p_PPPP__DIDFormat).

    Function from the .so: upper case, '-' inserted at the "letters<->digits" boundary,
    repeated '-' collapsed, length limited to 0x40.
    """
    did = did.strip().upper()
    if did.startswith(("PPCS-", "RTOS-")):
        return did
    out: list[str] = []
    prev_alpha = None
    for ch in did:
        if not (ch.isalnum() or ch == "-"):
            continue
        if ch == "-":
            if out and out[-1] != "-":
                out.append("-")
            prev_alpha = None
            continue
        alpha = ch.isalpha()
        if prev_alpha is not None and alpha != prev_alpha and out and out[-1] != "-":
            out.append("-")
        out.append(ch)
        prev_alpha = alpha
    return "".join(out).strip("-")[:0x40]


def real_from_virtual(virtual_id: str) -> str:
    """Recover the real DID from the application's "virtual" id (G… → GHBB…)."""
    virtual_id = virtual_id.strip().upper()
    if not virtual_id:
        return ""
    if virtual_id.startswith(tuple(VIRTUAL_TO_REAL.values())):
        return virtual_id
    head = VIRTUAL_TO_REAL.get(virtual_id[0])
    if not head:
        return virtual_id
    rest = virtual_id[1:].lstrip("-")
    return f"{head}{rest}"


def devlogin_packet(did: str, port: int = LAN_SEARCH_PORT, addr: str = "0.0.0.0",
                    account: bytes = b"", key: bytes = b"\x00\x00\x00",
                    flag: int = 1) -> bytes:
    """Build DevLogin (0x10) exactly according to the Write_DevLgn/Send_DevLgn layout (40-byte payload)."""
    p = bytearray(40)
    p[0:len(account[:7])] = account[:7]
    struct.pack_into(">I", p, 8, 1)
    did7 = did.encode("ascii", "replace")[:7].ljust(7, b"\x00")
    p[0x0C:0x13] = did7
    p[0x14] = flag & 0xFF
    p[0x15:0x18] = key[:3].ljust(3, b"\x00")
    struct.pack_into(">HH", p, 0x18, 2, port)
    try:
        p[0x1C:0x20] = socket.inet_aton(addr)
    except OSError:
        p[0x1C:0x20] = b"\x00\x00\x00\x00"
    return struct.pack(">BBH", MAGIC, 0x10, len(p)) + bytes(p)


def check_device_packet(str_a: bytes = b"", str_b: bytes = b"", value: int = 1) -> bytes:
    """CheckDevice (0x05) — 28 bytes: F1 05 00 18 + [7] string, u32 BE, [7] string."""
    p = bytearray(24)
    p[0:len(str_a[:7])] = str_a[:7]
    struct.pack_into(">I", p, 8, value & 0xFFFFFFFF)
    p[0x0C:0x13] = str_b[:7].ljust(7, b"\x00")
    return struct.pack(">BBH", MAGIC, 0x05, len(p)) + bytes(p)


def p2p_req_packet(str_a: bytes = b"", str_b: bytes = b"", value: int = 1,
                   local_port: int = LAN_SEARCH_PORT,
                   local_ip: str = "0.0.0.0") -> bytes:
    """P2PReq (0x20) — 40 bytes: strings + BE number + local address for hole punching."""
    p = bytearray(36)
    p[0:len(str_a[:7])] = str_a[:7]
    struct.pack_into(">I", p, 8, value & 0xFFFFFFFF)
    p[0x0C:0x13] = str_b[:7].ljust(7, b"\x00")
    struct.pack_into(">HH", p, 0x14, 2, local_port)
    try:
        p[0x18:0x1C] = socket.inet_aton(local_ip)
    except OSError:
        p[0x18:0x1C] = b"\x00\x00\x00\x00"
    return struct.pack(">BBH", MAGIC, 0x20, len(p)) + bytes(p)


# Message order seen in cs2p2p_PPPP_Connect_Do:
#   Hello(0x00) -> CheckDevice(0x05) -> P2PReq(0x20) | LanSearch(0x30) -> Punch(0x40/0x41) -> P2PRdy(0x42)
SESSION_SEQUENCE = (0x00, 0x05, 0x20)


def send_devlogin(did: str, host: str, ports=(32108,), source_ip: str | None = None,
                  timeout: float = 1.2, account: bytes = b"",
                  key: bytes = b"\x00\x00\x00") -> list[Reply]:
    """Send DevLogin to the given ports and collect replies."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    for cand in ([(source_ip, LAN_SEARCH_PORT)] if source_ip else []) + \
                [("0.0.0.0", LAN_SEARCH_PORT), ("0.0.0.0", 0)]:
        try:
            s.bind(cand)
            break
        except OSError:
            continue
    s.settimeout(0.3)
    pkt = devlogin_packet(did, port=LAN_SEARCH_PORT, addr=source_ip or "0.0.0.0",
                          account=account, key=key)
    for port in ports:
        try:
            s.sendto(pkt, (host, port))
        except OSError:
            pass
    replies: dict[tuple[str, int, bytes], Reply] = {}
    end = time.time() + timeout
    while time.time() < end:
        try:
            data, addr = s.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        if data == pkt:
            continue
        parsed = parse(data)
        if not parsed:
            continue
        cmd, payload = parsed
        replies[(addr[0], addr[1], data)] = Reply(addr, cmd, payload, data,
                                                  decode(cmd, payload))
    s.close()
    return sorted(replies.values(), key=lambda r: (r.addr[0], r.addr[1]))


def did_hint(did: str) -> str:
    if not did:
        return ""
    if did.startswith(MYKJ_PREFIXES):
        return " (MyKJ-платформа — connect через PPCS_Connect)"
    if did.startswith("PPCS-") or did.startswith("RTOS-"):
        return " (PPCS-нумерация — connect через PPCS_ConnectByServer)"
    return " (сторонняя нумерация — connect через PPCS_ConnectByServer)"


def describe(reply: Reply) -> str:
    """Line for output."""
    d = reply.decoded
    extra = ""
    if reply.cmd == 0x41:
        extra = (f"id={d.get('id')!r}{did_hint(d.get('id', ''))} "
                 f"value=0x{d.get('value', 0):08x} field2={d.get('field2')!r}")
    elif reply.cmd == 0x01:
        extra = f"{d.get('ip')}:{d.get('port')} (family={d.get('family')})"
    elif reply.cmd == 0x11:
        extra = f"result={d.get('result')}"
    elif reply.cmd == 0x69:
        extra = f"count={d.get('count')} " + ", ".join(
            f"{i['ip']}:{i['port']}" for i in d.get("items", []))
    else:
        extra = reply.payload[:24].hex(" ")
    return f"{reply.addr[0]}:{reply.addr[1]:<5} {reply.name:<12} len={len(reply.payload):<4} {extra}"
