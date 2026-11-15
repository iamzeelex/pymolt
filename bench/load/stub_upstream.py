"""Stub FX-rate upstream for the orders service (stdlib only, never traced).

    GET /rates/<CUR>  -> 200 {"base": "USD", "currency": CUR, "rate": <fixed>}
                         503 for CHF (the "flaky provider" path), 404 for unknown codes

Binds 127.0.0.1 on a free port (or --port) and writes {"pid", "port"} to --ready-file once
listening. HTTP/1.1 keep-alive, one thread per connection. --delay-ms adds a fixed service
time per request to emulate a network hop (default 2 ms).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RATES = {"EUR": 0.92, "GBP": 0.79, "JPY": 151.3, "CAD": 1.37, "AUD": 1.52, "USD": 1.0}
UNAVAILABLE = {"CHF"}


def make_handler(delay_s: float):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quiet
            pass

        def _send(self, status: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            if delay_s:
                time.sleep(delay_s)
            parts = self.path.strip("/").split("/")
            if len(parts) != 2 or parts[0] != "rates":
                return self._send(404, {"error": "no such route"})
            cur = parts[1].upper()
            if cur in UNAVAILABLE:
                return self._send(503, {"error": "provider unavailable", "currency": cur})
            if cur not in RATES:
                return self._send(404, {"error": "unknown currency", "currency": cur})
            return self._send(200, {"base": "USD", "currency": cur, "rate": RATES[cur]})

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--ready-file", default=None)
    ap.add_argument("--delay-ms", type=float, default=2.0)
    args = ap.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args.delay_ms / 1000.0))
    server.daemon_threads = True

    def _stop(signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    if args.ready_file:
        tmp = args.ready_file + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"pid": os.getpid(), "port": server.server_address[1]}, fh)
        os.replace(tmp, args.ready_file)
    try:
        server.serve_forever(poll_interval=0.2)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
