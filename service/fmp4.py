"""Minimal fMP4 multiplexer: init segment + `moof`/`mdat` per frame.

Needed by two paths:

* the MSE player (`/ws/live-mp4`) when WebCodecs H.264 is unavailable;
* manual recording to MP4 without transcoding (`cache_dir/records`).

H.264 is stored in AVCC form: every NAL with a 4-byte length in `mdat`, the
parameters (avcC) in `stsd`. No decoding and no external libraries.

Boxes are built per ISO/IEC 14496-12; they are verified by parsing them back in
`tests/test_service.py` and open in VLC/ffmpeg.
"""
from __future__ import annotations

import struct
import time
from dataclasses import dataclass

from . import proto

TIMESCALE = 90000
MATRIX = struct.pack(">9i", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)
UNITY = 0x00010000


def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + 8) + kind + payload


def full_box(kind: bytes, version: int, flags: int, payload: bytes) -> bytes:
    return box(kind, bytes([version]) + flags.to_bytes(3, "big") + payload)


def _mvhd() -> bytes:
    payload = struct.pack(">IIII", 0, 0, TIMESCALE, 0)
    payload += struct.pack(">IHH", UNITY, 0x0100, 0)       # rate, volume, reserved
    payload += b"\x00" * 8
    payload += MATRIX + b"\x00" * 24 + struct.pack(">I", 2)
    return full_box(b"mvhd", 0, 0, payload)


def _tkhd(width: int, height: int, track_id: int = 1) -> bytes:
    payload = struct.pack(">IIIII", 0, 0, track_id, 0, 0)
    payload += b"\x00" * 8
    payload += struct.pack(">hhHH", 0, 0, 0, 0)            # layer, alt, volume, res
    payload += MATRIX
    payload += struct.pack(">II", int(width * UNITY), int(height * UNITY))
    return full_box(b"tkhd", 0, 0x000003, payload)


def _mdhd() -> bytes:
    payload = struct.pack(">IIIIHH", 0, 0, TIMESCALE, 0, 0x55C4, 0)
    return full_box(b"mdhd", 0, 0, payload)


def _hdlr() -> bytes:
    payload = struct.pack(">I", 0) + b"vide" + b"\x00" * 12 + b"LookCam\x00"
    return full_box(b"hdlr", 0, 0, payload)


def _vmhd() -> bytes:
    return full_box(b"vmhd", 0, 1, struct.pack(">HHHH", 0, 0, 0, 0))


def _dinf() -> bytes:
    url = full_box(b"url ", 0, 1, b"")
    dref = full_box(b"dref", 0, 0, struct.pack(">I", 1) + url)
    return box(b"dinf", dref)


def avcc(sps: bytes, pps: bytes) -> bytes:
    """AVCDecoderConfigurationRecord from SPS/PPS (H.264)."""
    if len(sps) < 4 or len(pps) < 1:
        raise ValueError("нужны SPS и PPS")
    profile = sps[1] if len(sps) > 3 else 0x4D
    compat = sps[2] if len(sps) > 3 else 0x00
    level = sps[3] if len(sps) > 3 else 0x28
    payload = bytes([1, profile & 0xFF, compat & 0xFF, level & 0xFF, 0xFF, 0xE1])
    payload += struct.pack(">H", len(sps)) + sps
    payload += b"\x01" + struct.pack(">H", len(pps)) + pps
    return box(b"avcC", payload)


def _avc1(sps: bytes, pps: bytes, width: int, height: int) -> bytes:
    compressor = b"LookCam".ljust(32, b"\x00")
    payload = b"\x00" * 6 + struct.pack(">H", 1)           # reserved, dref index
    payload += struct.pack(">HHIII", 0, 0, 0, 0, 0)
    payload += struct.pack(">HH", width, height)
    payload += struct.pack(">II", 0x00480000, 0x00480000)
    payload += b"\x00" * 4 + struct.pack(">H", 1) + compressor
    payload += struct.pack(">Hh", 0x0018, -1)
    return box(b"avc1", payload + avcc(sps, pps))


def _stbl(sps: bytes, pps: bytes, width: int, height: int) -> bytes:
    stsd = full_box(b"stsd", 0, 0,
                    struct.pack(">I", 1) + _avc1(sps, pps, width, height))
    stts = full_box(b"stts", 0, 0, struct.pack(">I", 0))
    stsc = full_box(b"stsc", 0, 0, struct.pack(">I", 0))
    stsz = full_box(b"stsz", 0, 0, struct.pack(">II", 0, 0))
    stco = full_box(b"stco", 0, 0, struct.pack(">I", 0))
    return box(b"stbl", stsd + stts + stsc + stsz + stco)


