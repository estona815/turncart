# TurnCart

An original local prototype: a trained English intent classifier proposes
checkout, edit, abandon or status. A separate catalog review fixes the exact
items and price before a PayPal sandbox order can be created. The original CLI
separates buyer approval and operator-confirmed capture. An optional browser
demonstration uses PayPal's shared public Sandbox merchant and never captures
payments. No model output authorizes payment. The working local web interface
provides intent analysis, exact-cart review and durable local review creation.

The corpus is original fictional text. A deterministic softmax classifier learns
weights from 48 training examples in memory, using Python's standard library.
Negated words have separate features, and punctuation ends negation scope.
Previously unseen plural forms resolve only to a unique word already present
in training. The model uses present features rather than letting absent
vocabulary dominate short requests. Lexical coverage measures familiar input
words independently of unfamiliar word pairs. Confidence and margin gates
remain 0.65 and 0.25, with minimum lexical coverage 0.35; scores are uncalibrated.
Quoted and explanatory requests, conditional action requests and unfamiliar
negations of cancellation language require clarification. An opening single
quote, even without a closing quote, is distinguished from apostrophes inside
contractions and possessives. A checkout score also
abstains when the request includes deferral, prerequisites or requests for
clarification. This conservative guard can defer valid immediate instructions
when an unrelated clause mentions a future delivery time; a fresh instruction
may be needed. Historical-order questions cannot turn a checkout score into a
new purchase suggestion. These gates change no learned weights or payment
controls and do not constitute a complete natural-language parser.

A checkout score also abstains on explicit cart amendments or subsets, past
intent statements, and advice questions such as "should I buy". Change the
items in the catalog interface, then give a fresh present-tense instruction.
These lexical checks can defer benign mixed clauses; they do not parse or apply
arbitrary shopping instructions. Four newly reproduced failures are regression
cases, not independent accuracy measurements.

The 16-example development set and 20 synthetic diagnostic requests have been
used for feature design and regularization selection. They do **not** establish
independent production accuracy. The preceding revision scored 12/16 and 15/20,
including abstentions as errors. These totals hide remaining weaknesses:
some short checkout/cancellation and mixed edit requests still abstain, and a
declined payment can be classified as abandon rather than clarify. The short
request "buy now" now suggests checkout; the earlier edit request mistaken
for checkout now suggests edit. Suggestions remain advisory and never authorize
payment. Non-English and unfamiliar input can abstain. No new training rows or
learned model vocabulary were added. Earlier revisions were evaluated on
separately frozen synthetic sets of 60 and 30 examples. Feedback informed later
gates, so both sets are now development regression data for this revision.
In that preceding revision they contained no false checkout suggestions, with only
23/60 and 11/30 requests receiving a non-clarify suggestion. Their earlier blind
results do not measure this revision's independent accuracy. Broader data and
separate unseen evaluation are still needed; submission readiness is not claimed.

## Run locally

Python 3.9+ is enough. There are no dependencies, hosted models or paid APIs.
Use a new app-state path, separate from the competition coordinator database.

```sh
python turncart.py --state /your/project/turncart-demo.sqlite3 catalog
python turncart.py --state /your/project/turncart-demo.sqlite3 plan \
  --intent 'I am ready to buy my basket' --cart NOTEBOOK:1,PEN:2
python turncart.py --state /your/project/turncart-demo.sqlite3 status OPERATION_ID
python -m unittest -v test_checkout test_intent_model test_turncart test_web
python intent_model.py
```

`plan` runs the actual trained local model and saves a real local review plan
only if its suggestion is checkout. Neither local plans nor test fixture IDs
are PayPal receipts. Cart prices are fixed in integer USD cents, quantities
1–5, and this fictional prototype limits totals to USD50. The user's text is
processed in memory and is not stored in application SQLite.

## Interactive local interface

```sh
turncart_demo_dir=$(mktemp -d)
python3 web.py --state "$turncart_demo_dir/turncart-demo.sqlite3" --port 0
```

Open the printed loopback URL. The default sample basket has one notebook and
two pens for $14.95.

1. Enter “I want to cancel my basket” and choose **Understand my request**.
   **Create checkout review** stays disabled.
2. Enter “I am ready to buy my basket” and analyze it. Confirm the exact
   basket, then choose **Create checkout review**. The saved review shows
   $14.95 and states that payment has not started.
3. Change the pen quantity to three. The basket changes to $17.45 and
   requires a fresh confirmation. The saved review still shows $14.95.

Creating a review saves a
LOCAL_REVIEW operation without a PayPal call. Editing the request or quantities
invalidates previous confirmation. The current browser tab can recover the
review after reload without storing the user's request text. Its operation ID
can also be used with the sandbox CLI below, using the same app-state path.

The server binds only to 127.0.0.1 and checks exact Host, Origin, body size,
content type and a session CSRF value. Local review request nonces are recorded
transactionally so retrying the same nonce/cart after a process restart does
not create another operation. These are application controls, not OS isolation
or a claim that other processes on the same computer cannot access its files.
The web service has no credential input, account creation, provider REST create
or capture endpoint. Its default mode does not load PayPal. The separate CLI
handles operator-authorized REST operations; the opt-in browser mode below
passes the reviewed fictional cart to the public PayPal SDK.

