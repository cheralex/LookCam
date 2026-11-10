"""SD card: file listing, download queue, resume and cache.

The channel 3 contract (spec §5.2) is the most capricious one in the project:

* acks are mandatory and no longer than 64 numbers (they are sent by the session,
  not by this module);
* only **one** download runs at a time, live video is paused for that period
  (`Camera.exclusive`);
* `state:3` in the start response means the camera still considers the transfer
  active; retry after 5/10/20 s;
* 12 s without data is an error; `state:2` (stop) is sent on any exit;
* resume continues from the length of `.temp`, the finished file is renamed
  atomically.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import os
import random
import re
import struct
import time
from dataclasses import dataclass, field

from . import camera as camera_mod
from . import mp4_index
from . import proto

log = logging.getLogger("lookcam.sd")

NO_DATA_TIMEOUT = 12.0
STATE3_DELAYS = (5.0, 10.0, 20.0)
PAGE_COUNT = 30
MAX_PAGES = 100
#: Data block size in channel 3 (spec §1.4.4): 6096 for all but the last one.
DOWNLOAD_BLOCK = 6096
#: Tail fetched for the MP4 moov; usually 35-40 KB, 512 KB gives headroom.
SEEK_TAIL = 512 * 1024
#: Head needed for ftyp/mdat and for browsers that start at the beginning.
SEEK_HEAD = 64 * 1024
#: How much video to prefetch from the chosen keyframe (about 40 s at 600 kbps).
SEEK_PREFETCH = 3 * 1024 * 1024
#: Maximum single HTTP range served without a new download (browser will ask again).
SEEK_RANGE_CHUNK = 2 * 1024 * 1024


class SDError(RuntimeError):
    pass


class SDTimeout(SDError):
    pass


class SDNotFound(SDError):
    pass


class _Canceled(Exception):
    pass


@dataclass
class DownloadTask:
    n: int
    camera_id: str
    patch: str
    name: str
    size: int = 0
    downloaded: int = 0
    state: str = "queued"          # queued|running|done|error|canceled|partial
    error: str = ""
    userid: int = field(default_factory=lambda: random.randint(1, 100))
    temp_path: str = ""
    final_path: str = ""
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    done_event: asyncio.Event = field(default_factory=asyncio.Event)
    cancel_requested: bool = False
    last_data: float = 0.0
    sha1: str = ""
    next_block: int | None = None

    def to_dict(self) -> dict:
        pct = round(self.downloaded * 100 / self.size, 1) if self.size else 0.0
        return {
            "n": self.n, "camera_id": self.camera_id, "patch": self.patch,
            "name": self.name, "size": self.size, "downloaded": self.downloaded,
            "progress": pct, "state": self.state, "error": self.error,
            "created": self.created, "updated": self.updated,
            "sha1": self.sha1,
            "file": os.path.basename(self.final_path) if self.final_path else "",
        }


def safe_name(name: str, patch: str = "") -> str:
    base = os.path.basename(name or patch or "download.mp4")
    base = re.sub(r"[^\w.\- ()\[\]]+", "_", base, flags=re.UNICODE).strip(" ._")
    if not base:
        base = "download.mp4"
    if not os.path.splitext(base)[1]:
        base += ".mp4"
    return base[:120]


class SDCard:
    """List/delete/format + a download queue with resume."""

    def __init__(self, config, manager: camera_mod.CameraManager) -> None:
        self.config = config
        self.manager = manager
        self.tasks: dict[int, DownloadTask] = {}
        self.queue: asyncio.Queue = asyncio.Queue()
        self._next_n = 1
        self._worker: asyncio.Task | None = None
        self.active: DownloadTask | None = None
        self._fh = None
        self._last_progress_event = 0.0
        self._range_lock = asyncio.Lock()
        manager.download_callback = self.on_block

    # ------------------------------------------------------------------ startup
    async def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="sd-downloader")

    async def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None
        if self.active is not None:
            await self._send_stop(self.active)

    # ------------------------------------------------------------------ listing
    async def list_files(self, camera, page: int = 1, count: int = PAGE_COUNT,
                         all_pages: bool = True) -> dict:
        page = max(1, int(page))
        count = max(1, min(int(count), 100))
        files: list[dict] = []
        seen: set[str] = set()
        all_count = 0
        result = 0
        async with camera.acquire("control"):
            if camera.session is None:
                raise SDError("нет сессии с камерой")
            for _ in range(MAX_PAGES if all_pages else 1):
                msg = await camera.session.bus.request(
                    {"cmd": "GetTfVideoList", "page": page, "count": count},
                    "GetTfVideoList", 30000)
                result = proto.lenient_int(msg.get("result"), 0)
                all_count = proto.lenient_int(msg.get("allCount"), 0)
                batch = msg.get("value") or []
                new = 0
                for raw in batch:
                    if not isinstance(raw, dict):
                        continue
                    patch = str(raw.get("patch") or "")
                    if not patch or patch in seen:
                        continue
                    seen.add(patch)
                    new += 1
                    files.append(self._normalize_file(raw))
                if not all_pages or not batch or new == 0:
                    break
                if all_count and len(files) >= all_count:
                    break
                page += 1
        return {"result": result, "allCount": all_count, "count": len(files),
                "files": files}

    @staticmethod
    def _normalize_file(raw: dict) -> dict:
        ts = proto.lenient_int(raw.get("time"), 0)
        iso = ""
        text = ""
        if ts:
            try:
                stamp = dt.datetime.fromtimestamp(ts, dt.timezone.utc).replace(tzinfo=None)
                iso = stamp.isoformat() + "Z"
                # camera time is stored as "local in UTC": show it without an offset
                text = stamp.strftime("%Y-%m-%d %H:%M:%S")
            except (OverflowError, OSError, ValueError):
                iso = ""
        return {"name": str(raw.get("name") or ""),
                "patch": str(raw.get("patch") or ""),
                "size": proto.lenient_int(raw.get("size"), 0),
                "time": ts, "time_iso": iso, "time_text": text}

    # ------------------------------------------------------------------ deletion
    async def delete_files(self, camera, patches: list[str]) -> dict:
        patches = [p for p in patches if p]
        if not patches:
            raise SDError("не передано ни одного patch")
        async with camera.acquire("control"):
            if camera.session is None:
                raise SDError("нет сессии")
            msg = await camera.session.bus.request(
                {"cmd": "DeleteTfFile", "count": len(patches),
                 "value": [{"patch": p} for p in patches]},
                "DeleteTfFile", 20000)
        return proto.scrub_secrets(msg)

    async def format_card(self, camera) -> dict:
        await camera.ensure_online()
        async with camera.exclusive("форматирование SD", pause_live=True):
            if camera.session is None:
                raise SDError("нет сессии")
            msg = await camera.session.bus.request({"cmd": "FormatCard"},
                                                   "FormatCard", 30000)
        return proto.scrub_secrets(msg)

    # ------------------------------------------------------------------ queue
    def request(self, camera, patch: str, name: str = "", size: int = 0) -> DownloadTask:
        if not patch:
            raise SDError("не передан patch файла")
        name = safe_name(name, patch)
        base = os.path.join(self.config.cache_dir(), "sd", camera.id)
        os.makedirs(base, exist_ok=True)
        final = os.path.join(base, name)
        if os.path.exists(final) and size and os.path.getsize(final) == size:
            task = DownloadTask(self._new_n(), camera.id, patch, name, size=size)
            task.final_path = final
            task.downloaded = size
            task.state = "done"
            self.tasks[task.n] = task
            return task
        task = DownloadTask(self._new_n(), camera.id, patch, name, size=size)
        task.final_path = final
        task.temp_path = final + ".temp"
        self.tasks[task.n] = task
        self.queue.put_nowait(task)
        self.manager.notify({"type": "download", **task.to_dict()})
        return task

    def _new_n(self) -> int:
        n = self._next_n
        self._next_n += 1
        return n

    def list_downloads(self) -> list[dict]:
        return [t.to_dict() for t in sorted(self.tasks.values(), key=lambda t: t.n)]

    def get(self, n: int) -> DownloadTask | None:
        return self.tasks.get(n)

    def cancel(self, n: int) -> bool:
        task = self.tasks.get(n)
        if task is None:
            return False
        task.cancel_requested = True
        if task.state in ("queued",):
            task.state = "canceled"
            task.updated = time.time()
            self.manager.notify({"type": "download", **task.to_dict()})
        if self.active is task:
            task.done_event.set()
        return True

    async def _run(self) -> None:
        while True:
            task = await self.queue.get()
            if task.cancel_requested:
                task.state = "canceled"
                continue
            try:
                await self._download(task)
            except _Canceled:
                task.state = "canceled"
                task.error = "отменено"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                task.state = "error"
                task.error = str(exc)
                log.warning("download %s: %s", task.name, exc)
            finally:
                task.updated = time.time()
                self.manager.notify({"type": "download", **task.to_dict()})
                self._cleanup_cache()

    async def _download(self, task: DownloadTask) -> None:
        camera = self.manager.require(task.camera_id)
        task.state = "running"
        task.updated = time.time()
        self.manager.notify({"type": "download", **task.to_dict()})
        try:
            async with camera.exclusive(f"скачивание {task.name}", pause_live=True):
                for attempt in range(len(STATE3_DELAYS) + 1):
                    try:
                        await self._attempt(task, camera)
                        break
                    except _BusyCapture as exc:
                        if attempt >= len(STATE3_DELAYS):
                            raise SDError(str(exc))
                        delay = STATE3_DELAYS[attempt]
                        task.error = f"камера занята, повтор через {delay:.0f} с"
                        self.manager.notify({"type": "download", **task.to_dict()})
                        await asyncio.sleep(delay)
        finally:
            # The APK releases the DeviceFile session lease after the stop. Do
            # not leave the PPPP session in SD-transfer state until idle_ttl:
            # some camera firmwares keep their internal SD recorder stopped
            # until the session is actually closed.
            if camera.consumers_total == 0 and not camera.keep_alive:
                await camera.disconnect()

    @staticmethod
    def _resume_offset(path: str, size: int) -> int:
        """Length at which `.temp` can be resumed.

        The camera serves blocks of 6096 bytes. If an old `.temp` stopped in the
        middle of a block (for example, an earlier version wrote only the first
        DRW fragment), resuming from an arbitrary byte is not allowed: it would
        produce a shift and a corrupt MP4. We cut off the incomplete block.
        """
        if not os.path.exists(path):
            return 0
        resume = os.path.getsize(path)
        if size and resume >= size:
            os.remove(path)
            return 0
        rem = resume % DOWNLOAD_BLOCK
        if rem:
            aligned = resume - rem
            log.warning("SD: .temp size %d is not aligned to block %d — truncating to %d",
                        resume, DOWNLOAD_BLOCK, aligned)
            with open(path, "r+b") as fh:
                fh.truncate(aligned)
            resume = aligned
        return resume

    async def _attempt(self, task: DownloadTask, camera) -> None:
        if camera.session is None:
            await camera.ensure_online()
        if camera.session is None:
            raise SDError("нет сессии")
        session = camera.session
        size = task.size
        resume = self._resume_offset(task.temp_path, size)
        if task.cancel_requested:
            raise _Canceled()
        task.downloaded = resume
        task.next_block = None
        task.done_event.clear()
        task.last_data = time.time()
        self.active = task
        self._fh = open(task.temp_path, "ab")
        log.info("%s: DownloadFile start pos=%d size=%d userid=%d",
                 task.camera_id, resume, size, task.userid)
        try:
            try:
                answer = await session.bus.request({
                    "cmd": "DownloadFile", "pwd": session.password,
                    "userid": task.userid, "patch": task.patch,
                    "pos": resume, "state": 1,
                }, "DownloadFile", 5000)
            except Exception as exc:
                raise SDError(f"камера не ответила на DownloadFile: {exc}")
            state = proto.lenient_int(answer.get("state"), -1)
            log.info("%s: DownloadFile response state=%s", task.camera_id, state)
            if state == 3:
                raise _BusyCapture("камера ещё занята предыдущей передачей")
            if state == 2:
                raise SDNotFound("файл не найден на карте")
            if state not in (0, 1):
                raise SDError(f"камера отказала в скачивании (state={state})")
            await self._wait_data(task)
            if task.state == "error":
                raise SDError(task.error or "ошибка данных SD")
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._fh.close()
            self._fh = None
            if task.cancel_requested:
                task.state = "canceled"
                task.error = "отменено"
                raise _Canceled()
            downloaded = os.path.getsize(task.temp_path)
            if size and downloaded < size:
                task.state = "partial"
                task.error = f"получено {downloaded} из {size} байт"
                return
            os.replace(task.temp_path, task.final_path)
            task.state = "done"
            task.downloaded = downloaded
            task.error = ""
            task.sha1 = _sha1(task.final_path)
        finally:
            if self._fh is not None:
                try:
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None
            if self.active is task:
                self.active = None
            await self._send_stop(task, session)

    async def _wait_data(self, task: DownloadTask) -> None:
        while not task.done_event.is_set():
            if task.cancel_requested:
                raise _Canceled()
            try:
                await asyncio.wait_for(task.done_event.wait(), 0.5)
            except asyncio.TimeoutError:
                if time.time() - task.last_data > NO_DATA_TIMEOUT:
                    task.state = "error"
                    task.error = "нет данных 12 с"
                    raise SDTimeout("нет данных 12 с")
        if task.cancel_requested:
            raise _Canceled()

    async def _send_stop(self, task: DownloadTask | None, session=None,
                         patch: str = "", userid: int | None = None,
                         pos: int = 0) -> None:
        if session is None and task is not None:
            camera = self.manager.get(task.camera_id)
            session = camera.session if camera else None
        if session is None or not session.is_connected:
            return
        # The camera deliberately does not answer DownloadFile{state:2}
        # (spec §1.4.4: stop fields are only pwd and state; after stop it still
        # treats the transfer as active for 10-20 s). Do not wait for a reply
        # here and do not add userid/patch/pos — the APK sends the same minimal
        # stop and then closes the session.
        try:
            await session.bus.send({"cmd": "DownloadFile",
                                    "pwd": session.password, "state": 2})
        except Exception as exc:
            log.warning("SD: DownloadFile stop failed: %s", exc)

    # ------------------------------------------------------------------ data
    def on_block(self, camera, header: proto.DownloadHeader, data: bytes) -> None:
        """Channel 3 block from the session: write only the data of our active download."""
        task = self.active
        if task is None or task.camera_id != camera.id:
            return
        if header.userid != task.userid:
            return
        if not header.last:
            if task.next_block is None:
                task.next_block = header.block_no
            if header.block_no < task.next_block:
                return                    # repeat of an already written block
            if header.block_no != task.next_block:
                task.state = "error"
                task.error = (f"пропуск блока SD: ждали {task.next_block}, "
                              f"пришёл {header.block_no}")
                task.done_event.set()
                return
            task.next_block += 1
            if len(data) != DOWNLOAD_BLOCK:
                task.state = "error"
                task.error = f"блок {header.block_no} длиной {len(data)}, ждали {DOWNLOAD_BLOCK}"
                task.done_event.set()
                return
        if data:
            try:
                if self._fh is not None:
                    self._fh.write(data)
            except OSError as exc:
                task.state = "error"
                task.error = f"ошибка записи: {exc}"
                task.done_event.set()
                return
            task.downloaded += len(data)
            task.last_data = time.time()
            task.updated = time.time()
            if task.downloaded % (256 * 1024) < len(data):
                log.debug("%s: downloaded %d bytes (block %s)",
                          task.camera_id, task.downloaded, header.block_no)
            now = time.time()
            if now - self._last_progress_event > 0.5:
                self._last_progress_event = now
                self.manager.notify({"type": "download", **task.to_dict()})
        if header.last:
            task.done_event.set()
        elif task.size and task.downloaded >= task.size:
            task.done_event.set()

    # ------------------------------------------------------------------ seek
    def seek_paths(self, camera_id: str, name: str) -> tuple[str, str]:
        base = os.path.join(self.config.cache_dir(), "sd", camera_id)
        os.makedirs(base, exist_ok=True)
        path = os.path.join(base, safe_name(name) + ".seek")
        return path, path + ".json"

    def seek_info(self, camera_id: str, name: str) -> dict | None:
        path, meta_path = self.seek_paths(camera_id, name)
        if not os.path.isfile(path) or not os.path.isfile(meta_path):
            return None
        try:
            with open(meta_path, "r", encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            return None
        if int(meta.get("size") or 0) != os.path.getsize(path):
            return None
        return meta

    @staticmethod
    def _merge_range(ranges: list[list[int]], start: int, end: int) -> list[list[int]]:
        items = [list(x) for x in ranges] + [[int(start), int(end)]]
        items.sort()
        out: list[list[int]] = []
        for a, b in items:
            if b <= a:
                continue
            if out and a <= out[-1][1]:
                out[-1][1] = max(out[-1][1], b)
            else:
                out.append([a, b])
        return out

    @staticmethod
    def _has_range(ranges: list[list[int]], start: int, end: int) -> bool:
        for a, b in ranges:
            if a <= start and b >= end:
                return True
        return False

    @staticmethod
    def _ftyp_size(head: bytes) -> int:
        for kind, _pos, _body, box_end in mp4_index._boxes(head):
            if kind == "ftyp":
                return box_end
        return 28

    @staticmethod
    def _patch_moov(moov: bytes, delta: int) -> bytes:
        """Shift all chunk offsets in a moov by ``delta`` bytes."""
        data = bytearray(moov)
        containers = {"moov", "trak", "mdia", "minf", "stbl", "dinf", "edts"}

        def walk(start: int, end: int) -> None:
            for kind, _pos, body, box_end in mp4_index._boxes(data, start, end):
                if kind in containers:
                    walk(body, box_end)
                elif kind in ("stco", "co64"):
                    count = struct.unpack_from(">I", data, body + 4)[0]
                    step = 4 if kind == "stco" else 8
                    if body + 8 + count * step > box_end:
                        continue
                    for i in range(count):
                        off = body + 8 + i * step
                        value = (struct.unpack_from(">I", data, off)[0]
                                 if step == 4 else struct.unpack_from(">Q", data, off)[0])
                        value = (value + delta) & ((1 << (step * 8)) - 1)
                        struct.pack_into(">I" if step == 4 else ">Q", data, off, value)

        walk(0, len(data))
        return bytes(data)

    def _load_seek(self, path: str, meta_path: str, camera_id: str, patch: str,
                   name: str, size: int) -> dict:
        if os.path.isfile(path) and os.path.isfile(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as fh:
                    meta = json.load(fh)
                if meta.get("version") == 2 and meta.get("patch") == patch \
                        and int(meta.get("source_size") or 0) == size \
                        and int(meta.get("size") or 0) == os.path.getsize(path):
                    return meta
            except (OSError, ValueError):
                pass
        meta = {"version": 2, "camera_id": camera_id, "patch": patch,
                "name": name, "source_size": size, "size": size,
                "delta": 0, "ftyp_size": 28, "ranges": [],
                "keyframes": [], "duration_ms": 0.0, "moov_offset": 0}
        with open(path, "wb") as fh:
            fh.truncate(size)
        self._save_seek(meta_path, meta)
        return meta

    @staticmethod
    def _save_seek(meta_path: str, meta: dict) -> None:
        tmp = meta_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, meta_path)

    @staticmethod
    def _write_at(path: str, offset: int, data: bytes) -> None:
        if not data:
            return
        with open(path, "r+b") as fh:
            fh.seek(offset)
            fh.write(data)

    async def _download_range(self, camera, patch: str, start: int,
                              end: int, timeout: float) -> bytes:
        """Fetch ``[start,end)`` from an already-exclusive camera session.

        The camera sends channel-3 blocks sequentially from a block-aligned
        position. Callers align ``start`` themselves; the returned buffer starts
        at that aligned position.
        """
        session = camera.session
        if session is None:
            raise SDError("нет сессии")
        end = min(int(end), 1 << 40)
        if end <= start:
            return b""
        got = bytearray()
        done = asyncio.Event()
        userid = random.randint(1, 100)

        def on_block(_cam, header, data: bytes) -> None:
            if header.userid != userid:
                return
            got.extend(data)
            if len(got) >= end - start:
                done.set()

        camera.download_callback = on_block
        try:
            try:
                answer = await session.bus.request({
                    "cmd": "DownloadFile", "pwd": session.password,
                    "userid": userid, "patch": patch,
                    "pos": int(start), "state": 1,
                }, "DownloadFile", 5000)
            except Exception as exc:
                raise SDError(f"камера не ответила на DownloadFile: {exc}") from exc
            state = proto.lenient_int(answer.get("state"), -1)
            if state == 3:
                raise SDError("камера занята SD-передачей")
            if state not in (0, 1):
                raise SDError(f"камера отказала в скачивании (state={state})")
            try:
                await asyncio.wait_for(done.wait(), timeout)
            except asyncio.TimeoutError:
                log.warning("SD seek: range %d-%d timeout after %d bytes",
                            start, end, len(got))
        finally:
            await self._send_stop(None, session, patch=patch, userid=userid,
                                  pos=start)
            await asyncio.sleep(0.2)
            camera.download_callback = None
        return bytes(got[:end - start])

    async def _fetch_into(self, camera, patch: str, path: str, size: int,
                          start: int, end: int, timeout: float) -> int:
        start = (max(0, start) // DOWNLOAD_BLOCK) * DOWNLOAD_BLOCK
        end = min(size, max(end, start + DOWNLOAD_BLOCK))
        data = await self._download_range(camera, patch, start, end, timeout)
        if not data:
            raise SDError(f"пустой диапазон SD {start}-{end}")
        self._write_at(path, start, data)
        return len(data)

    async def prepare_seek(self, camera, patch: str, name: str, size: int,
                           pos_ms: float) -> dict:
        """Prepare a fast-start MP4 with the keyframe window around pos_ms.

        The source MP4 has ``moov`` at the end and sample offsets in ``stco``.
        We move a copy of ``moov`` to the front (after ``ftyp``), shift every
        chunk offset by its size and write the fetched data at the shifted
        positions. The result is a sparse file that browsers can seek with
        ordinary HTTP Range requests without downloading the whole recording.
        """
        if not patch or not size:
            raise SDError("не передан patch/размер файла")
        name = safe_name(name, patch)
        base = os.path.join(self.config.cache_dir(), "sd", camera.id)
        final = os.path.join(base, name)
        if os.path.isfile(final) and os.path.getsize(final) == size:
            url = f"/media/{camera.id}/{name}"
            return {"file": name, "url": url, "full": True,
                    "size": size, "source_size": size,
                    "pos_ms": max(0.0, float(pos_ms)),
                    "keyframe_ms": max(0.0, float(pos_ms))}

        path, meta_path = self.seek_paths(camera.id, name)
        async with self._range_lock:
            await camera.ensure_online()
            async with camera.exclusive("SD seek", pause_live=True):
                meta = self._load_seek(path, meta_path, camera.id, patch, name, size)
                try:
                    if not meta.get("keyframes"):
                        head = await self._download_range(
                            camera, patch, 0, min(SEEK_HEAD, size), 8.0)
                        if not head:
                            raise SDError("пустой head SD-файла")
                        ftyp_size = self._ftyp_size(head)
                        tail = None
                        index = None
                        tail_start = 0
                        for extra in (SEEK_TAIL, SEEK_TAIL * 2,
                                      SEEK_TAIL * 4, size):
                            tail_start = max(0, ((size - extra) // DOWNLOAD_BLOCK)
                                             * DOWNLOAD_BLOCK)
                            timeout = max(8.0, (size - tail_start) / 180_000 + 4)
                            tail = await self._download_range(
                                camera, patch, tail_start, size, timeout)
                            if not tail:
                                continue
                            try:
                                index = mp4_index.parse_moov(
                                    tail, tail_start, size)
                                break
                            except mp4_index.Mp4Error:
                                index = None
                        if tail is None or index is None:
                            raise SDError("не удалось найти moov в хвосте MP4")
                        delta = index.moov_size
                        target_size = size + delta
                        moov_start = index.moov_offset - tail_start
                        moov = tail[moov_start:moov_start + index.moov_size]
                        if len(moov) != index.moov_size:
                            raise SDError("moov обрезан при переносе")
                        patched = self._patch_moov(moov, delta)
                        with open(path, "wb") as fh:
                            fh.truncate(target_size)
                        self._write_at(path, 0, head[:ftyp_size])
                        self._write_at(path, ftyp_size, patched)
                        self._write_at(path, ftyp_size + delta,
                                       head[ftyp_size:])
                        data_start = ftyp_size + delta
                        meta.update({
                            "version": 2, "size": target_size,
                            "source_size": size, "delta": delta,
                            "ftyp_size": ftyp_size, "moov_offset": index.moov_offset,
                            "duration_ms": index.duration_ms,
                            "keyframes": [[round(s.time_ms, 3), s.offset, s.size]
                                          for s in index.keyframes],
                            "ranges": [
                                [0, ftyp_size],
                                [ftyp_size, ftyp_size + delta],
                                [data_start, data_start + len(head) - ftyp_size],
                            ],
                        })
                        self._save_seek(meta_path, meta)
                    key = None
                    for key_ms, offset, sample_size in meta["keyframes"]:
                        if key_ms <= max(0.0, float(pos_ms)):
                            key = (key_ms, int(offset), int(sample_size))
                    if key is None and meta["keyframes"]:
                        k = meta["keyframes"][0]
                        key = (float(k[0]), int(k[1]), int(k[2]))
                    if key is None:
                        raise SDError("в файле нет ключевых кадров")
                    key_ms, key_off, key_size = key
                    delta = int(meta.get("delta") or 0)
                    start = (key_off // DOWNLOAD_BLOCK) * DOWNLOAD_BLOCK
                    end = min(size, start + max(SEEK_PREFETCH,
                                                key_size + 512 * 1024))
                    target_start = start + delta
                    target_end = end + delta
                    if not self._has_range(meta["ranges"], target_start, target_end):
                        data = await self._download_range(
                            camera, patch, start, end, 40.0)
                        self._write_at(path, target_start, data)
                        target_end = min(target_end, target_start + len(data))
                        meta["ranges"] = self._merge_range(
                            meta["ranges"], target_start, target_end)
                    meta["updated"] = time.time()
                    self._save_seek(meta_path, meta)
                except Exception:
                    self._save_seek(meta_path, meta)
                    raise
        url = f"/media/{camera.id}/{name}"
        return {"file": name, "url": url, "full": False,
                "size": int(meta.get("source_size") or size),
                "source_size": int(meta.get("source_size") or size),
                "stream_size": int(meta.get("size") or 0),
                "pos_ms": max(0.0, float(pos_ms)),
                "keyframe_ms": round(key_ms, 3),
                "duration_ms": round(float(meta.get("duration_ms") or 0), 3),
                "ranges": meta.get("ranges", [])}

    async def ensure_media_range(self, camera, name: str, start: int,
                                 end: int) -> dict | None:
        """Download a missing byte range for an already prepared fast-start file."""
        path, meta_path = self.seek_paths(camera.id, name)
        meta = self.seek_info(camera.id, name)
        if meta is None or int(meta.get("version") or 0) != 2:
            return None
        target_size = int(meta["size"])
        source_size = int(meta.get("source_size") or target_size)
        ftyp_size = int(meta.get("ftyp_size") or 0)
        delta = int(meta.get("delta") or 0)
        data_start = ftyp_size + delta
        start = max(0, min(target_size, int(start)))
        end = max(start, min(target_size, int(end)))
        if start == end or self._has_range(meta.get("ranges", []), start, end):
            return meta
        # Metadata and the shifted head are already present; only fetch the data
        # portion of the requested target range.
        data_start_req = max(start, data_start)
        if data_start_req >= end:
            return meta
        source_start = max(ftyp_size, data_start_req - delta)
        source_end = min(source_size, end - delta)
        if source_end <= source_start:
            return meta
        fetch_start = (source_start // DOWNLOAD_BLOCK) * DOWNLOAD_BLOCK
        async with self._range_lock:
            meta = self.seek_info(camera.id, name)
            if meta is None or self._has_range(meta.get("ranges", []), start, end):
                return meta
            patch = str(meta.get("patch") or "")
            await camera.ensure_online()
            async with camera.exclusive("SD seek", pause_live=True):
                data = await self._download_range(
                    camera, patch, fetch_start, source_end,
                    max(8.0, (source_end - fetch_start) / 180_000 + 4))
                skip = max(0, ftyp_size - fetch_start)
                data = data[skip:]
                target_write = fetch_start + delta + skip
                self._write_at(path, target_write, data)
                meta["ranges"] = self._merge_range(
                    meta.get("ranges", []), target_write,
                    target_write + len(data))
                meta["updated"] = time.time()
                self._save_seek(meta_path, meta)
        return meta

    # ------------------------------------------------------------------ cache
    def _cleanup_cache(self) -> None:
        limit = int(self.config.data.get("cache_limit_mb", 4096)) * 1024 * 1024
        root = os.path.join(self.config.cache_dir(), "sd")
        files: list[tuple[float, int, str]] = []
        total = 0
        for dirpath, _dirs, names in os.walk(root):
            for name in names:
                path = os.path.join(dirpath, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                files.append((st.st_mtime, st.st_size, path))
                total += st.st_size
        if total <= limit:
            return
        files.sort()
        for _mtime, size, path in files:
            if total <= limit:
                break
            if path.endswith(".temp") and time.time() - _mtime < 3600:
                continue
            try:
                os.remove(path)
                total -= size
            except OSError:
                pass


class _BusyCapture(Exception):
    pass


def _sha1(path: str) -> str:
    import hashlib
    h = hashlib.sha1()
    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()
