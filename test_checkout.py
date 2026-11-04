"""Adversarial local tests. Fixtures never count as PayPal receipts."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from checkout import (API_HOST, CheckoutError, CheckoutStore, SandboxClient,
                      _json, approval_url, cart_payload, order_facts)

CART = [{"sku": "NOTEBOOK", "quantity": 1}]
AMOUNT = {"currency_code": "USD", "value": "9.95"}


def representation(status="CREATED", *, capture=False):
    # Shape based on official Orders create/capture examples; fictional IDs.
    unit = {"amount": copy.deepcopy(AMOUNT)}
    if capture:
        unit = {"reference_id": "default", "payments": {"captures": [{
            "id": "TESTCAPTURE1", "status": "COMPLETED", "amount": copy.deepcopy(AMOUNT)}]}}
    return {"id": "TESTORDER1", "status": status, "purchase_units": [unit],
            "links": [{"rel": "payer-action", "method": "GET",
                       "href": "https://www.sandbox.paypal.com/checkoutnow?token=TESTORDER1"}]}


class FixtureClient:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def call(self, *args):
        self.calls.append(args)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return copy.deepcopy(answer)


class CheckoutTests(unittest.TestCase):
    def setUp(self):
        # Preserve test state for inspection; no automatic deletion.
        root = Path(__file__).resolve().parents[3] / "_tmp" / "paypal__turncart-tests__2026-10-02__v001"
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / (str(uuid.uuid4()) + ".sqlite3")
        self.store = CheckoutStore(self.path)
        self.plan = self.store.prepare(CART)

    def tearDown(self):
        self.store.close()

    def create(self, client):
        return self.store.create(self.plan["operation_id"], client,
            reviewed_cart_sha256=self.plan["cart_sha256"], review_confirmed=True)

    def test_integer_price_order_independent_hash(self):
        a = [{"sku": "PEN", "quantity": 2}, {"sku": "NOTEBOOK", "quantity": 1}]
        self.assertEqual(cart_payload(a)[1], cart_payload(a[::-1])[1])
        self.assertEqual(cart_payload(a)[2]["value"], "14.95")

    def test_cart_rejects_user_price_float_bool_duplicates_and_limit(self):
        bad = [[{"sku": "PEN", "quantity": True}], [{"sku": "PEN", "quantity": 1.0}],
               [{"sku": "PEN", "quantity": 1, "price": 0}], CART + CART,
               [{"sku": "BAG", "quantity": 5}]]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(CheckoutError):
                cart_payload(value)

    def test_model_cannot_replace_review_or_exact_cart(self):
        client = FixtureClient()
        for confirmed, fingerprint in [(False, self.plan["cart_sha256"]), (True, "0" * 64), (1, self.plan["cart_sha256"])]:
            with self.assertRaises(CheckoutError):
                self.store.create(self.plan["operation_id"], client,
                    reviewed_cart_sha256=fingerprint, review_confirmed=confirmed)
        self.assertEqual(client.calls, [])

    def test_creation_saved_and_single_send(self):
        client = FixtureClient(representation())
        self.create(client)
        with self.assertRaises(CheckoutError):
            self.create(client)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.store.status(self.plan["operation_id"])["phase"], "CREATED")

    def test_create_timeout_survives_restart_without_resend(self):
        client = FixtureClient(TimeoutError("private-provider-body"))
        with self.assertRaisesRegex(CheckoutError, "^create_unknown_do_not_resend$"):
            self.create(client)
        self.store.close()
        self.store = CheckoutStore(self.path)
        with self.assertRaises(CheckoutError):
            self.create(client)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.store.status(self.plan["operation_id"])["phase"], "CREATE_UNKNOWN")

    def test_new_review_cannot_resend_unknown_create_for_same_cart(self):
        client = FixtureClient(TimeoutError("synthetic response loss"))
        with self.assertRaisesRegex(CheckoutError, "^create_unknown_do_not_resend$"):
            self.create(client)
        self.store.close()
        self.store = CheckoutStore(self.path)
        duplicate = self.store.prepare(CART)
        self.assertNotEqual(duplicate["operation_id"], self.plan["operation_id"])

        with self.assertRaisesRegex(CheckoutError, "^existing_rest_order_blocks_create$"):
            self.store.create(duplicate["operation_id"], client,
                reviewed_cart_sha256=duplicate["cart_sha256"], review_confirmed=True)

        self.assertEqual([call[0] for call in client.calls], ["POST"])
        self.assertEqual(self.store.status(duplicate["operation_id"])["phase"], "LOCAL_REVIEW")
        self.assertEqual(self.store.status(self.plan["operation_id"])["phase"], "CREATE_UNKNOWN")

    def test_real_documented_capture_shape_without_unit_amount(self):
        facts = order_facts(representation("COMPLETED", capture=True), AMOUNT, "TESTORDER1", completed=True)
        self.assertEqual(facts["capture_id"], "TESTCAPTURE1")

    def test_capture_needs_confirmation_and_remote_buyer_approval(self):
        client = FixtureClient(representation(), representation("CREATED"))
        self.create(client)
        with self.assertRaises(CheckoutError):
            self.store.capture(self.plan["operation_id"], client)
        with self.assertRaisesRegex(CheckoutError, "buyer_approval_required"):
            self.store.capture(self.plan["operation_id"], client, capture_confirmed=True)
        self.assertEqual([x[0] for x in client.calls], ["POST", "GET"])

    def test_approved_capture_and_repeated_capture_denied(self):
        client = FixtureClient(representation(), representation("APPROVED"), representation("COMPLETED", capture=True))
        self.create(client)
        receipt = self.store.capture(self.plan["operation_id"], client, capture_confirmed=True)
        self.assertEqual(receipt["capture_id"], "TESTCAPTURE1")
        self.assertEqual([x[0] for x in client.calls], ["POST", "GET", "POST"])
        with self.assertRaises(CheckoutError):
            self.store.capture(self.plan["operation_id"], client, capture_confirmed=True)
        self.assertEqual(len(client.calls), 3)

    def test_capture_timeout_only_get_readback_after_restart(self):
        client = FixtureClient(representation(), representation("APPROVED"), TimeoutError("private-body"),
                               representation("COMPLETED", capture=True))
        self.create(client)
        with self.assertRaisesRegex(CheckoutError, "capture_unknown_readback_only"):
            self.store.capture(self.plan["operation_id"], client, capture_confirmed=True)
        self.store.close()
        self.store = CheckoutStore(self.path)
        with self.assertRaises(CheckoutError):
            self.store.capture(self.plan["operation_id"], client, capture_confirmed=True)
        self.store.refresh(self.plan["operation_id"], client)
        self.assertEqual([x[0] for x in client.calls], ["POST", "GET", "POST", "GET"])
        self.assertEqual(self.store.status(self.plan["operation_id"])["phase"], "COMPLETED")

    def test_capture_id_amount_state_binding(self):
        for field, value in [("id", "OTHERORDER"), ("status", "APPROVED")]:
            response = representation("COMPLETED", capture=True)
            response[field] = value
            with self.assertRaises(CheckoutError):
                order_facts(response, AMOUNT, "TESTORDER1", completed=True)
        response = representation("COMPLETED", capture=True)
        response["purchase_units"][0]["payments"]["captures"][0]["amount"]["value"] = "0.01"
        with self.assertRaises(CheckoutError):
            order_facts(response, AMOUNT, "TESTORDER1", completed=True)

    def test_completed_malformed_payments_rejected(self):
        response = representation("COMPLETED", capture=True)
        response["purchase_units"][0]["payments"] = []
        with self.assertRaises(CheckoutError):
            order_facts(response, AMOUNT, "TESTORDER1", completed=True)

    def test_approval_link_rejects_live_external_tokens_extra_blank_params(self):
        base = representation()
        bad = ["https://www.paypal.com/checkoutnow?token=TESTORDER1",
               "https://evil.example/checkoutnow?token=TESTORDER1",
               "https://sandbox.paypal.com/checkoutnow?token=OTHER",
               "https://sandbox.paypal.com/checkoutnow?token=TESTORDER1&redirect=",
               "https://[bad/checkoutnow?token=TESTORDER1",
               "https://sandbox.paypal.com/checkoutnow?token=TESTORDER1#x"]
        for href in bad:
            base["links"][0]["href"] = href
            with self.subTest(href=href), self.assertRaises(CheckoutError):
                approval_url(base, "TESTORDER1")

    def test_duplicate_nonfinite_json_rejected(self):
        for value in [b'{"status":"COMPLETED","status":"CREATED"}', b'{"x":NaN}', b'[]']:
            with self.assertRaises(CheckoutError):
                _json(value)

    def test_https_constructor_error_sanitized(self):
        client = SandboxClient("fixture", "fixture")
        with patch("checkout.http.client.HTTPSConnection", side_effect=RuntimeError("private-secret")):
            with self.assertRaisesRegex(CheckoutError, "^sandbox_transport_unknown$"):
                client._request("POST", "/v1/oauth2/token", {})

    def test_transport_fixed_sandbox_no_redirect(self):
        with patch("checkout.http.client.HTTPSConnection") as constructor:
            response = constructor.return_value.getresponse.return_value
            response.status = 302
            response.read.return_value = b'{}'
            with self.assertRaisesRegex(CheckoutError, "provider_http_status"):
                SandboxClient("fixture", "fixture")._request("GET", "/v2/checkout/orders/TESTORDER1", {})
            self.assertEqual(constructor.call_args.args[0], API_HOST)
            self.assertEqual(constructor.return_value.request.call_count, 1)

    def test_readback_failure_sanitized(self):
        client = FixtureClient(representation(), RuntimeError("private-body"))
        self.create(client)
        with self.assertRaisesRegex(CheckoutError, "^readback_unverified$"):
            self.store.refresh(self.plan["operation_id"], client)

    def test_process_exit_during_capture_allows_get_only_recovery(self):
        class InterruptedClient(FixtureClient):
            def call(inner, *args):
                if args[0] == "POST" and args[1].endswith("/capture"):
                    inner.calls.append(args)
                    raise SystemExit("process_exit_fixture")
                return super(InterruptedClient, inner).call(*args)
        client = InterruptedClient(representation(), representation("APPROVED"),
                                   representation("COMPLETED", capture=True))
        self.create(client)
        with self.assertRaises(SystemExit):
            self.store.capture(self.plan["operation_id"], client, capture_confirmed=True)
        self.assertEqual(self.store.status(self.plan["operation_id"])["phase"], "CAPTURE_SENT")
        self.store.close()
        self.store = CheckoutStore(self.path)
        with self.assertRaises(CheckoutError):
            self.store.capture(self.plan["operation_id"], client, capture_confirmed=True)
        self.store.refresh(self.plan["operation_id"], client)
        self.assertEqual([x[0] for x in client.calls], ["POST", "GET", "POST", "GET"])
        self.assertEqual(self.store.status(self.plan["operation_id"])["phase"], "COMPLETED")

    def test_invalid_request_rejected_before_oauth(self):
        client = SandboxClient("fixture", "fixture")
        invalid = [("GET", "https://api-m.paypal.com/", None, None),
                   ("POST", "/v2/checkout/orders", {}, "bad\nkey"),
                   ("POST", "/v2/checkout/orders", {"amount": float("nan")}, None)]
        with patch.object(client, "_request") as transport:
            for args in invalid:
                with self.assertRaises(CheckoutError):
                    client.call(*args)
            self.assertEqual(transport.call_count, 0)

    def test_malformed_credentials_sanitized(self):
        with self.assertRaisesRegex(CheckoutError, "^sandbox_credentials_encoding$"):
            SandboxClient("fixture", "\ud800")


if __name__ == "__main__":
    unittest.main()
