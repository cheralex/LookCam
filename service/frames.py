"""Assemble channel 0 media frames from DRW fragments.

The camera delivers channel 0 as fragments of ~1024 bytes with continuous numbering.
A frame starts with a fragment carrying the magic ``01 AF AF AF`` (video) or
``02 AF AF AF`` (audio), and its length is declared in the header: video —
``u32LE@79 + 87``, audio — ``u32LE@74 + 82``. Waiting for the next frame just to find
the boundary is not allowed: it costs one inter-frame interval of latency.

When a fragment is lost the assembler does not write "through the hole": it discards the
incomplete frame and waits for a key frame (IDR), otherwise the decoder errors until GOP end.

Unlike the old `webcodecs.FrameWriter`, `AccessUnit.payload` does not contain the
4-byte container trailer (`F4 F3 F2 F1`): on the RTSP/fMP4 output it would end up in
the last NAL. The frame boundaries are known from the header anyway.
"""
from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field

from . import proto

try:  # SPS parser is vendored into lookcam_deepsik/vendor, the external lookcam is not needed
    from ..vendor.lookcam import sps as sps_mod
except ImportError:  # `python -m service` from the package directory
    from vendor.lookcam import sps as sps_mod
except Exception:  # pragma: no cover — the package is mandatory in the repository
    sps_mod = None


MAX_AU = 8 << 20
GAP_TIMEOUT = 0.5


@dataclass
class AccessUnit:
    """A finished frame for all consumers: browser, RTSP, MP4, recording."""

    kind: str = "video"                 # video | audio
    payload: bytes = b""                # Annex B for video, codec data for audio
    ts_ms: int = 0                      # camera clock (u32LE@46)
    frame_no: int = 0
    key: bool = False
    codec: str = "h264"                 # h264 | h265 (audio — aac/pcm/g711a)
    width: int = 0                      # from SPS, not from the unreliable header
    height: int = 0
    duration_ms: int = 0
    audio_type: int = 0
    arrival: float = field(default_factory=time.time)

    @property
    def annexb(self) -> bytes:
        return self.payload if self.kind == "video" else b""

    @property
    def is_audio(self) -> bool:
        return self.kind == "audio"


