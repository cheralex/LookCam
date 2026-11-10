"""Viewing via WebCodecs: H.264 is decoded by the browser itself.

Why: our previous path (OpenCV -> resize -> JPEG -> MJPEG) adds
re-encoding and a frame buffer. Here the PC does not decode video at all: Annex B
bytes go to the browser, where the hardware `VideoDecoder`
(WebCodecs) parses them, like the native decoder in the application.

Stream format `/h264`: a sequence of `[u32 BE length][access unit]`,
where an access unit is one frame in Annex B (with SPS/PPS on a key frame).
"""
from __future__ import annotations

import socketserver
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import p2p
from . import sps as sps_mod

PAGE = """<!doctype html>
<meta charset="utf-8">
<title>LookCam</title>
<style>
  body { margin:0; background:#111; color:#ddd; font:13px system-ui,sans-serif }
  #wrap { position:relative; display:inline-block }
  canvas { display:block; max-width:100vw; max-height:100vh }
  #status { position:fixed; left:8px; top:6px; padding:2px 6px; border-radius:4px;
            background:rgba(0,0,0,.45); font:12px ui-monospace,monospace }
  #msg { padding:20px; line-height:1.5 }
</style>
<div id="wrap"><canvas id="c" width="1920" height="1080"></canvas></div>
<div id="status">…</div>
<div id="msg"></div>
<script>
const statusEl = document.getElementById('status');
const msgEl = document.getElementById('msg');
const canvas = document.getElementById('c');
const ctx = canvas.getContext('2d', { desynchronized: true });

function fail(text) {
  msgEl.innerHTML = text;
  statusEl.textContent = 'остановлено';
}

if (!('VideoDecoder' in window)) {
  fail('<b>Этот браузер не умеет WebCodecs.</b><br>' +
       'Откройте Chrome/Edge, либо запустите сервер с <code>--mode mjpeg</code>.');
} else {
  main();
}

async function main() {
  document.title = 'подключаюсь…';
  while (true) {
    try { await session(); } catch (e) {
      document.title = 'ошибка: ' + (e && e.message ? e.message : e);
    }
    if (document.title.indexOf('ошибка') < 0) document.title = 'переподключение…';
    await new Promise(r => setTimeout(r, 700));
  }
}

async function session() {
  let decoder = null;
  let frames = 0;
  let t0 = performance.now();
  let pending = new Uint8Array(0);
  // Время подачи каждого AU в декодер. Поток строго IPPP (max_num_ref_frames=1,
  // B-кадров нет), поэтому выход декодера идёт в том же порядке: N-й кадр на
  // выходе отвечает N-й подаче. Разность и есть задержка декодера.
  const submitted = [];
  let lat = [];

  function draw(frame) {
    if (canvas.width !== frame.displayWidth || canvas.height !== frame.displayHeight) {
      canvas.width = frame.displayWidth;
      canvas.height = frame.displayHeight;
    }
    ctx.drawImage(frame, 0, 0, canvas.width, canvas.height);
    frame.close();
    window.__drawn = (window.__drawn || 0) + 1;
    const nowMs = performance.now();
    const sub = submitted.shift();
    if (sub !== undefined) { lat.push(nowMs - sub); window.__lat = Math.round(nowMs - sub); }
    if (window.__lastDraw) {
      const gap = nowMs - window.__lastDraw;
      const since = nowMs - (window.__gapWindowStart || nowMs);
      if (since > 5000) { window.__gapWindowStart = nowMs; window.__maxGap = 0; }
      if (gap < 3000) window.__maxGap = Math.max(window.__maxGap || 0, gap);
    }
    window.__lastDraw = nowMs;
    frames++;
    if ((window.__drawn & 7) === 0) {
      fetch('/stat').then(r => r.json()).then(d => { window.__age = d.age_ms; })
        .catch(() => {});
    }
    const now = performance.now();
    if (now - t0 > 1000) {
      const l = lat.slice().sort((a, b) => a - b);
      const med = l.length ? Math.round(l[l.length >> 1]) : 0;
      const mx = l.length ? Math.round(l[l.length - 1]) : 0;
      const text =
        `${(frames * 1000 / (now - t0)).toFixed(1)} кадр/с  ${canvas.width}x${canvas.height}` +
        `  декодер ${med}/${mx} мс  очередь ${submitted.length}`;
      statusEl.textContent = text;
      document.title = text;
      frames = 0;
      lat = [];
      t0 = now;
    }
  }

  function codecFromSps(bytes) {
    // ищем SPS: start code + тип NAL 7 (байт 0x27 у этой камеры, бывает и 0x67)
    for (let i = 0; i + 8 < bytes.length; i++) {
      if (bytes[i] === 0 && bytes[i+1] === 0 && bytes[i+2] === 0 &&
          bytes[i+3] === 1 && (bytes[i+4] & 0x1f) === 7) {
        const hex = (n) => n.toString(16).padStart(2, '0');
        return 'avc1.' + hex(bytes[i+5]) + hex(bytes[i+6]) + hex(bytes[i+7]);
      }
    }
    return null;
  }

  const res = await fetch('/h264', { cache: 'no-store' });
  if (!res.ok || !res.body) throw new Error('нет /h264');
  const reader = res.body.getReader();
  document.title = 'поток открыт, ждём данные';
  let totalBytes = 0;
  let auCount = 0;
  let lastTitle = performance.now();

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    if (decoder && decoder.state === 'closed') break;
    totalBytes += value.length;
    if (performance.now() - lastTitle > 300) {
      document.title = `${(totalBytes / 1024).toFixed(0)} КБ, AU ${auCount}, ` +
                       `кадров ${window.__drawn || 0}, возраст ${window.__age || '?'} мс, ` +
                       `пауза ${Math.round(window.__maxGap || 0)} мс` +
                       (decoder ? `, ${decoder.state}` : '');
      lastTitle = performance.now();
    }

    const merged = new Uint8Array(pending.length + value.length);
    merged.set(pending, 0);
    merged.set(value, pending.length);
    pending = merged;

    while (pending.length >= 4) {
      const size = (pending[0] * 0x1000000) + (pending[1] << 16) +
                   (pending[2] << 8) + pending[3];
      if (pending.length < 4 + size) break;
      const au = pending.slice(4, 4 + size);
      pending = pending.slice(4 + size);
      auCount++;

      if (!decoder) {
        const codec = codecFromSps(au);
        if (!codec) continue;                 // ждём ключевой кадр с SPS
        decoder = new VideoDecoder({
          output: draw,
          error: (e) => { statusEl.textContent = 'декодер: ' + e.message; },
        });
        decoder.configure({ codec, optimizeForLatency: true });
      }
      if (decoder.state !== 'configured') continue;
      const nalType = au[4];
      const isKey = (nalType === 0x27 || nalType === 0x67 || nalType === 0x25 ||
                     nalType === 0x65);
      try {
        submitted.push(performance.now());
        decoder.decode(new EncodedVideoChunk({
          type: isKey ? 'key' : 'delta',
          timestamp: Math.round(performance.now() * 1000),
          data: au,
        }));
      } catch (e) {
        submitted.pop();
        statusEl.textContent = 'decode: ' + e.message;
      }
    }
    // flush() здесь звать нельзя: по спецификации он выставляет «нужен ключевой
    // кадр», и все дельта-кадры до следующего IDR декодер отвергает.
    // Задержку переупорядочивания снимаем иначе — чиня SPS, см. sps.py.
  }
  if (decoder && decoder.state !== 'closed') decoder.close();
}
</script>
"""


