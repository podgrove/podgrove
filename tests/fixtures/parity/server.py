"""Dependency-free HTTP and WebSocket fixture; all credentials are public test values."""

import base64
import hashlib
import json
import os
from pathlib import Path
import runpy
import struct
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def read(path):
    source = Path(path)
    return source.read_text() if source.exists() else None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def respond(self, value):
        body = json.dumps(value, sort_keys=True).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/ws":
            self.websocket()
            return
        if self.path == "/health":
            self.respond({"healthy": True})
            return
        if self.path == "/counter":
            self.respond({"count": int(read("/state/count") or "0")})
            return
        self.respond({
            "message": read("/app/content/message.txt"),
            "python_value": runpy.run_path("/app/content/live.py")["VALUE"],
            "single": read("/app/single.txt"),
            "config": read("/app/config.txt"),
            "secret": read("/run/secrets/demo"),
            "overlay": read("/app/overlay.txt"),
            "initialized": read("/state/initialized"),
            "mode": os.getenv("API_MODE"),
            "env_file": os.getenv("FIXTURE_ENV"),
            "optional": os.getenv("OPTIONAL_VALUE"),
            "files": sorted(str(p.relative_to("/app/content")) for p in Path("/app/content").rglob("*") if p.is_file()),
        })

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        path = Path("/state/count")
        count = int(read(path) or "0") + 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(count))
        self.respond({"count": count})

    def websocket(self):
        key = self.headers["Sec-WebSocket-Key"]
        digest = hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", base64.b64encode(digest).decode())
        self.end_headers()
        head = self.rfile.read(2)
        length = head[1] & 127
        if length == 126:
            length = struct.unpack("!H", self.rfile.read(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self.rfile.read(8))[0]
        mask = self.rfile.read(4) if head[1] & 128 else b""
        data = self.rfile.read(length)
        if mask:
            data = bytes(value ^ mask[index % 4] for index, value in enumerate(data))
        prefix = bytes([129, len(data)]) if len(data) < 126 else bytes([129, 126]) + struct.pack("!H", len(data))
        self.wfile.write(prefix + data)
        self.wfile.flush()
        self.close_connection = True


ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
