"""Mini RTSP client on the standard library.

Supports:
  * Basic and Digest authorization;
  * transport RTP over TCP (interleaved) and RTP over UDP;
  * DEPACKETIZE: JPEG/MJPEG (RFC 2435), H.264 (RFC 6184), H.265 (RFC 7798);
  * SDP parsing, track selection.

Video is NOT decoded: H.264/H.265 are output as an Annex-B stream (played by VLC/ffmpeg),
JPEG frames are output as ready-made pictures.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import socket
import struct
import time
import urllib.parse
from dataclasses import dataclass, field


class RtspError(RuntimeError):
    pass


# --------------------------------------------------------------------------- SDP

@dataclass
class Media:
    kind: str                    # video | audio | application
    port: int
    proto: str
    payloads: list[int] = field(default_factory=list)
    control: str = ""
    rtpmap: dict[int, tuple[str, int]] = field(default_factory=dict)
    fmtp: dict[int, str] = field(default_factory=dict)
    sprop: bytes = b""           # parameter sets from SDP (H.264/H.265)
    width: int = 0
    height: int = 0

    @property
    def codec(self) -> str:
        for pt in self.payloads:
            if pt in self.rtpmap:
                return self.rtpmap[pt][0].lower()
        return ""

    @property
    def clock(self) -> int:
        for pt in self.payloads:
            if pt in self.rtpmap:
                return self.rtpmap[pt][1]
        return 90000


def parse_sdp(text: str) -> list[Media]:
    medias: list[Media] = []
    cur: Media | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k == "m":
            parts = v.split()
            if len(parts) >= 4:
                cur = Media(parts[0], int(parts[1]), parts[2],
                            [int(x) for x in parts[3:] if x.isdigit()])
                medias.append(cur)
        elif cur is None:
            continue
        elif k == "a":
            if v.startswith("control:"):
                cur.control = v.split(":", 1)[1].strip()
            elif v.startswith("rtpmap:"):
                m = re.match(r"rtpmap:(\d+)\s+([^/]+)/(\d+)", v)
                if m:
                    cur.rtpmap[int(m.group(1))] = (m.group(2), int(m.group(3)))
            elif v.startswith("fmtp:"):
                m = re.match(r"fmtp:(\d+)\s+(.*)", v)
                if m:
                    pt = int(m.group(1))
                    fmtp = m.group(2)
                    cur.fmtp[pt] = fmtp
                    sm = re.search(r"sprop-parameter-sets=([A-Za-z0-9+/=,]+)", fmtp)
                    if sm:
                        chunks = []
                        for part in sm.group(1).split(","):
                            try:
                                chunks.append(base64.b64decode(part + "=" * (-len(part) % 4)))
                            except Exception:
                                pass
                        cur.sprop = b"".join(chunks)
                    dm = re.search(r"(?:width|Width)=(\d+).*?(?:height|Height)=(\d+)", fmtp)
                    if not dm:
                        dm = re.search(r"(\d+)x(\d+)", fmtp)
                    if dm:
                        cur.width, cur.height = int(dm.group(1)), int(dm.group(2))
            elif v.startswith("framesize:"):
                m = re.search(r"(\d+)-(\d+)", v)
                if m:
                    cur.width, cur.height = int(m.group(1)), int(m.group(2))
    return medias


# ------------------------------------------------------------------------- RTP

@dataclass
class RtpPacket:
    payload_type: int
    seq: int
    timestamp: int
    marker: bool
    payload: bytes
    channel: int = 0


def parse_rtp(data: bytes, channel: int = 0) -> RtpPacket | None:
    if len(data) < 12:
        return None
    b0 = data[0]
    if (b0 >> 6) != 2:
        return None
    padding = (b0 >> 5) & 1
    extension = (b0 >> 4) & 1
    cc = b0 & 0x0F
    b1 = data[1]
    marker = bool(b1 >> 7)
    pt = b1 & 0x7F
    seq, ts = struct.unpack(">HI", data[2:8])
    off = 12 + cc * 4
    if extension and len(data) >= off + 4:
        elen = struct.unpack(">H", data[off + 2:off + 4])[0]
        off += 4 + elen * 4
    if padding and data:
        pad = data[-1]
        end = len(data) - pad
    else:
        end = len(data)
    if off > end:
        return None
    return RtpPacket(pt, seq, ts, marker, data[off:end], channel)


# ------------------------------------------------------------------ depacketizers

class JpegDepacketizer:
    """RFC 2435: assemble a JPEG by fragment offset."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.open = False

    def push(self, pkt: RtpPacket) -> bytes | None:
        payload = pkt.payload
        if len(payload) < 8:
            return None
        offset = (payload[1] << 16) | (payload[2] << 8) | payload[3]
        data = payload[8:]
        if not self.open or offset == 0:
            self.buf = bytearray()
            self.open = True
        if len(self.buf) < offset:
            self.buf.extend(b"\x00" * (offset - len(self.buf)))
        self.buf[offset:offset + len(data)] = data
        if pkt.marker:
            frame = bytes(self.buf)
            self.open = False
            self.buf = bytearray()
            start = frame.find(b"\xff\xd8\xff")
            if start < 0:
                return None
            frame = frame[start:]
            end = frame.find(b"\xff\xd9")
            if end < 0:
                return None
            return frame[:end + 2]
        return None