class FrameWriter:
    """Assembles access units (Annex B) from camera fragments and feeds them to the sink.

    A frame goes to the sink as soon as the length declared in its header has been collected.
    Waiting for the next frame's magic is not allowed: a ready frame would sit for one more
    inter-frame interval (on the media_capture recording — median 89 ms, p90 161 ms).
    """

    GAP_TIMEOUT = 0.5

    # Frame header layout — verified on 592 frames of media_capture.lcraw,
    # including all 20 key frames: frame length == u32LE(79) + 87.
    HEADER_LEN = 83          # container header, immediately followed by the start code
    LEN_OFFSET = 79          # u32LE: frame length minus LEN_EXTRA
    LEN_EXTRA = 87           # HEADER_LEN + 4 bytes of the start code
    MAX_AU = 4 << 20

    def __init__(self, sink):
        self.sink = sink
        self.frames = 0
        self.dropped = 0
        self.gaps = 0
        self.resyncs = 0
        self._started = False
        self._resync = False
        self._next = 0
        self._pending: dict[int, bytes] = {}
        self._au: bytearray | None = None
        self._au_target: int | None = None
        self._last = time.time()

    def feed(self, frag: int, data: bytes) -> None:
        keyframe = (data.startswith(p2p.FRAME_MAGIC)
                    and (p2p.SPS_NAL in data or p2p.SPS_NAL_ALT in data))
        if not self._started:
            if keyframe:
                self._started = True
                self._next = frag
            else:
                self.dropped += 1
                return
        if self._resync:
            if keyframe:
                self._leave_resync(frag, data)
            return
        if frag < self._next - 1000:
            self._enter_resync()
            if keyframe:
                self._leave_resync(frag, data)
            return
        if frag < self._next:
            return
        self._pending.setdefault(frag, data)
        self._flush()

    def _enter_resync(self) -> None:
        self._resync = True
        self.gaps += len(self._pending)
        self._pending.clear()
        self._au = None                 # discard the incompletely assembled frame
        self._au_target = None
        self.resyncs += 1

    def _leave_resync(self, frag: int, data: bytes) -> None:
        self._resync = False
        self._next = frag
        self._emit(data)
        self._next += 1
        self._last = time.time()

    def _flush(self) -> None:
        while True:
            data = self._pending.pop(self._next, None)
            if data is None:
                if self._pending and time.time() - self._last > self.GAP_TIMEOUT:
                    self._enter_resync()
                return
            self._emit(data)
            self._next += 1
            self._last = time.time()

    def _emit(self, data: bytes) -> None:
        if data.startswith(p2p.FRAME_MAGIC):
            self._flush_au()
            pos = p2p.nal_start(data)
            if pos < 0 or pos >= len(data):
                return
            self._au = bytearray(data[pos:])
            self._au_target = self._declared_au_len(data, pos)
            self.frames += 1
            self._emit_if_complete()
        elif self._au is not None:
            self._au += data
            self._emit_if_complete()

    def _declared_au_len(self, data: bytes, pos: int) -> int | None:
        """Access unit length from the frame header; None — if the layout is not the expected one."""
        if pos != self.HEADER_LEN or len(data) < self.LEN_OFFSET + 4:
            return None
        size = struct.unpack_from("<I", data, self.LEN_OFFSET)[0] + self.LEN_EXTRA - pos
        return size if 0 < size <= self.MAX_AU else None

    def _emit_if_complete(self) -> None:
        if self._au is not None and self._au_target is not None \
                and len(self._au) >= self._au_target:
            self._flush_au()

    def _flush_au(self) -> None:
        if self._au:
            self.sink(bytes(self._au))
        self._au = None
        self._au_target = None

    def close(self) -> None:
        self._flush()
        self._flush_au()


