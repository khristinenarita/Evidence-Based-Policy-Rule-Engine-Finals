"""Dependency-free HTTP server exposing the same endpoints as app.main.

Handy when FastAPI can't be installed. Run:  python -m app.simple_server [port]
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .engine import InvalidPlanError, UnknownPolicyError, evaluate, load_foods, load_policies

POLICIES = load_policies()
FOODS = load_foods()
MAX_BODY = 1_000_000


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/health":
            self._send(200, {"status": "ok"})
        elif path == "/policies":
            self._send(200, [{"id": p["id"], "name": p["name"], "description": p["description"],
                              "jurisdiction": p["jurisdiction"], "status": p["status"],
                              "rule_count": len(p["rules"])} for p in POLICIES.values()])
        elif path.startswith("/policies/"):
            policy = POLICIES.get(path.split("/", 2)[2])
            self._send(200, policy) if policy else self._send(404, {"detail": "unknown policy_id"})
        elif path == "/foods":
            self._send(200, list(FOODS.values()))
        else:
            self._send(404, {"detail": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?")[0].rstrip("/") != "/validate":
            return self._send(404, {"detail": "not found"})
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return self._send(400, {"detail": "bad Content-Length"})
        if length <= 0 or length > MAX_BODY:
            return self._send(413 if length > MAX_BODY else 400, {"detail": "body missing or too large"})
        try:
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict) or not isinstance(data.get("policy_id"), str):
                raise ValueError("body must be an object with a string 'policy_id'")
        except (ValueError, UnicodeDecodeError) as exc:
            return self._send(400, {"detail": f"invalid JSON body: {exc}"})
        plan = {k: v for k, v in data.items() if k != "policy_id"}
        try:
            self._send(200, evaluate(plan, data["policy_id"], POLICIES, FOODS))
        except UnknownPolicyError as exc:
            self._send(404, {"detail": str(exc.args[0])})
        except InvalidPlanError as exc:
            self._send(422, {"detail": str(exc)})

    def log_message(self, fmt, *args) -> None:  # keep test output quiet
        pass


def make_server(port: int = 8000, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    print(f"Serving on http://127.0.0.1:{port}  (Ctrl+C to stop)")
    make_server(port).serve_forever()
