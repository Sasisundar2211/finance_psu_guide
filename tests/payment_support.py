"""Shared fixtures for the Phase 8 payment tests.

Every credential here is a dummy. Razorpay is never contacted: `razorpay_api`
replaces the SDK client at the one place `core.payments` builds it, and
`PaymentBase` additionally makes any Python-level socket connect fail loudly, so
a test that slipped past the mock would break instead of calling out.

No test methods live here on purpose (unittest would collect the base class again
in every importing module).
"""

import hashlib
import hmac
import json
import socket
from contextlib import contextmanager
from unittest.mock import patch

from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from core.models import (
    Enrollment,
    MockTestEnrollment,
    MockTestPricingPlan,
    Order,
    OrderStatus,
    PricingPlan,
)
from tests.test_student import StudentTestCase, make_course, make_mock_test

KEY_ID = "rzp_test_phase8_dummy"
KEY_SECRET = "phase8-dummy-secret"
WEBHOOK_SECRET = "phase8-dummy-webhook-secret"
SENSITIVE_MARKER = "SENSITIVE-MARKER-4111"  # stands in for card/UPI data inside a payload

AUTO = object()  # "sign this body with the real webhook secret"

__all__ = [
    "AUTO", "KEY_ID", "KEY_SECRET", "SENSITIVE_MARKER", "WEBHOOK_SECRET", "PaymentBase",
    "captured_body", "razorpay_api", "sign", "write_statements",
]


def sign(body, secret=WEBHOOK_SECRET):
    """Razorpay's scheme, computed independently of the code under test."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def captured_body(order_id, payment_id="pay_TEST1", amount=149900, currency="INR", *,
                  event="payment.captured", status="captured"):
    entity = {
        "id": payment_id,
        "entity": "payment",
        "amount": amount,
        "currency": currency,
        "status": status,
        "order_id": order_id,
        "method": "upi",
        "email": SENSITIVE_MARKER,
    }
    return json.dumps({
        "entity": "event",
        "account_id": "acc_dummy",
        "event": event,
        "contains": ["payment"],
        "payload": {"payment": {"entity": entity}},
        "created_at": 1700000000,
    }).encode()


@contextmanager
def razorpay_api(order_id="order_TEST1", response=AUTO, error=None):
    """Replace the Razorpay SDK client; yields (client_class, client_instance).

    `response` may be any value, including None, to model a malformed reply."""
    with patch("core.payments.razorpay.Client") as client_class:
        instance = client_class.return_value
        if error is not None:
            instance.order.create.side_effect = error
        else:
            instance.order.create.return_value = (
                {"id": order_id, "entity": "order", "status": "created"}
                if response is AUTO else response
            )
        yield client_class, instance


def write_statements(captured):
    """The INSERT/UPDATE/DELETE statements a CaptureQueriesContext recorded."""
    return [
        q["sql"] for q in captured.captured_queries
        if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
    ]


@override_settings(
    RAZORPAY_KEY_ID=KEY_ID,
    RAZORPAY_KEY_SECRET=KEY_SECRET,
    RAZORPAY_WEBHOOK_SECRET=WEBHOOK_SECRET,
)
class PaymentBase(StudentTestCase):
    """alice and bob (no purchases), a published 6-month course plan at Rs 1,499 and a
    published standalone mock test at Rs 499."""

    def setUp(self):
        super().setUp()
        guard = patch.object(
            socket.socket, "connect",
            side_effect=AssertionError("network call attempted in a payment test"),
        )
        guard.start()
        self.addCleanup(guard.stop)
        self.course = make_course("PSU Finance")
        self.plan = PricingPlan.objects.create(
            course=self.course, duration_months=6, price_paise=149900)
        self.mock = make_mock_test("Standalone Mock")
        self.mock_plan = MockTestPricingPlan.objects.create(
            mock_test=self.mock, price_paise=49900)
        self.create_url = reverse("checkout_create_order")
        self.webhook_url = reverse("checkout_webhook")

    # -- create-order ---------------------------------------------------------
    def post_create(self, payload, client=None):
        return (client or self.client).post(
            self.create_url, data=json.dumps(payload), content_type="application/json")

    def create_via_api(self, target_type, plan, razorpay_order_id, user=None):
        """The real create-order endpoint against a mocked Razorpay; returns the Order."""
        self.login(user or self.alice)
        with razorpay_api(razorpay_order_id):
            response = self.post_create(
                {"target_type": target_type, "pricing_plan_id": plan.pk})
        self.assertEqual(response.status_code, 200, response.content)
        return Order.objects.get(razorpay_order_id=razorpay_order_id)

    # -- direct fixtures ------------------------------------------------------
    def make_order(self, razorpay_order_id="order_A1", *, plan=None, mock_plan=None,
                   student=None, amount=None, currency="INR",
                   status=OrderStatus.CREATED, payment_id=None):
        if plan is None and mock_plan is None:
            plan = self.plan
        target = {"course_pricing_plan": plan} if plan is not None else {
            "mock_test_pricing_plan": mock_plan}
        chosen = plan or mock_plan
        return Order.objects.create(
            student=student or self.alice,
            razorpay_order_id=razorpay_order_id,
            razorpay_payment_id=payment_id,
            amount_paise=chosen.price_paise if amount is None else amount,
            currency=currency,
            status=status,
            **target,
        )

    # -- webhook --------------------------------------------------------------
    def deliver(self, body, signature=AUTO, client=None, secret=WEBHOOK_SECRET):
        """POST a webhook. `signature=None` omits the header entirely."""
        headers = {}
        if signature is AUTO:
            signature = sign(body, secret)
        if signature is not None:
            headers["HTTP_X_RAZORPAY_SIGNATURE"] = signature
        return (client or self.client_class()).post(
            self.webhook_url, data=body, content_type="application/json", **headers)

    # -- observation ----------------------------------------------------------
    def counts(self):
        return (Order.objects.count(), Enrollment.objects.count(),
                MockTestEnrollment.objects.count())

    def order_state(self, order):
        row = Order.objects.get(pk=order.pk)
        return (row.status, row.razorpay_payment_id, row.verified_at,
                row.amount_paise, row.currency)

    def assert_nothing_granted(self):
        self.assertEqual(Enrollment.objects.count(), 0)
        self.assertEqual(MockTestEnrollment.objects.count(), 0)

    def capture_writes(self):
        return CaptureQueriesContext(connection)