class Feed:
    """Byte accumulator of the stream: a consumer reads as data arrives.

    The stream is a sequence of records `[u32 length][AU]`. We keep marks of record
    starts: a new reader starts exactly at a record boundary, otherwise it would parse
    garbage. When trimming the buffer we also cut only at a mark.
    """

    CAP = 2 * 1024 * 1024

    def __init__(self) -> None:
        self._buf = bytearray()
        self._base = 0                      # how many bytes dropped from the start
        self._marks = [0]                   # absolute record starts
        self._cond = threading.Condition()
        self._stop = False

    def feed(self, data: bytes) -> None:
        with self._cond:
            self._buf += data
            self._marks.append(self._base + len(self._buf))
            if len(self._buf) > self.CAP:
                target = self._base + len(self._buf) - self.CAP // 2
                while len(self._marks) > 1 and self._marks[0] < target:
                    self._marks.pop(0)
                new_base = min(self._marks[0], self._base + len(self._buf))
                drop = new_base - self._base
                if drop > 0:
                    del self._buf[:drop]
                    self._base = new_base
            self._cond.notify_all()

    def aligned_end(self) -> int:
        """Position from which parsing can start (a record boundary)."""
        with self._cond:
            end = self._base + len(self._buf)
            return self._marks[-1] if self._marks and self._marks[-1] == end else end

    def stop(self) -> None:
        self._stop = True
        with self._cond:
            self._cond.notify_all()

    @property
    def stopped(self) -> bool:
        return self._stop

    def wait_chunk(self, pos: int) -> tuple[bytes, int]:
        """Return (data, new absolute position)."""
        with self._cond:
            if pos < self._base:
                pos = self._base        # fell behind — read from the buffer boundary
            if pos >= self._base + len(self._buf):
                self._cond.wait(0.2)
                if pos < self._base:
                    pos = self._base
            start = pos - self._base
            chunk = bytes(self._buf[start:])
            return chunk, pos + len(chunk)


class _Server(ThreadingHTTPServer):
    """HTTP server without reverse DNS (otherwise it fails on a non-ASCII hostname)."""

    daemon_threads = True

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = "localhost"
        self.server_port = port


