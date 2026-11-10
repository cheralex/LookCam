"""Manual recording to MP4 without transcoding: fMP4 from the same access units.

Recording starts from the nearest IDR in the GOP cache and writes `moof`/`mdat`
per frame. Stopping is done by a button; the file lives in
`cache_dir/records/<id>-<time>.mp4` and opens in VLC/ffmpeg/a browser
(fragmented MP4).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time

from . import fmp4, frames
from . import hub as hub_mod

log = logging.getLogger("lookcam.record")


class RecordingError(RuntimeError):
    pass


class Recording:
    def __init__(self, camera_id: str, path: str) -> None:
        self.camera_id = camera_id
        self.path = path
        self.started = time.time()
        self.stopped: float | None = None
        self.frames = 0
        self.bytes = 0
        self.state = "starting"
        self.error = ""
        self.task: asyncio.Task | None = None
        self.stop_event = asyncio.Event()
        self.config: hub_mod.StreamConfig | None = None
        self.hold = None

    def to_dict(self) -> dict:
        return {
            "camera_id": self.camera_id, "file": os.path.basename(self.path),
            "path": self.path, "started": self.started, "stopped": self.stopped,
            "frames": self.frames, "bytes": self.bytes, "state": self.state,
            "error": self.error,
            "url": f"/media/records/{os.path.basename(self.path)}",
        }


class RecorderManager:
    def __init__(self, config, manager) -> None:
        self.config = config
        self.manager = manager
        self.records: dict[str, Recording] = {}

    def _dir(self) -> str:
        path = os.path.join(self.config.cache_dir(), "records")
        os.makedirs(path, exist_ok=True)
        return path

    def active(self, camera_id: str) -> Recording | None:
        rec = self.records.get(camera_id)
        if rec is not None and rec.state in ("starting", "recording"):
            return rec
        return None

    async def start(self, camera) -> Recording:
        if self.active(camera.id) is not None:
            raise RecordingError("запись уже идёт")
        name = f"{camera.id}-{time.strftime('%Y%m%d-%H%M%S')}.mp4"
        rec = Recording(camera.id, os.path.join(self._dir(), name))
        # keep a video consumer, otherwise the stream stops right after start
        rec.hold = camera.acquire("video")
        await rec.hold.__aenter__()
        self.records[camera.id] = rec
        rec.task = asyncio.create_task(self._run(camera, rec),
                                       name=f"record-{camera.id}")
        return rec

    async def stop(self, camera_id: str) -> Recording | None:
        rec = self.records.get(camera_id)
        if rec is None:
            return None
        rec.stop_event.set()
        if rec.task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(rec.task, 5.0)
        if not rec.error:
            rec.state = "done"
        return rec

    async def stop_all(self) -> None:
        for camera_id in list(self.records):
            await self.stop(camera_id)

    async def _run(self, camera, rec: Recording) -> None:
        sub = camera.hub.subscribe(f"record-{camera.id}")
        writer: fmp4.FMP4Writer | None = None
        last_ts = 0
        fh = None
        try:
            deadline = time.monotonic() + 15.0
            # config first, then a key frame (the GOP cache already starts with an IDR)
            while time.monotonic() < deadline and not rec.stop_event.is_set():
                try:
                    event = await asyncio.wait_for(sub.get(), 1.0)
                except asyncio.TimeoutError:
                    continue
                if isinstance(event, hub_mod.ConfigEvent) and event.config \
                        and event.config.ready and writer is None:
                    cfg = event.config
                    rec.config = cfg
                    writer = fmp4.FMP4Writer(cfg.sps, cfg.pps, cfg.width,
                                             cfg.height, camera.hub.fps() or 10.0)
                    fh = open(rec.path, "wb")
                    fh.write(writer.init)
                    rec.bytes = len(writer.init)
                    continue
                if isinstance(event, frames.AccessUnit) and event.kind == "video" \
                        and event.key and writer is not None:
                    data = writer.fragment(event.payload, True, event.duration_ms)
                    fh.write(data)
                    rec.bytes += len(data)
                    rec.frames += 1
                    rec.state = "recording"
                    last_ts = event.ts_ms
                    self.manager.notify({"type": "record", **rec.to_dict()})
                    break
            if writer is None or fh is None:
                raise RecordingError("не дождались SPS/PPS или ключевого кадра")
            while not rec.stop_event.is_set():
                try:
                    event = await asyncio.wait_for(sub.get(), 1.0)
                except asyncio.TimeoutError:
                    continue
                if isinstance(event, hub_mod.ResetEvent):
                    rec.error = f"поток сброшен: {event.reason}"
                    break
                if isinstance(event, hub_mod.ConfigEvent):
                    if (event.config and rec.config
                            and event.config.sps != rec.config.sps):
                        rec.error = "качество изменилось — запись остановлена"
                        break
                    continue
                if not isinstance(event, frames.AccessUnit) or event.kind != "video":
                    continue
                duration_ms = (event.ts_ms - last_ts) if last_ts else 0
                if duration_ms < 0 or duration_ms > 1000:
                    duration_ms = 0
                data = writer.fragment(event.payload, event.key, duration_ms)
                if data:
                    fh.write(data)
                    rec.bytes += len(data)
                    rec.frames += 1
                    last_ts = event.ts_ms
            rec.state = "done" if not rec.error else "error"
        except asyncio.CancelledError:
            rec.state = "done"
            raise
        except Exception as exc:
            rec.state = "error"
            rec.error = str(exc)
            log.warning("recording %s: %s", rec.camera_id, exc)
        finally:
            rec.stopped = time.time()
            camera.hub.unsubscribe(sub)
            if fh is not None:
                try:
                    fh.flush()
                    os.fsync(fh.fileno())
                except OSError:
                    pass
                fh.close()
            if rec.hold is not None:
                with contextlib.suppress(Exception):
                    await rec.hold.__aexit__(None, None, None)
                rec.hold = None
            if rec.state == "starting":
                rec.state = "error"
                rec.error = rec.error or "запись не началась"
            self.manager.notify({"type": "record", **rec.to_dict()})

    def list(self) -> list[dict]:
        out = [rec.to_dict() for rec in self.records.values()]
        directory = self._dir()
        known = {os.path.basename(r.path) for r in self.records.values()}
        for name in sorted(os.listdir(directory)):
            if not name.endswith(".mp4") or name in known:
                continue
            path = os.path.join(directory, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            out.append({"camera_id": name.split("-", 1)[0], "file": name,
                        "path": path, "started": st.st_mtime, "stopped": st.st_mtime,
                        "frames": 0, "bytes": st.st_size, "state": "done",
                        "error": "",
                        "url": f"/media/records/{name}"})
        out.sort(key=lambda r: r.get("started") or 0, reverse=True)
        return out
