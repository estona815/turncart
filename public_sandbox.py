"""Local durable intent and unverified browser reports for public Sandbox SDK.

No provider requests, credentials, approval verification or capture operations.
An attempted cart remains consumed after any crash, error or browser reload.
"""
import json
import sqlite3
import uuid

from checkout import CheckoutError, ORDER_ID, cart_payload


SDK_URL = 'https://www.paypal.com/sdk/js?client-id=sb&currency=USD&intent=capture&components=buttons'
SAMPLE_CART = [{'sku': 'NOTEBOOK', 'quantity': 1}, {'sku': 'PEN', 'quantity': 2}]
SAMPLE_PAYLOAD, SAMPLE_HASH, SAMPLE_AMOUNT = cart_payload(SAMPLE_CART)
MERCHANT = 'PayPal public Sandbox test merchant (client-id=sb), not your merchant'
UNRESOLVED_REST = ('CREATE_SENT', 'CREATE_UNKNOWN', 'CAPTURE_SENT', 'CAPTURE_UNKNOWN')


class PublicSandboxStore:
    def __init__(self, checkout):
        self.checkout = checkout
        self.db = checkout.db
        self.db.execute('''CREATE TABLE IF NOT EXISTS public_sdk_attempts (
            cart_hash TEXT PRIMARY KEY, attempt_id TEXT UNIQUE NOT NULL,
            operation_id TEXT NOT NULL, phase TEXT NOT NULL, order_id TEXT UNIQUE,
            buyer_approval_reported INTEGER NOT NULL, last_client_event TEXT)''')

    def _review(self, identity, fingerprint):
        row = self.checkout._row(identity)
        try:
            stored = json.loads(row[1])
            items = stored['purchase_units'][0]['items']
            payload, expected_hash, amount = cart_payload([
                {'sku': item['sku'], 'quantity': int(item['quantity'])} for item in items])
            valid = (row[4] == 'LOCAL_REVIEW' and fingerprint == row[2] == expected_hash
                and stored == payload and json.loads(row[3]) == amount)
        except (ValueError, KeyError, TypeError, IndexError):
            valid = False
        if not valid:
            raise CheckoutError('exact_local_catalog_review_required')
        return row

    def _rest_clear(self, fingerprint):
        marks = ','.join('?' for _ in UNRESOLVED_REST)
        row = self.db.execute('SELECT 1 FROM operations WHERE phase IN (' + marks +
            ') OR (cart_hash=? AND phase!=?) LIMIT 1',
            (*UNRESOLVED_REST, fingerprint, 'LOCAL_REVIEW')).fetchone()
        if row:
            raise CheckoutError('existing_rest_operation_blocks_public_sdk')

    @staticmethod
    def _status(row):
        return {'cart_sha256': row[0], 'attempt_id': row[1], 'operation_id': row[2],
            'phase': row[3], 'order_id': row[4], 'buyer_approval_reported': bool(row[5]),
            'last_client_event': row[6], 'server_provider_verified': False,
            'client_callback_only': True, 'payment_completed_verified': False,
            'server_capture_implemented': False, 'merchant': MERCHANT,
            'retry_allowed': False}

    def status(self, identity):
        review = self.checkout.review(identity)
        row = self.db.execute('SELECT * FROM public_sdk_attempts WHERE cart_hash=?',
            (review['cart_sha256'],)).fetchone()
        if row:
            return {'enabled': True, 'eligible': False, 'attempt': self._status(row),
                'reason': 'public_attempt_already_consumed'}
        try:
            self._review(identity, review['cart_sha256'])
            self._rest_clear(review['cart_sha256'])
        except CheckoutError as exc:
            return {'enabled': True, 'eligible': False, 'attempt': None, 'reason': str(exc)}
        return {'enabled': True, 'eligible': True, 'attempt': None,
            'cart_sha256': review['cart_sha256'], 'amount': review['amount'], 'sdk_url': SDK_URL,
            'merchant': MERCHANT}

    def begin(self, data):
        if type(data) is not dict or set(data) != {'operation_id', 'reviewed_cart_sha256', 'confirmed'}:
            raise CheckoutError('public_sdk_begin_schema')
        if data['confirmed'] is not True:
            raise CheckoutError('explicit_public_sandbox_review_required')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            review = self._review(data['operation_id'], data['reviewed_cart_sha256'])
            self._rest_clear(data['reviewed_cart_sha256'])
            if self.db.execute('SELECT 1 FROM public_sdk_attempts WHERE cart_hash=?',
                    (review[2],)).fetchone():
                raise CheckoutError('public_attempt_consumed_do_not_retry')
            identity = str(uuid.uuid4())
            # Commit BEFORE the browser may invoke actions.order.create. Unknown
            # includes a crash before that invocation; it never authorizes retry.
            self.db.execute('INSERT INTO public_sdk_attempts VALUES(?,?,?,?,?,?,?)',
                (review[2], identity, data['operation_id'], 'SDK_CREATE_OUTCOME_UNKNOWN',
                    None, 0, None))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        row = self.db.execute('SELECT * FROM public_sdk_attempts WHERE attempt_id=?',
            (identity,)).fetchone()
        return {**self._status(row), 'order_payload': json.loads(review[1]), 'amount': json.loads(review[3])}

    def report(self, data):
        expected = {'attempt_id', 'cart_sha256', 'event', 'order_id'}
        if (type(data) is not dict or set(data) != expected
                or type(data['attempt_id']) is not str
                or type(data['cart_sha256']) is not str
                or type(data['event']) is not str or data['event'] not in {
                'order_id', 'approval', 'cancel', 'error'}):
            raise CheckoutError('public_sdk_report_schema')
        order_id = data['order_id']
        if order_id is not None and (type(order_id) is not str or not ORDER_ID.fullmatch(order_id)):
            raise CheckoutError('public_sdk_client_order_id')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute('SELECT * FROM public_sdk_attempts WHERE attempt_id=?',
                (data['attempt_id'],)).fetchone()
            if row is None or row[0] != data['cart_sha256']:
                raise CheckoutError('public_sdk_attempt_binding')
            event = data['event']
            if event == 'order_id':
                if order_id is None or row[4] not in (None, order_id):
                    raise CheckoutError('public_sdk_client_order_conflict')
                self.db.execute('UPDATE public_sdk_attempts SET phase=?,order_id=?,last_client_event=? WHERE attempt_id=?',
                    ('SDK_CLIENT_REPORTED_ORDER_ID', order_id, event, row[1]))
            elif event == 'approval':
                if order_id is None or row[4] != order_id:
                    raise CheckoutError('public_sdk_client_approval_binding')
                self.db.execute('UPDATE public_sdk_attempts SET buyer_approval_reported=1,last_client_event=? WHERE attempt_id=?',
                    (event, row[1]))
            else:
                if order_id is not None and row[4] != order_id:
                    raise CheckoutError('public_sdk_client_order_conflict')
                self.db.execute('UPDATE public_sdk_attempts SET last_client_event=? WHERE attempt_id=?',
                    (event, row[1]))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        return self._status(self.db.execute('SELECT * FROM public_sdk_attempts WHERE attempt_id=?',
            (data['attempt_id'],)).fetchone())
