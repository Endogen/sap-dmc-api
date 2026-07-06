#!/usr/bin/env python3
"""Lightweight web server for browsing SAP DMC API specs with Swagger UI."""
from __future__ import annotations

import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"
SPECS_DIR = OUTPUT_DIR / "specs"
TEMPLATES_DIR = ROOT / "templates"
STATIC_DIR = ROOT / "static"
COLLECTIONS_DIR = OUTPUT_DIR / "collections"

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", 8080))


class APIHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        path = unquote(self.path.split("?")[0])

        if path == "/" or path == "/index.html":
            self._serve_file(TEMPLATES_DIR / "index.html", "text/html")
        elif path == "/favicon.ico" or path == "/favicon.png":
            self._serve_file(STATIC_DIR / "favicon.png", "image/png")
        elif path == "/logo.png":
            self._serve_file(STATIC_DIR / "logo.png", "image/png")
        elif path == "/summary.json":
            self._serve_file(OUTPUT_DIR / "summary.json", "application/json")
        elif path == "/changelog.json":
            self._serve_file(OUTPUT_DIR / "changelog.json", "application/json")
        elif path.startswith("/specs/") and path.endswith(".json"):
            self._serve_from_dir(SPECS_DIR, path[7:], "application/json")
        elif path.startswith("/collections/") and path.endswith(".json"):
            self._serve_from_dir(COLLECTIONS_DIR, path[13:], "application/json")
        else:
            self.send_error(404)

    def _serve_from_dir(self, base_dir: Path, name: str, content_type: str):
        # Resolve and confine to base_dir — blocks ../ path traversal
        target = (base_dir / name).resolve()
        if not target.is_relative_to(base_dir.resolve()):
            self.send_error(404)
            return
        self._serve_file(target, content_type)

    def _serve_file(self, filepath: Path, content_type: str):
        try:
            data = filepath.read_bytes()
        except FileNotFoundError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        # Quieter logging — only show path, not full HTTP line
        sys.stderr.write(f"  {args[0]}\n")


def main():
    server = ThreadingHTTPServer((HOST, PORT), APIHandler)
    print(f"SAP DMC API Browser")
    print(f"  http://{HOST}:{PORT}")
    print(f"  Serving {len(list(SPECS_DIR.glob('*.json')))} API specs")
    print(f"  Press Ctrl+C to stop\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
