"""极简的异步 HTTP 服务器，只够管理台和 OneBot 上报用：一次连接处理一个请求，只监听本机。"""
from __future__ import annotations

import asyncio
import json
import logging
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger(__name__)

REASONS = {200: "OK", 204: "No Content", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
           404: "Not Found", 405: "Method Not Allowed", 413: "Payload Too Large", 500: "Internal Server Error"}


class Request:
    def __init__(self, method, path, query, headers, body):
        self.method, self.path, self.query, self.headers, self.body = method, path, query, headers, body

    def json(self):
        return json.loads(self.body or b"{}")

    def cookie(self, name):
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("cookie", ""))
        except Exception:
            return ""
        return jar[name].value if name in jar else ""


class Response:
    def __init__(self, status=200, body=b"", content_type="application/json; charset=utf-8", headers=None):
        self.status = status
        self.body = body.encode() if isinstance(body, str) else body
        self.content_type = content_type
        self.headers = headers or {}


def json_response(obj, status=200, headers=None):
    return Response(status, json.dumps(obj, ensure_ascii=False), headers=headers)


class Server:
    def __init__(self, host, port, handler, max_body=1_000_000):
        self.host, self.port, self.handler, self.max_body = host, port, handler, max_body
        self._server = None

    async def start(self):
        self._server = await asyncio.start_server(self._conn, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]  # port=0 时拿到系统分配的端口

    async def close(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _conn(self, reader, writer):
        try:
            resp = await self._handle(reader)
            if resp is not None:
                head = [f"HTTP/1.1 {resp.status} {REASONS.get(resp.status, '')}",
                        f"Content-Length: {len(resp.body)}", "Connection: close",
                        "X-Content-Type-Options: nosniff", "Cache-Control: no-store"]
                if resp.body:
                    head.append(f"Content-Type: {resp.content_type}")
                head += [f"{k}: {v}" for k, v in resp.headers.items()]
                writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + resp.body)
                await writer.drain()
        except (ConnectionError, asyncio.TimeoutError):
            pass
        finally:
            writer.close()

    async def _handle(self, reader):
        try:
            line = await asyncio.wait_for(reader.readline(), 30)
            if not line:
                return None
            method, target, _ = line.decode("latin-1").split(" ", 2)
            headers = {}
            while True:
                h = await asyncio.wait_for(reader.readline(), 30)
                if h in (b"\r\n", b"\n", b""):
                    break
                key, _, value = h.decode("latin-1").partition(":")
                headers[key.strip().lower()] = value.strip()
            length = int(headers.get("content-length") or 0)
            if length > self.max_body:
                return json_response({"error": "请求太大"}, 413)
            body = await asyncio.wait_for(reader.readexactly(length), 30) if length else b""
        except (ValueError, asyncio.IncompleteReadError):
            return json_response({"error": "请求格式不对"}, 400)
        url = urlsplit(target)
        req = Request(method.upper(), url.path, parse_qs(url.query), headers, body)
        try:
            return await self.handler(req)
        except Exception:
            log.exception("处理 %s %s 时出错", req.method, req.path)
            return json_response({"error": "内部错误，详情看日志"}, 500)
