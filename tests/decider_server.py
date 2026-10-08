"""A mocked decider server on an ephemeral loopback port, for tests."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from jevhl.prompt import question_text

Q60 = question_text(60)


def good_body(p_up=0.3832, question=Q60):
    choice = "up" if p_up >= 0.5 else "down"
    return {question: {"choice": choice, "confidence": 0.6, "type": "choice",
                       "probabilities": {"up": p_up, "down": 1 - p_up}}}


class FakeDecider:
    """mode: 'ok' | 'sleep' (waits for release) | 'status' | 'raw'. Records every request."""

    def __init__(self, body=None, mode="ok", status=200, raw=b"", delay_s=0.0):
        self.body = good_body() if body is None else body
        self.mode, self.status, self.raw, self.delay_s = mode, status, raw, delay_s
        self.requests: list[dict] = []
        self.release = threading.Event()
        self.arrived = threading.Event()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self._send(200, json.dumps({"ok": True}).encode())

            def do_POST(self):
                n = int(self.headers["content-length"])
                outer.requests.append({"path": self.path, "json": json.loads(self.rfile.read(n))})
                outer.arrived.set()
                if outer.mode == "sleep":
                    outer.release.wait(30)
                    return
                if outer.delay_s:
                    outer.release.wait(outer.delay_s)
                if outer.mode == "raw":
                    return self._send(outer.status, outer.raw)
                self._send(outer.status, json.dumps(outer.body).encode())

            def _send(self, status, data):
                try:
                    self.send_response(status)
                    self.send_header("content-length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._t = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *a):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
