"""A tiny Order Service implementing openapi.yaml (v2), with one deliberate contract bug:
GET /orders/{id} still returns `total` as a JSON number, but v2 says it's a string.

  python demo/order-service/server.py --port 8080
"""

from __future__ import annotations

import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ORDER = {"id": "1001", "status": "paid", "total": 42.5, "currency": "USD", "customer": {"id": "c-7"},
         "items": [{"sku": "BACKPACK-1", "qty": 1, "price": 42.5}]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code: int, body=None):
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(code)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/orders":
            return self._send(200, [{"id": "1001", "status": "paid"}, {"id": "1002", "status": "pending"}])
        if m := re.fullmatch(r"/orders/([^/]+)/tracking", path):
            return self._send(200, {"carrier": "UPS", "trackingNumber": "1Z999"})
        if m := re.fullmatch(r"/orders/([^/]+)", path):
            return self._send(200, ORDER) if m.group(1) == "1001" else self._send(404)
        return self._send(404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path != "/orders" or not body.get("channel"):
            return self._send(400, {"error": "channel is required"})
        return self._send(201, {**ORDER, "id": "1003", "status": "pending", "total": "42.50"})


class OrderService:
    def __init__(self, port: int = 0):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    s = OrderService(ap.parse_args().port)
    print(f"Order Service (v2, with one contract bug) on {s.url}  (Ctrl-C to stop)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        s.close()