class H264Depacketizer:
    START = b"\x00\x00\x00\x01"

    def __init__(self) -> None:
        self.fu_buf = bytearray()
        self.fu_header = b""
        self.fu_active = False

    @staticmethod
    def _nal(payload: bytes) -> bytes:
        return H264Depacketizer.START + payload

    def push(self, pkt: RtpPacket) -> bytes | None:
        p = pkt.payload
        if not p:
            return None
        ntype = p[0] & 0x1F
        out = bytearray()

        if 1 <= ntype <= 23:                       # single NAL
            out += self._nal(p)
        elif ntype == 24:                          # STAP-A
            off = 1
            while off + 2 <= len(p):
                size = struct.unpack(">H", p[off:off + 2])[0]
                off += 2
                out += self._nal(p[off:off + size])
                off += size
        elif ntype == 28:                          # FU-A
            fu = p[1]
            start = bool(fu & 0x80)
            end = bool(fu & 0x40)
            orig_type = fu & 0x1F
            if start:
                self.fu_buf = bytearray([(p[0] & 0xE0) | orig_type])
                self.fu_active = True
            elif not self.fu_active:
                return None
            self.fu_buf += p[2:]
            if end:
                out += self._nal(bytes(self.fu_buf))
                self.fu_buf = bytearray()
                self.fu_active = False
        else:
            return None
        return bytes(out) if out else None


class H265Depacketizer:
    START = b"\x00\x00\x00\x01"

    def __init__(self) -> None:
        self.fu_buf = bytearray()
        self.fu_active = False

    def push(self, pkt: RtpPacket) -> bytes | None:
        p = pkt.payload
        if len(p) < 3:
            return None
        ntype = (p[0] >> 1) & 0x3F
        out = bytearray()
        if ntype < 48:                             # single NAL
            out += self.START + p
        elif ntype == 48:                          # AP
            off = 2
            while off + 2 <= len(p):
                size = struct.unpack(">H", p[off:off + 2])[0]
                off += 2
                out += self.START + p[off:off + size]
                off += size
        elif ntype == 49:                          # FU
            fu = p[2]
            start = bool(fu & 0x80)
            end = bool(fu & 0x40)
            orig_type = fu & 0x3F
            if start:
                self.fu_buf = bytearray([(p[0] & 0x81) | (orig_type << 1), p[1]])
                self.fu_active = True
            elif not self.fu_active:
                return None
            self.fu_buf += p[3:]
            if end:
                out += self.START + bytes(self.fu_buf)
                self.fu_buf = bytearray()
                self.fu_active = False
        return bytes(out) if out else None


