"""Asyncio HTTP/1.1 + WebSocket (RFC 6455) with no third-party libraries.

`http.server` is synchronous and has no WebSocket, so the service needs its
own small server. It does exactly what is needed: keep-alive, JSON handlers,
Range requests for files (for `<video>` and downloads), static files, WebSocket.

The request body is read only by `Content-Length` (no chunked requests),
and file responses go in 256 KB chunks, so a 30 MB video never ends up
in memory as a whole.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import mimetypes
import os
import re
import struct
import urllib.parse

log = logging.getLogger("lookcam.http")

MAX_HEADER = 64 * 1024
MAX_BODY = 32 * 1024 * 1024
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# ---------------------------------------------------------------------- request

class Request:
    """A parsed HTTP request."""

    def __init__(self, method: str, target: str, version: str,
                 headers: dict[str, str], body: bytes, client: tuple) -> None:
        self.method = method.upper()
        self.target = target
        self.version = version
        self.headers = headers
        self.body = body
        self.client = client
        parsed = urllib.parse.urlsplit(target)
        self.path = urllib.parse.unquote(parsed.path) or "/"
        self.raw_query = parsed.query
        self.query: dict[str, str] = {}
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
            self.query.setdefault(key, value)
        self.websocket: "WebSocket | None" = None

    @property
    def ip(self) -> str:
        return self.client[0] if self.client else "-"

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)

    def json(self) -> dict:
        if not self.body:
            return {}
        try:
            data = json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise BadRequest(f"некорректный JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise BadRequest("ожидался JSON-объект")
        return data

    def int_arg(self, name: str, default: int = 0) -> int:
        try:
            return int(self.query.get(name, default))
        except (TypeError, ValueError):
            return default

    @property
    def is_websocket(self) -> bool:
        return (self.header("upgrade").lower() == "websocket"
                and "upgrade" in self.header("connection").lower())


class BadRequest(ValueError):
    pass


# ---------------------------------------------------------------------- response

class Response:
    """Response: JSON/text/bytes or a file (with Range)."""

    def __init__(self, status: int = 200, body: bytes = b"",
                 headers: dict | None = None, file_path: str | None = None,
                 file_offset: int = 0, file_length: int = 0) -> None:
        self.status = int(status)
        self.body = body
        self.headers = dict(headers or {})
        self.file_path = file_path
        self.file_offset = file_offset
        self.file_length = file_length

    @classmethod
    def json(cls, data, status: int = 200, headers: dict | None = None) -> "Response":
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        hdrs = {"Content-Type": "application/json; charset=utf-8",
                "Cache-Control": "no-store"}
        hdrs.update(headers or {})
        return cls(status, body, hdrs)

    @classmethod
    def text(cls, text: str, status: int = 200,
             content_type: str = "text/plain; charset=utf-8") -> "Response":
        return cls(status, text.encode("utf-8"), {"Content-Type": content_type})

    @classmethod
    def error(cls, status: int, message: str,
              headers: dict | None = None) -> "Response":
        return cls.json({"error": message, "status": status}, status, headers)

    @classmethod
    def redirect(cls, location: str, status: int = 302) -> "Response":
        return cls(status, b"", {"Location": location})

    @classmethod
    def file(cls, req: Request, path: str, content_type: str | None = None,
             download_name: str | None = None,
             range_override: tuple[int, int] | None = None) -> "Response":
        """File with Range support (206) and "Save as"."""
        if not os.path.isfile(path):
            return cls.error(404, "файл не найден")
        size = os.path.getsize(path)
        ctype = content_type or mimetypes.guess_type(path)[0] or \
            "application/octet-stream"
        if (ctype.startswith("text/") or ctype == "application/javascript") \
                and "charset=" not in ctype:
            ctype += "; charset=utf-8"
        headers = {"Content-Type": ctype, "Accept-Ranges": "bytes",
                   "Cache-Control": "no-cache"}
        if download_name:
            quoted = urllib.parse.quote(download_name)
            headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quoted}"
        start, end = 0, size - 1
        status = 200
        if range_override is not None:
            start, end = int(range_override[0]), int(range_override[1])
            start = max(0, min(size - 1, start))
            end = max(start, min(size - 1, end))
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        else:
            range_header = req.header("range")
            if range_header:
                m = re.match(r"bytes=(\d*)-(\d*)", range_header.strip())
                if m:
                    if m.group(1):
                        start = int(m.group(1))
                        if m.group(2):
                            end = min(int(m.group(2)), size - 1)
                    elif m.group(2):          # suffix range: the last N bytes
                        start = max(0, size - int(m.group(2)))
                    if 0 <= start <= end < size:
                        status = 206
                        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
                    else:
                        return cls(416, b"", {"Content-Range": f"bytes */{size}"})
        length = max(0, end - start + 1)
        headers["Content-Length"] = str(length)
        return cls(status, b"", headers, file_path=path,
                   file_offset=start, file_length=length)


# ---------------------------------------------------------------------- WebSocket

class WebSocket:
    """Server side of RFC 6455: text/binary frames, ping/pong, close."""

    def __init__(self, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter, req: Request) -> None:
        self.reader = reader
        self.writer = writer
        self.req = req
        self.closed = False
        self._wlock = asyncio.Lock()
        self._frag_opcode = 0
        self._frag = bytearray()

    async def handshake(self) -> bool:
        key = self.req.header("sec-websocket-key")
        if not key:
            return False
        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")
        head = ("HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n")
        try:
            self.writer.write(head.encode("ascii"))
            await self.writer.drain()
        except (ConnectionError, OSError):
            return False
        return True

    async def send_bytes(self, data: bytes) -> bool:
        return await self._send(0x2, data)

    async def send_text(self, text: str) -> bool:
        return await self._send(0x1, text.encode("utf-8"))

    async def send_json(self, obj) -> bool:
        return await self.send_text(json.dumps(obj, ensure_ascii=False,
                                              separators=(",", ":")))

    async def _send(self, opcode: int, data: bytes) -> bool:
        if self.closed:
            return False
        header = bytearray([0x80 | opcode])
        length = len(data)
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header += struct.pack(">H", length)
        else:
            header.append(127)
            header += struct.pack(">Q", length)
        async with self._wlock:
            try:
                self.writer.write(bytes(header) + data)
                await self.writer.drain()
                return True
            except (ConnectionError, OSError):
                self.closed = True
                return False

    async def recv(self) -> tuple[int, bytes] | None:
        """The next message (opcode, data) or None on close."""
        while not self.closed:
            try:
                head = await self.reader.readexactly(2)
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                self.closed = True
                return None
            b0, b1 = head[0], head[1]
            opcode = b0 & 0x0F
            masked = bool(b1 & 0x80)
            length = b1 & 0x7F
            try:
                if length == 126:
                    length = struct.unpack(">H", await self.reader.readexactly(2))[0]
                elif length == 127:
                    length = struct.unpack(">Q", await self.reader.readexactly(8))[0]
                if length > (4 << 20):
                    await self.close(1009, "too big")
                    return None
                mask = await self.reader.readexactly(4) if masked else b""
                payload = await self.reader.readexactly(length) if length else b""
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                self.closed = True
                return None
            if masked:
                payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
            if opcode == 0x8:                    # close
                try:
                    await self._send(0x8, payload[:2])
                except Exception:
                    pass
                self.closed = True
                return None
            if opcode == 0x9:                    # ping -> pong
                await self._send(0xA, payload)
                continue
            if opcode == 0xA:                    # pong
                continue
            if opcode == 0x0:                    # continuation
                self._frag += payload
                if b0 & 0x80:
                    data = bytes(self._frag)
                    self._frag.clear()
                    return self._frag_opcode or 0x1, data
                continue
            self._frag_opcode = opcode
            if b0 & 0x80:
                return opcode, payload
            self._frag = bytearray(payload)
        return None

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed:
            return
        try:
            await self._send(0x8, struct.pack(">H", code) + reason.encode("utf-8")[:120])
        except Exception:
            pass
        self.closed = True
        try:
            self.writer.close()
        except Exception:
            pass


# ---------------------------------------------------------------------- server

class Router:
    """Routes like `/api/cameras/{id}/info`."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern, object]] = []
        self.fallback = None

    def add(self, method: str, pattern: str, handler) -> None:
        regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")
        self.routes.append((method.upper(), regex, handler))

    def get(self, pattern: str, handler) -> None:
        self.add("GET", pattern, handler)

    def post(self, pattern: str, handler) -> None:
        self.add("POST", pattern, handler)

    def put(self, pattern: str, handler) -> None:
        self.add("PUT", pattern, handler)

    def patch(self, pattern: str, handler) -> None:
        self.add("PATCH", pattern, handler)

    def delete(self, pattern: str, handler) -> None:
        self.add("DELETE", pattern, handler)

    async def dispatch(self, req: Request):
        method = "GET" if req.method == "HEAD" else req.method
        for route_method, regex, handler in self.routes:
            m = regex.match(req.path)
            if m and route_method == method:
                try:
                    result = await handler(req, **m.groupdict())
                except BadRequest as exc:
                    return Response.error(400, str(exc))
                except PermissionError:
                    return Response.error(403, "доступ запрещён")
                except Exception as exc:
                    log.exception("handler %s %s", req.method, req.path)
                    return Response.error(500, f"внутренняя ошибка: {exc}")
                if result is None:
                    return Response.error(500, "обработчик не вернул ответ")
                return result
        if self.fallback is not None:
            return await self.fallback(req)
        return Response.error(404, "не найдено")


