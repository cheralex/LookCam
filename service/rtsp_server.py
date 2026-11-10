"""RTSP 1.0 server: H.264 → RTP (RFC 6184) without transcoding.

What VLC/ffmpeg/Home Assistant/Frigate actually need is supported:

* OPTIONS, DESCRIBE, SETUP, PLAY, PAUSE, TEARDOWN, GET_PARAMETER;
* RTP over UDP and over TCP (interleaved `$`), RTCP SR every 5 s;
* Digest authentication (HA1 is kept in the config);
* SDP with `sprop-parameter-sets`, and SPS/PPS are duplicated in the stream before
  every IDR — this survives a quality change without restarting the client;
* a new client starts from the last IDR in the GOP cache instead of waiting 3.5 s.

RTP timestamps are built from the camera clock (`u32LE@46`, ms × 90); on an
implausible jump (new session, quality change) time shifts monotonically rather
than jumping.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import logging
import os
import socket
import struct
import time
import urllib.parse

from . import auth as auth_mod
from . import camera as camera_mod
from . import frames, hub as hub_mod, proto

log = logging.getLogger("lookcam.rtsp")

SERVER = "LookCam RTSP/0.1"
DEFAULT_MTU = 1400
RTP_PT = 96
RTCP_INTERVAL = 5.0
SESSION_TIMEOUT = 60


class RtspError(RuntimeError):
    pass


def _is_loopback(host: str) -> bool:
    try:
        addr = ipaddress.ip_address(str(host).split("%")[0])
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        return addr.is_loopback
    except ValueError:
        return str(host).lower() == "localhost"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _parse_transport(value: str) -> dict:
    """Parse Transport: RTP/AVP;unicast;client_port=5000-5001 and the TCP variant."""
    out: dict = {"tcp": False, "client_ports": (0, 0), "interleaved": (0, 1)}
    if not value:
        return out
    parts = [p.strip() for p in value.split(";")]
    spec = parts[0].upper()
    out["tcp"] = "TCP" in spec
    for part in parts[1:]:
        key, _, val = part.partition("=")
        key = key.lower().strip()
        if key == "client_port":
            try:
                a, _, b = val.partition("-")
                out["client_ports"] = (int(a), int(b or a))
            except ValueError:
                pass
        elif key == "interleaved":
            try:
                a, _, b = val.partition("-")
                out["interleaved"] = (int(a), int(b or a))
            except ValueError:
                pass
    return out


def sdp_for(camera_id: str, cfg: hub_mod.StreamConfig, host: str) -> str:
    profile = cfg.profile_level or proto.profile_level_id(cfg.sps)
    sprop = f"{_b64(cfg.sps)},{_b64(cfg.pps)}"
    return (
        "v=0\r\n"
        f"o=- 0 0 IN IP4 {host}\r\n"
        f"s=LookCam {camera_id}\r\n"
        "c=IN IP4 0.0.0.0\r\n"
        "t=0 0\r\n"
        "a=control:*\r\n"
        "m=video 0 RTP/AVP 96\r\n"
        "a=rtpmap:96 H264/90000\r\n"
        f"a=fmtp:96 packetization-mode=1;profile-level-id={profile};"
        f"sprop-parameter-sets={sprop}\r\n"
        f"a=control:trackID=0\r\n"
    )


class RtpSender:
    """Hub subscription → RTP packets and RTCP SR."""

    def __init__(self, client: "RtspClient", sub: hub_mod.Subscription) -> None:
        self.client = client
        self.sub = sub
        self.cfg: hub_mod.StreamConfig | None = None
        self.seq = 0
        self.ssrc = struct.unpack(">I", os.urandom(4))[0]
        self.timestamp = 0
        self._last_raw: int | None = None
        self.packets = 0
        self.octets = 0
        self.frames = 0
        self._last_sr = 0.0

    async def run(self) -> None:
        log.debug("RTSP %s: sender start, ssrc=%08x, tcp=%s", self.client.peer,
                  self.ssrc, self.client.tcp)
        try:
            while not self.client.closed:
                try:
                    event = await asyncio.wait_for(self.sub.get(), RTCP_INTERVAL)
                except asyncio.TimeoutError:
                    await self._send_sr()
                    continue
                if isinstance(event, hub_mod.ConfigEvent):
                    self.cfg = event.config
                    log.debug("RTSP %s: config %s", self.client.peer,
                              self.cfg.codec if self.cfg else None)
                    continue
                if isinstance(event, hub_mod.ResetEvent):
                    self.cfg = None
                    continue
                if not isinstance(event, frames.AccessUnit) or event.kind != "video":
                    continue
                if self.cfg is None:
                    continue
                if self.client.playing:
                    await self._send_au(event)
                if time.time() - self._last_sr >= RTCP_INTERVAL:
                    await self._send_sr()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.info("RTSP %s: sending stopped: %s", self.client.peer, exc)

    def _next_ts(self, au: frames.AccessUnit) -> int:
        if au.ts_ms:
            raw = (au.ts_ms * 90) & 0xFFFFFFFF
            if self._last_raw is None:
                self.timestamp = raw
            else:
                delta = (raw - self._last_raw) & 0xFFFFFFFF
                if delta < 0x80000000 and delta <= 90000 * 5:
                    self.timestamp = (self.timestamp + delta) & 0xFFFFFFFF
                else:
                    self.timestamp = (self.timestamp + 3000) & 0xFFFFFFFF
            self._last_raw = raw
        else:
            self.timestamp = (self.timestamp + 3000) & 0xFFFFFFFF
        return self.timestamp

    async def _send_au(self, au: frames.AccessUnit) -> None:
        ts = self._next_ts(au)
        nals = proto.split_annexb(au.payload)
        if not nals:
            nals = [au.payload] if au.payload else []
        if not nals:
            return
        payloads: list[bytes] = []
        for nal in nals:
            if not nal:
                continue
            if len(nal) <= DEFAULT_MTU - 12:
                payloads.append(nal)
                continue
            # FU-A: indicator | FU header | data (RFC 6184 §5.8)
            fu_ind = (nal[0] & 0xE0) | 28
            nal_type = nal[0] & 0x1F
            body = nal[1:]
            step = DEFAULT_MTU - 12 - 2
            first = True
            while body:
                chunk, body = body[:step], body[step:]
                fu_head = (0x80 if first else 0) | (0x40 if not body else 0) | nal_type
                payloads.append(bytes([fu_ind, fu_head]) + chunk)
                first = False
        # SPS/PPS before an IDR if the camera did not send them (after a quality change)
        if au.key and self.cfg is not None and not any(
                (n[0] & 0x1F) == 7 for n in nals):
            sps = self.cfg.sps
            pps = self.cfg.pps
            if sps and len(sps) <= DEFAULT_MTU - 12:
                payloads.insert(0, sps)
            if pps and len(pps) <= DEFAULT_MTU - 12:
                payloads.insert(1, pps)
        total = len(payloads)
        log.debug("RTSP %s: AU key=%s nals=%d packets=%d ts=%d", self.client.peer,
                  au.key, len(nals), total, ts)
        for index, payload in enumerate(payloads):
            marker = index == total - 1
            await self._send_rtp(payload, marker, ts)
        self.frames += 1

    async def _send_rtp(self, payload: bytes, marker: bool, ts: int) -> None:
        self.seq = (self.seq + 1) & 0xFFFF
        header = struct.pack(">BBHII", 0x80, (0x80 if marker else 0) | RTP_PT,
                             self.seq, ts, self.ssrc)
        packet = header + payload
        self.packets += 1
        self.octets += len(payload)
        await self.client.send_rtp(packet)

    async def _send_sr(self) -> None:
        if self._last_sr and time.time() - self._last_sr < 1.0:
            return
        self._last_sr = time.time()
        ntp = time.time() + 2208988800
        whole = int(ntp)
        frac = int((ntp - whole) * (1 << 32)) & 0xFFFFFFFF
        sr = struct.pack(">BBHIIIIII", 0x80, 200, 6, self.ssrc, whole, frac,
                         self.timestamp, self.packets, self.octets)
        await self.client.send_rtcp(sr)


class RtspClient:
    """A single RTSP connection: requests, transport, subscription, RTP loop."""

    def __init__(self, server: "RtspServer", reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter) -> None:
        self.server = server
        self.reader = reader
        self.writer = writer
        self.peer = writer.get_extra_info("peername") or ("-", 0)
        self.session_id = os.urandom(8).hex()
        self.cseq = 0
        self.closed = False
        self.camera = None
        self.hold = None
        self.sub: hub_mod.Subscription | None = None
        self.sender: RtpSender | None = None
        self.rtp_task: asyncio.Task | None = None
        self.tcp = False
        self.rtp_channel = 0
        self.rtcp_channel = 1
        self.udp_rtp: socket.socket | None = None
        self.udp_rtcp: socket.socket | None = None
        self.client_rtp_port = 0
        self.client_rtcp_port = 0
        self.client_ip = self.peer[0] if self.peer else "0.0.0.0"
        self._wlock = asyncio.Lock()
        self.playing = False

    # ------------------------------------------------------------------ I/O
    async def send_rtp(self, packet: bytes) -> None:
        if self.closed:
            return
        if self.tcp:
            data = b"$" + bytes([self.rtp_channel]) + struct.pack(">H", len(packet)) + packet
            async with self._wlock:
                if self.closed:
                    return
                self.writer.write(data)
                try:
                    await self.writer.drain()
                except (ConnectionError, OSError):
                    self.closed = True
        elif self.udp_rtp is not None:
            try:
                self.udp_rtp.sendto(packet, (self.client_ip, self.client_rtp_port))
            except OSError:
                self.closed = True

    async def send_rtcp(self, packet: bytes) -> None:
        if self.closed:
            return
        if self.tcp:
            data = b"$" + bytes([self.rtcp_channel]) + struct.pack(">H", len(packet)) + packet
            async with self._wlock:
                if self.closed:
                    return
                self.writer.write(data)
                try:
                    await self.writer.drain()
                except (ConnectionError, OSError):
                    self.closed = True
        elif self.udp_rtcp is not None:
            try:
                self.udp_rtcp.sendto(packet, (self.client_ip, self.client_rtcp_port))
            except OSError:
                pass

    async def respond(self, status: int, headers: dict | None = None,
                      body: bytes = b"", cseq: int | None = None) -> None:
        reason = {200: "OK", 400: "Bad Request", 401: "Unauthorized",
                  404: "Not Found", 405: "Method Not Allowed", 451: "Parameter Not Understood",
                  461: "Unsupported Transport", 500: "Internal Server Error",
                  503: "Service Unavailable"}.get(status, "OK")
        lines = [f"RTSP/1.0 {status} {reason}"]
        if cseq is not None:
            lines.append(f"CSeq: {cseq}")
        lines.append(f"Server: {SERVER}")
        for key, value in (headers or {}).items():
            lines.append(f"{key}: {value}")
        lines.append(f"Content-Length: {len(body)}")
        raw = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1", "replace") + body
        async with self._wlock:
            if self.closed:
                return
            try:
                self.writer.write(raw)
                await self.writer.drain()
            except (ConnectionError, OSError):
                self.closed = True

    # ------------------------------------------------------------------ loop
    async def handle(self) -> None:
        try:
            while not self.closed:
                try:
                    request = await asyncio.wait_for(
                        self._read_request(), SESSION_TIMEOUT)
                except asyncio.TimeoutError:
                    if self.playing and not self.closed:
                        # An NVR/VLC client may receive RTP for a long time and
                        # send no RTSP requests. Keep the PLAY session alive; it
                        # is torn down by TEARDOWN, the RTP task, or the next
                        # explicit client action.
                        log.debug("RTSP %s: idle while playing; keeping session",
                                  self.peer)
                        continue
                    break
                if request is None:
                    break
                method, uri, headers, body = request
                await self._dispatch(method, uri, headers, body)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        except Exception:
            log.exception("RTSP connection %s", self.peer)
        finally:
            await self.close()

    async def _read_request(self):
        first = await self.reader.read(1)
        if not first:
            return None
        if first == b"$":                       # interleaved RTCP from the client
            rest = await self.reader.readexactly(3)
            length = struct.unpack(">H", rest[1:3])[0]
            await self.reader.readexactly(length)
            return await self._read_request()
        line = first + await self.reader.readuntil(b"\r\n")
        try:
            method, uri, version = line.decode("latin-1").strip().split(" ", 2)
        except ValueError:
            return None
        headers: dict[str, str] = {}
        while True:
            raw = await self.reader.readuntil(b"\r\n")
            if raw in (b"\r\n", b"\n"):
                break
            text = raw.decode("latin-1").strip()
            if ":" in text:
                key, value = text.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        length = 0
        with contextlib.suppress(ValueError):
            length = int(headers.get("content-length", "0") or 0)
        body = await self.reader.readexactly(length) if length else b""
        with contextlib.suppress(ValueError):
            self.cseq = int(headers.get("cseq", self.cseq))
        return method.upper(), uri, headers, body

    async def _dispatch(self, method: str, uri: str, headers: dict,
                        body: bytes) -> None:
        if method != "OPTIONS" and not self._authorized(method, uri, headers):
            await self.respond(401, {"WWW-Authenticate": self.server.realm.challenge()},
                               cseq=self.cseq)
            return
        if method == "OPTIONS":
            await self.respond(200, {
                "Public": "OPTIONS, DESCRIBE, SETUP, PLAY, PAUSE, TEARDOWN, GET_PARAMETER"},
                cseq=self.cseq)
        elif method == "DESCRIBE":
            await self._describe(uri, headers)
        elif method == "SETUP":
            await self._setup(uri, headers)
        elif method == "PLAY":
            await self._play(uri, headers)
        elif method == "PAUSE":
            self.playing = False
            await self.respond(200, {"Session": self.session_id}, cseq=self.cseq)
        elif method == "TEARDOWN":
            await self.respond(200, {"Session": self.session_id}, cseq=self.cseq)
            await self.close()
        elif method == "GET_PARAMETER":
            await self.respond(200, {"Session": self.session_id}, cseq=self.cseq)
        else:
            await self.respond(405, {"Allow": "OPTIONS, DESCRIBE, SETUP, PLAY, "
                                              "PAUSE, TEARDOWN, GET_PARAMETER"},
                               cseq=self.cseq)

    def _authorized(self, method: str, uri: str, headers: dict) -> bool:
        realm = self.server.realm
        if not realm.enabled:
            return True
        return realm.verify(method, uri, headers.get("authorization", ""))

    def session_header(self) -> str:
        # Advertise the keepalive interval so NVR clients do not treat a
        # naturally silent PLAY connection as dead.
        return f"{self.session_id};timeout={int(SESSION_TIMEOUT)}"

    # ------------------------------------------------------------------ methods
    def camera_id(self, uri: str) -> str:
        parsed = urllib.parse.urlsplit(uri)
        path = (parsed.path or "/").strip("/")
        if not path:
            return self.camera.id if self.camera is not None else ""
        first = path.split("/")[0]
        if self.camera is not None and (
                first.startswith("trackID") or first == ""):
            return self.camera.id
        return first

    async def _ensure_camera(self, uri: str):
        cid = self.camera_id(uri)
        if not cid:
            raise RtspError("в URL нет id камеры: rtsp://host:port/<id>")
        cam = self.server.manager.get(cid)
        if cam is None:
            raise RtspError(f"нет камеры {cid}")
        if self.camera is cam and self.hold is not None:
            return cam
        if self.camera is not None:
            await self._release_camera()
        self.camera = cam
        self.hold = cam.acquire("video")
        await self.hold.__aenter__()
        return cam

    async def _describe(self, uri: str, headers: dict) -> None:
        try:
            cam = await self._ensure_camera(uri)
        except RtspError as exc:
            await self.respond(404 if "нет камеры" in str(exc) else 503, {},
                               str(exc).encode("utf-8"), cseq=self.cseq)
            return
        deadline = time.monotonic() + 8.0
        while cam.hub.config is None and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        cfg = cam.hub.config
        if cfg is None:
            await self.respond(503, {}, b"camera did not provide SPS",
                               cseq=self.cseq)
            return
        host = self.writer.get_extra_info("sockname")[0] if self.writer.get_extra_info(
            "sockname") else "0.0.0.0"
        sdp = sdp_for(cam.id, cfg, host)
        await self.respond(200, {
            "Content-Type": "application/sdp",
            "Content-Base": uri if uri.endswith("/") else uri + "/",
            "Session": self.session_header(),
        }, sdp.encode("utf-8"), cseq=self.cseq)

    async def _setup(self, uri: str, headers: dict) -> None:
        try:
            await self._ensure_camera(uri)
        except RtspError as exc:
            await self.respond(404 if "нет камеры" in str(exc) else 503, {},
                               str(exc).encode("utf-8"), cseq=self.cseq)
            return
        transport = _parse_transport(headers.get("transport", ""))
        if transport["tcp"]:
            self.tcp = True
            self.rtp_channel, self.rtcp_channel = transport["interleaved"]
            await self.respond(200, {
                "Session": self.session_header(),
                "Transport": f"RTP/AVP/TCP;unicast;interleaved="
                             f"{self.rtp_channel}-{self.rtcp_channel}",
            }, cseq=self.cseq)
            return
        ports = transport["client_ports"]
        if not ports[0]:
            await self.respond(461, {}, cseq=self.cseq)
            return
        self.client_rtp_port, self.client_rtcp_port = ports
        self.client_ip = self.peer[0] if self.peer else "0.0.0.0"
        try:
            self.udp_rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udp_rtp.bind((self.server.bind or "0.0.0.0", 0))
            self.udp_rtp.setblocking(False)
            self.udp_rtcp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udp_rtcp.bind((self.server.bind or "0.0.0.0", 0))
            self.udp_rtcp.setblocking(False)
        except OSError as exc:
            await self.respond(500, {}, str(exc).encode(), cseq=self.cseq)
            return
        sport = self.udp_rtp.getsockname()[1]
        await self.respond(200, {
            "Session": self.session_header(),
            "Transport": f"RTP/AVP;unicast;client_port={ports[0]}-{ports[1]};"
                         f"server_port={sport}-{self.udp_rtcp.getsockname()[1]}",
        }, cseq=self.cseq)

    async def _play(self, uri: str, headers: dict) -> None:
        try:
            cam = await self._ensure_camera(uri)
        except RtspError as exc:
            await self.respond(404 if "нет камеры" in str(exc) else 503, {},
                               str(exc).encode("utf-8"), cseq=self.cseq)
            return
        start_sender = False
        if self.sub is None:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(uri).query)
            default_low = bool(self.server.config.data.get("rtsp", {}).get(
                "low_latency", True))
            low_latency = str(query.get("lowlatency", ["1" if default_low else "0"])[0]).lower() in (
                "1", "true", "yes", "on")
            self.sub = cam.hub.subscribe(f"rtsp-{self.session_id[:6]}",
                                         gop=not low_latency)
            log.debug("RTSP %s: subscribe, config=%s, gop=%d low_latency=%s",
                      self.peer, bool(cam.hub.config), len(cam.hub.gop()),
                      low_latency)
            self.sender = RtpSender(self, self.sub)
            start_sender = True
        self.playing = True
        log.debug("RTSP %s: PLAY %s, tcp=%s, channels %s-%s", self.peer, uri,
                  self.tcp, self.rtp_channel, self.rtcp_channel)
        # Reply PLAY before the first RTP packet. Otherwise a zero-buffer
        # client may still be in SETUP state and discard the initial IDR.
        # RTP-Info is intentionally omitted: its seq/timestamp are not known
        # until the first AU has actually been sent.
        await self.respond(200, {
            "Session": self.session_header(),
            "Range": "npt=0.000-",
        }, cseq=self.cseq)
        if start_sender:
            self.rtp_task = asyncio.create_task(
                self.sender.run(), name=f"rtsp-rtp-{self.session_id[:6]}")

    async def _release_camera(self) -> None:
        if self.sub is not None and self.camera is not None:
            self.camera.hub.unsubscribe(self.sub)
            self.sub = None
        if self.rtp_task is not None:
            self.rtp_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.rtp_task
            self.rtp_task = None
        if self.hold is not None:
            with contextlib.suppress(Exception):
                await self.hold.__aexit__(None, None, None)
            self.hold = None
        self.camera = None

    async def close(self) -> None:
        if self.closed and self.hold is None and self.udp_rtp is None:
            return
        self.closed = True
        await self._release_camera()
        for sock in (self.udp_rtp, self.udp_rtcp):
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.close()
        self.udp_rtp = self.udp_rtcp = None
        with contextlib.suppress(Exception):
            self.writer.close()


class RtspServer:
    """asyncio RTSP server on port 8554 (or 0 for tests)."""

    def __init__(self, config, manager, bind: str = "0.0.0.0",
                 port: int = 8554) -> None:
        self.config = config
        self.manager = manager
        self.bind = bind
        self.port = int(port)
        self.server: asyncio.AbstractServer | None = None
        rtsp = config.data.get("rtsp", {})
        self.realm = auth_mod.DigestRealm(
            rtsp.get("user") or "", rtsp.get("password_hash") or "",
            rtsp.get("realm") or "LookCam")
        self.clients: set[RtspClient] = set()
        self.enabled = bool(rtsp.get("enable", True))

    async def start(self) -> "RtspServer":
        if not self.enabled:
            log.info("RTSP disabled in config")
            return self
        if not _is_loopback(self.bind) and not self.realm.enabled:
            raise RtspError(
                "RTSP on a non-loopback bind requires a configured password")
        self.server = await asyncio.start_server(self._client, self.bind, self.port,
                                                 backlog=32)
        if self.port == 0:
            self.port = self.server.sockets[0].getsockname()[1]
        log.info("RTSP listening on rtsp://%s:%s/<id>", self.bind, self.port)
        return self

    async def _client(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        client = RtspClient(self, reader, writer)
        self.clients.add(client)
        try:
            await client.handle()
        finally:
            self.clients.discard(client)

    async def stop(self) -> None:
        for client in list(self.clients):
            await client.close()
        self.clients.clear()
        if self.server is not None:
            self.server.close()
            with contextlib.suppress(Exception):
                await self.server.wait_closed()
            self.server = None