# -------------------------------------------------------------------- RTSP session

class RtspSession:
    def __init__(self, url: str, username: str = "", password: str = "",
                 transport: str = "tcp", timeout: float = 6.0):
        self.url = url
        self.username = username
        self.password = password
        self.transport = transport.lower()
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.sdp = ""
        self.medias: list[Media] = []
        self.session_id = ""
        self.cseq = 0
        self._auth = None                      # (scheme, params)
        self._buf = b""
        self.track: Media | None = None
        self.interleaved = (0, 1)
        self.udp_rtp: socket.socket | None = None
        self.udp_rtcp: socket.socket | None = None
        self._depack = None
        self._sprop_sent = False

    # ------------------------------------------------------------------ low level
    def _connect(self) -> None:
        u = urllib.parse.urlsplit(self.url)
        host = u.hostname or ""
        port = u.port or 554
        self.sock = socket.create_connection((host, port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        self._buf = b""

    def _auth_header(self, method: str) -> str | None:
        if not self._auth:
            return None
        scheme, params = self._auth
        if scheme == "basic":
            token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
            return f"Basic {token}"
        # digest
        realm = params.get("realm", "")
        nonce = params.get("nonce", "")
        ha1 = hashlib.md5(f"{self.username}:{realm}:{self.password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"{method}:{self.url}".encode()).hexdigest()
        resp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        return (f'Digest username="{self.username}", realm="{realm}", nonce="{nonce}", '
                f'uri="{self.url}", response="{resp}"')

    def request(self, method: str, url: str | None = None,
                headers: dict | None = None) -> tuple[int, dict, bytes]:
        if self.sock is None:
            self._connect()
        url = url or self.url
        self.cseq += 1
        lines = [f"{method} {url} RTSP/1.0", f"CSeq: {self.cseq}", "User-Agent: LookCamPy/1.0"]
        if self.session_id:
            lines.append(f"Session: {self.session_id}")
        auth = self._auth_header(method)
        if auth:
            lines.append(f"Authorization: {auth}")
        for k, v in (headers or {}).items():
            lines.append(f"{k}: {v}")
        raw = ("\r\n".join(lines) + "\r\n\r\n").encode()
        self.sock.sendall(raw)
        status, hdrs, body = self._read_response()
        if status == 401 and self.username:
            chal = hdrs.get("www-authenticate", "")
            self._auth = _parse_auth(chal)
            return self.request(method, url, headers)
        return status, hdrs, body

    def _read_response(self) -> tuple[int, dict, bytes]:
        assert self.sock is not None
        while b"\r\n\r\n" not in self._buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RtspError("соединение закрыто сервером")
            self._buf += chunk
        head, self._buf = self._buf.split(b"\r\n\r\n", 1)
        text = head.decode("utf-8", "replace")
        first, *rest = text.split("\r\n")
        parts = first.split()
        if len(parts) < 2 or not parts[1].isdigit():
            raise RtspError(f"непонятный ответ: {first!r}")
        status = int(parts[1])
        hdrs: dict[str, str] = {}
        for line in rest:
            if ":" in line:
                k, v = line.split(":", 1)
                hdrs[k.strip().lower()] = v.strip()
        body = b""
        if "content-length" in hdrs:
            need = int(hdrs["content-length"])
            while len(self._buf) < need:
                chunk = self.sock.recv(65536)
                if not chunk:
                    break
                self._buf += chunk
            body, self._buf = self._buf[:need], self._buf[need:]
        return status, hdrs, body

    # ------------------------------------------------------------------- control
    def describe(self) -> tuple[int, dict, bytes]:
        return self.request("DESCRIBE", headers={"Accept": "application/sdp"})

    def setup(self, media: Media, index: int) -> tuple[int, dict]:
        control = media.control
        if not control:
            control = f"trackID={index}"
        if control.startswith("rtsp://"):
            setup_url = control
        elif control == "*":
            setup_url = self.url
        else:
            base = self.url if self.url.endswith("/") else self.url.rsplit("/", 1)[0] + "/"
            setup_url = base + control.lstrip("/")

        if self.transport == "udp":
            self.udp_rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udp_rtp.bind(("0.0.0.0", 0))
            self.udp_rtp.settimeout(self.timeout)
            self.udp_rtcp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udp_rtcp.bind(("0.0.0.0", 0))
            rp = self.udp_rtp.getsockname()[1]
            qp = self.udp_rtcp.getsockname()[1]
            transport_hdr = f"RTP/AVP;unicast;client_port={rp}-{qp}"
        else:
            transport_hdr = f"RTP/AVP/TCP;unicast;interleaved={self.interleaved[0]}-{self.interleaved[1]}"

        status, hdrs, _ = self.request("SETUP", setup_url, {"Transport": transport_hdr})
        if status == 461 and self.transport == "tcp":
            self.transport = "udp"
            return self.setup(media, index)
        if "session" in hdrs:
            self.session_id = hdrs["session"].split(";")[0].strip()
        return status, hdrs

    def play(self) -> tuple[int, dict]:
        return self.request("PLAY", headers={"Range": "npt=0.000-"})[:2]

    def teardown(self) -> None:
        try:
            if self.sock is not None:
                self.request("TEARDOWN")
        except Exception:
            pass
        for s in (self.sock, self.udp_rtp, self.udp_rtcp):
            try:
                if s is not None:
                    s.close()
            except Exception:
                pass
        self.sock = None

    # --------------------------------------------------------------------- open
    def open(self, want: str = "video") -> Media:
        self._connect()
        status, hdrs, body = self.describe()
        if status != 200:
            raise RtspError(f"DESCRIBE вернул {status}")
        self.sdp = body.decode("utf-8", "replace")
        medias = parse_sdp(self.sdp)
        if not medias:
            raise RtspError("SDP пустой — сервер не отдал описание потоков")
        self.medias = medias

        candidates = [m for m in medias if m.kind == want] or medias
        errors = []
        for idx, media in enumerate(medias):
            if media not in candidates:
                continue
            status, hdrs = self.setup(media, idx)
            if status != 200:
                errors.append(f"SETUP {media.kind}#{idx}: {status}")
                continue
            self.track = media
            codec = media.codec
            if "h264" in codec:
                self._depack = H264Depacketizer()
            elif "h265" in codec or "hevc" in codec:
                self._depack = H265Depacketizer()
            else:
                self._depack = JpegDepacketizer()
            status, hdrs = self.play()
            if status != 200:
                errors.append(f"PLAY: {status}")
                continue
            return media
        raise RtspError("не удалось настроить поток: " + "; ".join(errors))

    # -------------------------------------------------------------------- frames
    def _read_interleaved(self) -> tuple[int, bytes] | None:
        assert self.sock is not None

        def recv() -> bytes | None:
            """None — the stream stalled (socket timeout): do not block the application forever."""
            try:
                return self.sock.recv(65536)
            except socket.timeout:
                return None

        # RTSP messages (keep-alive) may arrive between packets — skip them
        while True:
            while not self._buf:
                chunk = recv()
                if chunk is None or chunk == b"":
                    return None
                self._buf = chunk
            if self._buf[:1] == b"$":
                while len(self._buf) < 4:
                    chunk = recv()
                    if chunk is None or chunk == b"":
                        return None
                    self._buf += chunk
                ch = self._buf[1]
                length = struct.unpack(">H", self._buf[2:4])[0]
                while len(self._buf) < 4 + length:
                    chunk = recv()
                    if chunk is None or chunk == b"":
                        return None
                    self._buf += chunk
                payload = self._buf[4:4 + length]
                self._buf = self._buf[4 + length:]
                return ch, payload
            # RTSP message inside the stream (e.g. ANNOUNCE/keep-alive)
            if b"\r\n\r\n" in self._buf:
                _, self._buf = self._buf.split(b"\r\n\r\n", 1)
                continue
            chunk = recv()
            if chunk is None or chunk == b"":
                return None
            self._buf += chunk

    def packets(self):
        while True:
            if self.transport == "udp" and self.udp_rtp is not None:
                try:
                    data, _ = self.udp_rtp.recvfrom(65536)
                except socket.timeout:
                    continue
                except OSError:
                    return
                pkt = parse_rtp(data)
                if pkt:
                    yield pkt
                continue
            got = self._read_interleaved()
            if got is None:
                return
            ch, data = got
            if ch != self.interleaved[0]:
                continue                       # RTCP and the like
            pkt = parse_rtp(data, ch)
            if pkt:
                yield pkt

    def frames(self, max_frames: int = 0, on_packet=None):
        """Yield (kind, bytes): ('jpeg'|'h264'|'h265', data, timestamp)."""
        count = 0
        param_sets = self.track.sprop if self.track else b""
        if param_sets and isinstance(self._depack, (H264Depacketizer, H265Depacketizer)):
            yield ("annexb", _with_start_codes(param_sets), 0)
        for pkt in self.packets():
            if on_packet:
                on_packet(pkt)
            out = self._depack.push(pkt) if self._depack else None
            if not out:
                continue
            if isinstance(self._depack, JpegDepacketizer):
                yield ("jpeg", out, pkt.timestamp)
            else:
                yield ("annexb", out, pkt.timestamp)
            count += 1
            if max_frames and count >= max_frames:
                return


def _with_start_codes(data: bytes) -> bytes:
    """Split the SPS/PPS concatenation from SDP into NALs with 4-byte start codes."""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        # NAL header: for H.264 it is 1 byte, the NAL length is determined heuristically
        # (in SDP the sets already go one after another) — just split at the start of each NAL
        j = i + 1
        while j < n and not (data[j] & 0x80):
            j += 1
        out += b"\x00\x00\x00\x01" + data[i:j]
        i = j
    return bytes(out)


def _parse_auth(challenge: str) -> tuple[str, dict] | None:
    challenge = challenge.strip()
    if not challenge:
        return None
    scheme = challenge.split(" ", 1)[0].lower()
    params = {}
    for m in re.finditer(r'(\w+)=(?:"([^"]*)"|([^,]*))', challenge):
        params[m.group(1).lower()] = (m.group(2) or m.group(3) or "").strip()
    return scheme, params


# --------------------------------------------------------------------- helpers

def probe_rtsp(url: str, timeout: float = 4.0, username: str = "", password: str = ""):
    """Quick check: does RTSP respond, and which codecs does it offer."""
    session = RtspSession(url, username, password, transport="tcp", timeout=timeout)
    try:
        status, hdrs, body = session.describe()
        sdp = body.decode("utf-8", "replace")
        medias = parse_sdp(sdp)
        return {
            "url": url,
            "status": status,
            "server": hdrs.get("server", ""),
            "tracks": [
                {"kind": m.kind, "codec": m.codec, "clock": m.clock,
                 "size": f"{m.width}x{m.height}" if m.width else ""}
                for m in medias
            ],
        }
    except Exception as exc:
        return {"url": url, "status": 0, "error": str(exc)}
    finally:
        session.teardown()


def guess_urls(host: str, port: int = 554, username: str = "", password: str = "") -> list[str]:
    """Typical RTSP URLs for ODM cameras."""
    cred = ""
    if username:
        cred = f"{urllib.parse.quote(username)}:{urllib.parse.quote(password)}@"
    base = f"rtsp://{cred}{host}:{port}"
    paths = [
        "/", "/live", "/live/ch0", "/live/ch1", "/stream1", "/stream2",
        "/h264", "/h264/ch1/main/av_stream", "/cam/realmonitor?channel=1&subtype=0",
        "/11", "/12", "/ch0.h264", "/Profile1", "/onvif1", "/media/video1",
        "/av0_0", "/video1", "/Streaming/Channels/101",
    ]
    return [base + p for p in paths]
