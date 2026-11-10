"""PPPP camera emulator for service tests.

One "camera" is one UDP socket on loopback (a free port is picked). It supports
exactly what the service needs: LanSearch/PunchPkt/P2PRdy, LoginDev/AppointDev,
multi-packet replies of channel 1, live video of channel 0, channel 3 download,
Alive and Close. Media and commands go over the same socket — like on a real camera.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import socket
import struct
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
PARENT = os.path.dirname(PKG)
if PARENT not in sys.path:
    sys.path.insert(0, PARENT)

from lookcam_deepsik.service import proto  # noqa: E402

# The real SPS of this camera (profile 77, level 40); PPS is from test_units.py.
CAM_SPS = bytes.fromhex(
    "27 4d 00 28 e7 40 3c 01 13 f2 e0 29 41 41 41 7c 00 00 03 00"
    "04 00 00 03 00 7a c9 00 06 1a 80 02 49 ff ff f0 28")
CAM_PPS = bytes.fromhex("28 ee 3c b0")
START = b"\x00\x00\x00\x01"


def make_media_frame(key: bool, frame_no: int, ts_ms: int,
                     width: int = 1920, height: int = 1080) -> bytes:
    """Video frame in the camera format: 83-byte header + Annex B + trailer."""
    if key:
        payload = (START + CAM_SPS + START + CAM_PPS + START + b"\x25"
                   + bytes((frame_no + i) & 0xFF for i in range(600)))
    else:
        payload = START + b"\x21" + bytes((frame_no * 7 + i) & 0xFF for i in range(180))
    total = proto.VIDEO_HEADER + len(payload) + 4
    header = bytearray(proto.VIDEO_HEADER)
    struct.pack_into("<I", header, 0, 0xAFAFAF01)
    header[4] = 123
    struct.pack_into("<I", header, 41, total - 49)
    struct.pack_into("<I", header, 46, ts_ms & 0xFFFFFFFF)
    struct.pack_into("<I", header, 58, frame_no & 0xFFFFFFFF)
    header[62] = 1 if key else 0
    bits = ((height & 0x1FFF) << 30) | ((width & 0x1FFF) << 17)
    struct.pack_into("<Q", header, 63, bits)
    struct.pack_into("<I", header, 79, len(payload))
    return bytes(header) + payload + b"\xf4\xf3\xf2\xf1"


def make_download_frame(userid: int, block_no: int, data: bytes) -> bytes:
    header = bytearray(proto.DOWNLOAD_HEADER)
    struct.pack_into("<I", header, 0, 0xAFAFAFA0)
    struct.pack_into("<I", header, 4, userid)
    struct.pack_into("<i", header, 8, block_no)
    header[12] = 0x08
    struct.pack_into("<I", header, 21, len(data))
    return bytes(header) + data + b"\xf4\xf3\xf2\xf1"


class FakePpppCamera:
    def __init__(self, did: str = "GHBB016638JHDXM", password: str = "265600",
                 bind: str = "127.0.0.1", port: int = 0,
                 sd_files: dict[str, bytes] | None = None,
                 networks: list[dict] | None = None,
                 value: int = 0x40FE) -> None:
        self.did = did
        self.password = password
        self.value = value & 0xFFFFFFFF
        self.bind = bind
        self.port = port
        self.sd_files = dict(sd_files or {})
        self.networks = networks or [
            {"ssid": "HomeNet", "sig": -45}, {"ssid": "Neighbour", "sig": -72},
            {"ssid": "", "sig": -1},
        ]
        self.ledstatus = 1
        self.lightstatus = 1
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._send_lock = threading.Lock()
        self.session_addr: tuple[str, int] | None = None
        self.discovery_addr: tuple[str, int] | None = None
        self.last_msg_id: int | None = None
        self.requests: list[bytes] = []
        self.request_addrs: list[tuple[str, int]] = []
        self.send_errors = 0
        self.last_send_error = ""
        self.lansearch_replies = 0
        self.last_lansearch_to = None
        self.cmd_idx = 0
        self.media_idx = 0
        self.download_idx = 0
        self.media_stop = threading.Event()
        self.download_stop = threading.Event()
        self._media_thread: threading.Thread | None = None
        self._download_thread: threading.Thread | None = None
        self.sent_video: list[bytes] = []
        self.acks: list[tuple[int, list[int]]] = []
        self.close_count = 0
        self.logged_in = False
        self.wifi_requested: dict | None = None
        self.download_userid = 0
        self.download_patch = ""
        self.frames_sent = 0
        self.last_command = ""
        self.video_info = {"brightness": 30, "contrast": 32, "stream": 1,
                           "hue": 32, "saturation": 28, "rate": 10}

    # ------------------------------------------------------------------ startup
    def start(self) -> "FakePpppCamera":
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.bind, self.port))
        s.settimeout(0.1)
        self.port = s.getsockname()[1]
        self._sock = s
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"fake-cam-{self.port}")
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self.media_stop.set()
        self.download_stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    # ------------------------------------------------------------------ loop
    def _run(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data or data[0] != 0xF1 or len(data) < 4:
                continue
            self.requests.append(data)
            self.request_addrs.append(addr)
            if len(self.requests) > 2000:
                del self.requests[:1000]
                del self.request_addrs[:1000]
            cmd = data[1]
            payload = data[4:4 + struct.unpack(">H", data[2:4])[0]]
            try:
                self._handle(cmd, payload, addr)
            except Exception as exc:  # the emulator must not crash because of a test
                print(f"fake-cam {self.port}: {exc}", flush=True)

    def _handle(self, cmd: int, payload: bytes, addr) -> None:
        if cmd == proto.CMD_LANSEARCH:
            self.discovery_addr = addr
            ident = self.did[:4].encode().ljust(8, b"\x00")
            field2 = self.did[-5:].encode().ljust(8, b"\x00")
            pkt = struct.pack(">BBH", 0xF1, 0x41, 20) + ident \
                + struct.pack(">I", self.value) + field2
            self._sendto(pkt, addr)
            self.lansearch_replies += 1
            self.last_lansearch_to = addr
            return
        if cmd == proto.CMD_PUNCHPKT:
            # The camera replies with P2PRdy only to its own value from PunchPkt.
            if len(payload) >= 12 and struct.unpack_from(">I", payload, 8)[0] != self.value:
                return
            self.session_addr = addr
            self._sendto(struct.pack(">BBH", 0xF1, 0x42, 0), addr)
            return
        if cmd == proto.CMD_DATA:
            self._on_data(payload, addr)
            return
        if cmd == proto.CMD_ACK:
            if len(payload) >= 4 and payload[0] == 0xD1:
                channel = payload[1]
                count = struct.unpack_from(">H", payload, 2)[0]
                nums = list(struct.unpack_from(f">{count}H", payload, 4)) if count else []
                self.acks.append((channel, nums))
            return
        if cmd == proto.CMD_ALIVE:
            self._sendto(struct.pack(">BBH", 0xF1, 0xE1, len(payload)) + payload, addr)
            return
        if cmd == proto.CMD_CLOSE:
            self.close_count += 1
            self.session_addr = addr
            self.media_stop.set()
            self.download_stop.set()
            return

    def _on_data(self, payload: bytes, addr) -> None:
        if len(payload) < 5:
            return
        self.session_addr = addr
        channel = payload[1]
        if channel == 0:
            return
        if channel != 1 or payload[0] != 0xD1:
            return
        msg_id = struct.unpack_from(">H", payload, 2)[0]
        if msg_id == self.last_msg_id:
            return                       # repeat of the same command (10 times)
        self.last_msg_id = msg_id
        body = payload[4:]
        frames = proto.cut_lu_frames(bytearray(body))
        for raw in frames:
            try:
                msg = json.loads(raw.decode("utf-8"))
            except Exception:
                continue
            self._on_command(msg)

    # ------------------------------------------------------------------ commands
    def _on_command(self, msg: dict) -> None:
        cmd = str(msg.get("cmd") or "")
        self.last_command = cmd
        if cmd == "LoginDev":
            ok = str(msg.get("pwd") or "") == self.password
            self.logged_in = ok
            self._respond({
                "cmd": "LoginDev", "result": 0 if ok else -1, "connectNum": 1,
                "devtype": "ipc", "audioType": 2, "SampleRate": 8000,
                "time": 0, "timeStatus": 0, "csType": 1,
            } if ok else {"cmd": "LoginDev", "result": -1})
        elif cmd == "AppointDev":
            self._respond({"cmd": "AppointDev", "state": 0})
        elif cmd == "GetDevInfo":
            self._respond({
                "cmd": "GetDevInfo", "id": "GHBB-016638-JHDXM",
                "ver": "Mar 16 2026 14:28:19", "ip": "192.168.100.1",
                "wifissid": "HomeNet", "wifipwd": "secret-net", "wifisig": -45,
                "4G": 0, "4GHotspot": 0, "4Gssid": "", "4Gpwd": "",
                "4Gsig": 0, "lock": 0, "iccid": "", "imei": "",
                "ledstatus": self.ledstatus, "lightstatus": self.lightstatus,
            })
        elif cmd == "GetDevStream":
            self._respond({"cmd": "GetDevStream", "mainstream": "4K",
                           "substream": "2K", "threestream": "1080P"})
        elif cmd == "GetDevVideoInfo":
            self._respond({"cmd": "GetDevVideoInfo", **self.video_info})
        elif cmd == "GetDevExInfo":
            self._respond({"cmd": "GetDevExInfo", "exposure": 1, "shutter": 2,
                           "AnalogGain": 3, "DigitalGain": 4, "GainFlag": 0,
                           "ExGain": 0, "MaxAnalog": 10, "MinAnalog": 0,
                           "MaxNumber": 10, "MinNumber": 0, "ExCompens": 0})
        elif cmd == "GetAlarmInfo":
            self._respond({"cmd": "GetAlarmInfo", "flag": 0, "time": 0,
                           "starttime": "00:00", "endtime": "23:59",
                           "twotime": "00:00", "twoend": "00:00", "audio": 0,
                           "putalarm": 0, "osd": 0, "goptime": 30,
                           "sensitivity": 1})
        elif cmd == "SetAlarmInfo":
            self._respond({"cmd": "SetAlarmInfo", "state": 0})
        elif cmd == "GetLedInfo":
            self._respond({"cmd": "GetLedInfo", "control": 0, "starttime": "18:00",
                           "endtime": "06:00", "flag": 1, "twotime": "00:00",
                           "twoend": "00:00"})
        elif cmd == "SetLedInfo":
            self._respond({"cmd": "SetLedInfo", "state": 0, "result": 0})
        elif cmd == "GetDevTfParam":
            self._respond({"cmd": "GetDevTfParam", "state": 1,
                           "totalSpace": 30536, "freeSpace": 20480, "openRes": 1,
                           "alarmRes": 0, "loopRes": 1, "audio": 0, "videoRes": 0,
                           "videoCycle": 300})
        elif cmd == "SetDevTfParam":
            self._respond({"cmd": "SetDevTfParam", "state": 0, "result": 0})
        elif cmd == "GetCloseTime":
            self._respond({"cmd": "GetCloseTime", "values": 0, "state": 0,
                           "result": 0})
        elif cmd == "SetCloseTime":
            self._respond({"cmd": "SetCloseTime", "state": 0, "result": 0})
        elif cmd == "GetTfVideoList":
            self._on_tf_list(msg)
        elif cmd == "DownloadFile":
            self._on_download(msg)
        elif cmd == "DeleteTfFile":
            self._respond({"cmd": "DeleteTfFile", "state": 0, "result": 0})
        elif cmd == "FormatCard":
            self._respond({"cmd": "FormatCard", "state": 0})
        elif cmd == "searchWiFiList":
            self._respond({"cmd": "searchWiFiList",
                           "value": list(self.networks)}, chunks=5)
        elif cmd == "OpenWifi":
            self.wifi_requested = {"sid": msg.get("sid"), "state": msg.get("state")}
        elif cmd == "ModifyPwd":
            if str(msg.get("pwd") or "") != self.password:
                self._respond({"cmd": "ModifyPwd", "result": -1})
            else:
                self.password = str(msg.get("newpwd") or self.password)
                self._respond({"cmd": "ModifyPwd", "result": 0})
        elif cmd == "OpenVideo":
            if int(msg.get("state") or 0) == 0:
                self.media_stop.set()
            else:
                self.media_stop = threading.Event()
                if self._media_thread is None or not self._media_thread.is_alive():
                    self._media_thread = threading.Thread(
                        target=self._media_loop, daemon=True,
                        name=f"fake-media-{self.port}")
                    self._media_thread.start()
        elif cmd in ("SetLed", "SetLight"):
            field = "ledstatus" if cmd == "SetLed" else "lightstatus"
            setattr(self, field, int(msg.get(field) or 0))
        elif cmd in ("SetDevVideoInfo", "LevelFlip",
                     "VerticalFlip", "OpenAudio", "OpenDevVoice", "DoCapture"):
            if cmd == "SetDevVideoInfo":
                for key in ("brightness", "contrast", "hue", "saturation", "rate"):
                    if key in msg:
                        self.video_info[key] = int(msg[key])
            return
        elif cmd == "TimeFlag":
            self._respond({"cmd": "TimeFlag", "state": 0, "result": 0})
        elif cmd.startswith("Get"):
            self._respond({"cmd": cmd, "state": 0, "result": 0})
        else:
            self._respond({"cmd": cmd, "state": 0, "result": 0})

    def _on_tf_list(self, msg: dict) -> None:
        page = int(msg.get("page") or 1)
        count = int(msg.get("count") or 30)
        files = []
        base = 1793825945
        for i, (name, data) in enumerate(sorted(self.sd_files.items())):
            files.append({"name": os.path.basename(name), "patch": name,
                          "size": len(data), "time": base + i * 300})
        start = (page - 1) * count
        batch = files[start:start + count]
        self._respond({"cmd": "GetTfVideoList", "page": page, "count": len(batch),
                       "allCount": len(files), "result": 0 if files else -1,
                       "value": batch}, chunks=max(2, len(batch)))

    def _on_download(self, msg: dict) -> None:
        state = int(msg.get("state") or 0)
        if state == 2:
            self.download_stop.set()
            self._respond({"cmd": "DownloadFile", "state": 2})
            return
        patch = str(msg.get("patch") or "")
        data = self.sd_files.get(patch)
        if data is None:
            self._respond({"cmd": "DownloadFile", "state": 2})
            return
        self.download_userid = int(msg.get("userid") or 0)
        self.download_patch = patch
        pos = int(msg.get("pos") or 0)
        self.download_stop = threading.Event()
        self._respond({"cmd": "DownloadFile", "state": 0})
        self._download_thread = threading.Thread(
            target=self._download_loop, args=(data, pos, self.download_userid),
            daemon=True, name=f"fake-dl-{self.port}")
        self._download_thread.start()

    # ------------------------------------------------------------------ sending
    def _respond(self, obj: dict, chunks: int = 1) -> None:
        body = proto.lu_frame(obj, tz_hours=3)
        step = max(1, (len(body) + chunks - 1) // max(1, chunks))
        for part in [body[i:i + step] for i in range(0, len(body), step)]:
            packet = (struct.pack(">BBH", 0xF1, 0xD0, 4 + len(part))
                      + bytes([0xD1, 0x01])
                      + struct.pack(">H", self.cmd_idx & 0xFFFF) + part)
            self.cmd_idx += 1
            self._send_packet(packet)
        return

    def _send_packet(self, packet: bytes) -> None:
        if self.session_addr is not None:
            self._sendto(packet, self.session_addr)

    def _sendto(self, packet: bytes, addr) -> None:
        if self._sock is None:
            return
        try:
            with self._send_lock:
                self._sock.sendto(packet, addr)
        except OSError as exc:
            self.send_errors += 1
            self.last_send_error = repr(exc)

    # ------------------------------------------------------------------ media
    def _media_loop(self) -> None:
        frame_no = 0
        ts = int(time.time() * 1000) & 0xFFFFFFFF
        keys_since = 99
        while not self.media_stop.is_set() and not self._stop.is_set():
            key = keys_since >= 5
            if key:
                keys_since = 0
            keys_since += 1
            data = make_media_frame(key, frame_no, ts)
            self.sent_video.append(data)
            if len(self.sent_video) > 300:
                del self.sent_video[:100]
            self._send_media_fragments(data)
            frame_no += 1
            self.frames_sent += 1
            ts = (ts + 100) & 0xFFFFFFFF
            time.sleep(0.1)

    def _send_media_fragments(self, data: bytes) -> None:
        for pos in range(0, len(data), 1024):
            if self.media_stop.is_set() or self.session_addr is None:
                return
            chunk = data[pos:pos + 1024]
            packet = (struct.pack(">BBH", 0xF1, 0xD0, 4 + len(chunk))
                      + bytes([0xD1, 0x00])
                      + struct.pack(">H", self.media_idx & 0xFFFF) + chunk)
            self.media_idx += 1
            self._send_packet(packet)

    # ------------------------------------------------------------------ download
    def _download_loop(self, data: bytes, pos: int, userid: int) -> None:
        block = 1
        while pos < len(data) and not self.download_stop.is_set():
            chunk = data[pos:pos + 6096]
            pos += len(chunk)
            last = pos >= len(data)
            body = make_download_frame(userid, -1000 if last else block, chunk)
            # Like a real camera: the channel 3 Lu frame is split into 1024-byte DRW chunks.
            for off in range(0, len(body), 1024):
                part = body[off:off + 1024]
                packet = (struct.pack(">BBH", 0xF1, 0xD0, 4 + len(part))
                          + bytes([0xD1, 0x03])
                          + struct.pack(">H", self.download_idx & 0xFFFF) + part)
                self.download_idx += 1
                self._send_packet(packet)
            block += 1
            time.sleep(0.003)

    # ------------------------------------------------------------------ helpers
    def make_sd_file(self, name: str, size: int = 30000, seed: int = 1) -> bytes:
        rnd = random.Random(seed)
        data = bytes(rnd.randrange(256) for _ in range(size))
        self.sd_files[name] = data
        return data

    @property
    def did_parts(self) -> tuple[str, str]:
        return self.did[:4], self.did[-5:]