class RawVideo:
    """Camera -> access units -> browser (WebCodecs), without decoding on the PC."""

    def __init__(self, host: str, password: str, source_ip: str | None = None,
                 stream: int = 1, state: int = 1, bind: str = "127.0.0.1",
                 port: int = 8081, verbose: bool = True, patch_sps: bool = True):
        self.host = host
        self.password = password
        self.source_ip = source_ip
        self.stream = stream
        self.state = state
        self.bind = bind
        self.port = port
        self.verbose = verbose
        self.patch_sps = patch_sps
        self.error: str | None = None

        self.sps_patched = 0
        self.frames_total = 0
        self._last_send = 0.0
        self._feed = Feed()
        self._stop = threading.Event()
        self._server: _Server | None = None
        self._threads: list[threading.Thread] = []

    def log(self, text: str) -> None:
        if self.verbose:
            print(text, flush=True)

    # ----------------------------------------------------------- startup
    def start(self) -> "RawVideo":
        self._server = self._make_server()
        threading.Thread(target=self._server.serve_forever, daemon=True,
                         name="lookcam-http").start()
        self._threads = [threading.Thread(target=self._capture, daemon=True,
                                          name="lookcam-capture")]
        for t in self._threads:
            t.start()
        return self

    @property
    def url(self) -> str:
        port = self._server.server_address[1] if self._server else self.port
        return f"http://{self.bind}:{port}/"

    def stop(self) -> None:
        self._stop.set()
        self._feed.stop()
        for t in self._threads:
            if t.is_alive():
                t.join(timeout=1.0)
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass

    # -------------------------------------------------------- HTTP server
    def _make_server(self) -> _Server:
        feed = self._feed
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def handle_one_request(self):
                """The browser dropping the connection is normal, do not dump a traceback."""
                try:
                    super().handle_one_request()
                except OSError:
                    self.close_connection = True

            def do_GET(self):                     # noqa: N802
                if self.path.startswith("/stat"):
                    age = max(0.0, (time.time() - owner._last_send) * 1000)
                    body = f'{{"age_ms": {age:.0f}}}'.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if not self.path.startswith("/h264"):
                    body = PAGE.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path.startswith("/h264"):
                    self.send_response(200)
                    self.send_header("Content-Type", "video/h264")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    pos = feed.aligned_end()      # live stream only, from a record boundary
                    try:
                        while not feed.stopped:
                            chunk, new_pos = feed.wait_chunk(pos)
                            if not chunk:
                                continue
                            pos = new_pos
                            self.wfile.write(f"{len(chunk):x}\r\n".encode())
                            self.wfile.write(chunk)
                            self.wfile.write(b"\r\n")
                            self.wfile.flush()
                    except OSError:
                        return
                    return
                self.send_error(404)

        return _Server((self.bind, self.port), Handler)

    # ------------------------------------------------------------ capture
    def _capture(self) -> None:
        client = None
        try:
            client = self._connect()
            writer = FrameWriter(self._sink)
            stream = p2p.MediaStream(client)
            stream.recorder = writer
            client.send_repeated({"cmd": "OpenVideo", "pwd": self.password,
                                  "state": self.state, "stream": self.stream,
                                  "userid": 45})
            self.log("  OpenVideo : запрошен поток, ждём кадры…")
            while not self._stop.is_set():
                stream.pump(0.01)
            writer.close()
        except Exception as exc:
            self.error = str(exc)
            self.log(f"  ОШИБКА захвата: {exc}")
        finally:
            try:
                if client is not None:
                    client.close()
            except Exception:
                pass
            self._stop.set()

    def _sink(self, au: bytes) -> None:
        """Hand the access unit over with a 4-byte length — that is easier for the browser to parse."""
        if self.patch_sps:
            fixed = sps_mod.patch_au(au)
            if fixed is not au and fixed != au:
                self.sps_patched += 1
                if self.sps_patched == 1:
                    self.log("  SPS       : объявили max_num_reorder_frames = 0 "
                             "(камера не объявляет — декодер мог придерживать кадры)")
                au = fixed
        self.frames_total += 1
        self._last_send = time.time()
        self._feed.feed(struct.pack(">I", len(au)) + au)

    def _connect(self) -> p2p.P2PClient:
        client = p2p.P2PClient(self.host, source_ip=self.source_ip)
        t0 = time.time()
        addr = client.discover()
        self.log(f"  камера    : {addr[0]}:{addr[1]} (поиск {time.time() - t0:.2f} с)")
        t1 = time.time()
        if not client.punch():
            raise RuntimeError("камера не ответила на punch (занята?)")
        self.log(f"  punch     : P2PRdy за {time.time() - t1:.2f} с")
        self._command(client, {"pwd": self.password, "cmd": "LoginDev"}, "LoginDev", 1.5)
        self._command(client, {"cmd": "AppointDev", "pwd": self.password, "state": 3,
                               "time": int(time.time()), "zone": 3, "zone_s": 10800},
                      "AppointDev", 1.2)
        return client

    @staticmethod
    def _command(client: p2p.P2PClient, obj: dict, expect: str, timeout: float) -> dict | None:
        for _ in range(10):
            client.send_command(obj)
            time.sleep(0.011)
        deadline = time.time() + timeout
        while time.time() < deadline:
            for msg in client.read_messages(0.1):
                if isinstance(msg, dict) and msg.get("cmd") == expect:
                    return msg
        return None
