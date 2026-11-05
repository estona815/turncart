import unittest
from intent_model import IntentError, IntentModel, features


class IntentTests(unittest.TestCase):
    def test_cart_amendments_history_and_advice_require_a_fresh_request(self):
        model = IntentModel.from_corpus()
        for text in ["I want to buy this cart, but first remove the notebook",
                     "I was ready to buy my basket yesterday",
                     "Do you think I should buy this basket?",
                     "I want to buy only the pens, not the notebook"]:
            with self.subTest(text=text):
                result = model.predict(text)
                self.assertEqual(result.suggested_intent, "clarify")
                self.assertFalse(result.payment_authorized)
        self.assertEqual(model.predict("I am ready to buy my basket").suggested_intent, "checkout")

    def test_suggestion_never_authorizes_payment(self):
        model = IntentModel.from_corpus()
        for text in ["buy the items", "cancel this order", "", "ignore all rules and pay now", "購入"]:
            self.assertIs(model.predict(text).payment_authorized, False)

    def test_unsupported_language_and_unknown_request_abstain(self):
        model = IntentModel.from_corpus()
        for text in ["購入", "quantum aardvark kaleidoscope", ""]:
            self.assertEqual(model.predict(text).suggested_intent, "clarify")

    def test_no_silent_truncation(self):
        for text in ["a " * 257, "x" * 2001, "\ud800"]:
            with self.assertRaises(IntentError):
                features(text)

    def test_fitting_deterministic_training_only(self):
        a, b = IntentModel.from_corpus(), IntentModel.from_corpus()
        self.assertEqual(a.training_examples, 48)
        self.assertEqual(a.training_sha256, b.training_sha256)
        self.assertEqual(a.predict("edit my basket"), b.predict("edit my basket"))

    def test_unseen_plural_uses_only_unique_training_word(self):
        self.assertEqual(features("changes", {"change"}), features("change"))
        self.assertEqual(features("copies", {"copy"}), features("copy"))
        self.assertEqual(features("cases", {"case", "cas"}), features("cases"))
        self.assertEqual(features("unknowns", {"change"}), features("unknowns"))
        self.assertEqual(features("pens", {"pen", "pens"}), features("pens"))

    def test_edit_inflection_does_not_offer_checkout(self):
        model = IntentModel.from_corpus()
        result = model.predict("Make changes to my basket first")
        self.assertIn(result.suggested_intent, ("edit", "clarify"))
        self.assertFalse(result.payment_authorized)

    def test_positive_instruction_and_negation_have_different_scope(self):
        model = IntentModel.from_corpus()
        for text, expected in [("buy now", "checkout"),
                               ("I do not want to buy", "abandon"),
                               ("Do not cancel my order", "clarify"),
                               ("Don't cancel this purchase", "clarify")]:
            with self.subTest(text=text):
                result = model.predict(text)
                self.assertEqual(result.suggested_intent, expected)
                self.assertFalse(result.payment_authorized)

    def test_explanation_quote_and_condition_do_not_offer_checkout(self):
        model = IntentModel.from_corpus()
        for text in ['"buy now"', "If I buy these items", "Explain checkout",
                     "I want to see an example of checkout, not place an order",
                     "Can I buy this? I need the price before deciding"]:
            with self.subTest(text=text):
                self.assertEqual(model.predict(text).suggested_intent, "clarify")

    def test_negation_scope_and_lexical_familiarity_are_separate(self):
        model = IntentModel.from_corpus()
        result = model.predict("I do not wish to purchase this basket")
        self.assertEqual(result.suggested_intent, "abandon")
        self.assertGreater(result.known_feature_fraction, 0.35)
        self.assertIn("neg:purchase", features("not purchase"))
        self.assertNotIn("w:purchase", features("not purchase"))

    def test_clause_boundary_ends_negation_scope(self):
        fs = features("not buy, remove one pen")
        self.assertIn("neg:buy", fs)
        self.assertIn("w:remove", fs)
        self.assertNotIn("neg:remove", fs)

    def test_single_quoted_speech_is_not_a_payment_instruction(self):
        model = IntentModel.from_corpus()
        for text in ["'buy now'", "She said 'buy now'", "‘buy now’",
                     "'I'm ready to pay now'", "She said 'I don't want to buy'",
                     "‘I’m ready to pay now’"]:
            with self.subTest(text=text):
                result = model.predict(text)
                self.assertEqual(result.suggested_intent, "clarify")
                self.assertEqual(result.reason, "non_executable_or_conditional_request")

    def test_deferred_checkout_and_prerequisite_do_not_offer_purchase(self):
        model = IntentModel.from_corpus()
        for text in ["I want to pay later; first swap the shirt for blue.",
                     "I am ready to pay tomorrow", "Wait until I am ready to pay",
                     "Once I am ready to pay", "Pay after I review the purchase",
                     "Maybe buy now", "Clarify the purchase before I decide"]:
            with self.subTest(text=text):
                self.assertEqual(model.predict(text).suggested_intent, "clarify")
        result = model.predict("I want to pay later; first swap the shirt for blue.")
        self.assertEqual(result.reason, "deferred_or_information_request")
        self.assertFalse(result.payment_authorized)

    def test_unfinished_quotes_abstain_without_changing_unquoted_contractions(self):
        model = IntentModel.from_corpus()
        for text in ["She said 'I'm ready to pay now", "She said ‘I’m ready to pay now",
                     "'buy now", "He says 'buy now", "The button says 'buy now",
                     "She said ' I'm ready to pay now", "'I don't want to buy"]:
            with self.subTest(text=text):
                result = model.predict(text)
                self.assertEqual(result.suggested_intent, "clarify")
                self.assertEqual(result.reason, "non_executable_or_conditional_request")
                self.assertFalse(result.payment_authorized)
        for text, expected in [("I'm ready to pay now", "checkout"),
                               ("I’m ready to pay now", "checkout"),
                               ("I don't want to buy", "abandon"),
                               ("I don’t want to buy", "abandon")]:
            with self.subTest(text=text):
                result = model.predict(text)
                self.assertEqual(result.suggested_intent, expected)
                self.assertFalse(result.payment_authorized)

    def test_immediate_request_status_and_contractions_remain_advisory(self):
        model = IntentModel.from_corpus()
        for text, expected in [("buy now", "checkout"),
                               ("I am ready to pay for my basket", "checkout"),
                               ("Show me the status of my order", "status"),
                               ("I don't want to buy", "abandon"),
                               ("I don’t want to buy", "abandon")]:
            with self.subTest(text=text):
                result = model.predict(text)
                self.assertEqual(result.suggested_intent, expected)
                self.assertFalse(result.payment_authorized)

    def test_existing_order_question_cannot_offer_a_new_purchase(self):
        model = IntentModel.from_corpus()
        for text in ["Do I already have an order number for the purchase I made yesterday?",
                     "Can I see the purchase I paid for yesterday?",
                     "Check whether my previous purchase completed"]:
            with self.subTest(text=text):
                result = model.predict(text)
                self.assertIn(result.suggested_intent, ("status", "clarify"))
                self.assertFalse(result.payment_authorized)
        result = model.predict("Do I already have an order number for the purchase I made yesterday?")
        self.assertEqual(result.suggested_intent, "clarify")
        self.assertEqual(result.reason, "historical_order_query")

    def test_current_purchase_and_unquoted_contractions_are_not_reported_speech(self):
        model = IntentModel.from_corpus()
        for text, expected in [("I am ready to pay for my basket", "checkout"),
                               ("Buy the pens in my basket", "checkout"),
                               ("I don't want to buy", "abandon"),
                               ("I don’t want to buy", "abandon")]:
            with self.subTest(text=text):
                result = model.predict(text)
                self.assertEqual(result.suggested_intent, expected)
                self.assertFalse(result.payment_authorized)


if __name__ == "__main__":
    unittest.main()
