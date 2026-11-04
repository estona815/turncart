"""Original catalog review + durable sandbox-only Orders v2 controller.

No account creation, credentials lookup, live endpoint, provider inference,
automatic capture/retry or fabricated receipts. Real transport is opt-in by
constructing SandboxClient with an operator's existing sandbox app credentials.
"""
import base64
import hashlib
import http.client
import json
from pathlib import Path
import re
import sqlite3
import ssl
import time
from urllib.parse import urlsplit, parse_qs
import uuid


CATALOG = {"NOTEBOOK": ("Fictional notebook", 995),
           "PEN": ("Fictional pen", 250), "BAG": ("Fictional tote", 1595)}
API_HOST = "api-m.sandbox.paypal.com"
ORDER_ID = re.compile(r"[A-Z0-9]{1,36}\Z")


class CheckoutError(ValueError):
    """Sanitized codes only; no credentials or provider bodies in diagnostics."""


def cart_payload(cart):
    if type(cart) is not list or not 1 <= len(cart) <= len(CATALOG):
        raise CheckoutError("cart_schema")
    total, items, seen = 0, [], set()
    for row in cart:
        if type(row) is not dict or set(row) != {"sku", "quantity"}:
            raise CheckoutError("cart_row")
        sku, quantity = row["sku"], row["quantity"]
        if (type(sku) is not str or sku not in CATALOG or sku in seen or
                type(quantity) is not int or not 1 <= quantity <= 5):
            raise CheckoutError("cart_value")
        seen.add(sku)
        name, cents = CATALOG[sku]
        total += cents * quantity
        items.append({"name": name, "sku": sku, "quantity": str(quantity),
                      "category": "PHYSICAL_GOODS", "unit_amount": {
                          "currency_code": "USD", "value": "%d.%02d" % divmod(cents, 100)}})
    if total > 5000:
        raise CheckoutError("fictional_cart_limit")
    amount = {"currency_code": "USD", "value": "%d.%02d" % divmod(total, 100)}
    items.sort(key=lambda item: item["sku"])
    payload = {"intent": "CAPTURE", "purchase_units": [{"amount": {
        **amount, "breakdown": {"item_total": amount}}, "items": items}],
        "payment_source": {"paypal": {"experience_context": {
            "shipping_preference": "NO_SHIPPING", "user_action": "PAY_NOW"}}}}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return payload, hashlib.sha256(encoded.encode()).hexdigest(), amount


