"""PPPP LAN client: discovery -> punch -> commands in Lu frames (data is not encrypted!).

Data frame format (from the dump and the disassembler):
    F1 D0 <len BE16>            PPPP: cmd 0xD0, len = length of the rest
    D1 01 <idx:2 BE>            DRW: channel 1 (commands), packet number
    A0 AF AF AF                 Lu command magic
    <0:1> <tz:1> <ts:8 LE>      0, time zone offset in hours (we have type_=3 =
                                UTC+3), local time (as encodeData in the .so)
    <8 zeros>                   reserved
    <len:4 LE>                  JSON length
    <JSON>
    F4 F3 F2 F1                 trailer
"""
from __future__ import annotations

import json
import os
import socket
import struct
import time

MAGIC = 0xF1
LU_CMD = 0xAFAFAFA0
LU_TRAILER = 0xF1F2F3F4
SUBMAGIC = b"\xd1\x01\x00\x00"

CMD_LANSEARCH = 0x30
CMD_PUNCHPKT = 0x41
CMD_DATA = 0xD0
CMD_ACK = 0xD1
CMD_P2PRDY = 0x42
CMD_TRYLAN = 0x43
CMD_CLOSE = 0xF0

MEDIA_ACK_BATCH = 8         # in the application dump, batches of up to 17; fewer — the window moves faster
MEDIA_ACK_INTERVAL = 0.03   # the application acked roughly every 83 ms; 30 ms raises the rate
# How many numbers to put into one ack packet. The camera tears down the session (sends Close 0xF0)
# if an ack has more than 64 numbers: verified on SD downloads (77 and 108 numbers — Close after
# 1-2 s, 64 — fine). The application sends up to ~17; 32 is a safe margin, and no extra packets.
ACK_MAX_NUMS = 32
CMD_CHANNEL = 1             # DRW channel of commands and replies
# How long to wait for a missing DRW packet of the command channel before skipping over the gap.
CMD_GAP_TIMEOUT = 1.5

RAW_MAGIC = b"LCRAW1\n"     # header of the file with raw fragments (for offline analysis)
RAW_HEAD = ">IHI"           # record: ms from stream start, fragment number, data length

FRAME_MAGIC = b"\x01\xaf\xaf\xaf"   # a media frame starts with this (header + H.264 NAL)
START_CODE = b"\x00\x00\x00\x01"    # Annex B: the bitstream starts with this in a frame
SPS_NAL = b"\x00\x00\x00\x01\x27"   # H.264 SPS as this camera sends it (nal_ref_idc=1)
SPS_NAL_ALT = b"\x00\x00\x00\x01\x67"
NAL_TYPES = {1, 5, 6, 7, 8, 9, 20}  # valid H.264 NAL types for finding the start


def lu_frame(obj: dict, seq: int = 0, type_: int = 3, ts: int | None = None) -> bytes:
    """Build a Lu frame with JSON inside (like encodeData in libLuPPCSParseTool)."""
    payload = json.dumps(obj, separators=(",", ":")).encode()
    ts = int(time.time()) if ts is None else ts
    head = struct.pack("<I", LU_CMD) + bytes([seq & 0xFF, type_ & 0xFF]) \
        + struct.pack("<Q", ts) + b"\x00" * 8 + struct.pack("<I", len(payload))
    return head + payload + struct.pack("<I", LU_TRAILER)


def data_packet(obj: dict, msg_id: int = 0, type_: int = 3) -> bytes:
    """Data frame: F1 D0 len | D1 01 00 <msg_id> | Lu frame.

    msg_id — message counter (0,1,2,…): the camera uses it to tell new commands
    from repeats. The application always sends a new number.
    """
    body = b"\xd1\x01\x00" + bytes([msg_id & 0xFF]) + lu_frame(obj, seq=0, type_=type_)
    return struct.pack(">BBH", MAGIC, CMD_DATA, len(body)) + body


def punch_packet(a: bytes = b"GHBB", b: bytes = b"JHDXM", value: int = 0x40FE) -> bytes:
    payload = a[:7].ljust(8, b"\x00") + struct.pack(">I", value) + b[:7].ljust(8, b"\x00")
    return struct.pack(">BBH", MAGIC, CMD_PUNCHPKT, len(payload)) + payload