def _minf(sps: bytes, pps: bytes, width: int, height: int) -> bytes:
    return box(b"minf", _vmhd() + _dinf() + _stbl(sps, pps, width, height))


def _mdia(sps: bytes, pps: bytes, width: int, height: int) -> bytes:
    return box(b"mdia", _mdhd() + _hdlr() + _minf(sps, pps, width, height))


def init_segment(sps: bytes, pps: bytes, width: int = 0, height: int = 0,
                 fps: float = 10.0) -> bytes:
    """ftyp + moov: everything the MSE player needs before the first fragment."""
    if not width or not height:
        width, height = 1920, 1080
    ftyp = box(b"ftyp", b"isom" + struct.pack(">I", 0x200)
               + b"isomiso5iso6mp41avc1")
    trak = box(b"trak", _tkhd(width, height) + _mdia(sps, pps, width, height))
    trex = full_box(b"trex", 0, 0,
                    struct.pack(">IIIII", 1, 1, 0, 0, 0))
    mvex = box(b"mvex", trex)
    moov = box(b"moov", _mvhd() + trak + mvex)
    return ftyp + moov


# ---------------------------------------------------------------------- fragments

@dataclass
class FragmentState:
    sequence: int = 1
    base_time: int = 0


def avcc_sample(annexb: bytes) -> bytes:
    """Annex B → AVCC: length-prefixed NALs without start codes."""
    out = bytearray()
    for nal in proto.split_annexb(annexb):
        if not nal:
            continue
        out += struct.pack(">I", len(nal)) + nal
    return bytes(out)


def fragment(annexb: bytes, sequence: int, base_time: int, duration: int,
             key: bool, width: int = 0, height: int = 0,
             default_sample_flags: int | None = None) -> tuple[bytes, int]:
    """Build moof+mdat for a single frame. Returns (bytes, new base_time)."""
    sample = avcc_sample(annexb)
    if not sample:
        return b"", base_time
    if duration <= 0:
        duration = TIMESCALE // 10
    if default_sample_flags is None:
        flags = 0x02000000 if key else 0x01010000
    else:
        flags = default_sample_flags
    mfhd = full_box(b"mfhd", 0, 0, struct.pack(">I", sequence))
    tfhd = full_box(b"tfhd", 0, 0x020000, struct.pack(">I", 1))
    tfdt = full_box(b"tfdt", 1, 0, struct.pack(">Q", base_time))

    def build_trun(offset: int) -> bytes:
        payload = struct.pack(">I", 1)                       # sample_count
        payload += struct.pack(">i", offset)                  # data_offset
        payload += struct.pack(">II", duration, len(sample))
        payload += struct.pack(">I", flags)
        return full_box(b"trun", 0, 0x000701, payload)

    trun = build_trun(0)
    traf = box(b"traf", tfhd + tfdt + trun)
    moof = box(b"moof", mfhd + traf)
    offset = len(moof) + 8                                  # mdat header
    trun = build_trun(offset)
    traf = box(b"traf", tfhd + tfdt + trun)
    moof = box(b"moof", mfhd + traf)
    mdat = box(b"mdat", sample)
    return moof + mdat, base_time + duration


class FMP4Writer:
    """Streaming fMP4 writer: init once, then one fragment per frame."""

    def __init__(self, sps: bytes, pps: bytes, width: int = 0, height: int = 0,
                 fps: float = 10.0) -> None:
        self.init = init_segment(sps, pps, width, height, fps)
        self.sequence = 1
        self.base_time = 0
        self.fps = fps or 10.0
        self.bytes = len(self.init)
        self.samples = 0
        self.started_at = time.time()
        self.first_key: bytes | None = None

    def fragment(self, annexb: bytes, key: bool, duration_ms: int = 0) -> bytes:
        if key and self.first_key is None:
            self.first_key = annexb
        duration = int(duration_ms * 90) if duration_ms > 0 else \
            int(TIMESCALE / self.fps)
        data, self.base_time = fragment(annexb, self.sequence, self.base_time,
                                        duration, key)
        if data:
            self.sequence += 1
            self.samples += 1
            self.bytes += len(data)
        return data


# ---------------------------------------------------------------------- parsing

