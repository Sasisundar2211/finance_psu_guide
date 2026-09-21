"""Razorpay payments: order creation, webhook verification, and the ONE entitlement grant.

REQ-PAY-01..03, API.md §2, SECURITY.md §4, DECISIONS.md D10/D11.1/D12.

The single path that ever creates an `Enrollment` / `MockTestEnrollment` for a
payment is `verify_and_grant`, and it is only ever called by the signature-
verified webhook view. Nothing a browser reports (a Checkout success callback, a
payment id, a signature) reaches it.

Rules `verify_and_grant` follows, in this order, all inside one
`transaction.atomic()`:

* the local Order is found by `razorpay_order_id` (never by payment id, which is
  NULL until this very function sets it) and its row is locked with
  `select_for_update()` before anything about it is read;
* amount and currency are compared against the Order's own immutable values, never
  the current PricingPlan and never anything from the browser;
* an already-verified Order is an idempotent no-op (same payment id) or a logged
  anomaly (different payment id); neither writes anything;
* only an unverified Order is moved to `verified` and, in the same transaction,
  gets exactly one entitlement row. A failure anywhere rolls all of it back.

Provider exception text is never logged or returned: it can echo request data.
"""

import calendar
import hashlib
import hmac
import logging

import razorpay
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import Enrollment, MockTestEnrollment, Order, OrderStatus

logger = logging.getLogger(__name__)

# IMPLEMENTATION DETAIL: bound the outbound Orders API call so a stalled gateway
# cannot pin a Gunicorn worker.
RAZORPAY_TIMEOUT_SECONDS = 10

CAPTURED_EVENT = "payment.captured"
CAPTURED_STATUS = "captured"
MAX_ID_LENGTH = 64  # Order.razorpay_order_id / razorpay_payment_id max_length

# Outcomes of verify_and_grant (the webhook maps them to HTTP status codes).
VERIFIED = "verified"
DUPLICATE = "duplicate"
CONFLICT = "conflict"
UNKNOWN_ORDER = "unknown_order"
AMOUNT_MISMATCH = "amount_mismatch"
CURRENCY_MISMATCH = "currency_mismatch"
INVALID_TARGET = "invalid_target"
INTEGRITY_ERROR = "integrity_error"


class PaymentError(Exception):
    """A payment step failed. Deliberately carries no provider detail."""


def create_razorpay_order(amount_paise, currency):
    """Create a Razorpay-side order and return its id, or raise PaymentError.

    `amount_paise`/`currency` must come from the server-side pricing plan. Fails
    closed on missing credentials, any SDK/network/API error, and a response
    without a usable order id: an id is never invented.
    """
    key_id = settings.RAZORPAY_KEY_ID
    key_secret = settings.RAZORPAY_KEY_SECRET
    if not key_id or not key_secret:
        logger.error("Razorpay API credentials are not configured")
        raise PaymentError
    try:
        client = razorpay.Client(auth=(key_id, key_secret))
        # payment_capture=1: the entitlement contract keys on `payment.captured`,
        # so this order must capture automatically rather than rely on account defaults.
        response = client.order.create(
            data={"amount": amount_paise, "currency": currency, "payment_capture": 1},
            timeout=RAZORPAY_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # SDK, requests and API errors alike; never surface detail
        logger.error("Razorpay order creation failed (%s)", type(exc).__name__)
        raise PaymentError from None
    order_id = response.get("id") if isinstance(response, dict) else None
    if not is_usable_id(order_id):
        logger.error("Razorpay order creation returned no usable order id")
        raise PaymentError
    return order_id


def is_usable_id(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= MAX_ID_LENGTH


def verify_webhook_signature(raw_body, signature, secret):
    """True only if `signature` is the HMAC-SHA256 hex digest of the exact raw body.

    An empty secret is never a valid key, so a missing configuration cannot make a
    forged event verify. Compared in constant time, as bytes so a non-ASCII header
    is a mismatch rather than an exception.
    """
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode("ascii"), signature.strip().encode("utf-8"))


def add_months(moment, months):
    """`moment` plus whole calendar months; the day clamps to the target month's end.

    IMPLEMENTATION DETAIL: the frozen docs say only that `expires_at` derives from
    `PricingPlan.duration_months`. Calendar months (not 30-day blocks) with a
    deterministic month-end clamp: 31 Aug + 6 months = 28/29 Feb, 31 Jan + 3 = 30 Apr.
    """
    index = moment.month - 1 + months
    year = moment.year + index // 12
    month = index % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def _plan_target(order):
    """('course'|'mock_test', plan) for an Order, or None for a corrupt target state."""
    if order.course_pricing_plan_id is not None and order.mock_test_pricing_plan_id is None:
        return "course", order.course_pricing_plan
    if order.mock_test_pricing_plan_id is not None and order.course_pricing_plan_id is None:
        return "mock_test", order.mock_test_pricing_plan
    return None


def verify_and_grant(razorpay_order_id, payment_id, amount, currency):
    """Apply one signature-verified, captured payment to its local Order.

    Called only by the webhook after HMAC, event and status checks. Returns one of
    the module-level outcome constants; nothing is written for any outcome other
    than VERIFIED.
    """
    try:
        with transaction.atomic():
            order = (
                Order.objects.select_for_update()
                .filter(razorpay_order_id=razorpay_order_id)
                .first()
            )
            if order is None:
                return UNKNOWN_ORDER

            # Immutable server-captured values, not the browser and not today's plan price.
            if not (
                isinstance(amount, int)
                and not isinstance(amount, bool)
                and amount == order.amount_paise
            ):
                return AMOUNT_MISMATCH
            if currency != order.currency:
                return CURRENCY_MISMATCH

            if order.status == OrderStatus.VERIFIED:
                if order.razorpay_payment_id == payment_id:
                    return DUPLICATE
                logger.warning(
                    "Conflicting payment on verified order: order_pk=%s existing_payment_id=%s "
                    "conflicting_payment_id=%s",
                    order.pk,
                    order.razorpay_payment_id,
                    payment_id,
                )
                return CONFLICT

            target = _plan_target(order)
            if target is None:
                logger.error("Order %s has no single pricing-plan target", order.pk)
                return INVALID_TARGET
            kind, plan = target

            now = timezone.now()
            order.razorpay_payment_id = payment_id
            order.status = OrderStatus.VERIFIED
            order.verified_at = now
            order.save(update_fields=["razorpay_payment_id", "status", "verified_at"])

            if kind == "course":
                Enrollment.objects.create(
                    student_id=order.student_id,
                    course_id=plan.course_id,
                    pricing_plan=plan,
                    expires_at=add_months(now, plan.duration_months),
                    order=order,
                )
            else:
                MockTestEnrollment.objects.create(
                    student_id=order.student_id,
                    mock_test_id=plan.mock_test_id,
                    order=order,
                )
            return VERIFIED
    except IntegrityError:
        # e.g. this payment id is already stored on a different Order. The atomic
        # block has rolled back, so nothing was verified or granted.
        logger.error("Payment verification hit a database integrity conflict")
        return INTEGRITY_ERROR
