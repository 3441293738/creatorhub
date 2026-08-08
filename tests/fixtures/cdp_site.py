from __future__ import annotations

import base64
import threading
from collections import Counter
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII=")


class FixtureSite:
    def __init__(self):
        self.counts = Counter()
        self.lock = threading.Lock()
        self.server = None
        self.thread = None

    @property
    def base_url(self):
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def start(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                with owner.lock:
                    owner.counts[path] += 1
                if path == "/pixel.png":
                    body, content_type = _PNG, "image/png"
                elif path == "/font.woff2":
                    body, content_type = b"fixture-font", "font/woff2"
                else:
                    body = b"""<!doctype html><meta charset=utf-8>
                    <style>@font-face{font-family:f;src:url('/font.woff2')}
                    body{font-family:f,sans-serif;height:2400px}</style>
                    <img id=asset src=/pixel.png><input id=short>
                    <textarea id=long></textarea><button id=submit>submit</button>
                    <div id=sentinel style='margin-top:1800px'>sentinel</div>"""
                    content_type = "text/html; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def close(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=3)


@contextmanager
def running_fixture_site():
    site = FixtureSite().start()
    try:
        yield site
    finally:
        site.close()
