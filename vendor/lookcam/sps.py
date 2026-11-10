"""Patch the SPS on the fly: tell the decoder that there is no reordering.

Why. The camera sends an SPS with `bitstream_restriction_flag = 0`, i.e.
`max_num_reorder_frames` is not declared. By the standard (H.264, E.2.1) in this case
the value equals the DPB size for the level, and for level 4.0 at 1920x1088 it is
4 frames. The decoder is entitled to hold them back before output — at 10 fps that is ~400 ms
of pure delay. The stream is strictly IPPP (`max_num_ref_frames = 1`,
no B-frames), so there is nothing to hold back.

`optimizeForLatency` in WebCodecs is only a hint; the hardware path does not always
honor it. The hold-back can be reliably removed by declaring the restriction explicitly:
we append `bitstream_restriction_flag = 1` with `max_num_reorder_frames = 0` to the VUI.

This task cannot be solved through `flush()`: according to the WebCodecs specification it
sets "a key frame is required", and all delta frames until the next IDR are
rejected by the decoder.

The parsing was verified on this camera's SPS (`27 4d 00 28 ...`, profile 77, level 40).
"""
from __future__ import annotations

START_CODE = b"\x00\x00\x00\x01"

# Profiles whose SPS has chroma_format_idc and other High-branch fields.
HIGH_PROFILES = frozenset({100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135})


class _Reader:
    """Reading an RBSP bitstream (exp-Golomb and fixed length)."""

    def __init__(self, data: bytes):
        self.d = data
        self.p = 0

    def u(self, n: int) -> int:
        v = 0
        for _ in range(n):
            if self.p >= len(self.d) * 8:
                raise ValueError("SPS кончился раньше времени")
            byte = self.d[self.p >> 3]
            v = (v << 1) | ((byte >> (7 - (self.p & 7))) & 1)
            self.p += 1
        return v

    def ue(self) -> int:
        z = 0
        while self.u(1) == 0:
            z += 1
            if z > 32:
                raise ValueError("битый exp-golomb")
        return (1 << z) - 1 + (self.u(z) if z else 0)

    def se(self) -> int:
        k = self.ue()
        return (k + 1) // 2 if k % 2 else -(k // 2)


class _Writer:
    """Building a bitstream. The SPS is short, so we just keep a list of bits."""

    def __init__(self) -> None:
        self.bits: list[int] = []

    def u(self, value: int, n: int) -> None:
        for i in range(n - 1, -1, -1):
            self.bits.append((value >> i) & 1)

    def ue(self, value: int) -> None:
        code = value + 1
        n = code.bit_length()
        self.u(0, n - 1)
        self.u(code, n)

    def copy(self, src: _Reader, n: int) -> None:
        for _ in range(n):
            self.bits.append(src.u(1))

    def finish(self) -> bytes:
        """Append rbsp_trailing_bits and align to a byte."""
        self.bits.append(1)
        while len(self.bits) % 8:
            self.bits.append(0)
        out = bytearray()
        for i in range(0, len(self.bits), 8):
            byte = 0
            for bit in self.bits[i:i + 8]:
                byte = (byte << 1) | bit
            out.append(byte)
        return bytes(out)


def unescape(d: bytes) -> bytes:
    """Remove start-code emulation prevention bytes (00 00 03 -> 00 00)."""
    out = bytearray()
    i = 0
    while i < len(d):
        if i + 2 < len(d) and d[i] == 0 and d[i + 1] == 0 and d[i + 2] == 3:
            out += b"\x00\x00"
            i += 3
        else:
            out.append(d[i])
            i += 1
    return bytes(out)


def escape(rbsp: bytes) -> bytes:
    """Insert start-code emulation prevention bytes."""
    out = bytearray()
    zeros = 0
    for b in rbsp:
        if zeros >= 2 and b <= 3:
            out.append(3)
            zeros = 0
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return bytes(out)


def _skip_hrd(r: _Reader) -> None:
    cnt = r.ue() + 1
    r.u(4)
    r.u(4)
    for _ in range(cnt):
        r.ue()
        r.ue()
        r.u(1)
    r.u(5)
    r.u(5)
    r.u(5)
    r.u(5)