def iter_boxes(data: bytes, start: int = 0, end: int | None = None):
    """Iterator (type, data_start, end) over the top-level boxes."""
    end = len(data) if end is None else end
    pos = start
    while pos + 8 <= end:
        size = struct.unpack_from(">I", data, pos)[0]
        kind = data[pos + 4:pos + 8]
        header = 8
        if size == 1:
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            break
        yield kind, pos + header, pos + size
        pos += size


def find_box(data: bytes, kinds) -> tuple[int, int] | None:
    if isinstance(kinds, (bytes, str)):
        kinds = [kinds]
    wanted = {k.encode() if isinstance(k, str) else k for k in kinds}
    for kind, body, end in iter_boxes(data):
        if kind in wanted:
            return body, end
    return None


def parse_init(data: bytes) -> dict:
    """Extract codec, dimensions and SPS/PPS from the init segment (for tests/diagnostics)."""
    info: dict = {"sps": b"", "pps": b"", "width": 0, "height": 0,
                  "timescale": TIMESCALE}
    moov = find_box(data, b"moov")
    if not moov:
        return info
    start, end = moov

    def walk(pos: int, stop: int):
        for kind, body, box_end in iter_boxes(data, pos, stop):
            if kind == b"mdhd":
                info["timescale"] = struct.unpack_from(">I", data, body + 12)[0]
            elif kind == b"tkhd":
                w = struct.unpack_from(">I", data, box_end - 8)[0]
                h = struct.unpack_from(">I", data, box_end - 4)[0]
                info["width"], info["height"] = w >> 16, h >> 16
            if kind in (b"moov", b"trak", b"mdia", b"minf", b"stbl", b"mvex",
                        b"traf", b"moof"):
                walk(body, box_end)
            elif kind == b"stsd":
                walk(body + 8, box_end)          # version/flags + entry_count
            elif kind == b"avc1":
                walk(body + 78, box_end)         # VisualSampleEntry
            elif kind == b"avcC":
                if body + 7 <= box_end:
                    sps_len = struct.unpack_from(">H", data, body + 6)[0]
                    sps_start = body + 8
                    info["sps"] = data[sps_start:sps_start + sps_len]
                    pps_pos = sps_start + sps_len
                    if pps_pos + 3 <= box_end:
                        pps_len = struct.unpack_from(">H", data, pps_pos + 1)[0]
                        info["pps"] = data[pps_pos + 3:pps_pos + 3 + pps_len]

    walk(start, end)
    if not info["width"] or not info["height"]:
        info["width"], info["height"] = 1920, 1080
    return info


def parse_fragments(data: bytes) -> list[dict]:
    """Parse moof/mdat back: a list of {sequence, timestamp, key, sample}."""
    out: list[dict] = []
    pos = 0
    n = len(data)
    while pos + 8 <= n:
        kind = data[pos + 4:pos + 8]
        size = struct.unpack_from(">I", data, pos)[0]
        if size < 8 or pos + size > n:
            break
        if kind != b"moof":
            pos += size
            continue
        moof_start, moof_end = pos, pos + size
        seq = ts = 0
        key = True
        data_offset = None
        sample_size = 0
        for k, body, end in iter_boxes(data, moof_start + 8, moof_end):
            if k == b"mfhd":
                seq = struct.unpack_from(">I", data, body + 4)[0]
            elif k == b"traf":
                for k2, b2, e2 in iter_boxes(data, body, end):
                    if k2 == b"tfdt":
                        ts = struct.unpack_from(">Q", data, b2 + 4)[0]
                    elif k2 == b"trun":
                        data_offset = struct.unpack_from(">i", data, b2 + 8)[0]
                        sample_size = struct.unpack_from(">I", data, b2 + 16)[0]
                        flags = struct.unpack_from(">I", data, b2 + 20)[0]
                        key = not bool(flags & 0x00010000)
        if data_offset is not None and moof_start + data_offset + 8 <= n:
            # data_offset points to the first byte of the sample (mdat payload)
            mstart = moof_start + data_offset
            sample = data[mstart:mstart + sample_size]
            out.append({"sequence": seq, "timestamp": ts, "key": key,
                        "sample": sample})
        pos = moof_end
    return out


def avcc_to_annexb(sample: bytes) -> bytes:
    """AVCC → Annex B (to verify a recording by parsing it back)."""
    out = bytearray()
    pos = 0
    while pos + 4 <= len(sample):
        size = struct.unpack_from(">I", sample, pos)[0]
        pos += 4
        if size <= 0 or pos + size > len(sample):
            break
        out += proto.START_CODE + sample[pos:pos + size]
        pos += size
    return bytes(out)