def ack_packet(nums, channel: int = 0) -> bytes:
    """Acknowledgement of channel DRW packets: F1 D1 <len> | D1 <channel> <N BE16> | N × BE16.

    The format was taken from a working dump (03_29_04.pcap): the application acknowledges
    every received fragment, in batches of up to ~17 numbers every ~90 ms. Repeats (the camera
    resends unacknowledged ones) are acknowledged too. One packet without a length
    limit — for sending use ack_packets(), which splits into safe batches.
    """
    body = bytes([0xD1, channel & 0xFF]) + struct.pack(">H", len(nums)) + b"".join(
        struct.pack(">H", n & 0xFFFF) for n in nums)
    return struct.pack(">BBH", MAGIC, CMD_ACK, len(body)) + body


def ack_packets(nums, channel: int = 0, limit: int = ACK_MAX_NUMS) -> list[bytes]:
    """Ack packets of at most limit numbers (the camera tears down the session on an ack longer than 64)."""
    nums = list(nums)
    step = max(1, limit)
    return [ack_packet(nums[i:i + step], channel) for i in range(0, len(nums), step)]


def cut_lu_frames(stream: bytearray) -> list[bytes]:
    """Cut complete Lu frames out of the command channel stream, return their JSON bodies.

    Frame: A0 AF AF AF | 0 | tz | u64 time | 8 zeros | u32 len @22 | JSON | F4 F3 F2 F1.
    An incomplete frame stays in stream until the next bytes; garbage before the magic and a frame
    with a wrong trailer are skipped (we look for the next magic).
    """
    magic = struct.pack("<I", LU_CMD)
    bodies = []
    while True:
        pos = stream.find(magic)
        if pos < 0:
            del stream[:max(0, len(stream) - 3)]     # the tail may be the beginning of the magic
            return bodies
        del stream[:pos]
        if len(stream) < 26:
            return bodies
        ln = struct.unpack_from("<I", stream, 22)[0]
        if ln > 1 << 20:                                # clearly not a length — false magic
            del stream[:1]
            continue
        if len(stream) < 26 + ln + 4:
            return bodies
        if struct.unpack_from("<I", stream, 26 + ln)[0] != LU_TRAILER:
            del stream[:1]
            continue
        bodies.append(bytes(stream[26:26 + ln]))
        del stream[:26 + ln + 4]


def read_raw(path: str):
    """Read the recording of raw fragments: (ms from start, fragment number, data)."""
    with open(path, "rb") as fh:
        if fh.read(len(RAW_MAGIC)) != RAW_MAGIC:
            raise ValueError(f"{path}: нет заголовка {RAW_MAGIC!r}")
        while True:
            head = fh.read(struct.calcsize(RAW_HEAD))
            if len(head) < struct.calcsize(RAW_HEAD):
                return
            ms, frag, ln = struct.unpack(RAW_HEAD, head)
            data = fh.read(ln)
            if len(data) < ln:
                return
            yield ms, frag, data