async def _read_request(reader: asyncio.StreamReader,
                        client) -> Request | None:
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError,
            asyncio.TimeoutError, ConnectionError, OSError):
        return None
    if len(head) > MAX_HEADER:
        return None
    try:
        text = head.decode("utf-8", "replace")
        lines = text.split("\r\n")
        method, target, version = lines[0].split(" ", 2)
    except (UnicodeDecodeError, ValueError):
        return None
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    body = b""
    length = 0
    try:
        length = int(headers.get("content-length", "0") or 0)
    except ValueError:
        return None
    if length > MAX_BODY:
        return None
    if length:
        try:
            body = await asyncio.wait_for(reader.readexactly(length), 30)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError,
                ConnectionError, OSError):
            return None
    return Request(method, target, version, headers, body, client)


async def _write_response(writer: asyncio.StreamWriter, req: Request,
                          response: Response) -> bool:
    """Write the response. True means the connection can be closed."""
    bodyless = req.method == "HEAD" or response.status in (204, 304)
    headers = dict(response.headers)
    if "Content-Length" not in headers and response.file_path is None:
        headers["Content-Length"] = str(len(response.body))
    headers.setdefault("Server", "LookCamService/0.1")
    if req.header("connection").lower() == "close":
        headers["Connection"] = "close"
        close = True
    else:
        close = False
    reason = {200: "OK", 206: "Partial Content", 401: "Unauthorized",
              403: "Forbidden", 404: "Not Found", 400: "Bad Request",
              500: "Internal Server Error", 416: "Range Not Satisfiable",
              501: "Not Implemented"}.get(response.status, "OK")
    lines = [f"HTTP/1.1 {response.status} {reason}"]
    for key, value in headers.items():
        lines.append(f"{key}: {value}")
    try:
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1", "replace"))
        if not bodyless:
            if response.file_path is not None:
                remaining = response.file_length
                offset = response.file_offset
                with open(response.file_path, "rb") as fh:
                    fh.seek(offset)
                    while remaining > 0:
                        chunk = fh.read(min(256 * 1024, remaining))
                        if not chunk:
                            break
                        writer.write(chunk)
                        remaining -= len(chunk)
                        await writer.drain()
            elif response.body:
                writer.write(response.body)
        await writer.drain()
    except (ConnectionError, OSError):
        close = True
    return close


