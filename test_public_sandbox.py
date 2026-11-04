"""Public SDK local crash/race/result guards. No PayPal/credential calls."""
import copy
import http.client
import json
import queue
import sqlite3
import threading
import unittest
from unittest.mock import patch

from checkout import CheckoutError
from checkout import CheckoutStore
from intent_model import IntentModel
from public_sandbox import SAMPLE_HASH, SAMPLE_PAYLOAD, SDK_URL
from test_web import data_for, state_path
from web import ReviewApplication, ReviewServer


class PublicStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Reuse one genuine immutable trained model; no mocked predictions.
        cls.model = IntentModel.from_corpus()
        cls.model_cache = patch.object(IntentModel, 'from_corpus', return_value=cls.model)
        cls.model_cache.start()

    @classmethod
    def tearDownClass(cls):
        cls.model_cache.stop()

    def setUp(self):
        self.path = state_path(); self.app = ReviewApplication(self.path, public_sandbox=True)
        self.review = self.app.prepare(data_for(self.app))
        self.begin_data = {'operation_id': self.review['operation_id'],
            'reviewed_cart_sha256': SAMPLE_HASH, 'confirmed': True}
        self.block_network = patch.object(http.client, 'HTTPSConnection', side_effect=AssertionError('No PayPal network in local store'))
        self.block_network.start(); self.addCleanup(self.block_network.stop)

    def tearDown(self):
        self.app.store.close()  # Project-local fixtures remain; no deletion.

    def begin(self):
        return self.app.public_sandbox.begin(self.begin_data)

    def report(self, attempt, event, order_id):
        return self.app.public_sandbox.report({'attempt_id': attempt['attempt_id'],
            'cart_sha256': attempt['cart_sha256'], 'event': event, 'order_id': order_id})

    def test_default_mode_has_no_sdk_state_and_no_payment_calls(self):
        app = ReviewApplication(state_path())
        try:
            self.assertIsNone(app.public_sandbox)
            self.assertIsNone(app.store.db.execute("SELECT name FROM sqlite_master WHERE name='public_sdk_attempts'").fetchone())
        finally: app.store.close()

    def test_durable_before_external_write_and_rest_untouched(self):
        result = self.begin()
        other = sqlite3.connect(self.path)
        try:
            self.assertEqual(other.execute('SELECT phase FROM public_sdk_attempts').fetchone()[0], 'SDK_CREATE_OUTCOME_UNKNOWN')
        finally: other.close()
        self.assertEqual(result['order_payload'], SAMPLE_PAYLOAD)
        self.assertEqual(result['amount']['value'], '14.95')
        self.assertFalse(result['server_provider_verified'])
        self.assertFalse(result['server_capture_implemented'])
        review = self.app.store.review(self.review['operation_id'])
        self.assertEqual(review['phase'], 'LOCAL_REVIEW'); self.assertIsNone(review['order_id'])

    def test_tampered_amount_hash_and_confirmation_rejected(self):
        for key, value in [('amount', {'value': '0.01'}), ('reviewed_cart_sha256', 'a'*64), ('confirmed', 1)]:
            data = dict(self.begin_data, **{key: value})
            with self.subTest(key=key), self.assertRaises(CheckoutError): self.app.public_sandbox.begin(data)
        self.assertEqual(self.app.store.db.execute('SELECT count(*) FROM public_sdk_attempts').fetchone()[0], 0)

    def test_other_legal_catalog_basket_binds_exact_server_amount(self):
        data = data_for(self.app); data['cart'] = [{'sku': 'PEN', 'quantity': 2}]
        data['reviewed_cart_sha256'] = self.app.analyze({'intent': data['intent'], 'cart': data['cart']})['cart_sha256']
        review = self.app.prepare(data)
        self.assertTrue(self.app.public_sandbox.status(review['operation_id'])['eligible'])
        result = self.app.public_sandbox.begin(dict(self.begin_data, operation_id=review['operation_id'], reviewed_cart_sha256=review['cart_sha256']))
        self.assertEqual(result['amount'], {'currency_code':'USD','value':'5.00'})

    def test_crash_after_commit_and_restart_never_reserves_again(self):
        original = self.begin(); self.app.store.close(); self.app = ReviewApplication(self.path, public_sandbox=True)
        status = self.app.public_sandbox.status(self.review['operation_id'])
        self.assertFalse(status['eligible']); self.assertFalse(status['attempt']['retry_allowed'])
        self.assertEqual(status['attempt']['attempt_id'], original['attempt_id'])
        with self.assertRaisesRegex(CheckoutError, 'consumed'): self.begin()

    def test_new_review_of_same_cart_cannot_evade_unknown(self):
        first = self.begin(); review = self.app.prepare(data_for(self.app))
        with self.assertRaisesRegex(CheckoutError, 'consumed'):
            self.app.public_sandbox.begin(dict(self.begin_data, operation_id=review['operation_id']))
        self.assertEqual(self.app.public_sandbox.status(review['operation_id'])['attempt']['attempt_id'], first['attempt_id'])

    def test_client_order_and_approval_not_provider_or_rest_success(self):
        attempt = self.begin(); order = self.report(attempt, 'order_id', 'TESTORDER123')
        self.assertEqual(order['phase'], 'SDK_CLIENT_REPORTED_ORDER_ID')
        approved = self.report(attempt, 'approval', 'TESTORDER123')
        self.assertTrue(approved['buyer_approval_reported']); self.assertFalse(approved['server_provider_verified'])
        self.assertFalse(approved['payment_completed_verified']); self.assertFalse(approved['server_capture_implemented'])
        self.assertEqual(self.app.store.review(self.review['operation_id'])['phase'], 'LOCAL_REVIEW')

    def test_tampered_callback_cannot_replace_id_or_approve_unknown(self):
        attempt = self.begin()
        with self.assertRaises(CheckoutError): self.report(attempt, 'approval', 'FAKEID')
        self.report(attempt, 'order_id', 'FIRSTID')
        with self.assertRaises(CheckoutError): self.report(attempt, 'order_id', 'DIFFERENTID')
        with self.assertRaises(CheckoutError): self.report(attempt, 'approval', 'DIFFERENTID')
        with self.assertRaises(CheckoutError): self.report(attempt, 'capture', 'FIRSTID')
        self.assertEqual(self.app.public_sandbox.status(self.review['operation_id'])['attempt']['order_id'], 'FIRSTID')

    def test_error_cancel_or_unreported_result_remains_consumed(self):
        attempt = self.begin()
        for event in ('cancel', 'error'):
            result = self.report(attempt, event, None)
            self.assertEqual(result['phase'], 'SDK_CREATE_OUTCOME_UNKNOWN')
            self.assertFalse(result['retry_allowed'])
        with self.assertRaises(CheckoutError): self.begin()

    def test_rest_unknown_any_cart_and_created_same_cart_block(self):
        self.app.store.db.execute("UPDATE operations SET phase='CREATE_UNKNOWN' WHERE id=?", (self.review['operation_id'],))
        self.assertFalse(self.app.public_sandbox.status(self.review['operation_id'])['eligible'])
        with self.assertRaises(CheckoutError): self.begin()
        self.app.store.db.execute("UPDATE operations SET phase='LOCAL_REVIEW' WHERE id=?", (self.review['operation_id'],))
        other = self.app.store.prepare([{'sku':'PEN','quantity':1}])
        self.app.store.db.execute("UPDATE operations SET phase='CAPTURE_UNKNOWN' WHERE id=?", (other['operation_id'],))
        with self.assertRaisesRegex(CheckoutError, 'existing_rest'): self.begin()

    def test_concurrent_connections_only_one_durable_attempt(self):
        barrier = threading.Barrier(2); results = queue.Queue()
        def attempt():
            app = ReviewApplication(self.path, public_sandbox=True)
            try:
                barrier.wait(timeout=5)
                try: results.put(('ok', app.public_sandbox.begin(self.begin_data)['attempt_id']))
                except CheckoutError as exc: results.put(('blocked', str(exc)))
            finally: app.store.close()
        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for t in threads: t.start()
        for t in threads: t.join(timeout=10); self.assertFalse(t.is_alive())
        outcomes = [results.get(timeout=2)[0] for _ in threads]
        self.assertEqual(sorted(outcomes), ['blocked', 'ok'])
        self.assertEqual(self.app.store.db.execute('SELECT count(*) FROM public_sdk_attempts').fetchone()[0], 1)

    def test_public_unknown_blocks_rest_even_new_review_and_restart(self):
        self.begin(); review = self.app.prepare(data_for(self.app))
        self.app.store.close(); self.app = ReviewApplication(self.path)
        calls = []
        class Provider:
            def call(self, *args): calls.append(args); raise AssertionError('Must not create REST order')
        with self.assertRaisesRegex(CheckoutError, 'public_sdk_attempt_blocks_rest_create'):
            self.app.store.create(review['operation_id'], Provider(),
                reviewed_cart_sha256=review['cart_sha256'], review_confirmed=True)
        self.assertEqual(calls, [])
        self.assertEqual(self.app.store.review(review['operation_id'])['phase'], 'LOCAL_REVIEW')

    def test_rest_and_sdk_race_one_path_wins_other_never_calls_provider(self):
        barrier = threading.Barrier(2); results = queue.Queue(); calls = []
        amount = self.review['amount']
        class Provider:
            def call(self, *args):
                calls.append(args)
                return {'id':'RACEORDER1','status':'CREATED','purchase_units':[{'amount':amount}],
                    'links':[{'rel':'approve','method':'GET','href':'https://www.sandbox.paypal.com/checkoutnow?token=RACEORDER1'}]}
        def rest():
            store = CheckoutStore(self.path)
            try:
                barrier.wait(timeout=5)
                try:
                    store.create(self.review['operation_id'], Provider(),
                        reviewed_cart_sha256=self.review['cart_sha256'], review_confirmed=True)
                    results.put('rest_ok')
                except CheckoutError: results.put('rest_blocked')
            finally: store.close()
        def sdk():
            app = ReviewApplication(self.path, public_sandbox=True)
            try:
                barrier.wait(timeout=5)
                try: app.public_sandbox.begin(self.begin_data); results.put('sdk_ok')
                except CheckoutError: results.put('sdk_blocked')
            finally: app.store.close()
        threads = [threading.Thread(target=rest), threading.Thread(target=sdk)]
        for t in threads:t.start()
        for t in threads:t.join(timeout=10);self.assertFalse(t.is_alive())
        outcomes = {results.get(timeout=2),results.get(timeout=2)}
        self.assertIn(outcomes, ({'rest_ok','sdk_blocked'},{'rest_blocked','sdk_ok'}))
        self.assertEqual(len(calls), 1 if 'rest_ok' in outcomes else 0)


class PublicHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ready = queue.Queue()
        def serve():
            app = ReviewApplication(state_path(), public_sandbox=True)
            server = ReviewServer(('127.0.0.1', 0), app); ready.put(server)
            try: server.serve_forever(poll_interval=.02)
            finally: server.server_close(); app.store.close()
        cls.thread = threading.Thread(target=serve, daemon=True); cls.thread.start()
        cls.server = ready.get(timeout=5)
        cls.csrf = cls.request('GET', '/api/catalog')[1]['csrf']

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.thread.join(timeout=5)

    @classmethod
    def request(cls, method, path, data=None, extra=None):
        headers = {'Host': '127.0.0.1:%d' % cls.server.server_port}
        if method == 'POST': headers.update(Origin=cls.server.origin, **{'Content-Type':'application/json','X-TurnCart-CSRF':cls.csrf})
        headers.update(extra or {})
        conn = http.client.HTTPConnection('127.0.0.1', cls.server.server_port, timeout=5)
        try:
            conn.request(method, path, None if data is None else json.dumps(data), headers)
            reply = conn.getresponse(); raw = reply.read(); return reply.status, json.loads(raw), dict(reply.getheaders())
        finally: conn.close()

    def test_enabled_catalog_and_exact_csp_not_wildcard(self):
        status, data, headers = self.request('GET', '/api/catalog')
        self.assertEqual(status, 200); self.assertTrue(data['public_sandbox_enabled'])
        policy = headers['Content-Security-Policy']
        self.assertIn("'nonce-"+data['sdk_csp_nonce']+"'", policy)
        self.assertIn('https://www.sandbox.paypal.com', policy)
        self.assertNotIn('*', policy); self.assertNotIn('https:;', policy)

    def test_public_endpoints_keep_origin_host_csrf_guards(self):
        for extra in ({'Origin':'https://unrelated.invalid'}, {'Host':'wrong.invalid'}, {'X-TurnCart-CSRF':'wrong'}):
            with self.subTest(extra=extra): self.assertEqual(self.request('POST','/api/public-sandbox/begin',{},extra)[0],403)
        self.assertEqual(self.request('POST','/api/public-sandbox/capture',{})[0],404)
        self.assertEqual(self.request('POST','/api/public-sandbox/report',{'attempt_id':'x','cart_sha256':'x','event':[],'order_id':None})[0],400)


if __name__ == '__main__': unittest.main()