class P2PClient:
    """Mini PPPP client for access-point mode (data over UDP, no encryption)."""

    def __init__(self, host: str, source_ip: str | None = None, port: int = 32108,
                 verbose: bool = False):
        self.host = host
        self.source_ip = source_ip
        self.port = port
        self.verbose = verbose
        self.sock: socket.socket | None = None
        self.cam_addr: tuple[str, int] | None = None
        self.seq = 0
        self._reset_cmd_channel()

    def _reset_cmd_channel(self) -> None:
        """Command channel assembly state: camera DRW numbers in a new session start at 0."""
        self._cmd_next = 0
        self._cmd_buf: dict[int, bytes] = {}
        self._cmd_stream = bytearray()
        self._cmd_gap_since: float | None = None

    # ------------------------------------------------------------------ socket
    def _open(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        for cand in ([(self.source_ip, self.port)] if self.source_ip else []) + \
                    [("0.0.0.0", self.port), ("0.0.0.0", 0)]:
            try:
                s.bind(cand)
                break
            except OSError:
                continue
        s.settimeout(0.3)
        self.sock = s

    def _recv(self, timeout: float) -> list[tuple[tuple[str, int], int, bytes]]:
        """Collect (address, cmd, payload) for timeout seconds."""
        assert self.sock is not None
        out = []
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(data) < 4 or data[0] != MAGIC:
                continue
            cmd = data[1]
            ln = struct.unpack(">H", data[2:4])[0]
            out.append((addr, cmd, data[4:4 + ln]))
        return out

    # ---------------------------------------------------------------- stages
    @staticmethod
    def _directed_broadcast(ip: str | None) -> str | None:
        """Broadcast address /24 for ip ('192.168.0.179' -> '192.168.0.255')."""
        if not ip:
            return None
        parts = ip.split(".")
        if len(parts) != 4 or not all(p.isdigit() for p in parts):
            return None
        return ".".join(parts[:3] + ["255"])

    def _search_targets(self) -> list[str]:
        """Where to send LanSearch.

        A camera on the router's network answers only to the subnet's directed broadcast:
        it stays silent on unicast, and 255.255.255.255 on a machine with several
        interfaces goes into the default route (on ours it is taken by the VPN) and does not
        reach the camera. Verified with the work\\bcast_probe.py probe.
        """
        assert self.sock is not None
        bound = self.sock.getsockname()[0]
        targets: list[str] = []
        for cand in (self._directed_broadcast(self.host),
                     self._directed_broadcast(self.source_ip),
                     self._directed_broadcast(bound),
                     "255.255.255.255",
                     self.host):
            if cand and cand != "0.0.0.0" and cand not in targets:
                targets.append(cand)
        return targets

    def discover(self, timeout: float = 2.0) -> tuple[str, int]:
        """LanSearch broadcast → camera address (its reply comes from its port).

        We return as soon as a reply arrives: otherwise any run pays the
        full timeout (for viewing that is extra seconds before the picture).
        """
        self._open()
        self._reset_cmd_channel()
        assert self.sock is not None
        pkt = struct.pack(">BBH", MAGIC, CMD_LANSEARCH, 0)
        for target in self._search_targets():
            try:
                self.sock.sendto(pkt, (target, self.port))
            except OSError:
                continue
        deadline = time.time() + timeout
        while time.time() < deadline:
            for addr, cmd, payload in self._recv(min(0.3, max(0.05, deadline - time.time()))):
                if cmd in (0x41, 0x42, 0x43):    # PunchPkt / P2PRdy / TryLanTcp
                    self.cam_addr = addr
                    return addr
        raise RuntimeError("камера не ответила на LAN-поиск")

    def punch(self, timeout: float = 2.0, attempts: int = 3) -> bool:
        """Send PunchPkt and wait for P2PRdy (session ready).

        The camera is sometimes busy with a previous session — hence several attempts.
        """
        assert self.sock is not None and self.cam_addr
        pkt = punch_packet()
        for attempt in range(max(1, attempts)):
            deadline = time.time() + timeout
            while time.time() < deadline:
                self.sock.sendto(pkt, self.cam_addr)
                for _addr, cmd, _payload in self._recv(0.35):
                    if cmd == CMD_P2PRDY:
                        return True
            if attempt + 1 < attempts:
                time.sleep(0.8)
        return False

    def send_command(self, obj: dict, type_: int = 3) -> int:
        assert self.sock is not None and self.cam_addr
        pkt = data_packet(obj, msg_id=self.seq, type_=type_)
        used = self.seq
        self.seq = (self.seq + 1) & 0xFF
        self.sock.sendto(pkt, self.cam_addr)
        return used

    def send_repeated(self, obj: dict, times: int = 10, interval: float = 0.011) -> int:
        """Send the command times times with ONE msg_id (like the application).

        The camera tells a new command from a repeat by msg_id, so repeats
        must carry the same number — otherwise it sees 10 different commands.
        """
        assert self.sock is not None and self.cam_addr
        pkt = data_packet(obj, msg_id=self.seq)
        used = self.seq
        self.seq = (self.seq + 1) & 0xFF
        for _ in range(max(1, times)):
            self.sock.sendto(pkt, self.cam_addr)
            time.sleep(interval)
        return used

    def send_ack(self, nums, channel: int = 0) -> None:
        """Acknowledge the received channel DRW numbers (cmd 0xD1).

        We split into packets of at most ACK_MAX_NUMS: on an ack longer than 64 numbers the camera
        replies with Close and tears down the session. Long batches accumulate when the receive
        loop stalls (decoder, garbage collection) — so splitting is always needed.
        """
        assert self.sock is not None and self.cam_addr
        for pkt in ack_packets(nums, channel):
            self.sock.sendto(pkt, self.cam_addr)

    def _feed_cmd_packet(self, payload: bytes) -> int | None:
        """Accept a command channel DRW packet (D1 01 <idx> <data>), return its number.

        The camera's reply may span several DRW packets (GetTfVideoList for 30 files
        — 5 packets), so the channel is assembled as a stream: in number order, without
        repeats, and only then cut into Lu frames (cut_lu_frames).
        """
        if len(payload) < 4 or payload[0] != 0xD1 or payload[1] != CMD_CHANNEL:
            return None
        idx = struct.unpack_from(">H", payload, 2)[0]
        # number already issued (repeat) — "behind" the expected one modulo 2^16
        if (idx - self._cmd_next) & 0xFFFF < 0x8000:
            self._cmd_buf.setdefault(idx, payload[4:])
        return idx

    def _drain_cmd_channel(self) -> list[bytes]:
        """Emit what has accumulated in number order and cut out complete Lu frames."""
        if self._cmd_buf and self._cmd_next not in self._cmd_buf:
            # gap: the packet is lost for good (or numbering did not start at 0) — wait and
            # skip over, otherwise the command channel stalls forever
            now = time.time()
            if self._cmd_gap_since is None:
                self._cmd_gap_since = now
            elif now - self._cmd_gap_since > CMD_GAP_TIMEOUT:
                self._cmd_next = min(self._cmd_buf,
                                     key=lambda k: (k - self._cmd_next) & 0xFFFF)
        while self._cmd_next in self._cmd_buf:
            self._cmd_stream += self._cmd_buf.pop(self._cmd_next)
            self._cmd_next = (self._cmd_next + 1) & 0xFFFF
            self._cmd_gap_since = None
        return cut_lu_frames(self._cmd_stream)

    def read_messages(self, timeout: float = 3.0) -> list[dict]:
        """Receive JSON messages from the camera on the command channel for timeout seconds.

        Channel 1 packets are acknowledged (as the application's SDK does), otherwise the camera
        resends them; other channels are not parsed here.
        """
        acks = []
        for _addr, cmd, payload in self._recv(timeout):
            if cmd != CMD_DATA:
                continue
            idx = self._feed_cmd_packet(payload)
            if idx is not None:
                acks.append(idx)
        if acks and self.sock is not None and self.cam_addr:
            self.send_ack(acks, CMD_CHANNEL)
        msgs = []
        for body in self._drain_cmd_channel():
            try:
                msgs.append(json.loads(body.decode("utf-8", "replace")))
            except Exception:
                msgs.append({"_raw": body.hex(" ")})
        return msgs

    def login(self, password: str, timeout: float = 4.0) -> dict | None:
        """Send LoginDev and wait for the camera's reply."""
        self.send_command({"pwd": password, "cmd": "LoginDev"})
        deadline = time.time() + timeout
        while time.time() < deadline:
            for msg in self.read_messages(min(1.0, deadline - time.time())):
                if isinstance(msg, dict) and ("result" in msg or "pwd" in msg or "cmd" in msg):
                    return msg
        return None

    def close(self) -> None:
        """Close the session on the camera (Close 0xF0), then the socket.

        The camera holds a single active session: if it is not closed, the next
        connection (including the official application) gets stuck.
        """
        if self.sock is not None and self.cam_addr:
            try:
                self.sock.sendto(struct.pack(">BBH", MAGIC, CMD_CLOSE, 0), self.cam_addr)
                time.sleep(0.05)
                self.sock.sendto(struct.pack(">BBH", MAGIC, CMD_CLOSE, 0), self.cam_addr)
            except OSError:
                pass
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None


def nal_start(blob: bytes, scan: int = 256) -> int:
    """Start of the H.264 NAL in a frame.

    The frame header may itself contain the bytes ``00 00 00 01`` (for example in the
    length/counter fields), so the first start code found is not always the real one.
    We take the first one followed by a plausible NAL type.
    """
    pos = blob.find(START_CODE)
    while 0 <= pos < scan:
        if pos + 4 < len(blob) and (blob[pos + 4] & 0x1F) in NAL_TYPES:
            return pos
        pos = blob.find(START_CODE, pos + 4)
    return blob.find(START_CODE)


class H264Recorder:
    """Writes H.264 Annex B **as fragments arrive**, without waiting for a whole frame.

    A frame starts with a fragment carrying the ``01 AF AF AF`` magic and contains the container
    header (43/83 bytes, etc.), immediately followed by the H.264 NAL —
    ordinary Annex B. We cut off the header and write the NALs in sequence.

    Previously a frame was written only as a whole (we waited for all fragments) — because of that
    the stream stalled while a key frame was being transmitted (~27 fragments ≈ 0.5 s)
    and the video jerked. Now we write immediately, and hold reordered fragments
    in a small buffer.
    """

    GAP_TIMEOUT = 0.5      # how long to wait for a missing fragment before resynchronizing

    def __init__(self, path: str, rotate_seconds: float | None = None,
                 on_segment=None):
        self.path = path
        self.rotate_seconds = rotate_seconds
        self.on_segment = on_segment
        self.index = 0
        self.fh = open(self._name(0), "wb")
        self.frames = 0
        self.bytes = 0
        self.dropped = 0                     # fragments before the first SPS/IDR
        self.gaps = 0                        # skipped numbers
        self.resyncs = 0                     # recoveries after loss
        self.header_lens: set[int] = set()
        self._started = False
        self._resync = False                 # waiting for a key frame after loss
        self._next = 0                       # expected fragment number
        self._pending: dict[int, bytes] = {}
        self._last_progress = time.time()
        self._seg_start = time.time()
        self.committed = 0                   # end of the last whole frame in the file

    def _name(self, index: int) -> str:
        if not self.rotate_seconds:
            return self.path
        root, ext = os.path.splitext(self.path)
        return f"{root}_{index:04d}{ext or '.h264'}"

    def _rotate(self) -> None:
        """Close the current segment (at an SPS/IDR boundary) and start a new one."""
        self.fh.flush()
        self.committed = self.fh.tell()
        self.fh.close()
        if self.on_segment is not None:
            self.on_segment(self._name(self.index))
        self.index += 1
        self.fh = open(self._name(self.index), "wb")
        self._seg_start = time.time()

    def feed(self, frag: int, data: bytes) -> None:
        """Add a fragment; everything that comes in order is written to the file immediately."""
        keyframe = data.startswith(FRAME_MAGIC) and (SPS_NAL in data or SPS_NAL_ALT in data)
        if not self._started:
            # wait for a frame with SPS: otherwise the file cannot be decoded from the start
            if keyframe:
                self._started = True
                self._next = frag
            else:
                self.dropped += 1
                return
        if self._resync:
            if keyframe:
                self._leave_resync(frag, data)
            return
        if frag < self._next - 1000:         # numbering rolled back — the stream restarted
            self._enter_resync()
            if keyframe:
                self._leave_resync(frag, data)
            return
        if frag < self._next:                # already written (repeat/late)
            return
        self._pending.setdefault(frag, data)
        self._flush()

    def _enter_resync(self) -> None:
        """We lost a fragment: do not write further until a key frame arrives.

        Otherwise a NAL fragment gets into the file — the decoder will spew errors for the whole
        rest of the GOP (and a live decoder also re-reads the file many times).
        """
        self._resync = True
        self.gaps += len(self._pending)
        self._pending.clear()
        self.resyncs += 1
        if self.rotate_seconds:
            self._rotate()                   # close the dirty segment immediately

    def _leave_resync(self, frag: int, data: bytes) -> None:
        self._resync = False
        self._next = frag
        self._write_fragment(data)           # a key frame starts a frame by itself
        self._next += 1
        self._last_progress = time.time()

    def _flush(self) -> None:
        while True:
            data = self._pending.pop(self._next, None)
            if data is None:
                if self._pending and time.time() - self._last_progress > self.GAP_TIMEOUT:
                    self._enter_resync()
                return
            self._write_fragment(data)
            self._next += 1
            self._last_progress = time.time()

    def _write_fragment(self, data: bytes) -> None:
        if data.startswith(FRAME_MAGIC):
            # the end of the previous frame must already be on disk (the mirror reads the file
            # with a separate handle; a buffered tail would give a truncated NAL)
            self.fh.flush()
            self.committed = self.fh.tell()
            pos = nal_start(data)
            if pos < 0 or pos >= len(data):
                return
            payload = data[pos:]
            has_sps = SPS_NAL in payload or SPS_NAL_ALT in payload
            if (self.rotate_seconds and has_sps
                    and time.time() - self._seg_start >= self.rotate_seconds):
                self._rotate()                       # cut only on a key frame
            self.header_lens.add(pos)
            self.frames += 1
        else:
            payload = data                           # continuation of a NAL
        self.fh.write(payload)
        self.bytes += len(payload)

    def close(self) -> None:
        """Flush the buffer and close the file."""
        self._flush()
        self.fh.flush()
        self.committed = self.fh.tell()
        self.fh.close()
        if self.rotate_seconds and self.on_segment is not None and self.frames:
            self.on_segment(self._name(self.index))


class MediaStream:
    """Camera media stream: fragment parsing, acknowledgements (ack) and statistics.

    Media packet header: ``F1 D0 <len>`` + ``D1 00 <frag:u16 BE>`` + data.

    Fragment numbering is **continuous within a session** (verified on a live camera
    and on the application dump): it does not reset on every frame — in the dump it reached
    832 and only then rolled back to zero. Therefore "frames" cannot be determined by number:
    frame boundaries are inside the payload (after decryption).

    Without acknowledgements the camera resends fragments that have already been sent and does not
    move on; ack moves the window forward.
    """

    def __init__(self, client: P2PClient, ack_batch: int = MEDIA_ACK_BATCH,
                 ack_interval: float = MEDIA_ACK_INTERVAL, raw_path: str | None = None,
                 h264_path: str | None = None):
        self.client = client
        self.ack_batch = ack_batch
        self.ack_interval = ack_interval
        # counters
        self.packets = 0
        self.service = 0
        self.bytes = 0
        self.acks = 0
        self.acked = 0
        self.seen_frags: set[int] = set()
        self.max_frag = 0
        # recording of raw fragments (for offline analysis of the cipher)
        self.raw_path = raw_path
        self.raw = None
        self.raw_records = 0
        self.raw_bytes = 0
        # recording of ready H.264
        self.recorder = H264Recorder(h264_path) if h264_path else None
        self._t0 = time.time()
        if raw_path:
            self.raw = open(raw_path, "wb")
            self.raw.write(RAW_MAGIC)
        # acknowledgements
        self.pending: list[int] = []
        self.last_ack = time.time()

    # ------------------------------------------------------------- receive
    def feed(self, payload: bytes) -> bool:
        """Parse the payload of a 0xD0 frame. True — this is a media fragment."""
        if len(payload) < 4 or payload[0] != 0xD1 or payload[1] != 0x00:
            return False
        frag = struct.unpack_from(">H", payload, 2)[0]
        data = payload[4:]
        self.packets += 1
        self.bytes += len(data)
        self.seen_frags.add(frag)
        if frag > self.max_frag:
            self.max_frag = frag
        if self.raw is not None:
            ms = int((time.time() - self._t0) * 1000) & 0xFFFFFFFF
            self.raw.write(struct.pack(RAW_HEAD, ms, frag, len(data)))
            self.raw.write(data)
            self.raw_records += 1
            self.raw_bytes += len(data)
        self.pending.append(frag)
        if self.recorder is not None:
            self.recorder.feed(frag, data)
        return True

    # --------------------------------------------------------------- ack
    def flush(self, force: bool = False) -> None:
        """Send the accumulated numbers if a batch has filled up or the interval has elapsed."""
        if not self.pending:
            return
        if not force and len(self.pending) < self.ack_batch \
                and time.time() - self.last_ack < self.ack_interval:
            return
        nums = self.pending
        self.pending = []
        self.client.send_ack(nums)
        self.acks += 1
        self.acked += len(nums)
        self.last_ack = time.time()

    def pump(self, timeout: float = 0.05) -> int:
        """Read what has arrived and acknowledge the fragments. Returns the number of media packets."""
        got = 0
        for _addr, cmd, payload in self.client._recv(timeout):
            if cmd != CMD_DATA:
                continue
            if self.feed(payload):
                got += 1
            else:
                self.service += 1
        self.flush()
        return got

    def close(self) -> None:
        """Close the recording files (raw fragments and H.264)."""
        if self.raw is not None:
            self.raw.close()
            self.raw = None
        if self.recorder is not None:
            self.recorder.close()

    # ----------------------------------------------------------- statistics
    @property
    def unique_frags(self) -> int:
        """How many distinct fragment numbers arrived (a sign that the stream is moving)."""
        return len(self.seen_frags)

    @property
    def mb(self) -> float:
        return self.bytes / (1024 * 1024)

    def summary(self) -> str:
        return (f"медиа-пакетов {self.packets}, уникальных фрагментов {self.unique_frags} "
                f"(макс. номер {self.max_frag}), {self.mb:.1f} МБ, "
                f"ack {self.acks} на {self.acked} фрагментов")