class HTTPServer:
    """Server: router, static files, WebSocket handshake."""

    def __init__(self, router: Router, host: str = "0.0.0.0", port: int = 8080,
                 static_dir: str | None = None, max_connections: int = 200) -> None:
        self.router = router
        self.host = host
        self.port = int(port)
        self.static_dir = os.path.abspath(static_dir) if static_dir else None
        self.server: asyncio.AbstractServer | None = None
        self.max_connections = max_connections
        self._conns = 0
        self._tasks: set[asyncio.Task] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        if static_dir:
            router.fallback = self.static

    async def start(self) -> "HTTPServer":
        self.server = await asyncio.start_server(
            self._client, self.host, self.port, limit=MAX_HEADER * 2, backlog=64)
        if self.port == 0:
            self.port = self.server.sockets[0].getsockname()[1]
        log.info("HTTP listening on http://%s:%s", self.host, self.port)
        return self

    async def close(self) -> None:
        tasks = [t for t in self._tasks if t is not asyncio.current_task()]
        for writer in list(self._writers):
            with contextlib.suppress(Exception):
                writer.close()
        for task in tasks:
            task.cancel()
        if self.server is not None:
            self.server.close()
            try:
                await self.server.wait_closed()
            except Exception:
                pass
            self.server = None
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._writers.clear()
        self._tasks.clear()

    async def _client(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        if self._conns >= self.max_connections:
            writer.close()
            return
        self._conns += 1
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        self._writers.add(writer)
        client = writer.get_extra_info("peername") or ("-", 0)
        try:
            while True:
                req = await _read_request(reader, client)
                if req is None:
                    return
                if req.is_websocket:
                    ws = WebSocket(reader, writer, req)
                    if not await ws.handshake():
                        return
                    req.websocket = ws
                    response = await self.router.dispatch(req)
                    if isinstance(response, Response) and response.status >= 400:
                        await _write_response(writer, req, response)
                    if not ws.closed:
                        await ws.close()
                    return
                if "expect" in req.headers and req.headers["expect"].lower() == \
                        "100-continue" and not req.body:
                    writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
                    await writer.drain()
                response = await self.router.dispatch(req)
                close = await _write_response(writer, req, response)
                if close:
                    return
        except (ConnectionError, OSError, asyncio.IncompleteReadError,
                asyncio.CancelledError):
            return
        except Exception:
            log.exception("HTTP connection %s", client)
        finally:
            self._conns -= 1
            self._writers.discard(writer)
            if task is not None:
                self._tasks.discard(task)
            try:
                writer.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ static
    async def static(self, req: Request) -> Response:
        if req.method not in ("GET", "HEAD"):
            return Response.error(405, "метод не поддерживается")
        if not self.static_dir:
            return Response.error(404, "статика не настроена")
        rel = req.path.lstrip("/")
        if not rel or rel.endswith("/"):
            rel += "index.html"
        candidate = os.path.abspath(os.path.join(self.static_dir, rel))
        if os.path.commonpath([self.static_dir, candidate]) != self.static_dir:
            return Response.error(403, "выход за каталог статики")
        if os.path.isdir(candidate):
            candidate = os.path.join(candidate, "index.html")
        if not os.path.isfile(candidate):
            return Response.error(404, "не найдено")
        return Response.file(req, candidate)
