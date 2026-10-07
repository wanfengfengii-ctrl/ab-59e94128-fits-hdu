"""HTTP frontend for the strict FITS auditor.

Only the Python standard library is used: :mod:`http.server` is threaded so
that health probes never block behind a large upload, and the request body is
streamed into memory up to the 16 MiB policy limit.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .audit import MAX_FILE_SIZE, audit

MAX_UPLOAD = MAX_FILE_SIZE
READ_CHUNK = 64 * 1024


class _Handler(BaseHTTPRequestHandler):
    server_version = "fits-audit/1.0"
    protocol_version = "HTTP/1.1"

    # Silence default noisy logging; structured JSON goes to stdout instead.
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health" or self.path == "/healthz":
            self._send_json(200, {"status": "ok"})
        elif self.path == "/":
            self._send_json(200, {
                "service": "fits-audit",
                "endpoints": {"POST": "/api/fits/audit"},
            })
        else:
            self._send_json(404, {
                "status": "rejected",
                "error": {"reason": "NOT_FOUND",
                          "message": f"unknown path {self.path}",
                          "offset": None, "hdu": None},
            })

    def do_POST(self):
        if self.path != "/api/fits/audit":
            return self._send_json(404, {
                "status": "rejected",
                "error": {"reason": "NOT_FOUND", "message": "unknown path",
                          "offset": None, "hdu": None},
            })

        ctype = self.headers.get("Content-Type", "")
        main = ctype.split(";", 1)[0].strip().lower()
        if main != "application/fits":
            # An unread body cannot be skipped safely without a length; close
            # the connection after responding.
            self.close_connection = True
            return self._send_json(415, {
                "status": "rejected",
                "error": {"reason": "UNSUPPORTED_MEDIA_TYPE",
                          "message": "Content-Type must be application/fits",
                          "offset": None, "hdu": None},
            })

        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if length < 0:
            self.close_connection = True
            return self._send_json(411, {
                "status": "rejected",
                "error": {"reason": "LENGTH_REQUIRED",
                          "message": "Content-Length header is required",
                          "offset": None, "hdu": None},
            })
        if length > MAX_UPLOAD:
            self.close_connection = True
            return self._send_json(413, {
                "status": "rejected",
                "error": {"reason": "FILE_TOO_LARGE",
                          "message": f"declared size {length} exceeds "
                                     f"{MAX_UPLOAD} byte limit",
                          "offset": MAX_UPLOAD, "hdu": None},
            })

        # Stream exactly Content-Length bytes, rejecting oversized streams
        # that lie about their length or omit it.
        body = bytearray()
        remaining = length
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(READ_CHUNK, remaining))
                if not chunk:
                    break
                body.extend(chunk)
                remaining -= len(chunk)
        except (ConnectionError, OSError):
            return self._send_json(400, {
                "status": "rejected",
                "error": {"reason": "UNREADABLE_BODY",
                          "message": "request body ended prematurely",
                          "offset": len(body), "hdu": None},
            })

        if len(body) != length:
            return self._send_json(400, {
                "status": "rejected",
                "error": {"reason": "TRUNCATED_REQUEST",
                          "message": f"expected {length} body bytes, "
                                     f"received {len(body)}",
                          "offset": len(body), "hdu": None},
            })

        result = audit(bytes(body))
        doc = result.to_dict()
        self._send_json(200 if result.accepted else 422, doc)

    # Keep-alive safety: BaseHTTPRequestHandler handles this, but make sure
    # malformed requests never crash the worker thread.
    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionError, OSError):
            self.close_connection = True


def create_server(host: str = "0.0.0.0", port: int = 8080) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _Handler)
    server.daemon_threads = True
    return server


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="FITS audit HTTP service")
    parser.add_argument("--host", default=os.environ.get("FITS_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("FITS_PORT", "8080")))
    args = parser.parse_args(argv)

    server = create_server(args.host, args.port)
    print(f"fits-audit listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
