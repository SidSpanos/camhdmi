"""web.py - phone control panel for camhdmi (LAN only).

GET  /             the panel page (index.html)
GET  /stream.mjpg  live preview (hardware JPEG from the camera pipeline)
GET  /events       Server-Sent Events: full state JSON whenever it changes
GET  /api/state    current state JSON
POST /api/action   {"a": "...", ...} -> runs on the GLib main loop

Runs in its own threads; anything that touches the camera or pipeline is
handed to the main loop with GLib.idle_add so GStreamer/GTK stay single-threaded.
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from gi.repository import GLib

HERE = os.path.dirname(os.path.abspath(__file__))


class Web:
    def __init__(self, app, port=8080):
        self.app = app
        self.cond = threading.Condition()
        self.jpeg, self.jseq = None, 0
        self.state_json, self.sver = "{}", 0
        self.httpd = ThreadingHTTPServer(("0.0.0.0", port), _handler(self))
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        print(f"web: panel on http://0.0.0.0:{port}/", flush=True)

    # called from the GStreamer streaming thread
    def push_jpeg(self, data):
        with self.cond:
            self.jpeg = data
            self.jseq += 1
            self.cond.notify_all()

    # called from the main loop
    def publish(self, state_json):
        if state_json == self.state_json:
            return
        with self.cond:
            self.state_json = state_json
            self.sver += 1
            self.cond.notify_all()

    def call(self, fn, *args):
        """Run fn on the main loop and wait (max 3 s) for its result."""
        done, res = threading.Event(), {}

        def run():
            try:
                res["ok"] = fn(*args)
            except Exception as e:  # report, never crash the main loop
                res["err"] = f"{type(e).__name__}: {e}"
            done.set()
            return False
        GLib.idle_add(run)
        done.wait(3)
        return res


def _handler(web):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif path == "/manifest.json":
                self._send(200, json.dumps({
                    "name": "camhdmi", "short_name": "camhdmi", "display": "standalone",
                    "background_color": "#111111", "theme_color": "#111111", "start_url": "/"}),
                    "application/manifest+json")
            elif path == "/api/state":
                self._send(200, web.state_json)
            elif path == "/stream.mjpg":
                self._mjpeg()
            elif path == "/events":
                self._events()
            else:
                self._send(404, '{"error":"not found"}')

        def do_POST(self):
            if self.path.split("?")[0] != "/api/action":
                return self._send(404, '{"error":"not found"}')
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return self._send(400, '{"error":"bad json"}')
            res = web.call(web.app.web_action, payload)
            self._send(200 if "err" not in res else 500, json.dumps(res))

        def _mjpeg(self):
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            last = -1
            try:
                while True:
                    with web.cond:
                        web.cond.wait_for(lambda: web.jseq != last, timeout=5)
                        if web.jseq == last or web.jpeg is None:
                            continue
                        data, last = web.jpeg, web.jseq
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                     + str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def _events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            last = -1
            try:
                while True:
                    with web.cond:
                        web.cond.wait_for(lambda: web.sver != last, timeout=15)
                        changed = web.sver != last
                        data, last = web.state_json, web.sver
                    self.wfile.write(f"data: {data}\n\n".encode() if changed else b": ping\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
    return H
