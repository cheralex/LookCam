"""Live check of SD download from URFKG: why "no data for 12 s"."""
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
from lookcam_deepsik.service import config as config_mod  # noqa: E402
from lookcam_deepsik.service.main import ServiceApp  # noqa: E402

DID = "G550856URFKG"
PASSWORD = os.environ.get("LOOKCAM_PASSWORD", "")
HOST = "192.168.0.147"
CACHE = os.environ.get("LOOKCAM_CACHE", os.path.join(PKG, "lookcam_data"))
TMP = os.path.join(HERE, "_live_sd")
os.makedirs(TMP, exist_ok=True)
FRESH = os.environ.get("SD_FRESH") == "1"
NAME_PART = os.environ.get("SD_FILE", "2026-1110-001134.mp4")

if not PASSWORD:
    raise SystemExit("задайте LOOKCAM_PASSWORD")


async def main() -> int:
    logging.basicConfig(level=logging.DEBUG,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config_mod.ConfigStore(os.path.join(TMP, "lookcam_service.json"), data={
        "http": {"bind": "127.0.0.1", "port": 0},
        "rtsp": {"enable": False, "port": 0},
        "admin": {"auth": False},
        "cache_dir": CACHE,
        "idle_ttl_s": 5,
        "cameras": [{"id": "urfkg", "name": "URFKG", "did": DID,
                     "password": PASSWORD, "host_hint": HOST,
                     "quality": "HD"}],
    }, autosave=False)
    app = ServiceApp(cfg, http_port=0, rtsp_port=0, bind="127.0.0.1",
                     pppp_port=32108)
    await app.start()
    try:
        cam = app.manager.require("urfkg")
        listing = await app.downloads.list_files(cam, 1, 30, all_pages=False)
        files = listing.get("files") or []
        print(f"файлов: {len(files)}", flush=True)
        target = next((f for f in files if NAME_PART in f.get("name", "")), None)
        if target is None:
            for f in files:
                print(" ", f.get("name"), f.get("size"), f.get("patch"), flush=True)
            print("файл не найден", flush=True)
            return 2
        print(f"цель: {target}", flush=True)
        final = os.path.join(CACHE, "sd", "urfkg", target["name"])
        temp = final + ".temp"
        if FRESH and os.path.exists(temp):
            os.remove(temp)
            print("temp удалён, качаем с нуля", flush=True)
        elif os.path.exists(temp):
            print(f"продолжаем с {os.path.getsize(temp)} байт", flush=True)
        task = app.downloads.request(cam, target["patch"], target["name"],
                                     target["size"])
        print(f"задача n={task.n}, temp={task.temp_path}", flush=True)
        last = -1
        start = time.time()
        while time.time() - start < 120:
            await asyncio.sleep(1)
            print(f"  {time.time()-start:5.1f}s state={task.state} "
                  f"downloaded={task.downloaded} size={task.size} "
                  f"err={task.error}", flush=True)
            if task.state in ("done", "error", "partial", "canceled"):
                break
            if task.downloaded == last:
                pass
            last = task.downloaded
        print(f"итог: {task.state}, {task.downloaded}/{task.size}, "
              f"ошибка={task.error}", flush=True)
        print(f"сессия: {cam.session.stats() if cam.session else '—'}", flush=True)
        return 0 if task.state == "done" else 1
    finally:
        await app.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
