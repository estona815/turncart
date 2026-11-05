"""Real loopback protocol and durable review-state checks; no PayPal calls."""
import http.client
import json
from pathlib import Path
import queue
import threading
import unittest
import uuid

from checkout import CheckoutError
from web import ReviewApplication, ReviewServer, ROOT


TMP = ROOT.parents[2] / "_tmp"
TMP.mkdir(exist_ok=True)


def state_path():
    return TMP / ("paypal__web-test__" + str(uuid.uuid4()) + ".sqlite3")


def data_for(app):
    cart = [{"sku":"NOTEBOOK", "quantity":1}, {"sku":"PEN", "quantity":2}]
    intent = "I am ready to buy my basket"
    analysis = app.analyze({"intent": intent, "cart": cart})
    return {"intent":intent, "cart":cart, "reviewed_cart_sha256":analysis["cart_sha256"], "confirmed":True, "nonce":str(uuid.uuid4())}


class ReviewTests(unittest.TestCase):
    def test_conflicting_requests_cannot_save_a_checkout_review(self):
        data = data_for(self.app)
        for text in ["I want to buy this cart, but first remove the notebook",
                     "I was ready to buy my basket yesterday",
                     "Do you think I should buy this basket?",
                     "I want to buy only the pens, not the notebook"]:
            data["intent"] = text
            with self.subTest(text=text), self.assertRaisesRegex(CheckoutError, "checkout_suggestion_required"):
                self.app.prepare(data)
        self.assertEqual(self.app.store.db.execute("SELECT count(*) FROM operations").fetchone()[0], 0)

    def setUp(self):
        self.path = state_path()
        self.app = ReviewApplication(self.path)

    def tearDown(self):
        self.app.store.close()
        # Preserve all project-local test databases; no automatic cleanup.

    def test_durable_local_idempotency(self):
        data = data_for(self.app)
        first = self.app.prepare(data)
        self.assertEqual(first["amount"]["value"], "14.95")
        self.assertEqual(first["phase"], "LOCAL_REVIEW")
        self.assertEqual(first["items"][1]["quantity"], "2")
        self.app.store.close()
        self.app = ReviewApplication(self.path)
        second = self.app.prepare(data)
        self.assertEqual(first, second)
        self.assertEqual(self.app.store.db.execute("SELECT count(*) FROM operations").fetchone()[0], 1)
        self.assertEqual(self.app.store.db.execute("PRAGMA table_info(web_requests)").fetchall()[1][1], "cart_hash")
        self.assertNotIn(data["intent"].encode(), self.path.read_bytes())

    def test_changed_cart_cannot_reuse_confirmation(self):
        data = data_for(self.app)
        data["cart"][0]["quantity"] = 2
        with self.assertRaisesRegex(CheckoutError, "explicit_cart_review_required"):
            self.app.prepare(data)
        self.assertEqual(self.app.store.db.execute("SELECT count(*) FROM operations").fetchone()[0], 0)

    def test_nonce_cannot_change_cart(self):
        data = data_for(self.app)
        self.app.prepare(data)
        data["cart"][0]["quantity"] = 2
        data["reviewed_cart_sha256"] = self.app.analyze({"intent":data["intent"],"cart":data["cart"]})["cart_sha256"]
        with self.assertRaisesRegex(CheckoutError, "request_nonce_cart_conflict"):
            self.app.prepare(data)
        self.assertEqual(self.app.store.db.execute("SELECT count(*) FROM operations").fetchone()[0], 1)

    def test_explicit_review_and_model_action_required(self):
        data = data_for(self.app)
        data["confirmed"] = 1
        with self.assertRaises(CheckoutError): self.app.prepare(data)
        data["confirmed"] = True
        data["intent"] = "I want to cancel my basket"
        with self.assertRaisesRegex(CheckoutError, "checkout_suggestion_required"): self.app.prepare(data)

    def test_central_db_refused(self):
        with self.assertRaisesRegex(CheckoutError, "separate_application_state_required"):
            ReviewApplication(ROOT.parents[2] / "_artifacts/logs/video_pipeline/state.sqlite3")


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ready = queue.Queue()
        def serve():
            app = ReviewApplication(state_path())
            server = ReviewServer(("127.0.0.1", 0), app)
            cls.ready.put(server)
            try: server.serve_forever(poll_interval=.02)
            finally: server.server_close(); app.store.close()
        cls.thread = threading.Thread(target=serve, daemon=True)
        cls.thread.start()
        cls.server = cls.ready.get(timeout=5)
        cls.port = cls.server.server_port
        cls.origin = cls.server.origin
        status, body = cls.request("GET", "/api/catalog")
        assert status == 200
        cls.csrf = body["csrf"]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=5)

    @classmethod
    def request(cls, method, path, data=None, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=5)
        body = raw if raw is not None else json.dumps(data).encode() if data is not None else None
        selected = {"Host":"127.0.0.1:%d" % cls.port}
        if method == "POST": selected.update({"Origin":cls.origin,"Content-Type":"application/json","X-TurnCart-CSRF":cls.csrf})
        selected.update(headers or {})
        try:
            conn.request(method, path, body, selected)
            response = conn.getresponse()
            raw_reply = response.read()
            value = json.loads(raw_reply) if "application/json" in response.getheader("Content-Type", "") else raw_reply
            return response.status, value
        finally: conn.close()

    def test_real_http_model_review_and_readback(self):
        data = {"intent":"I am ready to buy my basket","cart":[{"sku":"PEN","quantity":2}]}
        code, analysis = self.request("POST", "/api/analyze", data)
        self.assertEqual(code,200)
        self.assertEqual(analysis["model_suggestion"]["suggested_intent"],"checkout")
        complete = {**data,"confirmed":True,"reviewed_cart_sha256":analysis["cart_sha256"],"nonce":str(uuid.uuid4())}
        code, result = self.request("POST", "/api/reviews", complete)
        self.assertEqual(code,200)
        self.assertEqual(result["amount"]["value"],"5.00")
        self.assertIsNone(result["order_id"])
        code, saved = self.request("GET", "/api/reviews/"+result["operation_id"])
        self.assertEqual(code,200)
        self.assertEqual(saved,result)

    def test_cross_origin_host_and_session_refused(self):
        data={"intent":"buy","cart":[{"sku":"PEN","quantity":1}]}
        for headers in ({"Origin":"https://unrelated.invalid"},{"Host":"rebind.invalid"},{"X-TurnCart-CSRF":"wrong"},{"X-TurnCart-CSRF":"é"}):
            self.assertEqual(self.request("POST","/api/analyze",data,headers)[0],403)

    def test_edit_inflection_cannot_start_review_even_when_confirmed(self):
        data = {"intent":"Make changes to my basket first",
                "cart":[{"sku":"PEN","quantity":2}]}
        code, analysis = self.request("POST", "/api/analyze", data)
        self.assertEqual(code, 200)
        self.assertIn(analysis["model_suggestion"]["suggested_intent"], ("edit", "clarify"))
        complete = {**data, "confirmed":True,
                    "reviewed_cart_sha256":analysis["cart_sha256"],
                    "nonce":str(uuid.uuid4())}
        code, result = self.request("POST", "/api/reviews", complete)
        self.assertEqual(code, 400)
        self.assertEqual(result["error"], "checkout_suggestion_required")
        self.assertFalse(result["payment_started"])

    def test_no_arbitrary_files_or_payment_route(self):
        for path in ("/corpus.json","/../checkout.py","/api/catalog?x=1","/api/reviews/../state.sqlite3"):
            self.assertEqual(self.request("GET",path)[0],404)
        self.assertEqual(self.request("POST","/api/payments",{})[0],404)

    def test_malformed_and_oversized_inputs_refused(self):
        self.assertEqual(self.request("POST","/api/analyze",raw=b'{"intent":"buy","intent":"cancel","cart":[]}')[0],400)
        self.assertEqual(self.request("POST","/api/analyze",raw=b'x'*16385)[0],413)
        self.assertEqual(self.request("POST","/api/analyze",{},headers={"Content-Type":"text/plain"})[0],400)


if __name__ == "__main__": unittest.main()
