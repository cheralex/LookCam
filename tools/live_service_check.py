"""Live check of the service on a camera in the LAN (not the emulator).

Starts ServiceApp with one camera, waits for the stream, snapshots hub
statistics, connects with the RTSP client (TCP) and prints the result. The
camera holds a single session, so at the end we must close the service
(Close twice).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PKG))

import _bootstrap  # noqa: E402,F401
from lookcam_deepsik.vendor.lookcam import rtsp as rtsp_client  # noqa: E402
from lookcam_deepsik.service import config as config_mod  # noqa: E402
from lookcam_deepsik.service.main import ServiceApp  # noqa: E402

DID = os.environ.get("LOOKCAM_DID", "G016638JHDXM")
PASSWORD = os.environ.get("LOOKCAM_PASSWORD", "265600")
HOST_HINT = os.environ.get("LOOKCAM_HOST", "192.168.0.145")
TMP = os.path.join(HERE, "_live_service")
os.makedirs(TMP, exist_ok=True)
os.makedirs(TMP, exist_ok=True)


async def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config_mod.ConfigStore(os.path.join(TMP, "lookcam_service.json"), data={
        "http": {"bind": "127.0.0.1", "port": 0},
        "rtsp": {"enable": True, "bind": "127.0.0.1", "port": 0,
                 "password_hash": "", "realm": "LookCam"},
        "admin": {"auth": False},
        "cache_dir": TMP,
        "idle_ttl_s": 5,
        "cameras": [{"id": "live", "name": "Живая", "did": DID,
                     "password": PASSWORD, "host_hint": HOST_HINT,
                     "quality": "HD", "keep_alive": False}],
    }, autosave=False)
    app = ServiceApp(cfg, http_port=0, rtsp_port=0, bind="127.0.0.1",
                     pppp_port=32108, public_host="")
    await app.start()
    print(f"HTTP :{app.http.port}  RTSP :{app.rtsp.port}", flush=True)
    try:
        cam = app.manager.require("live")
        t0 = time.time()
        async with cam.acquire("video"):
            print(f"сессия: {cam.session.addr_text if cam.session else '—'}, "
                  f"ожидание потока {time.time() - t0:.1f} с", flush=True)
            deadline = time.time() + 12
            frames = 0
            keys = 0
            while time.time() < deadline:
                await asyncio.sleep(0.5)
                frames = cam.hub.frames
                keys = sum(1 for au in cam.hub.gop() if au.key)
                if frames >= 15:
                    break
            cfg_now = cam.hub.config
            print(f"поток: кадров={cam.hub.frames}, fps={cam.hub.fps()}, "
                  f"битрейт={cam.hub.bitrate_kbps()} кбит/с, "
                  f"SPS={cfg_now.sps_patched if cfg_now else '—'}, "
                  f"{cfg_now.width if cfg_now else '?'}x"
                  f"{cfg_now.height if cfg_now else '?'}", flush=True)
            print(f"кадров за окно: {frames}, ключевых в GOP: {keys}", flush=True)
            print(f"статистика сессии: {cam.session.stats()}", flush=True)

        # RTSP: while the stream is open by a viewer, connect as a client.
        async with cam.acquire("video"):
            session = rtsp_client.RtspSession(
                f"rtsp://127.0.0.1:{app.rtsp.port}/live", transport="tcp",
                timeout=10.0)
            try:
                media = await asyncio.to_thread(session.open, "video")
                print(f"RTSP DESCRIBE: {media.codec} {media.width}x{media.height}, "
                      f"sprop={len(media.sprop)} б", flush=True)
                gen = session.frames()
                nal_count = 0
                key_nal = 0
                start = time.time()
                while nal_count < 20 and time.time() - start < 15:
                    kind, data, ts = await asyncio.wait_for(
                        asyncio.to_thread(next, gen), 8)
                    nal_count += 1
                    if data[:5] in (b"\x00\x00\x00\x01\x25", b"\x00\x00\x00\x01\x65"):
                        key_nal += 1
                print(f"RTSP получено NAL: {nal_count}, IDR: {key_nal}", flush=True)
            except Exception as exc:
                print(f"RTSP ОШИБКА: {exc!r}", flush=True)
                return 1
            finally:
                await asyncio.to_thread(session.teardown)

        print(f"состояние камеры: {cam.state}, ошибка: {cam.last_error or '—'}",
              flush=True)
        return 0
    finally:
        await app.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
