#!/usr/bin/env python3
"""Rundash server — serves the pre-rendered dashboard HTML. Static, no state
to persist; src.dashboard (run by ai-coach-dashboard.service, chained off
ai-coach-poll.service via OnSuccess=) regenerates the file this serves after
every Garmin poll.

No dependencies beyond Python's standard library.

Usage:
    python3 server.py            # runs on port 8082
    python3 server.py 3000       # runs on a custom port
"""

import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8082
DASHBOARD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "dashboard")
os.makedirs(DASHBOARD_DIR, exist_ok=True)  # fresh clone: nothing generated yet on first boot
os.chdir(DASHBOARD_DIR)


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("", "/"):
            self.path = "/index.html"
        return super().do_GET()

    def log_message(self, format, *args):
        # Quieter logs — only non-200s are worth seeing on a page nobody else hits.
        if "200" not in (args[1] if len(args) > 1 else "200"):
            super().log_message(format, *args)


if __name__ == "__main__":
    print(f"Rundash serving on :{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