def _scan(rbsp: bytes) -> dict:
    """Walk the SPS up to bitstream_restriction_flag and remember its bit position."""
    r = _Reader(rbsp)
    info: dict = {}
    info["profile_idc"] = r.u(8)
    r.u(8)                                   # constraint_set_flags + reserved
    info["level_idc"] = r.u(8)
    r.ue()                                   # seq_parameter_set_id
    if info["profile_idc"] in HIGH_PROFILES:
        if r.ue() == 3:                      # chroma_format_idc
            r.u(1)
        r.ue()
        r.ue()
        r.u(1)
        if r.u(1):
            raise ValueError("scaling matrix в SPS не поддержан")
    r.ue()                                   # log2_max_frame_num_minus4
    poc = r.ue()
    if poc == 0:
        r.ue()
    elif poc == 1:
        r.u(1)
        r.se()
        r.se()
        for _ in range(r.ue()):
            r.se()
    info["max_num_ref_frames"] = r.ue()
    r.u(1)                                   # gaps_in_frame_num_value_allowed
    info["width"] = (r.ue() + 1) * 16
    h = r.ue() + 1
    frame_mbs_only = r.u(1)
    if not frame_mbs_only:
        r.u(1)
    info["height"] = h * 16 * (2 - frame_mbs_only)
    r.u(1)                                   # direct_8x8_inference
    if r.u(1):                               # frame_cropping
        r.ue(); r.ue(); r.ue(); r.ue()
    info["vui"] = bool(r.u(1))
    if not info["vui"]:
        return info

    if r.u(1):                               # aspect_ratio_info_present
        if r.u(8) == 255:
            r.u(16)
            r.u(16)
    if r.u(1):                               # overscan_info_present
        r.u(1)
    if r.u(1):                               # video_signal_type_present
        r.u(3)
        r.u(1)
        if r.u(1):
            r.u(8); r.u(8); r.u(8)
    if r.u(1):                               # chroma_loc_info_present
        r.ue()
        r.ue()
    if r.u(1):                               # timing_info_present
        num = r.u(32)
        scale = r.u(32)
        r.u(1)                               # fixed_frame_rate_flag
        info["fps"] = round(scale / (2 * num), 2) if num else None
    nal_hrd = r.u(1)
    if nal_hrd:
        _skip_hrd(r)
    vcl_hrd = r.u(1)
    if vcl_hrd:
        _skip_hrd(r)
    if nal_hrd or vcl_hrd:
        r.u(1)                               # low_delay_hrd_flag
    r.u(1)                                   # pic_struct_present_flag

    info["restriction_pos"] = r.p            # bit where bitstream_restriction_flag is located
    info["bitstream_restriction"] = bool(r.u(1))
    if info["bitstream_restriction"]:
        r.u(1)
        r.ue(); r.ue(); r.ue(); r.ue()
        info["max_num_reorder_frames"] = r.ue()
        info["max_dec_frame_buffering"] = r.ue()
    return info


def describe(nal: bytes) -> dict:
    """Parse an SPS (without the start code, the first byte is the NAL header)."""
    return _scan(unescape(nal[1:]))


def patch(nal: bytes) -> bytes:
    """SPS with max_num_reorder_frames = 0 declared.

    Returns the original NAL unchanged if there is nothing to patch or it is impossible:
    the VUI is absent (it would have to be synthesized entirely) or the restriction
    is already declared.
    """
    head = nal[:1]
    rbsp = unescape(nal[1:])
    try:
        info = _scan(rbsp)
    except ValueError:
        return nal
    if not info.get("vui") or "restriction_pos" not in info:
        return nal
    if info.get("bitstream_restriction"):
        return nal

    w = _Writer()
    w.copy(_Reader(rbsp), info["restriction_pos"])
    w.u(1, 1)                                # bitstream_restriction_flag
    w.u(1, 1)                                # motion_vectors_over_pic_boundaries
    w.ue(0)                                  # max_bytes_per_pic_denom
    w.ue(0)                                  # max_bits_per_mb_denom
    w.ue(16)                                 # log2_max_mv_length_horizontal
    w.ue(16)                                 # log2_max_mv_length_vertical
    w.ue(0)                                  # max_num_reorder_frames — this is what it is all for
    # max_dec_frame_buffering cannot be less than the number of reference frames
    w.ue(max(1, info.get("max_num_ref_frames", 1)))
    return head + escape(w.finish())


def find_sps(au: bytes) -> tuple[int, int] | None:
    """Bounds of the SPS NAL inside an access unit: (start after the start code, end)."""
    pos = au.find(START_CODE)
    while pos >= 0:
        start = pos + len(START_CODE)
        if start < len(au) and (au[start] & 0x1F) == 7:
            nxt = au.find(START_CODE, start)
            return start, (nxt if nxt > 0 else len(au))
        pos = au.find(START_CODE, start)
    return None


def patch_au(au: bytes) -> bytes:
    """Replace the SPS inside the access unit, leave the rest untouched."""
    found = find_sps(au)
    if not found:
        return au
    start, end = found
    fixed = patch(au[start:end])
    if fixed == au[start:end]:
        return au
    return au[:start] + fixed + au[end:]
