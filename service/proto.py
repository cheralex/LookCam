"""PPPP/PPCS protocol primitives: packets, Lu frames, media headers.

There is no I/O and no asyncio here — only pure functions. The module grew out of
`lookcam/p2p.py` and `LookCam_app_spec.md` §1.4; the old client remains the reference,
but the service assembles packets itself to support several cameras on one socket.

Invariants without which the camera stays silent (see HANDOFF §4):

* ``msg_id`` grows for every new command, retries go out with the same number;
* LanSearch is sent from port 32108 to the directed subnet broadcast;
* an ack packet must not contain more than 64 numbers (we use 32);
* media and commands share one UDP stream, so the demultiplexer lives in the endpoint.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import struct
import time
from dataclasses import dataclass

# ------------------------------------------------------------------ PPPP
MAGIC = 0xF1
HEADER = struct.Struct(">BBH")          # magic, cmd, len payload (BE)

CMD_LANSEARCH = 0x30
CMD_PUNCHPKT = 0x41
CMD_P2PRDY = 0x42
CMD_TRYP2P = 0x43
CMD_DATA = 0xD0
CMD_ACK = 0xD1
CMD_ALIVE = 0xE0
CMD_ALIVE_ACK = 0xE1
CMD_CLOSE = 0xF0

#: How many numbers may go into one ack. Above 64 the camera replies with Close.
ACK_MAX_NUMS = 32
#: Batch size before a forced flush (same as the app — 8..17).
ACK_BATCH = 8
#: Period for sending accumulated acknowledgements.
ACK_INTERVAL = 0.03
#: DRW channel of commands/responses.
CMD_CHANNEL = 1
#: Media channel.
MEDIA_CHANNEL = 0
#: SD download channel.
DOWNLOAD_CHANNEL = 3
#: How long to wait for a lost command-channel packet before jumping over the gap.
CMD_GAP_TIMEOUT = 1.5

#: How many times each command is sent and at what interval.
CMD_REPEATS = 10
CMD_REPEAT_INTERVAL = 0.011

# ------------------------------------------------------------------ Lu frame
LU_CMD = 0xAFAFAFA0
LU_TRAILER = 0xF1F2F3F4
LU_HEAD = 26                            # length of the Lu frame header
LU_MAX_JSON = 1 << 20

# ------------------------------------------------------------------ media
FRAME_MAGIC = b"\x01\xaf\xaf\xaf"       # video frame
AUDIO_MAGIC = b"\x02\xaf\xaf\xaf"       # audio frame
DOWNLOAD_MAGIC = b"\xa0\xaf\xaf\xaf"    # download frame (same magic as commands)
START_CODE = b"\x00\x00\x00\x01"
SPS_NAL = b"\x00\x00\x00\x01\x27"       # SPS of this camera: type 7, nal_ref_idc=1
SPS_NAL_ALT = b"\x00\x00\x00\x01\x67"
PPS_NAL = b"\x00\x00\x00\x01\x28"
PPS_NAL_ALT = b"\x00\x00\x00\x01\x68"
NAL_TYPES = {1, 5, 6, 7, 8, 9, 20}      # plausible H.264 NAL types

VIDEO_HEADER = 83
AUDIO_HEADER = 78
DOWNLOAD_HEADER = 25
TRAILER = 4

#: Headers stripped from camera responses; they reach neither the API nor the log.
SECRET_KEYS = frozenset({
    "pwd", "newpwd", "oldpwd", "wifipwd", "wifiPwd", "4Gpwd", "lockpwd",
    "password", "password_hash", "secret", "token",
})


# ------------------------------------------------------------------ DID

MYKJ_PREFIXES = ("FHBB", "GHBB", "HHBB", "IHBB", "JHBB", "KHBB", "THBB", "XHBB")
VIRTUAL_TO_REAL = {
    "9": "AHCC", "B": "BHCC", "C": "CHCC", "F": "FHBB", "G": "GHBB",
    "H": "HHBB", "I": "IHBB", "J": "JHBB", "K": "KHBB", "T": "THBB", "X": "XHBB",
}
REAL_TO_VIRTUAL = {value: key for key, value in VIRTUAL_TO_REAL.items()}


def normalize_did(did: str) -> str:
    """Normalize a DID to the canonical SDK form (uppercase, dashes at boundaries)."""
    did = (did or "").strip().upper()
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
    """Virtual app DID (`G016638JHDXM`) → real one (`GHBB016638JHDXM`)."""
    did = (virtual_id or "").strip().upper()
    if not did:
        return ""
    if did.startswith(MYKJ_PREFIXES):
        return did
    head = VIRTUAL_TO_REAL.get(did[0])
    if not head:
        return did
    return head + did[1:].lstrip("-")


def real_did(did: str) -> str:
    """Real DID without dashes — this is the form packed into PunchPkt."""
    return real_from_virtual(did).replace("-", "")


def did_parts(did: str) -> tuple[bytes, bytes]:
    """The two DID parts for PunchPkt/LanSearch: the first 4 and the last 5 characters."""
    real = real_did(did)
    fail = (b"", b"")
    if len(real) < 9:
        return fail
    return real[:4].encode("ascii", "replace"), real[-5:].encode("ascii", "replace")


def did_matches(ident: bytes, field2: bytes, did: str) -> bool:
    """Whether a LanSearch response looks like our camera."""
    real = real_did(did)
    got1 = ident.rstrip(b"\x00").decode("ascii", "replace").upper()
    got2 = field2.rstrip(b"\x00").decode("ascii", "replace").upper()
    if real[:4] == got1 and real[-5:] == got2:
        return True
    # Some firmware puts the whole DID in the first field and leaves the second empty.
    if got1 and real.startswith(got1) and (not got2 or real.endswith(got2)):
        return True
    return False


def did_from_punch(ident: bytes, field2: bytes, value: int) -> str:
    """Rebuild the virtual DID from a PunchPkt.

    `value` is the numeric middle of the DID: `G016638JHDXM` ↔ 16638 (0x40FE),
    `G550856URFKG` ↔ 550856 (0x867C8). That is why LanSearch returns enough
    data for the full DID even though the fields hold only the prefix and suffix.
    """
    prefix = ident.rstrip(b"\x00").decode("ascii", "replace").upper()
    suffix = field2.rstrip(b"\x00").decode("ascii", "replace").upper()
    virtual = REAL_TO_VIRTUAL.get(prefix)
    if not virtual or not suffix or value < 0 or value > 999999:
        return ""
    return f"{virtual}{value:06d}{suffix}"


# ------------------------------------------------------------------ time

def local_tz_hours(ts: float | None = None) -> int:
    """Local timezone offset in whole hours (what the app puts into byte 5)."""
    try:
        dt = (_dt.datetime.fromtimestamp(time.time() if ts is None else ts)
              .astimezone())
        off = dt.utcoffset()
        return int(off.total_seconds() // 3600) if off else 0
    except Exception:
        return 0


# ------------------------------------------------------------------ packets

def lu_frame(obj: dict, seq: int = 0, tz_hours: int | None = None,
             ts: int | None = None) -> bytes:
    """Lu frame with JSON inside (like encodeData in libLuPPCSParseTool)."""
    payload = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    tz = local_tz_hours() if tz_hours is None else int(tz_hours)
    secs = int(time.time()) if ts is None else int(ts)
    head = (struct.pack("<I", LU_CMD) + bytes([seq & 0xFF, tz & 0xFF])
            + struct.pack("<Q", (secs + tz * 3600) & 0xFFFFFFFFFFFFFFFF)
            + b"\x00" * 8 + struct.pack("<I", len(payload)))
    return head + payload + struct.pack("<I", LU_TRAILER)


def data_packet(obj: dict, msg_id: int = 0, tz_hours: int | None = None) -> bytes:
    """F1 D0 len | D1 01 <msg_id u16 BE> | Lu frame."""
    body = (b"\xd1\x01" + struct.pack(">H", msg_id & 0xFFFF)
            + lu_frame(obj, seq=0, tz_hours=tz_hours))
    return HEADER.pack(MAGIC, CMD_DATA, len(body)) + body


def punch_packet(did: str, value: int = 0x40FE) -> bytes:
    """PunchPkt (0x41): [DID first 4][u32][DID last 5].

    `value` is **not a constant**: it must be taken from the LanSearch response of
    a particular camera (0x40FE for the old one, 0x867C8 for G550856URFKG). The camera
    does not answer P2PRdy for a foreign value. The function used to be hardcoded for
    `GHBB`/`JHDXM`; now both DID parts and the value come from the caller.
    """
    a, b = did_parts(did)
    payload = a[:7].ljust(8, b"\x00") + struct.pack(">I", value & 0xFFFFFFFF) \
        + b[:7].ljust(8, b"\x00")
    return HEADER.pack(MAGIC, CMD_PUNCHPKT, len(payload)) + payload


def lan_search_packet() -> bytes:
    return HEADER.pack(MAGIC, CMD_LANSEARCH, 0)


def p2p_ready_packet() -> bytes:
    return HEADER.pack(MAGIC, CMD_P2PRDY, 0)


def close_packet() -> bytes:
    return HEADER.pack(MAGIC, CMD_CLOSE, 0)


def alive_ack_packet(payload: bytes = b"") -> bytes:
    """AliveAck (0xE1). The SDK does not document the exact payload — an echo is safer than empty."""
    return HEADER.pack(MAGIC, CMD_ALIVE_ACK, len(payload)) + payload


def ack_packet(nums, channel: int = MEDIA_CHANNEL) -> bytes:
    """F1 D1 len | D1 <channel> <N BE16> | N × BE16."""
    body = bytes([0xD1, channel & 0xFF]) + struct.pack(">H", len(nums)) + b"".join(
        struct.pack(">H", n & 0xFFFF) for n in nums)
    return HEADER.pack(MAGIC, CMD_ACK, len(body)) + body


def ack_packets(nums, channel: int = MEDIA_CHANNEL,
                limit: int = ACK_MAX_NUMS) -> list[bytes]:
    """Split acknowledgements into packets of at most limit numbers."""
    items = list(nums)
    step = max(1, int(limit))
    return [ack_packet(items[i:i + step], channel) for i in range(0, len(items), step)]


def parse_pppp(data: bytes) -> tuple[int, bytes] | None:
    """Parse an outer PPPP packet: (cmd, payload) or None."""
    if len(data) < 4 or data[0] != MAGIC:
        return None
    ln = struct.unpack_from(">H", data, 2)[0]
    return data[1], data[4:4 + ln]


# ------------------------------------------------------------------ Lu parsing

def cut_lu_frames(stream: bytearray) -> list[bytes]:
    """Cut complete JSON bodies of Lu frames out of the command channel stream.

    Channel 1 arrives in DRW packets and is glued into a stream; one camera response
    (e.g. `GetTfVideoList` for 30 files) spans several packets,
    so it is the stream that must be cut, not the individual packets.
    """
    magic = struct.pack("<I", LU_CMD)
    bodies: list[bytes] = []
    while True:
        pos = stream.find(magic)
        if pos < 0:
            del stream[:max(0, len(stream) - 3)]
            return bodies
        if pos:
            del stream[:pos]
        if len(stream) < LU_HEAD:
            return bodies
        ln = struct.unpack_from("<I", stream, 22)[0]
        if ln > LU_MAX_JSON:
            del stream[:1]
            continue
        if len(stream) < LU_HEAD + ln + 4:
            return bodies
        if struct.unpack_from("<I", stream, LU_HEAD + ln)[0] != LU_TRAILER:
            del stream[:1]
            continue
        bodies.append(bytes(stream[LU_HEAD:LU_HEAD + ln]))
        del stream[:LU_HEAD + ln + 4]


def cut_download_frames(stream: bytearray) -> list[tuple[DownloadHeader, bytes]]:
    """Cut complete channel 3 frames out of the stream of DRW fragments.

    One file block is a Lu frame 25+len+4 (usually 6 KB of data), but DRW may
    deliver it in several packets; it is the stream that must be assembled, not the packet.
    """
    magic = DOWNLOAD_MAGIC
    out: list[tuple[DownloadHeader, bytes]] = []
    while True:
        pos = stream.find(magic)
        if pos < 0:
            del stream[:max(0, len(stream) - 3)]
            return out
        if pos:
            del stream[:pos]
        if len(stream) < DOWNLOAD_HEADER:
            return out
        ln = struct.unpack_from("<I", stream, 21)[0]
        if ln > (1 << 20):
            del stream[:1]
            continue
        need = DOWNLOAD_HEADER + ln + 4
        if len(stream) < need:
            return out
        if struct.unpack_from("<I", stream, DOWNLOAD_HEADER + ln)[0] != LU_TRAILER:
            del stream[:1]
            continue
        body = bytes(stream[:need])
        hdr = parse_download_header(body)
        if hdr is not None:
            out.append((hdr, body[DOWNLOAD_HEADER:DOWNLOAD_HEADER + ln]))
        del stream[:need]


def json_messages(bodies) -> list[dict]:
    """Parse JSON bodies without killing the channel on garbage."""
    out = []
    for body in bodies:
        try:
            msg = json.loads(body.decode("utf-8", "replace"))
        except Exception:
            out.append({"_raw": bytes(body).hex(" ")})
            continue
        out.append(msg if isinstance(msg, dict) else {"_value": msg})
    return out


# ------------------------------------------------------------------ media headers

@dataclass
class VideoHeader:
    """Video frame layout (spec §1.4.2), verified on 1185 frames from a live camera."""

    uid: int = 0
    params0: int = 0
    battery: float = 99.0
    full_len_hint: int = 0
    ts_ms: int = 0
    duration: int = 0
    frame_no: int = 0
    frame_type: int = 0
    codec_hint: int = 0
    params1: int = 0
    width: int = 0
    height: int = 0
    payload_len: int = 0

    @property
    def key(self) -> bool:
        return self.frame_type == 1

    @property
    def codec(self) -> str:
        return "h265" if self.codec_hint == 3 else "h264"


def parse_video_header(data: bytes) -> VideoHeader | None:
    if len(data) < VIDEO_HEADER or data[:4] != FRAME_MAGIC:
        return None
    h = VideoHeader()
    h.uid = data[4]
    h.params0 = struct.unpack_from("<I", data, 21)[0]
    try:
        h.battery = struct.unpack_from("<f", data, 25)[0]
    except struct.error:
        h.battery = 99.0
    h.full_len_hint = struct.unpack_from("<I", data, 41)[0]
    h.ts_ms = struct.unpack_from("<I", data, 46)[0]
    h.duration = struct.unpack_from("<I", data, 50)[0]
    h.frame_no = struct.unpack_from("<I", data, 58)[0]
    h.frame_type = data[62]
    bits = struct.unpack_from("<Q", data, 63)[0]
    h.codec_hint = bits & 0x7
    h.params1 = (bits >> 3) & 0x7F
    h.width = (bits >> 17) & 0x1FFF
    h.height = (bits >> 30) & 0x1FFF
    h.payload_len = struct.unpack_from("<I", data, 79)[0]
    return h


@dataclass
class AudioHeader:
    uid: int = 0
    ts_ms: int = 0
    frame_no: int = 0
    audio_type: int = 0
    payload_len: int = 0


def parse_audio_header(data: bytes) -> AudioHeader | None:
    if len(data) < AUDIO_HEADER or data[:4] != AUDIO_MAGIC:
        return None
    h = AudioHeader()
    h.uid = data[4]
    h.ts_ms = struct.unpack_from("<I", data, 46)[0] if len(data) >= 50 else 0
    h.frame_no = struct.unpack_from("<I", data, 58)[0] if len(data) >= 62 else 0
    h.audio_type = struct.unpack_from("<I", data, 58)[0] & 0x1F
    h.payload_len = struct.unpack_from("<I", data, 74)[0]
    return h


@dataclass
class DownloadHeader:
    """Channel 3 frame (spec §1.4.4). block_no = −1000 — the last block."""

    userid: int = 0
    block_no: int = 0
    flag: int = 0
    payload_len: int = 0

    @property
    def last(self) -> bool:
        return self.block_no == -1000


def parse_download_header(data: bytes) -> DownloadHeader | None:
    if len(data) < DOWNLOAD_HEADER or data[:4] != DOWNLOAD_MAGIC:
        return None
    return DownloadHeader(
        userid=struct.unpack_from("<I", data, 4)[0],
        block_no=struct.unpack_from("<i", data, 8)[0],
        flag=data[12],
        payload_len=struct.unpack_from("<I", data, 21)[0],
    )


# ------------------------------------------------------------------ H.264 helpers

def nal_start(blob: bytes, scan: int = 512) -> int:
    """Start of the real H.264 NAL inside a frame.

    Frame headers occasionally contain the bytes ``00 00 00 01`` (length fields),
    so the first start code is not always a NAL. Take the first one followed by
    a plausible NAL type.
    """
    pos = blob.find(START_CODE)
    while 0 <= pos < scan:
        if pos + 4 < len(blob) and (blob[pos + 4] & 0x1F) in NAL_TYPES:
            return pos
        pos = blob.find(START_CODE, pos + 4)
    return blob.find(START_CODE)


def annexb_offset(blob: bytes, header_len: int = VIDEO_HEADER,
                  alt_len: int = 43) -> int:
    """Start of Annex B in a frame, given the known header length.

    The 83-byte header contains fields among which, for certain clock/frame-number
    values, the bytes ``00 00 00 01`` occur with a "plausible" next byte —
    `nal_start` may then mistake them for a NAL. It is more reliable to check the
    known offsets 83 (this camera) and 43 (the old variant) and only then scan
    heuristically.
    """
    for pos in (header_len, alt_len):
        if pos + 5 <= len(blob) and blob[pos:pos + 4] == START_CODE \
                and (blob[pos + 4] & 0x1F) in NAL_TYPES:
            return pos
    return nal_start(blob)


def split_annexb(au: bytes) -> list[bytes]:
    """Split Annex B into NALs without start codes (for RTP/MP4)."""
    out: list[bytes] = []
    pos = au.find(START_CODE)
    if pos < 0:
        return out
    while pos >= 0:
        start = pos + len(START_CODE)
        nxt = au.find(START_CODE, start)
        end = nxt if nxt > 0 else len(au)
        if end > start:
            out.append(au[start:end])
        pos = nxt
    return out


def find_parameter_sets(au: bytes) -> tuple[bytes, bytes, bytes]:
    """Return (sps_nal, pps_nal, codec string) from an access unit; empty if absent."""
    sps = pps = b""
    seen_sps = False
    for nal in split_annexb(au):
        if not nal:
            continue
        t = nal[0] & 0x1F
        if t == 7 and not sps:
            sps = nal
            seen_sps = True
        elif t == 8 and not pps and seen_sps:
            pps = nal
    codec = codec_string(sps) if sps else ""
    return sps, pps, codec


def codec_string(sps_nal: bytes) -> str:
    """WebCodecs codec string from the SPS NAL: avc1.PPCCLL."""
    if len(sps_nal) < 4:
        return ""
    return "avc1." + bytes(sps_nal[1:4]).hex().upper()


def profile_level_id(sps_nal: bytes) -> str:
    if len(sps_nal) < 4:
        return "4D0028"
    return bytes(sps_nal[1:4]).hex().upper()


# ------------------------------------------------------------------ secrets

def scrub_secrets(value):
    """Recursively strip secret fields from camera responses (API and logs)."""
    if isinstance(value, dict):
        return {k: ("***" if str(k) in SECRET_KEYS else scrub_secrets(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_secrets(v) for v in value]
    return value


def lenient_int(value, default: int = 0) -> int:
    """A camera numeric field may arrive as a string, a float, and so on."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        text = str(value).strip()
        if not text:
            return default
        return int(float(text))
    except (TypeError, ValueError):
        return default


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "да")