Desktop Chrome and an actual 390px IAB viewport exercised the local workflow.
The original interface tests include real loopback HTTP,
cross-origin rejection, exact-cart invalidation, restart idempotency and rejection
of the previously misclassified edit request even with a supplied confirmation.

## Optional public Sandbox browser demonstration

```sh
python web.py --state /your/project/turncart-demo.sqlite3 --port 0 --public-sandbox
python -m unittest -v test_checkout test_intent_model test_turncart test_web test_public_sandbox
node --test test_app.cjs test_public_app.cjs
```

This mode still requires the exact local cart review before a separate opt-in
loads PayPal's test buttons. A separate PayPal button click may create an order
using `client-id=sb`: PayPal's shared public test merchant, **not your merchant
or developer app**. All goods and prices are fictional. No buyer account,
credentials, real card or real-money payment is needed for the order-creation
observation. This browser implementation does not capture or authorize payments.

Before invoking the SDK create helper, the server commits a consumed cart hash
and an uncertain attempt. A crash or failed callback does not permit another
attempt for that cart. A returned order ID and any buyer-approval callback remain
explicitly **client-reported**, not server-verified provider states. The original
REST operation is not promoted to CREATED or COMPLETED. Changing quantities or
intent invalidates confirmation; an old asynchronous response cannot supply an
order ID to a new review. The same retained Promise cannot repeat the SDK call.
REST and public-SDK create paths also check each other inside the same app-state
database. These controls do not cover separate app-state files, other processes
using different databases, or undocumented retries inside PayPal's SDK.

On 2026-10-19, a real browser loaded the official SDK, reviewed the notebook/pen
USD14.95 basket and clicked the PayPal button once. The SDK returned Sandbox
order ID. The buyer window was closed without login or
approval; no capture was performed. The local attempt persisted as
`SDK_CLIENT_REPORTED_ORDER_ID`, and reloading restored the result with SDK load
and retry disabled. This is a live SDK order-creation observation, not proof of
merchant ownership, server-side order verification or completed payment.

The current Python checks pass 66/66. Earlier Node checks passed 11/11; the last mandatory
changes also passed the focused Python 34/34 regression. Independent reviews
exercised async cart binding, durable consumption, concurrent create guards,
Host/Origin/CSRF rejection and the absence of browser capture calls. Synthetic
SDK fixtures are separate from the single live observation above.

The live SDK warned that its client-side order helpers are deprecated and
recommended server-side integration. Keep this public mode a limited prototype;
use the existing credential-isolated server-side architecture for a future
participant-owned merchant integration. No removal date is claimed here.

Official references:

- [v5 public Sandbox configuration](https://developer.paypal.com/sdk/js/v5/configuration)
- [Server-side checkout integration](https://developer.paypal.com/platforms/checkout/standard/integrate)
- [PayPal client-helper migration guidance](https://github.com/paypal-examples/paypal-sdk-server-side-integration/blob/main/docs/update-from-client-side-helpers-to-server-side.md)

## Participant-owned Sandbox REST integration (not yet exercised here)

An operator must already have lawful access to an existing PayPal developer app
and sandbox buyer. Account creation, eligibility and credentials are separate
from this code. The CLI reads existing credentials through masked interactive
terminal prompts, keeps them in process memory and does not search credential
stores, environment variables or authentication files. Never paste keys into
chat. Use only credentials assigned to a **sandbox** app.

```sh
python turncart.py --state /your/project/turncart-demo.sqlite3 sandbox-create \
  OPERATION_ID --reviewed-cart-sha256 HASH_FROM_THE_REVIEW
# Approve using the returned sandbox.paypal.com link and your sandbox buyer.
python turncart.py --state /your/project/turncart-demo.sqlite3 sandbox-readback OPERATION_ID
python turncart.py --state /your/project/turncart-demo.sqlite3 sandbox-capture OPERATION_ID
```

Transport is fixed to `api-m.sandbox.paypal.com`, validates TLS, follows no
redirects and sets stable request IDs. The controller writes the send phase
before each mutation. A timeout is unknown; it cannot trigger a second create
or capture. A known order can be checked with GET only. An unknown creation
without an order ID requires independent provider-side reconciliation, rather
than pretending no order was created. Restarting the process preserves this
restriction. The controller binds order ID, currency, exact amount and completed
capture ID. It supports the documented capture representation that lacks the
top-level purchase-unit amount.

Official protocol sources:

- https://developer.paypal.com/api/rest/authentication/
- https://developer.paypal.com/api/orders/v2/orders-create
- https://developer.paypal.com/api/orders/v2/orders-capture

Actual participant-owned OAuth/REST, buyer approval and capture calls made
during this preparation: 0. The public-SDK observation above is separate.
Tests use fictional representations and do not establish provider REST
integration success. Public GitHub, the required English video, actual eligibility,
registration, final independent review and hackathon submission are unfinished.
No entry receipt or prize result is claimed.
