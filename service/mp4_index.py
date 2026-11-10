"""MP4 index for SD files: locate moov, video samples and keyframes.

The camera writes a regular MP4 with ``ftyp + mdat + moov``; ``moov`` is at the
end, so a playback range request can be prepared by fetching only the tail.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass


class Mp4Error(RuntimeError):
    pass


@dataclass(slots=True)
class VideoSample:
    offset: int
    size: int
    time_ms: float
    key: bool


@dataclass(slots=True)
class Mp4Index:
    file_size: int
    moov_offset: int
    moov_size: int
    timescale: int
    duration_ms: float
    samples: list[VideoSample]
    keyframes: list[VideoSample]

    def keyframe_for(self, pos_ms: float) -> VideoSample:
        """Last keyframe not after ``pos_ms`` (or the first one)."""
        if not self.keyframes:
            raise Mp4Error("no keyframes")
        before = [k for k in self.keyframes if k.time_ms <= max(0.0, pos_ms)]
        return before[-1] if before else self.keyframes[0]


def _boxes(data: bytes, start: int = 0, end: int | None = None):
    end = len(data) if end is None else end
    pos = start
    while pos + 8 <= end:
        size = struct.unpack_from(">I", data, pos)[0]
        kind = data[pos + 4:pos + 8].decode("latin1")
        header = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            return
        yield kind, pos, pos + header, pos + size
        pos += size


def _find(data: bytes, path: list[str], start: int = 0,
          end: int | None = None) -> tuple[int, int] | None:
    end = len(data) if end is None else end
    for kind, _pos, body, box_end in _boxes(data, start, end):
        if kind != path[0]:
            continue
        if len(path) == 1:
            return body, box_end
        found = _find(data, path[1:], body, box_end)
        if found:
            return found
    return None


def _moov_from_tail(tail: bytes, tail_start: int) -> tuple[int, int]:
    """Return (body_start, end) in *tail* coordinates for the last moov."""
    idx = tail.rfind(b"moov")
    if idx < 4:
        raise Mp4Error("moov not found in tail")
    start = idx - 4
    size = struct.unpack_from(">I", tail, start)[0]
    header = 8
    if size == 1:
        if start + 16 > len(tail):
            raise Mp4Error("truncated moov")
        size = struct.unpack_from(">Q", tail, start + 8)[0]
        header = 16
    if size < header or start + size > len(tail):
        raise Mp4Error("truncated moov")
    return start + header, start + size


def parse_moov(tail: bytes, tail_start: int, file_size: int) -> Mp4Index:
    """Parse a moov from a tail buffer.

    ``tail_start`` is the absolute file offset of ``tail[0]``. The parser only
    needs the moov box; sample offsets in stco/co64 are already absolute.
    """
    moov_body, moov_end = _moov_from_tail(tail, tail_start)
    # locate the box start from its body/end
    signature = tail.rfind(b"moov", 0, moov_end)
    box_start = signature - 4
    moov_size = moov_end - box_start
    moov_start = tail_start + box_start

    for kind, _pos, body, end in _boxes(tail, moov_body, moov_end):
        if kind != "trak":
            continue
        hdlr = _find(tail, ["mdia", "hdlr"], body, end)
        if not hdlr or tail[hdlr[0] + 8:hdlr[0] + 12] != b"vide":
            continue
        mdhd = _find(tail, ["mdia", "mdhd"], body, end)
        stbl = _find(tail, ["mdia", "minf", "stbl"], body, end)
        if not mdhd or not stbl:
            continue
        mb, _ = mdhd
        version = tail[mb]
        if version == 1:
            timescale = struct.unpack_from(">I", tail, mb + 20)[0]
            duration = struct.unpack_from(">Q", tail, mb + 24)[0]
        else:
            timescale = struct.unpack_from(">I", tail, mb + 12)[0]
            duration = struct.unpack_from(">I", tail, mb + 16)[0]
        if not timescale:
            continue
        sb, se = stbl
        stsz = _find(tail, ["stsz"], sb, se)
        stco = _find(tail, ["stco"], sb, se)
        co64 = _find(tail, ["co64"], sb, se)
        stsc = _find(tail, ["stsc"], sb, se)
        stts = _find(tail, ["stts"], sb, se)
        stss = _find(tail, ["stss"], sb, se)
        if not (stsz and stsc and stts and (stco or co64)):
            continue
        zb, _ = stsz
        sample_size = struct.unpack_from(">I", tail, zb + 4)[0]
        count = struct.unpack_from(">I", tail, zb + 8)[0]
        sizes = [sample_size] * count if sample_size else [
            struct.unpack_from(">I", tail, zb + 12 + 4 * i)[0]
            for i in range(count)]
        if stco:
            cb, _ = stco
            n = struct.unpack_from(">I", tail, cb + 4)[0]
            chunks = [struct.unpack_from(">I", tail, cb + 8 + 4 * i)[0]
                      for i in range(n)]
        else:
            cb, _ = co64
            n = struct.unpack_from(">I", tail, cb + 4)[0]
            chunks = [struct.unpack_from(">Q", tail, cb + 8 + 8 * i)[0]
                      for i in range(n)]
        scb, _ = stsc
        n = struct.unpack_from(">I", tail, scb + 4)[0]
        entries = [struct.unpack_from(">III", tail, scb + 8 + 12 * i)
                   for i in range(n)]
        sample_offsets: list[int] = []
        si = 0
        for i, (first, per, _desc) in enumerate(entries):
            last = entries[i + 1][0] - 1 if i + 1 < n else len(chunks)
            for chunk in range(first, last + 1):
                off = chunks[chunk - 1]
                for _ in range(per):
                    if si >= count:
                        break
                    sample_offsets.append(off)
                    off += sizes[si]
                    si += 1
        tb, _ = stts
        n = struct.unpack_from(">I", tail, tb + 4)[0]
        times: list[int] = []
        t = 0
        for i in range(n):
            cnt, delta = struct.unpack_from(">II", tail, tb + 8 + 8 * i)
            for _ in range(cnt):
                times.append(t)
                t += delta
        keys = set(range(count))
        if stss:
            kb, _ = stss
            n = struct.unpack_from(">I", tail, kb + 4)[0]
            keys = {struct.unpack_from(">I", tail, kb + 8 + 4 * i)[0] - 1
                    for i in range(n)}
        samples = [
            VideoSample(offset=sample_offsets[i], size=sizes[i],
                        time_ms=times[i] * 1000 / timescale, key=i in keys)
            for i in range(min(count, len(sample_offsets), len(times)))
        ]
        keys_list = [s for s in samples if s.key]
        return Mp4Index(
            file_size=file_size,
            moov_offset=moov_start,
            moov_size=moov_size,
            timescale=timescale,
            duration_ms=duration * 1000 / timescale,
            samples=samples,
            keyframes=keys_list,
        )
    raise Mp4Error("no video track in moov")