class FrameAssembler:
    """Channel 0 fragments → `AccessUnit`; deduplication, ordering, resynchronization."""

    def __init__(self, gap_timeout: float = GAP_TIMEOUT) -> None:
        self.gap_timeout = gap_timeout
        self.fragments = 0
        self.bytes = 0
        self.frames = 0
        self.audio_frames = 0
        self.dropped = 0
        self.gaps = 0
        self.resyncs = 0
        self.header_lens: set[int] = set()
        self._next: int | None = None
        self._pending: dict[int, bytes] = {}
        self._last_progress = time.time()
        self._resync = False
        self._wait_key = True
        self._vid: bytearray | None = None
        self._vid_hdr: proto.VideoHeader | None = None
        self._vid_start = proto.VIDEO_HEADER
        self._vid_target: int | None = None
        self._aud: bytearray | None = None
        self._aud_hdr: proto.AudioHeader | None = None
        self._aud_target: int | None = None
        self._open_kind: str | None = None
        self._last_video_ts = 0
        self._sps_cache: dict[bytes, tuple[int, int]] = {}

    # ------------------------------------------------------------------ receive
    def feed(self, frag: int, data: bytes) -> list[AccessUnit]:
        """Add a fragment; return finished frames (usually 0 or 1)."""
        self.fragments += 1
        self.bytes += len(data)
        if self._next is None:
            self._next = frag
        # numbering went backwards — the camera restarted the stream
        if frag < self._next - 1000:
            self.gaps += max(0, len(self._pending))
            self._hard_reset()
            self._next = frag
        if frag < self._next:
            return []                     # duplicate/late fragment
        if frag > self._next:
            self._pending.setdefault(frag, data)
            return []
        out = self._consume(data)
        self._next += 1
        while self._next in self._pending:
            out += self._consume(self._pending.pop(self._next))
            self._next += 1
        self._last_progress = time.time()
        return out

    def tick(self) -> list[AccessUnit]:
        """Call periodically: closes the gap if a fragment never arrived."""
        if not self._pending:
            return []
        if time.time() - self._last_progress > self.gap_timeout:
            self.gaps += len(self._pending)
            self._pending.clear()
            self._resync = True
            self.resyncs += 1
            self._wait_key = True
            self.dropped += 1
            self._vid = None
            self._vid_target = None
            self._aud = None
            self._aud_target = None
        return []

    def reset(self, keep_next: bool = True) -> None:
        """Drop the incomplete data (quality change); the session numbering is kept."""
        if not keep_next:
            self._next = None
        self._vid = None
        self._vid_hdr = None
        self._vid_target = None
        self._aud = None
        self._aud_hdr = None
        self._aud_target = None
        self._open_kind = None
        if keep_next:
            self._wait_key = True

    def _hard_reset(self) -> None:
        self._pending.clear()
        self._vid = None
        self._vid_target = None
        self._aud = None
        self._aud_target = None
        self._resync = True
        self.resyncs += 1
        self._wait_key = True
        self.dropped += 1

    # ------------------------------------------------------------------ parsing
    def _consume(self, data: bytes) -> list[AccessUnit]:
        if data.startswith(proto.FRAME_MAGIC):
            return self._start_video(data)
        if data.startswith(proto.AUDIO_MAGIC):
            return self._start_audio(data)
        # continuation of the current frame
        if self._open_kind == "video" and self._vid is not None:
            self._vid += data
            return self._finish_video_if_complete()
        if self._open_kind == "audio" and self._aud is not None:
            self._aud += data
            return self._finish_audio_if_complete()
        return []

    def _start_video(self, data: bytes) -> list[AccessUnit]:
        out: list[AccessUnit] = []
        # a new video frame closes the previous incomplete one (broken — discard it)
        if self._vid is not None and self._vid_target is None:
            out += self._emit_video()     # no length: emit what we have
        elif self._vid is not None:
            self.dropped += 1
            self._vid = None
            self._vid_target = None
        hdr = proto.parse_video_header(data)
        if hdr is None:
            return out
        key = hdr.key or self._has_idr(data)
        if self._wait_key:
            if not key:
                self.dropped += 1
                self._open_kind = None
                return out
            self._wait_key = False
            self._resync = False
        pos = proto.annexb_offset(data)
        if pos < 0:
            pos = proto.VIDEO_HEADER
        self.header_lens.add(pos)
        self._vid = bytearray(data)
        self._vid_hdr = hdr
        self._vid_start = pos
        # length in the header: payload_len bytes of Annex B; allow headers of 43 and 83
        plen = hdr.payload_len
        target = None
        if 0 < plen <= MAX_AU:
            candidate = pos + plen
            full = hdr.full_len_hint + 49
            if abs(candidate + proto.TRAILER - full) <= 16 or hdr.full_len_hint == 0:
                target = candidate
        self._vid_target = target
        self._open_kind = "video"
        return out + self._finish_video_if_complete()

    def _start_audio(self, data: bytes) -> list[AccessUnit]:
        out: list[AccessUnit] = []
        if self._aud is not None:
            self.dropped += 1          # incomplete audio frame
        hdr = proto.parse_audio_header(data)
        if hdr is None:
            return out
        self._aud = bytearray(data)
        self._aud_hdr = hdr
        plen = hdr.payload_len
        self._aud_target = (proto.AUDIO_HEADER + plen
                            if 0 < plen <= MAX_AU else None)
        self._open_kind = "audio"
        return out + self._finish_audio_if_complete()

    def _finish_video_if_complete(self) -> list[AccessUnit]:
        if self._vid is None:
            return []
        if self._vid_target is not None and len(self._vid) >= self._vid_target:
            return self._emit_video()
        return []

    def _emit_video(self) -> list[AccessUnit]:
        if self._vid is None:
            return []
        blob = bytes(self._vid)
        start = self._vid_start
        end = self._vid_target if self._vid_target is not None else len(blob)
        end = min(end, len(blob))
        payload = blob[start:end]
        hdr = self._vid_hdr or proto.VideoHeader()
        self._vid = None
        self._vid_hdr = None
        self._vid_target = None
        self._open_kind = None
        if not payload:
            self.dropped += 1
            return []
        key = hdr.key or self._has_idr(payload)
        width, height = self._dimensions(payload) if key else (0, 0)
        au = AccessUnit(kind="video", payload=payload, ts_ms=hdr.ts_ms,
                        frame_no=hdr.frame_no, key=key, codec=hdr.codec,
                        width=width, height=height, duration_ms=hdr.duration,
                        arrival=time.time())
        self.frames += 1
        self._last_video_ts = hdr.ts_ms
        return [au]

    def _finish_audio_if_complete(self) -> list[AccessUnit]:
        if self._aud is None:
            return []
        if self._aud_target is not None and len(self._aud) < self._aud_target:
            return []
        blob = bytes(self._aud)
        hdr = self._aud_hdr or proto.AudioHeader()
        self._aud = None
        self._aud_hdr = None
        self._aud_target = None
        self._open_kind = None
        end = hdr.payload_len if 0 < hdr.payload_len <= len(blob) else len(blob)
        payload = blob[proto.AUDIO_HEADER:proto.AUDIO_HEADER + end]
        if not payload:
            return []
        self.audio_frames += 1
        return [AccessUnit(kind="audio", payload=payload, ts_ms=hdr.ts_ms,
                           frame_no=hdr.frame_no, key=True,
                           codec=self.audio_codec_name(hdr.audio_type),
                           audio_type=hdr.audio_type, arrival=time.time())]

    def flush(self) -> list[AccessUnit]:
        """Emit the incomplete video (used when the stream stops)."""
        return self._emit_video()

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _has_idr(data: bytes) -> bool:
        if proto.SPS_NAL in data or proto.SPS_NAL_ALT in data:
            return True
        for nal in proto.split_annexb(data):
            if nal and (nal[0] & 0x1F) in (5, 7):
                return True
        return False

    def _dimensions(self, payload: bytes) -> tuple[int, int]:
        sps_nal, _pps, _codec = proto.find_parameter_sets(payload)
        if not sps_nal or sps_mod is None:
            return 0, 0
        cached = self._sps_cache.get(sps_nal)
        if cached is not None:
            return cached
        try:
            info = sps_mod.describe(sps_nal)
            dims = (int(info.get("width") or 0), int(info.get("height") or 0))
        except Exception:
            dims = (0, 0)
        self._sps_cache[sps_nal] = dims
        return dims

    @staticmethod
    def audio_codec_name(audio_type: int) -> str:
        return {0: "g711a", 2: "aac", 3: "g711u"}.get(audio_type & 0xFF, "pcm")

    # ------------------------------------------------------------------ statistics
    def summary(self) -> dict:
        return {
            "fragments": self.fragments, "bytes": self.bytes,
            "frames": self.frames, "audio_frames": self.audio_frames,
            "dropped": self.dropped, "gaps": self.gaps, "resyncs": self.resyncs,
            "header_lens": sorted(self.header_lens),
        }


def parse_media_payload(payload: bytes) -> tuple[int, bytes] | None:
    """DRW packet payload of channel 0 → (fragment number, data)."""
    if len(payload) < 4 or payload[0] != 0xD1:
        return None
    channel = payload[1]
    if channel != proto.MEDIA_CHANNEL:
        return None
    return struct.unpack_from(">H", payload, 2)[0], payload[4:]
