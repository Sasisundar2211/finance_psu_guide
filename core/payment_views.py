"""Checkout endpoints: create-order, order status, and the Razorpay webhook
(REQ-PAY-01..03, API.md §2, SECURITY.md §4, DECISIONS.md D11.1/D12).

* `create_order` and `order_status` are student endpoints: login required, CSRF
  protected, private/no-store. Neither ever creates an entitlement.
* `razorpay_webhook` is Razorpay -> server. It is the ONLY csrf_exempt view in the
  project and the ONLY caller of `core.payments.verify_and_grant`.

Webhook order of operations (API.md §2 as narrated in D12): raw body -> HMAC
against the raw bytes -> only then parse JSON -> read top-level `event` alone ->
anything but `payment.captured` is ignored with 200 without looking at nested
data -> for `payment.captured`, require entity status `captured` too -> hand off.
"""

import json
import logging

from django.conf import settings
from django.db import DatabaseError, transaction
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import payments
from .models import MockTestPricingPlan, Order, OrderStatus, PricingPlan
from .student_views import student_page

logger = logging.getLogger(__name__)

TARGET_COURSE = "course"
TARGET_MOCK_TEST = "mock_test"
CURRENCY = "INR"


def _error(code, status):
    return JsonResponse({"error": code}, status=status)


def _json_object(raw_body):
    """`raw_body` as a JSON object, or None if it is not one."""
    try:
        data = json.loads(raw_body or b"")
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _plan_id(value):
    """A positive JSON integer, or None. A bool is an int in Python but not an id."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _resolve_plan(target_type, plan_id):
    """The purchasable pricing plan for `target_type`, or None.

    IMPLEMENTATION DETAIL (docs are silent on a direct POST for an unpublished
    item): a plan whose course/mock test is not published is treated exactly like
    a nonexistent one, so a guessed id cannot buy what the public site does not
    offer and its existence is not disclosed.
    """
    if target_type == TARGET_COURSE:
        return PricingPlan.objects.filter(pk=plan_id, course__is_published=True).first()
    return MockTestPricingPlan.objects.filter(
        pk=plan_id, mock_test__is_published=True
    ).first()


@student_page(("POST",))
def create_order(request):
    """API.md §2: derive the price server-side, create the Razorpay order, then the
    local Order, and only then hand the browser what Checkout needs.

    Only `target_type` and `pricing_plan_id` are read from the body; any other key
    (amount, currency, student, course id...) is ignored, never trusted.
    """
    body = _json_object(request.body)
    if body is None:
        return _error("invalid_request", 400)
    target_type = body.get("target_type")
    plan_id = _plan_id(body.get("pricing_plan_id"))
    if target_type not in (TARGET_COURSE, TARGET_MOCK_TEST) or plan_id is None:
        return _error("invalid_request", 400)

    plan = _resolve_plan(target_type, plan_id)
    if plan is None:
        return _error("not_found", 404)

    amount_paise = plan.price_paise
    try:
        razorpay_order_id = payments.create_razorpay_order(amount_paise, CURRENCY)
        target = (
            {"course_pricing_plan": plan}
            if target_type == TARGET_COURSE
            else {"mock_test_pricing_plan": plan}
        )
        with transaction.atomic():
            Order.objects.create(
                student=request.user,
                razorpay_order_id=razorpay_order_id,
                amount_paise=amount_paise,
                currency=CURRENCY,
                status=OrderStatus.CREATED,
                **target,
            )
    except payments.PaymentError:
        return _payment_unavailable()
    except DatabaseError as exc:  # local persistence failed: checkout must not open
        logger.error("Local order could not be saved (%s)", type(exc).__name__)
        return _payment_unavailable()

    return JsonResponse(
        {
            "razorpay_order_id": razorpay_order_id,
            "amount_paise": amount_paise,
            "currency": CURRENCY,
            "razorpay_key_id": settings.RAZORPAY_KEY_ID,
        }
    )


def _payment_unavailable():
    return _error("payment_unavailable", 503)


@student_page()
def order_status(request, razorpay_order_id):
    """Read-only status of the caller's own Order, for the post-payment page to poll.

    IMPLEMENTATION DETAIL, not a frozen API. It reports state only; it never
    verifies or grants. Another student's order and an unknown id look identical.
    """
    status = (
        Order.objects.filter(student=request.user, razorpay_order_id=razorpay_order_id)
        .values_list("status", flat=True)
        .first()
    )
    if status is None:
        return _error("not_found", 404)
    return JsonResponse({"status": status})


def _ok(outcome):
    return JsonResponse({"status": outcome})


def _captured_entity(payload):
    """`payload.payment.entity` as a dict, or None if the shape is not as expected."""
    nested = payload.get("payload")
    payment = nested.get("payment") if isinstance(nested, dict) else None
    entity = payment.get("entity") if isinstance(payment, dict) else None
    return entity if isinstance(entity, dict) else None


@csrf_exempt
@never_cache
@require_POST
def razorpay_webhook(request):
    # 1. Raw bytes first: the signature is over exactly what Razorpay sent.
    raw_body = request.body

    # 2. HMAC. A missing secret fails closed; an empty secret never verifies anything.
    secret = settings.RAZORPAY_WEBHOOK_SECRET
    if not secret:
        logger.error("Razorpay webhook secret is not configured; event refused")
        return _error("webhook_unavailable", 503)
    signature = request.headers.get("X-Razorpay-Signature", "")
    if not payments.verify_webhook_signature(raw_body, signature, secret):
        # 3. Invalid or missing signature: stop. No JSON parsing, no DB access.
        return _error("invalid_signature", 400)

    # 4. Parse only now that the body is authenticated.
    payload = _json_object(raw_body)
    if payload is None:
        return _error("invalid_payload", 400)

    # 5-6. Top-level event only. Anything else is a valid-but-irrelevant event:
    # 200 so Razorpay does not retry, and nothing nested is inspected.
    if payload.get("event") != payments.CAPTURED_EVENT:
        return _ok("ignored")

    # 7. payment.captured: the entity must ALSO say captured (event AND status).
    entity = _captured_entity(payload)
    if entity is None or entity.get("status") != payments.CAPTURED_STATUS:
        return _error("invalid_payload", 400)
    order_id = entity.get("order_id")
    payment_id = entity.get("id")
    if not payments.is_usable_id(order_id) or not payments.is_usable_id(payment_id):
        return _error("invalid_payload", 400)

    outcome = payments.verify_and_grant(
        order_id, payment_id, entity.get("amount"), entity.get("currency")
    )
    if outcome in (payments.VERIFIED, payments.DUPLICATE, payments.CONFLICT):
        # A conflicting payment on an already-settled order is logged inside
        # verify_and_grant and answered 200 so Razorpay stops retrying it.
        return _ok(outcome)
    return _error(outcome, 400)

