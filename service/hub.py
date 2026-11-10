"""Stream hub: SPS patching, GOP cache and fan-out to many consumers.

The camera produces one stream, while several browsers, RTSP clients and a recorder
may want to watch it at the same time. The hub is the only place where the stream turns
into access units that are "identical for everyone":

* the SPS is patched **once** (`sps.patch_au`), otherwise WebCodecs holds frames back;
* the most recent IDR and the following P-frames sit in the cache — a new viewer starts
  instantly, without waiting 3.5 s for a key frame;
* each subscriber has its own bounded queue and overflow policy:
  above 15 frames non-key frames are dropped and "waiting for IDR" is set,
  above 30 the queue is cleared (as in the app, spec §4.4).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass

from . import frames, proto

try:
    from ..vendor.lookcam import sps as sps_mod
except ImportError:  # `python -m service` from the package directory
    from vendor.lookcam import sps as sps_mod
except Exception:  # pragma: no cover
    sps_mod = None

log = logging.getLogger("lookcam.hub")

#: How many bytes/frames of the GOP to keep for an instant start of new viewers.
GOP_MAX_BYTES = 4 << 20
GOP_MAX_FRAMES = 400
#: "Catch up / reset" threshold for the subscriber queue (in frames).
QUEUE_SOFT = 15
QUEUE_HARD = 30
#: Subscriber queue capacity (with headroom above QUEUE_HARD).
QUEUE_MAX = 64


@dataclass(frozen=True)
class StreamConfig:
    """Codec parameters shared by all consumers (after the SPS patch)."""

    codec: str = ""
    sps: bytes = b""
    pps: bytes = b""
    width: int = 0
    height: int = 0
    profile_level: str = ""
    version: int = 0
    sps_patched: bool = False

    @property
    def ready(self) -> bool:
        return bool(self.sps and self.pps and self.codec)

    def to_dict(self) -> dict:
        return {"codec": self.codec, "width": self.width, "height": self.height,
                "profile_level": self.profile_level, "version": self.version,
                "sps": self.sps.hex(), "pps": self.pps.hex(),
                "sps_patched": self.sps_patched}


@dataclass
class ConfigEvent:
    config: StreamConfig | None


@dataclass
class ResetEvent:
    reason: str = ""


class Subscription:
    """Consumer subscription: config events + access units."""

    __slots__ = ("queue", "wait_key", "dropped", "created", "name", "closed")

    def __init__(self, name: str = "viewer") -> None:
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX)
        self.wait_key = False
        self.dropped = 0
        self.created = time.time()
        self.name = name
        self.closed = False

    async def get(self):
        """Next event (`ConfigEvent`/`ResetEvent`/`AccessUnit`) or None."""
        if self.closed:
            return None
        return await self.queue.get()

    def get_nowait(self):
        try:
            return self.queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    def close(self) -> None:
        self.closed = True
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            self.queue.put_nowait(ResetEvent("подписка закрыта"))
        except asyncio.QueueFull:
            pass


class StreamHub:
    """SPS patch + GOP cache + subscribers. All methods are called on the asyncio loop."""

    def __init__(self, camera_id: str = "") -> None:
        self.camera_id = camera_id
        self.config: StreamConfig | None = None
        self._gop: deque[frames.AccessUnit] = deque()
        self._gop_bytes = 0
        self._subs: set[Subscription] = set()
        self._version = 0
        self.frames = 0
        self.bytes = 0
        self.dropped_no_config = 0
        self.sps_patched = 0
        self.last_frame_at = 0.0
        self.last_key_at = 0.0
        self._fps_window: deque[float] = deque(maxlen=60)
        self._bytes_window: deque[tuple[float, int]] = deque()

    # ------------------------------------------------------------------ subscription
    def subscribe(self, name: str = "viewer", gop: bool = True) -> Subscription:
        sub = Subscription(name)
        cfg = self.config
        if cfg is not None and cfg.ready:
            sub.queue.put_nowait(ConfigEvent(cfg))
        if gop:
            for au in self._gop:
                if sub.queue.full():
                    break
                sub.queue.put_nowait(au)
        else:
            # Low-latency RTSP: do not replay an already-captured GOP. Wait for
            # the next IDR and then send only live frames; this avoids a burst
            # of old frames that a zero-buffer client cannot absorb.
            sub.wait_key = True
        self._subs.add(sub)
        log.debug("%s: subscriber %s, queued %d, gop=%s", self.camera_id, name,
                  sub.queue.qsize(), gop)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        sub.close()
        self._subs.discard(sub)

    def drop_all(self, reason: str = "") -> None:
        """Broadcast a reset and detach everyone (quality change/camera restart)."""
        for sub in list(self._subs):
            self._push_event(sub, ResetEvent(reason))
        self._gop.clear()
        self._gop_bytes = 0
        self.config = None

    # ------------------------------------------------------------------ publishing
    def publish(self, au: frames.AccessUnit) -> None:
        """Accept an AU from the session: fix the SPS, update config/GOP, fan out."""
        if au.kind != "video":
            self._publish_audio(au)
            return
        cfg_changed = False
        payload = au.payload
        if sps_mod is not None and (proto.SPS_NAL in payload or proto.SPS_NAL_ALT in payload):
            fixed = sps_mod.patch_au(payload)
            if fixed != payload:
                self.sps_patched += 1
                payload = fixed
                au.payload = fixed
        sps_nal, pps_nal, codec = proto.find_parameter_sets(payload)
        if sps_nal and pps_nal:
            cfg_changed = self._update_config(sps_nal, pps_nal, codec, au)
        if self.config is None:
            self.dropped_no_config += 1
            return
        if au.width or au.height:
            pass
        elif self.config.width:
            au.width, au.height = self.config.width, self.config.height
        if au.key:
            self._gop.clear()
            self._gop_bytes = 0
            self.last_key_at = time.time()
            self._gop.append(au)
            self._gop_bytes = len(payload)
        elif self._gop:
            self._gop.append(au)
            self._gop_bytes += len(payload)
            while (self._gop and (self._gop_bytes > GOP_MAX_BYTES
                                  or len(self._gop) > GOP_MAX_FRAMES)):
                old = self._gop.popleft()
                self._gop_bytes -= len(old.payload)
        self.frames += 1
        self.bytes += len(payload)
        self.last_frame_at = time.time()
        self._fps_window.append(self.last_frame_at)
        self._bytes_window.append((self.last_frame_at, len(payload)))
        while self._bytes_window and self.last_frame_at - self._bytes_window[0][0] > 5.0:
            self._bytes_window.popleft()
        if cfg_changed:
            cfg = self.config
            for sub in list(self._subs):
                self._push_event(sub, ConfigEvent(cfg))
        for sub in list(self._subs):
            self._offer(sub, au)

    def _publish_audio(self, au: frames.AccessUnit) -> None:
        for sub in list(self._subs):
            self._offer(sub, au)

    def _update_config(self, sps_nal: bytes, pps_nal: bytes, codec: str,
                       au: frames.AccessUnit) -> bool:
        old = self.config
        if (old is not None and old.sps == sps_nal and old.pps == pps_nal
                and old.codec == codec):
            return False
        width, height = au.width, au.height
        profile = proto.profile_level_id(sps_nal)
        if (not width or not height) and sps_mod is not None:
            try:
                info = sps_mod.describe(sps_nal)
                width, height = int(info.get("width") or 0), int(info.get("height") or 0)
            except Exception:
                pass
        self._version += 1
        cfg = StreamConfig(codec=codec or "avc1.4D0028", sps=sps_nal, pps=pps_nal,
                           width=width, height=height, profile_level=profile,
                           version=self._version, sps_patched=True)
        self.config = cfg
        log.info("%s: config %s %sx%s (SPS %d bytes)", self.camera_id, cfg.codec,
                 cfg.width, cfg.height, len(sps_nal))
        return True

    def _push_event(self, sub: Subscription, event) -> None:
        try:
            sub.queue.put_nowait(event)
        except asyncio.QueueFull:
            # an event matters more than frames: clear the queue and put it in
            while not sub.queue.empty():
                try:
                    sub.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def _offer(self, sub: Subscription, au: frames.AccessUnit) -> None:
        if sub.closed:
            return
        qsize = sub.queue.qsize()
        if qsize >= QUEUE_HARD:
            while not sub.queue.empty():
                try:
                    sub.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            sub.wait_key = True
            sub.dropped += 1
            return
        if qsize >= QUEUE_SOFT and not au.key:
            sub.wait_key = True
            sub.dropped += 1
            return
        if sub.wait_key:
            if not au.key:
                sub.dropped += 1
                return
            sub.wait_key = False
        try:
            sub.queue.put_nowait(au)
        except asyncio.QueueFull:
            sub.dropped += 1

    # ------------------------------------------------------------------ queries
    def gop(self) -> list[frames.AccessUnit]:
        return list(self._gop)

    def latest_key(self) -> frames.AccessUnit | None:
        for au in reversed(self._gop):
            if au.key:
                return au
        return None

    def fps(self) -> float:
        now = time.time()
        recent = [t for t in self._fps_window if now - t <= 3.0]
        if len(recent) < 2 or recent[-1] <= recent[0]:
            return 0.0
        return round((len(recent) - 1) / (recent[-1] - recent[0]), 1)

    def bitrate_kbps(self, window: float = 3.0) -> float:
        if not self._bytes_window:
            return 0.0
        now = time.time()
        total = sum(size for ts, size in self._bytes_window if now - ts <= window)
        return round(total * 8 / 1000 / max(window, 0.1), 1)

    def age_ms(self) -> int | None:
        if not self.last_frame_at:
            return None
        return int((time.time() - self.last_frame_at) * 1000)

    def stats(self) -> dict:
        return {
            "camera_id": self.camera_id,
            "frames": self.frames,
            "bytes": self.bytes,
            "subscribers": len(self._subs),
            "gop_frames": len(self._gop),
            "gop_kb": round(self._gop_bytes / 1024, 1),
            "config": self.config.to_dict() if self.config else None,
            "fps": self.fps(),
            "age_ms": self.age_ms(),
            "sps_patched": self.sps_patched,
            "dropped_no_config": self.dropped_no_config,
            "last_key_age_ms": (int((time.time() - self.last_key_at) * 1000)
                                if self.last_key_at else None),
            "subscriber_queues": [{"name": s.name, "qsize": s.queue.qsize(),
                                   "dropped": s.dropped, "wait_key": s.wait_key}
                                  for s in self._subs],
        }
