"""Loopback-only model/catalog/review UI; optional public Sandbox browser SDK.

The server makes no PayPal calls; the enabled browser SDK is separate from the
original credential-based CLI. No intent or credentials are stored or printed.
"""
import argparse
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import re
import secrets
import sqlite3

from checkout import CATALOG, CheckoutError, CheckoutStore, cart_payload, _json
from intent_model import IntentError, IntentModel
from public_sandbox import PublicSandboxStore


UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
ROOT = Path(__file__).resolve().parent
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/styles.css": ("styles.css", "text/css; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8")}


class ReviewApplication:
    def __init__(self, state, *, public_sandbox=False):
        central = ROOT.parents[2] / "_artifacts/logs/video_pipeline/state.sqlite3"
        state = Path(state)
        if state.is_symlink() or state.resolve() == central.resolve():
            raise CheckoutError("separate_application_state_required")
        self.store = CheckoutStore(state)
        self.store.db.execute("CREATE TABLE IF NOT EXISTS web_requests (nonce TEXT PRIMARY KEY, cart_hash TEXT NOT NULL, operation_id TEXT NOT NULL)")
        self.model = IntentModel.from_corpus()
        self.csrf = secrets.token_urlsafe(32)
        self.csp_nonce = secrets.token_urlsafe(24)
        self.public_sandbox = PublicSandboxStore(self.store) if public_sandbox else None

    def analyze(self, data):
        if type(data) is not dict or set(data) != {"intent", "cart"}:
            raise CheckoutError("request_schema")
        suggestion = asdict(self.model.predict(data["intent"]))
        payload, fingerprint, amount = cart_payload(data["cart"])
        return {"model_suggestion": suggestion, "cart_sha256": fingerprint,
                "amount": amount, "items": payload["purchase_units"][0]["items"],
                "payment_started": False}

    def prepare(self, data):
        expected = {"intent", "cart", "reviewed_cart_sha256", "confirmed", "nonce"}
        if type(data) is not dict or set(data) != expected:
            raise CheckoutError("request_schema")
        checked = self.analyze({"intent": data["intent"], "cart": data["cart"]})
        if checked["model_suggestion"]["suggested_intent"] != "checkout":
            raise CheckoutError("checkout_suggestion_required")
        if data["confirmed"] is not True or data["reviewed_cart_sha256"] != checked["cart_sha256"]:
            raise CheckoutError("explicit_cart_review_required")
        nonce = data["nonce"]
        if type(nonce) is not str or not UUID.fullmatch(nonce):
            raise CheckoutError("request_nonce")
        self.store.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.store.db.execute("SELECT cart_hash,operation_id FROM web_requests WHERE nonce=?", (nonce,)).fetchone()
            if row:
                if row[0] != checked["cart_sha256"]:
                    raise CheckoutError("request_nonce_cart_conflict")
                identity = row[1]
            else:
                review = self.store.prepare(data["cart"])
                identity = review["operation_id"]
                self.store.db.execute("INSERT INTO web_requests VALUES(?,?,?)", (nonce,checked["cart_sha256"],identity))
            self.store.db.execute("COMMIT")
        except BaseException:
            self.store.db.execute("ROLLBACK")
            raise
        return self.store.review(identity)


class ReviewServer(HTTPServer):
    def __init__(self, address, app):
        if address[0] != "127.0.0.1":
            raise ValueError("loopback_only")
        super().__init__(address, Handler)
        self.app = app
        self.origin = "http://127.0.0.1:%d" % self.server_port