def _json(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError()
            value[key] = item
        return value
    try:
        value = json.loads(raw, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise CheckoutError("provider_json") from None
    if type(value) is not dict:
        raise CheckoutError("provider_schema")
    return value


def validate_route(method, path):
    if type(method) is not str or type(path) is not str:
        raise CheckoutError("sandbox_route")
    allowed = ((method == "POST" and path in {"/v1/oauth2/token", "/v2/checkout/orders"}) or
               (method == "GET" and re.fullmatch(r"/v2/checkout/orders/[A-Z0-9]{1,36}", path)) or
               (method == "POST" and re.fullmatch(r"/v2/checkout/orders/[A-Z0-9]{1,36}/capture", path)))
    if not allowed:
        raise CheckoutError("sandbox_route")


class SandboxClient:
    def __init__(self, client_id, client_secret):
        if any(type(s) is not str or not 1 <= len(s) <= 2000 for s in (client_id, client_secret)):
            raise CheckoutError("sandbox_credentials_required")
        try:
            self._credential = base64.b64encode((client_id + ":" + client_secret).encode()).decode()
        except UnicodeError:
            raise CheckoutError("sandbox_credentials_encoding") from None
        self._token, self._expires = None, 0

    def _request(self, method, path, headers, body=None):
        validate_route(method, path)
        # Direct stdlib HTTPS: no environment proxy and no redirect following.
        connection = None
        try:
            connection = http.client.HTTPSConnection(API_HOST, timeout=20,
                                                     context=ssl.create_default_context())
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(1000001)
            if len(raw) > 1000000:
                raise CheckoutError("provider_response_limit")
            if not 200 <= response.status < 300:
                raise CheckoutError("provider_http_status")
            return _json(raw)
        except CheckoutError:
            raise
        except Exception:
            raise CheckoutError("sandbox_transport_unknown") from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    def call(self, method, path, payload=None, request_id=None):
        # Validate locally before OAuth can create any external request.
        validate_route(method, path)
        if request_id is not None and (type(request_id) is not str or
                not re.fullmatch(r"[a-zA-Z0-9-]{1,64}", request_id)):
            raise CheckoutError("request_identifier")
        try:
            raw = json.dumps(payload, allow_nan=False).encode() if payload is not None else None
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise CheckoutError("request_json") from None
        if self._token is None or time.monotonic() >= self._expires:
            token = self._request("POST", "/v1/oauth2/token", {
                "Authorization": "Basic " + self._credential,
                "Content-Type": "application/x-www-form-urlencoded"}, b"grant_type=client_credentials")
            value, lifetime = token.get("access_token"), token.get("expires_in")
            if (type(value) is not str or not 1 <= len(value) <= 16000 or
                    any(ord(c) < 32 or ord(c) > 126 for c in value) or
                    type(lifetime) is not int or not 60 <= lifetime <= 86400):
                raise CheckoutError("sandbox_oauth_response")
            self._token, self._expires = value, time.monotonic() + lifetime - 30
        headers = {"Authorization": "Bearer " + self._token,
                   "Content-Type": "application/json", "Prefer": "return=representation"}
        if request_id is not None:
            headers["PayPal-Request-Id"] = request_id
        return self._request(method, path, headers, raw)


def order_facts(response, expected_amount, expected_id=None, *, completed=False):
    if type(response) is not dict:
        raise CheckoutError("order_response")
    order_id, status = response.get("id"), response.get("status")
    if (type(order_id) is not str or not ORDER_ID.fullmatch(order_id) or
            (expected_id is not None and order_id != expected_id) or
            status not in {"CREATED", "PAYER_ACTION_REQUIRED", "APPROVED", "COMPLETED", "VOIDED"}):
        raise CheckoutError("order_identity_or_state")
    units = response.get("purchase_units")
    if type(units) is not list or len(units) != 1 or type(units[0]) is not dict:
        raise CheckoutError("order_purchase_unit")
    amount = units[0].get("amount")
    # Capture representations can omit purchase_unit.amount. Bind that
    # representation to the single completed capture instead, as documented.
    if (amount is not None or not completed) and (
            type(amount) is not dict or amount.get("currency_code") != expected_amount["currency_code"]
            or amount.get("value") != expected_amount["value"]):
        raise CheckoutError("order_amount_binding")
    capture_id = None
    if completed:
        payments = units[0].get("payments")
        captures = payments.get("captures") if type(payments) is dict else None
        if (status != "COMPLETED" or type(captures) is not list or len(captures) != 1
                or type(captures[0]) is not dict):
            raise CheckoutError("capture_not_completed")
        capture = captures[0]
        if (capture.get("status") != "COMPLETED" or capture.get("amount") != expected_amount
                or type(capture.get("id")) is not str or not ORDER_ID.fullmatch(capture["id"])):
            raise CheckoutError("capture_receipt_binding")
        capture_id = capture["id"]
    return {"order_id": order_id, "status": status, "capture_id": capture_id}


def approval_url(response, order_id):
    links = response.get("links", [])
    if type(links) is not list:
        raise CheckoutError("approval_links")
    matches = [x for x in links if type(x) is dict and x.get("rel") in {"approve", "payer-action"}]
    if len(matches) != 1:
        raise CheckoutError("approval_link_required")
    href = matches[0].get("href")
    if type(href) is not str or len(href) > 2048:
        raise CheckoutError("approval_link")
    try:
        parsed = urlsplit(href)
        valid = (parsed.scheme == "https" and parsed.hostname in {
            "www.sandbox.paypal.com", "sandbox.paypal.com"} and parsed.port in {None, 443}
            and parsed.username is None and parsed.password is None and not parsed.fragment
            and parsed.path == "/checkoutnow" and matches[0].get("method", "GET") == "GET"
            and parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True,
                         max_num_fields=2) == {"token": [order_id]})
    except ValueError:
        valid = False
    if not valid:
        raise CheckoutError("approval_not_bound_sandbox")
    return href


class CheckoutStore:
    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, cart TEXT NOT NULL, cart_hash TEXT NOT NULL, amount TEXT NOT NULL, phase TEXT NOT NULL, order_id TEXT, create_key TEXT NOT NULL, capture_key TEXT NOT NULL, approval TEXT, capture_id TEXT)")

    def prepare(self, cart):
        payload, fingerprint, amount = cart_payload(cart)
        identity = str(uuid.uuid4())
        self.db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?)", (
            identity, json.dumps(payload), fingerprint, json.dumps(amount), "LOCAL_REVIEW", None,
            str(uuid.uuid4()), str(uuid.uuid4()), None, None))
        return {"operation_id": identity, "cart_sha256": fingerprint, "amount": amount,
                "phase": "LOCAL_REVIEW", "fictional_catalog": True}

    def _row(self, identity):
        row = self.db.execute("SELECT * FROM operations WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise CheckoutError("operation_missing")
        return row

    def _transition(self, identity, previous, following):
        cursor = self.db.execute("UPDATE operations SET phase=? WHERE id=? AND phase=?", (following, identity, previous))
        if cursor.rowcount != 1:
            raise CheckoutError("phase_no_resend")

    def create(self, identity, client, *, reviewed_cart_sha256, review_confirmed=False):
        # Serialize the REST pre-write with optional public SDK intent in this
        # same application DB. A new LOCAL_REVIEW cannot evade a consumed cart.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self._row(identity)
            if review_confirmed is not True or row[2] != reviewed_cart_sha256:
                raise CheckoutError("explicit_cart_review_required")
            public_table = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='public_sdk_attempts'").fetchone()
            if public_table and self.db.execute("SELECT 1 FROM public_sdk_attempts WHERE cart_hash=?", (row[2],)).fetchone():
                raise CheckoutError("public_sdk_attempt_blocks_rest_create")
            pending = self.db.execute(
                "SELECT 1 FROM operations WHERE cart_hash=? AND id!=? "
                "AND phase IN ('CREATE_SENT', 'CREATE_UNKNOWN', 'CREATED', "
                "'CAPTURE_SENT', 'CAPTURE_UNKNOWN') LIMIT 1",
                (row[2], identity),
            ).fetchone()
            if pending:
                raise CheckoutError("existing_rest_order_blocks_create")
            self._transition(identity, "LOCAL_REVIEW", "CREATE_SENT")
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        try:
            response = client.call("POST", "/v2/checkout/orders", json.loads(row[1]), row[6])
            facts = order_facts(response, json.loads(row[3]))
            if facts["status"] not in {"CREATED", "PAYER_ACTION_REQUIRED"}:
                raise CheckoutError("creation_state")
            link = approval_url(response, facts["order_id"])
        except Exception:
            self._transition(identity, "CREATE_SENT", "CREATE_UNKNOWN")
            raise CheckoutError("create_unknown_do_not_resend") from None
        self.db.execute("UPDATE operations SET phase='CREATED',order_id=?,approval=? WHERE id=? AND phase='CREATE_SENT'", (facts["order_id"], link, identity))
        return {**facts, "approval_url": link, "sandbox_only": True}

    def refresh(self, identity, client):
        row = self._row(identity)
        if row[5] is None or row[4] == "LOCAL_REVIEW":
            raise CheckoutError("no_known_order_to_readback")
        if row[4] not in {"CREATED", "CAPTURE_SENT", "CAPTURE_UNKNOWN", "COMPLETED"}:
            raise CheckoutError("readback_phase")
        try:
            response = client.call("GET", "/v2/checkout/orders/" + row[5])
            completed = type(response) is dict and response.get("status") == "COMPLETED"
            facts = order_facts(response, json.loads(row[3]), row[5], completed=completed)
        except Exception:
            raise CheckoutError("readback_unverified") from None
        if completed:
            self.db.execute("UPDATE operations SET phase='COMPLETED',capture_id=? WHERE id=?", (facts["capture_id"], identity))
        return facts

    def capture(self, identity, client, *, capture_confirmed=False):
        if capture_confirmed is not True:
            raise CheckoutError("explicit_capture_confirmation_required")
        row = self._row(identity)
        if row[4] != "CREATED":
            raise CheckoutError("phase_no_resend")
        facts = self.refresh(identity, client)
        if facts["status"] != "APPROVED":
            raise CheckoutError("buyer_approval_required")
        self._transition(identity, "CREATED", "CAPTURE_SENT")
        try:
            response = client.call("POST", "/v2/checkout/orders/" + row[5] + "/capture", {}, row[7])
            receipt = order_facts(response, json.loads(row[3]), row[5], completed=True)
        except Exception:
            self._transition(identity, "CAPTURE_SENT", "CAPTURE_UNKNOWN")
            raise CheckoutError("capture_unknown_readback_only") from None
        self.db.execute("UPDATE operations SET phase='COMPLETED',capture_id=? WHERE id=? AND phase='CAPTURE_SENT'", (receipt["capture_id"], identity))
        return {**receipt, "sandbox_only": True}

    def status(self, identity):
        row = self._row(identity)
        return {"operation_id": row[0], "cart_sha256": row[2], "amount": json.loads(row[3]),
                "phase": row[4], "order_id": row[5], "capture_id": row[9]}

    def review(self, identity):
        row = self._row(identity)
        payload = json.loads(row[1])
        items = payload["purchase_units"][0]["items"]
        return {**self.status(identity), "items": [{
            "sku": item["sku"], "name": item["name"],
            "quantity": item["quantity"], "unit_amount": item["unit_amount"]}
            for item in items]}

    def close(self):
        self.db.close()
