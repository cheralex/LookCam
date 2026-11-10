"""One UDP socket :32108 for the whole service and the PPPP demultiplexer.

Why one socket: the camera accepts LanSearch only from source port 32108, and
the active session continues on the same socket (HANDOFF §4.2). Five cameras —
five addresses; packets are routed by source.

The endpoint knows nothing about commands: it hands `D1 ...` payloads to the
registered session and answers service packets (Alive, LanSearch).
"""
from __future__ import annotations

import asyncio
import logging
import socket
import struct
import time
from dataclasses import dataclass, field

from . import proto

log = logging.getLogger("lookcam.pppp")

LAN_SEARCH_PORT = 32108
BROADCAST_LAST = 255


@dataclass
class Discovered:
    """Camera response to LanSearch (PunchPkt 0x41)."""

    addr: tuple[str, int]
    ident: bytes = b""
    field2: bytes = b""
    value: int = 0
    seen: float = field(default_factory=time.time)

    @property
    def ip(self) -> str:
        return self.addr[0]

    @property
    def prefix(self) -> str:
        return self.ident.rstrip(b"\x00").decode("ascii", "replace")

    @property
    def suffix(self) -> str:
        return self.field2.rstrip(b"\x00").decode("ascii", "replace")

    @property
    def label(self) -> str:
        if self.prefix and self.suffix:
            return f"{self.prefix}…{self.suffix}"
        return self.prefix or self.suffix or "?"

    @property
    def full_did(self) -> str:
        """Full virtual DID (value is its numeric middle)."""
        return proto.did_from_punch(self.ident, self.field2, self.value)

    def matches(self, did: str) -> bool:
        return bool(did) and proto.did_matches(self.ident, self.field2, did)

    def to_dict(self) -> dict:
        full = self.full_did
        return {"ip": self.ip, "port": self.addr[1],
                "did": full or self.label, "did_partial": self.label,
                "prefix": self.prefix, "suffix": self.suffix,
                "value": self.value}


