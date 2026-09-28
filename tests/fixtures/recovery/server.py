"""One fixture exits, another stays unhealthy, until a mirrored file appears."""
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import sys

mode = sys.argv[1]
required = Path("/fixture/required.txt")
if mode == "exited" and not required.is_file():
    raise SystemExit(7)
if mode == "healthy" or required.is_file():
    Path("/tmp/startup-ready").write_text("ready")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"podgrove-recovery-fixture\n")


HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
