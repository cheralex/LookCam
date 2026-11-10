"""One PPPP session with a camera: punch, login, DRW reception by channel, ack, Close.

The camera holds **one** active session, so the `CameraSession` object is a
monopoly: all service consumers (browsers, RTSP, downloads, settings) go
through it. Reception is non-blocking: the endpoint calls `on_data()` for every
DRW packet, while frame and command assembly happens here.

Media and commands share one UDP stream, so this is also where the demultiplexer
by channels 0/1/3 and the acknowledgement scheduler live (batches of at most 32 numbers).
"""
from __future__ import annotations

import asyncio
import logging
import random
import struct
import time

from . import commands, frames, proto

log = logging.getLogger("lookcam.session")

#: Timeout for the first frame after OpenVideo (spec §4.3 item 4).
FIRST_FRAME_TIMEOUT = 30.0
#: Timeout for waiting for P2PRdy per punch attempt.
PUNCH_TIMEOUT = 2.0
PUNCH_ATTEMPTS = 3


class CameraSession:
    """Active session with a single camera (transport + channels + commands)."""

    def __init__(self, endpoint, did: str, password: str,
                 host_hint: str | None = None, camera_id: str = "",
                 on_au=None, on_download=None, on_command_event=None,
                 on_close=None) -> None:
        self.endpoint = endpoint
        self.did = (did or "").strip().upper()
        self.real_did = proto.real_did(self.did)
        self.password = password or ""
        self.host_hint = host_hint or None
        self.camera_id = camera_id
        self.addr: tuple[str, int] | None = None
        self.connected = False
        self.closing = False
        self.created = time.time()
        self.connected_at: float | None = None
        self.closed_at: float | None = None
        self.last_error: str | None = None
        # Value from the PunchPkt of this particular camera: 0x40FE for the old one,
        # 0x867C8 for URFKG. The camera answers P2PRdy only for its own value, so
        # a constant cannot be stored (verified on a live G550856URFKG).
        self.punch_value = 0x40FE

        self.assembler = frames.FrameAssembler()
        self.bus = commands.CommandBus(self)
        self.on_au = on_au
        self.on_download = on_download
        self.on_command_event = on_command_event
        self.on_close = on_close

        self._ready = asyncio.Event()
        self._closed_event = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        self._first_frame = asyncio.Event()
        self._stream_gen = 0

        # channel 1: stream assembly in DRW-number order
        self._cmd_next: int | None = None
        self._cmd_buf: dict[int, bytes] = {}
        self._cmd_stream = bytearray()
        self._cmd_gap_since: float | None = None

        # channel 3: also a stream (a ~6 KB block may arrive in several DRW packets)
        self._dl_next: int | None = None
        self._dl_buf: dict[int, bytes] = {}
        self._dl_stream = bytearray()
        self._dl_gap_since: float | None = None
        self.download_frames = 0

        # acknowledgements: channel -> numbers waiting to be sent
        self._ack: dict[int, list[int]] = {0: [], 1: [], 3: []}
        self._ack_last = 0.0

        # statistics
        self.media_packets = 0
        self.media_bytes = 0
        self.cmd_packets = 0
        self.download_packets = 0
        self.acks_sent = 0
        self.acked = 0
        self.last_media_at = 0.0
        self.last_video_at = 0.0
        self.first_video_at: float | None = None
        self.frames_seen = 0

    # ------------------------------------------------------------------ properties
    @property
    def is_connected(self) -> bool:
        return self.connected and not self.closing

    @property
    def addr_text(self) -> str:
        return f"{self.addr[0]}:{self.addr[1]}" if self.addr else "—"

    # ------------------------------------------------------------------ startup
    async def connect(self) -> None:
        """LanSearch → punch → P2PRdy → LoginDev → AppointDev."""
        if self.connected:
            return
        started = time.monotonic()
        found = self.endpoint.find(self.did, self.host_hint)
        if found is None:
            found = await self.endpoint.discover(self.did, self.host_hint, timeout=4.0)
        if found is None:
            raise RuntimeError("камера не отвечает на LAN-поиск "
                               f"(DID {self.real_did})")
        self.addr = found.addr
        self.punch_value = found.value or self.punch_value
        self.endpoint.register(self, self.addr)
        log.info("camera %s: %s (%s)", self.camera_id or self.did,
                 self.addr_text, found.label)

        await self._punch()
        self.connected = True
        self.connected_at = time.time()
        self._start_tasks()
        try:
            await self.bus.login(self.password)
            await self.bus.appoint_time()
        except Exception:
            self.connected = False
            await self.close()
            raise
        log.info("session %s ready in %.2f s", self.camera_id or self.did,
                 time.monotonic() - started)

    async def _punch(self) -> None:
        assert self.addr is not None
        for cycle in range(2):
            pkt = proto.punch_packet(self.did, self.punch_value)
            for attempt in range(PUNCH_ATTEMPTS):
                self._ready.clear()
                deadline = time.monotonic() + PUNCH_TIMEOUT
                while time.monotonic() < deadline:
                    self.endpoint.send(self.addr, pkt)
                    try:
                        await asyncio.wait_for(self._ready.wait(), 0.35)
                        return
                    except asyncio.TimeoutError:
                        continue
                if attempt + 1 < PUNCH_ATTEMPTS:
                    await asyncio.sleep(0.6)
            # The camera may have rebooted: the PunchPkt port/value changed.
            fresh = await self.endpoint.discover(self.did, self.host_hint,
                                                 timeout=2.5, fresh=True)
            if fresh is None:
                break
            if fresh.addr == self.addr and fresh.value == self.punch_value:
                break
            self.addr = fresh.addr
            self.punch_value = fresh.value or self.punch_value
            self.endpoint.register(self, self.addr)
            log.info("%s: camera reselected %s, punch=0x%x",
                     self.camera_id, self.addr_text, self.punch_value)
        raise RuntimeError("камера не ответила на punch (занята другой сессией?)")

    def _start_tasks(self) -> None:
        if self._tasks:
            return
        self._tasks = [
            asyncio.create_task(self._ticker(), name=f"session-tick-{self.camera_id}"),
        ]

    # ------------------------------------------------------------------ network
    def on_ready(self, addr: tuple[str, int]) -> None:
        """P2PRdy arrived: the session is open (the address may differ from PunchPkt)."""
        if self.addr != addr:
            self.addr = addr
            self.endpoint.register(self, addr)
        self._ready.set()

    def on_data(self, payload: bytes) -> None:
        """DRW packet of channel 0/1/3 from the camera."""
        if self.closing or len(payload) < 4 or payload[0] != 0xD1:
            return
        channel = payload[1]
        idx = struct.unpack_from(">H", payload, 2)[0]
        body = payload[4:]
        if channel in self._ack:
            self._ack[channel].append(idx)
            if len(self._ack[channel]) >= 96:      # guards against unbounded growth
                self._flush_acks(force=True)
        if channel == proto.MEDIA_CHANNEL:
            self.media_packets += 1
            self.media_bytes += len(body)
            self.last_media_at = time.time()
            for au in self.assembler.feed(idx, body):
                self._deliver_au(au)
        elif channel == proto.CMD_CHANNEL:
            self.cmd_packets += 1
            self._feed_cmd(idx, body)
        elif channel == proto.DOWNLOAD_CHANNEL:
            self.download_packets += 1
            self._feed_download_packet(idx, body)

    def _deliver_au(self, au: frames.AccessUnit) -> None:
        now = time.time()
        self.last_video_at = now
        if au.kind == "video":
            self.frames_seen += 1
            if self.first_video_at is None:
                self.first_video_at = now
            self._first_frame.set()
        if self.on_au is not None:
            try:
                self.on_au(au)
            except Exception:
                log.exception("consumer rejected AU")
    def _feed_download_packet(self, idx: int, body: bytes) -> None:
        if self._dl_next is None:
            self._dl_next = idx
        if (idx - self._dl_next) & 0xFFFF < 0x8000:
            self._dl_buf[idx] = body
        self._drain_download()

    def _drain_download(self) -> None:
        if self._dl_next is None:
            return
        if self._dl_buf and self._dl_next not in self._dl_buf:
            now = time.time()
            if self._dl_gap_since is None:
                self._dl_gap_since = now
            elif now - self._dl_gap_since > proto.CMD_GAP_TIMEOUT:
                self._dl_next = min(self._dl_buf,
                                    key=lambda k: (k - self._dl_next) & 0xFFFF)
        while self._dl_next in self._dl_buf:
            self._dl_stream += self._dl_buf.pop(self._dl_next)
            self._dl_next = (self._dl_next + 1) & 0xFFFF
            self._dl_gap_since = None
        for hdr, data in proto.cut_download_frames(self._dl_stream):
            self.download_frames += 1
            if self.on_download is not None:
                try:
                    self.on_download(hdr, data)
                except Exception:
                    log.exception("download receiver")
    def _feed_cmd(self, idx: int, body: bytes) -> None:
        if self._cmd_next is None:
            self._cmd_next = idx
        if (idx - self._cmd_next) & 0xFFFF < 0x8000:
            self._cmd_buf[idx] = body
        self._drain_cmd()

    def _drain_cmd(self) -> None:
        if self._cmd_next is None:
            return
        if self._cmd_buf and self._cmd_next not in self._cmd_buf:
            now = time.time()
            if self._cmd_gap_since is None:
                self._cmd_gap_since = now
            elif now - self._cmd_gap_since > proto.CMD_GAP_TIMEOUT:
                self._cmd_next = min(self._cmd_buf,
                                     key=lambda k: (k - self._cmd_next) & 0xFFFF)
        assert self._cmd_next is not None
        while self._cmd_next in self._cmd_buf:
            self._cmd_stream += self._cmd_buf.pop(self._cmd_next)
            self._cmd_next = (self._cmd_next + 1) & 0xFFFF
            self._cmd_gap_since = None
        for msg in proto.json_messages(proto.cut_lu_frames(self._cmd_stream)):
            self.bus.dispatch(msg)

    # ------------------------------------------------------------------ ack
    def _flush_acks(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._ack_last < proto.ACK_INTERVAL:
            return
        sent_any = False
        for channel, nums in self._ack.items():
            if not nums:
                continue
            batch = nums[:proto.ACK_MAX_NUMS]
            del nums[:proto.ACK_MAX_NUMS]
            for pkt in proto.ack_packets(batch, channel):
                if self.addr:
                    self.endpoint.send(self.addr, pkt)
                    self.acks_sent += 1
                    self.acked += len(batch)
                    sent_any = True
        if sent_any:
            self._ack_last = now

    async def _ticker(self) -> None:
        """Periodic chores: ack, command channel gaps, frame assembly timeout."""
        try:
            while not self._stop.is_set():
                self.assembler.tick()
                self._drain_cmd()
                self._drain_download()
                self._flush_acks()
                try:
                    await asyncio.wait_for(self._stop.wait(), 0.05)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------ commands
    async def send_lu(self, obj: dict, msg_id: int) -> None:
        """Send an Lu command 10 times with one msg_id and one timestamp (11 ms)."""
        if not self.addr:
            raise commands.NotConnected("нет адреса камеры")
        pkt = proto.data_packet(obj, msg_id=msg_id)
        for _ in range(proto.CMD_REPEATS):
            if self.closing:
                raise commands.NotConnected("сессия закрывается")
            self.endpoint.send(self.addr, pkt)
            await asyncio.sleep(proto.CMD_REPEAT_INTERVAL)

    # ------------------------------------------------------------------ video
    async def open_video(self, state: int, stream: int) -> None:
        """OpenVideo — one-way; wait for the first frame up to 30 s (IDR arrives in ~3.5 s)."""
        self.assembler.reset(keep_next=True)
        self._first_frame.clear()
        self._stream_gen += 1
        await self.bus.send({
            "cmd": "OpenVideo", "pwd": self.password, "state": int(state),
            "stream": int(stream), "userid": random.randint(1, 100),
        })

    async def wait_first_frame(self, timeout: float = FIRST_FRAME_TIMEOUT) -> bool:
        try:
            await asyncio.wait_for(self._first_frame.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def close_video(self) -> None:
        """OpenVideo{state:0, stream:0}: stop the stream, keep the session."""
        self._first_frame.clear()
        self.assembler.reset(keep_next=True)
        try:
            await self.bus.send({"cmd": "OpenVideo", "pwd": self.password,
                                 "state": 0, "stream": 0,
                                 "userid": random.randint(1, 100)})
        except commands.CommandError:
            pass

    # ------------------------------------------------------------------ shutdown
    def request_close(self, reason: str = "") -> None:
        if not self.closing:
            self.last_error = reason or self.last_error
            self.closing = True
            self._closed_event.set()

    def on_remote_close(self) -> None:
        # The camera answers our own Close with Close. That is a normal
        # acknowledgement, not a busy/error condition; only warn when the
        # camera closes an otherwise healthy session on its own.
        if self.closing:
            log.debug("camera %s acknowledged Close", self.camera_id or self.did)
            return
        log.warning("camera %s closed the session (busy/error)",
                    self.camera_id or self.did)
        self.last_error = "камера закрыла сессию"
        self.request_close(self.last_error)

    async def close(self, send_close: bool = True) -> None:
        """Close the session: Close twice (otherwise the camera stays busy)."""
        # Set the flag before sending: the camera may reply with Close while
        # the first packet is still in flight, and that reply must not be
        # reported as an unexpected remote close.
        self.closing = True
        self.connected = False
        if send_close and self.addr:
            for _ in range(2):
                self.endpoint.send(self.addr, proto.close_packet())
                await asyncio.sleep(0.05)
        self._stop.set()
        self._closed_event.set()
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        self.bus.clear_waiters()
        self.endpoint.unregister(self)
        self.closed_at = time.time()
        if self.on_close is not None:
            try:
                self.on_close()
            except Exception:
                log.exception("session close handler")
    # ------------------------------------------------------------------ statistics
    def stats(self) -> dict:
        now = time.time()
        return {
            "did": self.real_did,
            "addr": self.addr_text,
            "punch_value": hex(self.punch_value),
            "connected": self.connected,
            "connected_at": self.connected_at,
            "uptime_s": round(now - self.connected_at, 1) if self.connected_at else 0,
            "last_media_age_ms": round((now - self.last_media_at) * 1000) if self.last_media_at else None,
            "last_video_age_ms": round((now - self.last_video_at) * 1000) if self.last_video_at else None,
            "media_packets": self.media_packets,
            "media_kb": round(self.media_bytes / 1024, 1),
            "cmd_packets": self.cmd_packets,
            "download_packets": self.download_packets,
            "download_frames": self.download_frames,
            "acks_sent": self.acks_sent,
            "acked": self.acked,
            "frames": self.frames_seen,
            "assembler": self.assembler.summary(),
            "commands": {"sent": self.bus.sent, "received": self.bus.received,
                         "timeouts": self.bus.timeouts, "msg_id": self.bus.msg_id},
            "last_error": self.last_error,
        }