class PpppEndpoint(asyncio.DatagramProtocol):
    """Shared PPPP transport: LanSearch, DRW routing, AliveAck."""

    def __init__(self, bind: str = "0.0.0.0", port: int = LAN_SEARCH_PORT) -> None:
        self.bind = bind
        self.port = port
        self.transport: asyncio.DatagramTransport | None = None
        self.sock: socket.socket | None = None
        self.sessions: dict[tuple[str, int], object] = {}
        self._by_ip: dict[str, list[object]] = {}
        self.discovered: dict[tuple[str, int], Discovered] = {}
        self._disc_event = asyncio.Event()
        self._targets: list[str] = []
        self._targets_at = 0.0
        self._extra_targets: set[tuple[str, int]] = set()
        self._closed = False
        self.packets_in = 0
        self.packets_out = 0

    # ------------------------------------------------------------------ startup
    async def start(self) -> "PpppEndpoint":
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            sock.bind((self.bind, self.port))
        except OSError as exc:
            sock.close()
            raise RuntimeError(
                f"не удалось занять UDP-порт {self.bind}:{self.port} ({exc}). "
                f"PPPP требует именно {LAN_SEARCH_PORT}: камера не отвечает на "
                f"LanSearch с другого порта. Остановите старый клиент/сервис.") from exc
        sock.setblocking(False)
        self.sock = sock
        self.port = sock.getsockname()[1]
        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(lambda: self, sock=sock)
        log.info("PPPP listening on %s:%s", self.bind, self.port)
        return self

    def connection_made(self, transport) -> None:  # noqa: D401 (asyncio API)
        self.transport = transport

    def connection_lost(self, exc) -> None:
        if exc and not self._closed:
            log.warning("PPPP socket closed: %s", exc)
    async def close(self) -> None:
        self._closed = True
        for session in list(self.sessions.values()):
            try:
                session.request_close("сервис останавливается")
            except Exception:
                pass
        if self.transport is not None:
            try:
                self.transport.close()
            except Exception:
                pass
            self.transport = None
        self.sock = None

    # ------------------------------------------------------------------ sending
    def send(self, addr: tuple[str, int], data: bytes) -> None:
        if self.transport is None:
            return
        try:
            self.transport.sendto(data, addr)
            self.packets_out += 1
        except (OSError, RuntimeError) as exc:
            log.debug("sendto %s: %s", addr, exc)

    # ------------------------------------------------------------------ sessions
    def register(self, session, addr: tuple[str, int]) -> None:
        old = getattr(session, "addr", None)
        if old and old != addr:
            self.unregister_addr(old, session)
        self.sessions[addr] = session
        bucket = self._by_ip.setdefault(addr[0], [])
        if session not in bucket:
            bucket.append(session)
        session.addr = addr

    def unregister(self, session) -> None:
        addr = getattr(session, "addr", None)
        if addr:
            self.unregister_addr(addr, session)

    def unregister_addr(self, addr: tuple[str, int], session) -> None:
        if self.sessions.get(addr) is session:
            self.sessions.pop(addr, None)
        bucket = self._by_ip.get(addr[0])
        if bucket and session in bucket:
            bucket.remove(session)
            if not bucket:
                self._by_ip.pop(addr[0], None)

    def _route(self, addr: tuple[str, int]):
        session = self.sessions.get(addr)
        if session is not None:
            return session
        bucket = self._by_ip.get(addr[0]) or []
        return bucket[0] if len(bucket) == 1 else None

    # ------------------------------------------------------------------ receive
    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.packets_in += 1
        parsed = proto.parse_pppp(data)
        if parsed is None:
            return
        cmd, payload = parsed
        try:
            if cmd in (proto.CMD_PUNCHPKT, proto.CMD_TRYP2P):
                self._on_punch(payload, addr)
            elif cmd == proto.CMD_P2PRDY:
                self._on_ready(payload, addr)
            elif cmd == proto.CMD_DATA:
                self._on_data(payload, addr)
            elif cmd == proto.CMD_ALIVE:
                self.send(addr, proto.alive_ack_packet(payload))
            elif cmd == proto.CMD_CLOSE:
                session = self._route(addr)
                if session is not None:
                    session.on_remote_close()
                else:
                    log.info("camera %s:%s closed the session", *addr)
        except Exception:
            log.exception("failed to parse packet cmd=0x%02x from %s", cmd, addr)
    def _on_punch(self, payload: bytes, addr: tuple[str, int]) -> None:
        if len(payload) < 20:
            return
        item = Discovered(addr=addr, ident=payload[0:8],
                          field2=payload[12:20],
                          value=struct.unpack_from(">I", payload, 8)[0])
        self.discovered[addr] = item
        self._disc_event.set()
        log.debug("LanSearch reply: %s:%s %s", addr[0], addr[1], item.label)

    def _on_ready(self, _payload: bytes, addr: tuple[str, int]) -> None:
        session = self._route(addr)
        if session is not None:
            session.on_ready(addr)
        else:
            # P2PRdy may arrive from a different port than PunchPkt: look up by IP
            for candidate in self._by_ip.get(addr[0], []):
                candidate.on_ready(addr)
                self.register(candidate, addr)
                break

    def _on_data(self, payload: bytes, addr: tuple[str, int]) -> None:
        session = self._route(addr)
        if session is None:
            log.debug("DRW from unknown camera %s:%s", *addr)
            return
        session.on_data(payload)

    # ------------------------------------------------------------------ discovery
    def _broadcast_targets(self) -> list[str]:
        """Directed broadcasts of all interfaces + the limited one.

        The limited 255.255.255.255 goes out via the default route (VPN) and never
        reaches the camera, so it comes last.
        """
        targets: list[str] = []
        try:
            try:
                from ..vendor.lookcam import net as netmod
            except ImportError:  # `python -m service` from the package directory
                from vendor.lookcam import net as netmod
            addresses = netmod.all_local_ipv4()
        except Exception:
            addresses = []
        for ip in addresses:
            try:
                parts = ip.split(".")
                if len(parts) == 4 and all(p.isdigit() for p in parts):
                    targets.append(".".join(parts[:3] + [str(BROADCAST_LAST)]))
            except Exception:
                continue
        for extra in ("255.255.255.255",):
            if extra not in targets:
                targets.append(extra)
        # deduplicate while preserving order
        seen: set[str] = set()
        return [t for t in targets if not (t in seen or seen.add(t))]

    def search_targets(self) -> list[str]:
        now = time.time()
        if not self._targets or now - self._targets_at > 60:
            self._targets = self._broadcast_targets()
            self._targets_at = now
        return self._targets

    def send_lan_search(self, host_hint: str | None = None) -> None:
        pkt = proto.lan_search_packet()
        targets = self.search_targets()
        for target in targets:
            self.send((target, self.port), pkt)
        if host_hint:
            self.send((host_hint, self.port), pkt)
        for addr in self._extra_targets:
            self.send(addr, pkt)

    def add_target(self, host: str, port: int) -> None:
        """Extra LanSearch address (emulator/camera on a non-standard port)."""
        self._extra_targets.add((host, int(port)))

    def recent(self, max_age: float = 15.0) -> list[Discovered]:
        now = time.time()
        return [d for d in self.discovered.values() if now - d.seen <= max_age]

    def find(self, did: str, host_hint: str | None = None,
             max_age: float = 15.0) -> Discovered | None:
        items = sorted(self.recent(max_age), key=lambda d: d.seen, reverse=True)
        for item in items:
            if item.matches(did):
                return item
        if host_hint:
            for item in items:
                if item.ip == host_hint:
                    return item
        return None

    async def discover(self, did: str = "", host_hint: str | None = None,
                       timeout: float = 3.0, fresh: bool = False) -> Discovered | None:
        """LanSearch and wait for a response from a specific camera (or any).

        `fresh=True` ignores responses received before the call: needed after
        a camera reboot, when its PunchPkt.value/port may have changed.
        """
        deadline = time.monotonic() + max(0.2, timeout)
        started = time.time()
        found = None if fresh else self.find(did, host_hint)
        next_send = 0.0
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_send:
                self.send_lan_search(host_hint)
                next_send = now + 0.45
            self._disc_event.clear()
            try:
                await asyncio.wait_for(self._disc_event.wait(), 0.12)
            except asyncio.TimeoutError:
                pass
            found = self.find(did, host_hint)
            if found is not None and (not fresh or found.seen >= started - 0.05):
                return found
        return None

    async def discover_all(self, timeout: float = 3.0) -> list[Discovered]:
        """LanSearch on all interfaces: the list of cameras found."""
        deadline = time.monotonic() + max(0.2, timeout)
        next_send = 0.0
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_send:
                self.send_lan_search()
                next_send = now + 0.45
            self._disc_event.clear()
            try:
                await asyncio.wait_for(self._disc_event.wait(), 0.12)
            except asyncio.TimeoutError:
                pass
        items = sorted(self.recent(), key=lambda d: (d.ip, d.addr[1]))
        return items

    # ------------------------------------------------------------------ diagnostics
    def stats(self) -> dict:
        return {
            "bind": f"{self.bind}:{self.port}",
            "packets_in": self.packets_in, "packets_out": self.packets_out,
            "sessions": len(self.sessions),
            "discovered": [d.to_dict() for d in self.recent(60)],
            "targets": self.search_targets(),
        }