class Handler(BaseHTTPRequestHandler):
    server_version = "TurnCart"
    sys_version = ""

    def log_message(self, *args):
        pass

    def send(self, status, body, mime="application/json; charset=utf-8"):
        raw = body if type(body) is bytes else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        policy = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        if self.server.app.public_sandbox is not None:
            policy = ("default-src 'self'; script-src 'self' 'nonce-" + self.server.app.csp_nonce + "' https://www.paypal.com https://www.paypalobjects.com; "
                "style-src 'self' 'nonce-" + self.server.app.csp_nonce + "'; connect-src 'self' https://www.paypal.com https://www.sandbox.paypal.com https://api-m.sandbox.paypal.com; "
                "frame-src https://www.paypal.com https://www.sandbox.paypal.com; "
                "img-src 'self' data: https://www.paypal.com https://www.paypalobjects.com https://www.sandbox.paypal.com; "
                "base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.send_header("Content-Security-Policy", policy)
        self.end_headers()
        self.wfile.write(raw)

    def valid_host(self):
        return self.headers.get_all("Host") == ["127.0.0.1:%d" % self.server.server_port]

    def do_GET(self):
        if not self.valid_host():
            return self.send(403, {"error": "local_host_required"})
        origin = self.headers.get_all("Origin")
        if origin is not None and origin != [self.server.origin]:
            return self.send(403, {"error": "local_origin_required"})
        if self.path in ASSETS:
            filename, mime = ASSETS[self.path]
            return self.send(200, (ROOT / filename).read_bytes(), mime)
        if self.path == "/api/catalog":
            return self.send(200, {"catalog": [{"sku": k, "name": n, "usd_cents": c} for k,(n,c) in CATALOG.items()], "csrf": self.server.app.csrf,
                "public_sandbox_enabled": self.server.app.public_sandbox is not None,
                "sdk_csp_nonce": self.server.app.csp_nonce if self.server.app.public_sandbox is not None else None})
        public_prefix = "/api/public-sandbox/status/"
        if self.path.startswith(public_prefix) and UUID.fullmatch(self.path[len(public_prefix):]):
            app = self.server.app
            if app.public_sandbox is None:
                return self.send(404, {"error": "public_sandbox_disabled"})
            try:
                return self.send(200, app.public_sandbox.status(self.path[len(public_prefix):]))
            except CheckoutError:
                return self.send(404, {"error": "review_not_found"})
        prefix = "/api/reviews/"
        if self.path.startswith(prefix) and UUID.fullmatch(self.path[len(prefix):]):
            try:
                return self.send(200, self.server.app.store.review(self.path[len(prefix):]))
            except CheckoutError:
                return self.send(404, {"error": "review_not_found"})
        return self.send(404, {"error": "route_not_found"})

    def do_POST(self):
        if not self.valid_host() or self.headers.get_all("Origin") != [self.server.origin]:
            return self.send(403, {"error": "local_origin_required"})
        tokens = self.headers.get_all("X-TurnCart-CSRF")
        if tokens is None or len(tokens) != 1 or not tokens[0].isascii() or not secrets.compare_digest(tokens[0], self.server.app.csrf):
            return self.send(403, {"error": "session_required"})
        if self.path not in {"/api/analyze", "/api/reviews", "/api/public-sandbox/begin", "/api/public-sandbox/report"}:
            return self.send(404, {"error": "route_not_found"})
        if self.path.startswith('/api/public-sandbox/') and self.server.app.public_sandbox is None:
            return self.send(404, {"error": "public_sandbox_disabled"})
        if self.headers.get("Transfer-Encoding") is not None or self.headers.get_all("Content-Type") != ["application/json"]:
            return self.send(400, {"error": "json_body_required"})
        lengths = self.headers.get_all("Content-Length")
        if lengths is None or len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,5}", lengths[0]) or not 1 <= int(lengths[0]) <= 16384:
            return self.send(413, {"error": "body_limit"})
        self.connection.settimeout(5)
        try:
            raw = self.rfile.read(int(lengths[0]))
            if len(raw) != int(lengths[0]):
                raise CheckoutError("incomplete_body")
            data = _json(raw)
            app = self.server.app
            if self.path == '/api/analyze':
                result = app.analyze(data)
            elif self.path == '/api/reviews':
                result = app.prepare(data)
            elif self.path == '/api/public-sandbox/begin':
                result = app.public_sandbox.begin(data)
            else:
                result = app.public_sandbox.report(data)
            return self.send(200, result)
        except (CheckoutError, IntentError) as exc:
            return self.send(400, {"error": str(exc), "payment_started": False})
        except (ValueError, sqlite3.Error, TimeoutError):
            return self.send(400, {"error": "request_failed", "payment_started": False})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--public-sandbox", action="store_true",
        help="Permit explicit browser opt-in to PayPal's public sb test merchant; never capture")
    args = parser.parse_args()
    app = ReviewApplication(args.state, public_sandbox=args.public_sandbox)
    server = ReviewServer(("127.0.0.1", args.port), app)
    print("TurnCart local interface: " + server.origin, flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        app.store.close()


if __name__ == "__main__":
    main()
