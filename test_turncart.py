from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from checkout import CheckoutError
from turncart import main


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[3] / "_tmp" / "paypal__turncart-cli-tests__2026-10-02__v001"
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / (str(uuid.uuid4()) + ".sqlite3")

    def run_cli(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["--state", str(self.path), *args]), 0)
        return json.loads(output.getvalue())

    def test_actual_model_to_plan_then_persistent_status(self):
        result = self.run_cli("plan", "--intent", "I am ready to buy my basket", "--cart", "NOTEBOOK:1,PEN:2")
        self.assertEqual(result["network_calls"], 0)
        self.assertIs(result["model_suggestion"]["payment_authorized"], False)
        status = self.run_cli("status", result["review"]["operation_id"])
        self.assertEqual(status["cart_sha256"], result["review"]["cart_sha256"])
        self.assertEqual(status["amount"]["value"], "14.95")
        self.assertEqual(status["phase"], "LOCAL_REVIEW")
        self.assertIsNone(status["order_id"])

    def test_noninteractive_create_never_prompts_for_credentials(self):
        result = self.run_cli("plan", "--intent", "I am ready to buy my basket", "--cart", "NOTEBOOK:1,PEN:2")
        output = io.StringIO()
        with patch("turncart.sys.stdin.isatty", return_value=False), patch("turncart.client_prompt") as client:
            with redirect_stdout(output), self.assertRaises(CheckoutError):
                main(["--state", str(self.path), "sandbox-create", result["review"]["operation_id"],
                      "--reviewed-cart-sha256", result["review"]["cart_sha256"]])
            self.assertEqual(client.call_count, 0)
        review = json.loads(output.getvalue())["review"]
        self.assertEqual([x["sku"] for x in review["items"]], ["NOTEBOOK", "PEN"])
        self.assertEqual(review["items"][1]["quantity"], "2")
        self.assertEqual(self.run_cli("status", result["review"]["operation_id"])["phase"], "LOCAL_REVIEW")

    def test_central_coordination_database_is_not_application_state(self):
        central = Path(__file__).resolve().parents[3] / "_artifacts/logs/video_pipeline/state.sqlite3"
        with self.assertRaisesRegex(CheckoutError, "separate_application_state_required"):
            main(["--state", str(central), "catalog"])


if __name__ == "__main__":
    unittest.main()
