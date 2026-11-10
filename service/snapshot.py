"""Snapshot: IDR from the GOP cache → JPEG via ffmpeg (subprocess) or lazy cv2.

The camera has no snapshot command (spec §4.7 — the app captures the last decoded
frame). The service takes a key frame from the cache and decodes exactly one
frame. If the machine has neither `ffmpeg` nor `cv2`, the endpoint honestly
answers 501 — there is nothing to transcode with, and being dependency-free is a
rule of this project.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil

from . import frames

log = logging.getLogger("lookcam.snapshot")

MAX_SNAPSHOT_BYTES = 12 << 20


class SnapshotUnavailable(RuntimeError):
    pass


class SnapshotService:
    def __init__(self, cache_dir: str) -> None:
        self.cache_dir = cache_dir
        self.dir = os.path.join(cache_dir, "snapshots")
        os.makedirs(self.dir, exist_ok=True)
        self.ffmpeg = shutil.which("ffmpeg")
        self._cv2 = None
        self._cv2_checked = False

    @property
    def available(self) -> bool:
        if self.ffmpeg:
            return True
        return self._has_cv2()

    def _has_cv2(self) -> bool:
        if not self._cv2_checked:
            self._cv2_checked = True
            try:
                import cv2  # noqa: F401
                self._cv2 = True
            except Exception:
                self._cv2 = False
        return bool(self._cv2)

    async def capture(self, camera, timeout: float = 8.0) -> bytes:
        if not self.available:
            raise SnapshotUnavailable("нет ffmpeg или cv2 — снимок на сервере недоступен")
        au = await self._wait_key(camera, timeout)
        if au is None:
            raise SnapshotUnavailable("в GOP-кэше нет ключевого кадра")
        path = os.path.join(self.dir, f"{camera.id}-{int(time_ms())}.h264")
        try:
            with open(path, "wb") as fh:
                fh.write(au.payload)
            if self.ffmpeg:
                data = await self._ffmpeg(path)
                if data:
                    return data
            if self._has_cv2():
                data = await asyncio.to_thread(self._cv2_frame, path)
                if data:
                    return data
            raise SnapshotUnavailable("не удалось декодировать ключевой кадр")
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    async def _wait_key(self, camera, timeout: float) -> frames.AccessUnit | None:
        au = camera.hub.latest_key()
        if au is not None:
            return au
        sub = camera.hub.subscribe("snapshot")
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while True:
                left = deadline - loop.time()
                if left <= 0:
                    return camera.hub.latest_key()
                try:
                    event = await asyncio.wait_for(sub.get(), min(left, 2.0))
                except asyncio.TimeoutError:
                    continue
                if isinstance(event, frames.AccessUnit) and event.kind == "video" \
                        and event.key:
                    return event
        finally:
            camera.hub.unsubscribe(sub)

    async def _ffmpeg(self, path: str) -> bytes:
        assert self.ffmpeg
        proc = await asyncio.create_subprocess_exec(
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-i", path,
            "-frames:v", "1", "-f", "image2", "-vcodec", "mjpeg", "pipe:1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 10)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return b""
        if proc.returncode != 0 or not out or len(out) > MAX_SNAPSHOT_BYTES:
            return b""
        return out

    def _cv2_frame(self, path: str) -> bytes:
        try:
            import cv2
        except Exception:
            return b""
        cap = cv2.VideoCapture(path)
        try:
            ok, frame = cap.read()
            if not ok or frame is None:
                return b""
            ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
            return bytes(buf) if ok else b""
        finally:
            cap.release()


def time_ms() -> int:
    import time
    return int(time.time() * 1000)
