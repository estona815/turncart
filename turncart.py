"""Working local intent-to-reviewed-cart CLI and opt-in real sandbox client.

No credentials are loaded from files/environment. Sandbox actions require an
operator's existing app credentials through masked terminal prompts. Never
paste those credentials into chat. A model suggestion cannot create/capture.
"""
import argparse
from dataclasses import asdict
import getpass
import json
from pathlib import Path
import sys

from checkout import CATALOG, CheckoutError, CheckoutStore, SandboxClient
from intent_model import IntentError, IntentModel


def client_prompt():
    if not sys.stdin.isatty():
        raise CheckoutError("interactive_sandbox_operator_required")
    return SandboxClient(getpass.getpass("Existing sandbox app client ID: "),
                         getpass.getpass("Existing sandbox app client secret: "))


def catalog_cart(value):
    cart = []
    try:
        for entry in value.split(","):
            sku, quantity = entry.split(":")
            if not quantity.isascii() or not quantity.isdecimal():
                raise ValueError()
            cart.append({"sku": sku.strip().upper(), "quantity": int(quantity)})
    except (ValueError, AttributeError):
        raise CheckoutError("cart_syntax_SKU_colon_integer") from None
    return cart


def main(argv=None):
    parser = argparse.ArgumentParser(description="TurnCart: learned local intent, explicit catalog review, sandbox checkout")
    parser.add_argument("--state", type=Path, required=True,
                        help="Your app operation database; never the competition coordination database")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("catalog")
    plan = commands.add_parser("plan")
    plan.add_argument("--intent", required=True)
    plan.add_argument("--cart", required=True, help="e.g. NOTEBOOK:1,PEN:2; catalog prices are fixed")
    for command in ("status", "sandbox-create", "sandbox-readback", "sandbox-capture"):
        child = commands.add_parser(command)
        child.add_argument("operation_id")
        if command == "sandbox-create":
            child.add_argument("--reviewed-cart-sha256", required=True)
    args = parser.parse_args(argv)
    # Refuse the known central contest DB and symlinks. This is separate
    # application state; no competition claim or acceptance is recorded here.
    project = Path(__file__).resolve().parents[3]
    central = project / "_artifacts/logs/video_pipeline/state.sqlite3"
    if args.state.is_symlink() or args.state.resolve() == central.resolve():
        raise CheckoutError("separate_application_state_required")
    if args.command == "catalog":
        print(json.dumps({key: {"name": name, "usd_cents": cents}
                          for key, (name, cents) in CATALOG.items()}, indent=2))
        return 0
    store = CheckoutStore(args.state)
    try:
        if args.command == "plan":
            suggestion = asdict(IntentModel.from_corpus().predict(args.intent))
            # Intent text is processed in memory and is not stored in SQLite.
            if suggestion["suggested_intent"] != "checkout":
                result = {"model_suggestion": suggestion, "operation_created": False,
                          "next_action": "Clarify, edit or inspect the cart before requesting checkout."}
            else:
                result = {"model_suggestion": suggestion,
                          "review": store.prepare(catalog_cart(args.cart)),
                          "cart": catalog_cart(args.cart), "network_calls": 0}
        elif args.command == "status":
            result = store.status(args.operation_id)
        elif args.command == "sandbox-create":
            review = store.review(args.operation_id)
            print(json.dumps({"review": review, "sandbox_only": True}, indent=2))
            if not sys.stdin.isatty() or input("Type CREATE after reviewing the exact amount and cart hash: ") != "CREATE":
                raise CheckoutError("explicit_cart_review_required")
            result = store.create(args.operation_id, client_prompt(),
                reviewed_cart_sha256=args.reviewed_cart_sha256, review_confirmed=True)
        elif args.command == "sandbox-readback":
            result = store.refresh(args.operation_id, client_prompt())
        else:
            print(json.dumps(store.review(args.operation_id), indent=2))
            if not sys.stdin.isatty() or input("Type CAPTURE to capture this buyer-approved SANDBOX order: ") != "CAPTURE":
                raise CheckoutError("explicit_capture_confirmation_required")
            result = store.capture(args.operation_id, client_prompt(), capture_confirmed=True)
        print(json.dumps(result, indent=2))
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CheckoutError, IntentError) as exc:
        print(json.dumps({"error": str(exc), "diagnostic": "sanitized", "receipt_verified": False}), file=sys.stderr)
        raise SystemExit(2)
