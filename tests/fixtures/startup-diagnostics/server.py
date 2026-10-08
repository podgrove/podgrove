"""A non-root reader plus two services needing a file before they can become healthy."""
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import os
import sys

mode = sys.argv[1]
assert os.geteuid() == 1000, "fixture must run as the service user"
value = Path("/fixture/shared/middleware/value.txt").read_text()
assert value == "readable\n", "restricted source was not mirrored"
required = Path("/fixture/required.txt")
if mode != "readable" and not required.is_file():
    for number in range(45):
        print(f"{mode} boot line {number:02d}", flush=True)
    print(f"{mode}: missing /fixture/required.txt; create content/required.txt then run podgrove up", flush=True)
    if mode == "exited":
        raise SystemExit(7)
else:
    Path("/tmp/startup-ready").write_text("ready")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"podgrove-diagnostics-fixture\n")


HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
