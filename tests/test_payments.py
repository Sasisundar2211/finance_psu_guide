"""Phase 8: Razorpay checkout + the verified-webhook entitlement grant
(REQ-PAY-01..03, API.md §2, SECURITY.md §4, DECISIONS.md D10/D11.1/D12,
TESTING.md "Integration - Payment").

Nothing here contacts Razorpay: the SDK client is mocked at `core.payments` and
`PaymentBase` fails any socket connect. Django tests cannot run browser JS, so the
checkout page script is pinned by source-contract tests here and exercised
separately as a scratch probe (never committed).
"""

import json
import re
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from unittest.mock import PropertyMock, patch

from django.conf import settings
from django.core.handlers.wsgi import WSGIRequest
from django.db import DatabaseError, IntegrityError
from django.test import Client
from django.urls import URLPattern, reverse
from django.utils import timezone

from config import urls as project_urls
from core import payments
from core.access import can_access_course, can_access_mock_test
from core.models import (
    CourseMockTest,
    Enrollment,
    MockTestEnrollment,
    MockTestPricingPlan,
    Order,
    OrderStatus,
    PricingPlan,
)
from tests.mcq_support import server_time
from tests.payment_support import (
    KEY_ID, KEY_SECRET, SENSITIVE_MARKER, WEBHOOK_SECRET, PaymentBase,
    captured_body, razorpay_api, sign, write_statements,
)
from tests.test_student import make_course, make_mock_test

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_TEMPLATE = ROOT / "templates" / "checkout" / "_checkout_script.html"


def inline_script():
    """The checkout driver's own <script> body, with // comments removed."""
    source = SCRIPT_TEMPLATE.read_text(encoding="utf-8")
    body = re.search(r"<script>(.*?)</script>", source, re.S).group(1)
    return re.sub(r"//[^\n]*", "", body)


# --------------------------------------------------------------------------
# create-order (API.md §2, TESTING.md 51)
# --------------------------------------------------------------------------
class CreateOrderTests(PaymentBase):
    def course_payload(self, **extra):
        return {"target_type": "course", "pricing_plan_id": self.plan.pk, **extra}

    def test_anonymous_is_redirected_to_login_and_nothing_is_created(self):
        with razorpay_api() as (client_class, _):
            response = self.post_create(self.course_payload())
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(reverse("account_login")))
        client_class.assert_not_called()
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_get_is_not_allowed(self):
        self.login(self.alice)
        self.assertEqual(self.client.get(self.create_url).status_code, 405)

    def test_csrf_is_enforced_and_the_token_rendered_on_the_page_is_accepted(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.alice)
        body = json.dumps(self.course_payload())
        with razorpay_api("order_CSRF") as (client_class, _):
            denied = client.post(self.create_url, data=body, content_type="application/json")
        self.assertEqual(denied.status_code, 403)
        client_class.assert_not_called()
        self.assertEqual(Order.objects.count(), 0)

        page = client.get(reverse("course_detail", args=[self.course.pk])).content.decode()
        token = re.search(r'data-csrf-token="([^"]+)"', page).group(1)
        with razorpay_api("order_CSRF"):
            allowed = client.post(self.create_url, data=body,
                                  content_type="application/json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(allowed.status_code, 200)

    def test_malformed_bodies_are_rejected_before_razorpay(self):
        self.login(self.alice)
        for body in (b"", b"{not json", b"[]", b'"x"', b"null", b"1"):
            with razorpay_api() as (client_class, _):
                response = self.client.post(
                    self.create_url, data=body, content_type="application/json")
            self.assertEqual(response.status_code, 400, body)
            self.assertEqual(response.json(), {"error": "invalid_request"}, body)
            client_class.assert_not_called()
        self.assertEqual(Order.objects.count(), 0)

    def test_bad_missing_and_mismatched_fields_are_rejected(self):
        self.login(self.alice)
        bad = [{}, {"target_type": "course"}, {"pricing_plan_id": self.plan.pk}]
        bad += [{"target_type": t, "pricing_plan_id": self.plan.pk}
                for t in ("book", "Course", "", None, 5, ["course"])]
        bad += [{"target_type": "course", "pricing_plan_id": p}
                for p in ("1", "", 1.5, 1.0, True, False, None, [1], {"a": 1}, 0, -3)]
        for payload in bad:
            with razorpay_api() as (client_class, _):
                response = self.post_create(payload)
            self.assertEqual(response.status_code, 400, payload)
            client_class.assert_not_called()
        self.assertEqual(Order.objects.count(), 0)

    def test_unknown_plan_ids_are_404_without_a_500_even_when_absurdly_large(self):
        self.login(self.alice)
        for target, pk in (("course", self.plan.pk + 100000),
                           ("mock_test", self.mock_plan.pk + 100000),
                           ("course", 10 ** 30), ("mock_test", 2 ** 63)):
            with razorpay_api() as (client_class, _):
                response = self.post_create({"target_type": target, "pricing_plan_id": pk})
            self.assertEqual(response.status_code, 404, (target, pk))
            self.assertEqual(response.json(), {"error": "not_found"})
            client_class.assert_not_called()
        self.assertEqual(Order.objects.count(), 0)

    def test_target_type_selects_the_plan_table_so_a_plan_id_from_the_other_table_fails(self):
        self.login(self.alice)
        # Explicit, disjoint pks: neither table has a row with the other table's id.
        only_course = PricingPlan.objects.create(
            pk=900001, course=self.course, duration_months=3, price_paise=99900)
        only_mock = MockTestPricingPlan.objects.create(
            pk=900002, mock_test=self.mock, price_paise=1000)
        for target, pk in (("mock_test", only_course.pk), ("course", only_mock.pk)):
            with razorpay_api() as (client_class, _):
                response = self.post_create({"target_type": target, "pricing_plan_id": pk})
            self.assertEqual(response.status_code, 404, (target, pk))
            client_class.assert_not_called()
        self.assertEqual(Order.objects.count(), 0)

    def test_a_plan_of_an_unpublished_item_cannot_be_bought(self):
        # IMPLEMENTATION DETAIL: same 404 as a nonexistent plan, so nothing is disclosed.
        hidden_course = make_course("Hidden Course", published=False)
        hidden_plan = PricingPlan.objects.create(
            course=hidden_course, duration_months=3, price_paise=50000)
        hidden_mock = make_mock_test("Hidden Mock", published=False)
        hidden_mock_plan = MockTestPricingPlan.objects.create(
            mock_test=hidden_mock, price_paise=20000)
        self.login(self.alice)
        for target, plan in (("course", hidden_plan), ("mock_test", hidden_mock_plan)):
            with razorpay_api() as (client_class, _):
                response = self.post_create({"target_type": target, "pricing_plan_id": plan.pk})
            self.assertEqual(response.status_code, 404, target)
            client_class.assert_not_called()
        self.assertEqual(Order.objects.count(), 0)

    def test_client_amount_currency_student_and_course_are_ignored(self):
        other_course = make_course("Other Course")
        self.login(self.alice)
        with razorpay_api("order_TAMPER") as (client_class, instance):
            response = self.post_create(self.course_payload(
                amount=1, amount_paise=1, price=1, currency="USD",
                student=self.bob.pk, student_id=self.bob.pk, course_id=other_course.pk))
        self.assertEqual(response.status_code, 200)
        sent = instance.order.create.call_args.kwargs["data"]
        self.assertEqual((sent["amount"], sent["currency"]), (149900, "INR"))
        order = Order.objects.get(razorpay_order_id="order_TAMPER")
        self.assertEqual((order.amount_paise, order.currency), (149900, "INR"))
        self.assertEqual(order.student, self.alice)
        self.assertEqual(order.course_pricing_plan, self.plan)
        self.assertEqual(response.json()["amount_paise"], 149900)
        self.assertEqual(response.json()["currency"], "INR")

    def test_course_order_stores_the_exact_server_side_values_and_grants_nothing(self):
        self.login(self.alice)
        with razorpay_api("order_COURSE1") as (client_class, instance):
            response = self.post_create(self.course_payload())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "razorpay_order_id": "order_COURSE1",
            "amount_paise": 149900,
            "currency": "INR",
            "razorpay_key_id": KEY_ID,
        })
        client_class.assert_called_once_with(auth=(KEY_ID, KEY_SECRET))
        instance.order.create.assert_called_once_with(
            data={"amount": 149900, "currency": "INR", "payment_capture": 1},
            timeout=payments.RAZORPAY_TIMEOUT_SECONDS,
        )
        order = Order.objects.get()
        self.assertEqual(order.student, self.alice)
        self.assertEqual(order.course_pricing_plan, self.plan)
        self.assertIsNone(order.mock_test_pricing_plan)
        self.assertEqual(order.razorpay_order_id, "order_COURSE1")
        self.assertIsNone(order.razorpay_payment_id)
        self.assertEqual((order.amount_paise, order.currency), (149900, "INR"))
        self.assertEqual(order.status, OrderStatus.CREATED)
        self.assertIsNone(order.verified_at)
        self.assert_nothing_granted()

    def test_mock_test_order_stores_the_exact_server_side_values_and_grants_nothing(self):
        self.login(self.alice)
        with razorpay_api("order_MOCK1") as (_, instance):
            response = self.post_create(
                {"target_type": "mock_test", "pricing_plan_id": self.mock_plan.pk,
                 "amount": 1, "currency": "USD"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["amount_paise"], 49900)
        self.assertEqual(instance.order.create.call_args.kwargs["data"]["amount"], 49900)
        order = Order.objects.get()
        self.assertEqual(order.mock_test_pricing_plan, self.mock_plan)
        self.assertIsNone(order.course_pricing_plan)
        self.assertEqual((order.amount_paise, order.currency, order.status),
                         (49900, "INR", OrderStatus.CREATED))
        self.assertIsNone(order.razorpay_payment_id)
        self.assert_nothing_granted()

    def test_response_and_page_expose_the_key_id_but_never_a_secret(self):
        self.login(self.alice)
        with razorpay_api("order_SECRETS"):
            response = self.post_create(self.course_payload())
        text = response.content.decode()
        self.assertIn(KEY_ID, text)
        self.assertNotIn(KEY_SECRET, text)
        self.assertNotIn(WEBHOOK_SECRET, text)
        page = self.get("course_detail", self.course.pk).content.decode()
        for secret in (KEY_SECRET, WEBHOOK_SECRET):
            self.assertNotIn(secret, page)

    def test_razorpay_failure_creates_no_order_and_leaks_nothing(self):
        self.login(self.alice)
        leaky = Exception(f"gateway said: {KEY_SECRET} {SENSITIVE_MARKER}")
        with razorpay_api(error=leaky):
            response = self.post_create(self.course_payload())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"error": "payment_unavailable"})
        text = response.content.decode()
        for leak in (KEY_SECRET, SENSITIVE_MARKER, "gateway said", KEY_ID):
            self.assertNotIn(leak, text)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_malformed_razorpay_responses_create_no_order(self):
        self.login(self.alice)
        for bad in ({}, {"id": ""}, {"id": "   "}, {"id": None}, {"id": 123},
                    {"id": "x" * 65}, {"order": "order_X"}, "order_X", None, [], 7):
            with razorpay_api(response=bad):
                response = self.post_create(self.course_payload())
            self.assertEqual(response.status_code, 503, bad)
            self.assertNotIn(KEY_ID, response.content.decode(), bad)
        self.assertEqual(Order.objects.count(), 0)

    def test_missing_razorpay_configuration_fails_closed_without_calling_out(self):
        self.login(self.alice)
        for blank in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET"):
            with self.settings(**{blank: ""}), razorpay_api() as (client_class, _):
                response = self.post_create(self.course_payload())
            self.assertEqual(response.status_code, 503, blank)
            client_class.assert_not_called()
        self.assertEqual(Order.objects.count(), 0)

    def test_a_local_persistence_failure_opens_no_checkout(self):
        self.login(self.alice)
        with razorpay_api("order_LOCALFAIL"), \
                patch("core.payment_views.Order.objects.create",
                      side_effect=DatabaseError("db down")):
            response = self.post_create(self.course_payload())
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(KEY_ID, response.content.decode())
        self.assertEqual(Order.objects.count(), 0)

    def test_a_duplicate_razorpay_order_id_is_a_generic_failure_and_touches_nothing(self):
        existing = self.make_order("order_DUP")
        before = self.order_state(existing)
        self.login(self.alice)
        with razorpay_api("order_DUP"):
            response = self.post_create(self.course_payload())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(self.order_state(existing), before)

    def test_create_order_response_is_private_no_store(self):
        self.login(self.alice)
        with razorpay_api("order_CACHE"):
            response = self.post_create(self.course_payload())
        control = response.headers["Cache-Control"]
        self.assertIn("no-store", control)
        self.assertIn("private", control)


# --------------------------------------------------------------------------
# webhook: signature, ordering, event/status (D11.1, D12, TESTING.md 41-46)
# --------------------------------------------------------------------------
class WebhookSignatureTests(PaymentBase):
    def setUp(self):
        super().setUp()
        self.order = self.make_order("order_A1")
        self.body = captured_body("order_A1")

    def test_the_signature_is_over_the_exact_raw_bytes_not_a_reserialization(self):
        odd = (b'{ "payload" :{"payment": {"entity": {"status":"captured",'
               b'"order_id":"order_A1","id":"pay_RAW","amount":149900,'
               b'"currency":"INR"}}},\n   "event"  :  "payment.captured" }')
        self.assertNotEqual(odd, json.dumps(json.loads(odd)).encode())
        self.assertEqual(self.deliver(odd).status_code, 200)
        self.assertEqual(Order.objects.get().status, OrderStatus.VERIFIED)

    def test_a_signature_of_the_reserialized_json_is_rejected(self):
        odd = (b'{"event" :  "payment.captured",  "payload":{"payment":{"entity":'
               b'{"status":"captured","order_id":"order_A1","id":"pay_RAW",'
               b'"amount":149900,"currency":"INR"}}}}')
        reserialized = json.dumps(json.loads(odd)).encode()
        self.assertNotEqual(odd, reserialized)
        response = self.deliver(odd, signature=sign(reserialized))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "invalid_signature"})
        self.assertEqual(self.order_state(self.order)[0], OrderStatus.CREATED)
        self.assert_nothing_granted()

    def test_tampered_body_wrong_secret_and_missing_or_junk_signatures_are_400(self):
        good = sign(self.body)
        tampered = self.body.replace(b"149900", b"100")
        cases = {
            "tampered body": (tampered, good),
            "wrong secret": (self.body, sign(self.body, "not-the-webhook-secret")),
            "empty-string secret": (self.body, sign(self.body, "")),
            "missing header": (self.body, None),
            "empty header": (self.body, ""),
            "truncated": (self.body, good[:-1]),
            "extra character": (self.body, good + "0"),
            "non-ascii": (self.body, "é" * 64),
        }
        before = self.order_state(self.order)
        for name, (body, signature) in cases.items():
            response = self.deliver(body, signature=signature)
            self.assertEqual(response.status_code, 400, name)
            self.assertEqual(response.json(), {"error": "invalid_signature"}, name)
        self.assertEqual(self.order_state(self.order), before)
        self.assert_nothing_granted()

    def test_an_invalid_signature_stops_before_parsing_any_query_or_grant(self):
        client = self.client_class()
        with patch("core.payment_views.json") as fake_json, \
                patch("core.payments.verify_and_grant") as grant, \
                patch.object(WSGIRequest, "POST", new_callable=PropertyMock,
                             side_effect=AssertionError("request.POST was read")), \
                self.assertNumQueries(0):
            response = self.deliver(self.body, signature="0" * 64, client=client)
        self.assertEqual(response.status_code, 400)
        fake_json.loads.assert_not_called()
        grant.assert_not_called()

    def test_a_valid_webhook_is_parsed_only_after_verification_and_never_via_request_post(self):
        seen = []
        real_verify = payments.verify_webhook_signature
        real_loads = json.loads

        def spy_verify(*args, **kwargs):
            seen.append("verify")
            return real_verify(*args, **kwargs)

        def spy_loads(*args, **kwargs):
            seen.append("parse")
            return real_loads(*args, **kwargs)

        with patch("core.payments.verify_webhook_signature", side_effect=spy_verify), \
                patch("core.payment_views.json.loads", side_effect=spy_loads), \
                patch.object(WSGIRequest, "POST", new_callable=PropertyMock,
                             side_effect=AssertionError("request.POST was read")):
            response = self.deliver(self.body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen, ["verify", "parse"])

    def test_malformed_json_that_is_correctly_signed_is_400_with_no_writes(self):
        for raw in (b"{not json", b"", b"[]", b'"x"', b"\xff\xfe"):
            response = self.deliver(raw)
            self.assertEqual(response.status_code, 400, raw)
            self.assertEqual(response.json(), {"error": "invalid_payload"}, raw)
        self.assertEqual(self.order_state(self.order)[0], OrderStatus.CREATED)
        self.assert_nothing_granted()

    def test_a_missing_webhook_secret_fails_closed_without_parsing_or_touching_the_db(self):
        with self.settings(RAZORPAY_WEBHOOK_SECRET=""), \
                patch("core.payment_views.json") as fake_json, \
                patch("core.payments.verify_and_grant") as grant, \
                self.assertNumQueries(0):
            # Even a body signed with the empty string, which an empty key would "verify".
            response = self.deliver(self.body, signature=sign(self.body, ""))
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(WEBHOOK_SECRET, response.content.decode())
        fake_json.loads.assert_not_called()
        grant.assert_not_called()
        self.assertEqual(self.order_state(self.order)[0], OrderStatus.CREATED)
        self.assert_nothing_granted()

    def test_only_post_is_allowed_and_no_login_or_csrf_token_is_needed(self):
        for method in ("get", "put", "delete", "patch"):
            response = getattr(self.client_class(), method)(self.webhook_url)
            self.assertEqual(response.status_code, 405, method)
            self.assertIn("no-store", response.headers["Cache-Control"], method)
        strict = Client(enforce_csrf_checks=True)  # anonymous, no CSRF token at all
        self.assertEqual(self.deliver(self.body, client=strict).status_code, 200)

    def test_every_webhook_response_is_never_cacheable(self):
        for response in (
            self.deliver(self.body, signature="bad"),
            self.deliver(captured_body("order_A1", event="order.paid")),
            self.deliver(self.body),
            self.deliver(self.body),
        ):
            control = response.headers["Cache-Control"]
            self.assertIn("no-store", control)
            self.assertIn("private", control)


class WebhookEventStatusTests(PaymentBase):
    """D11.1: event == payment.captured AND status == captured; event is read first."""

    def setUp(self):
        super().setUp()
        self.order = self.make_order("order_A1")
        self.before = self.order_state(self.order)

    def assert_untouched(self):
        self.assertEqual(self.order_state(self.order), self.before)
        self.assert_nothing_granted()

    def test_payment_authorized_with_a_captured_looking_entity_grants_nothing(self):
        body = captured_body("order_A1", event="payment.authorized", status="captured")
        with patch("core.payments.verify_and_grant") as grant, self.assertNumQueries(0):
            response = self.deliver(body)
        self.assertEqual(response.status_code, 200)
        grant.assert_not_called()
        self.assert_untouched()

    def test_other_events_are_ignored_with_200_including_with_no_payment_payload_at_all(self):
        bodies = [
            captured_body("order_A1", event="order.paid"),
            captured_body("order_A1", event="payment.failed"),
            json.dumps({"event": "order.paid"}).encode(),
            json.dumps({"event": "refund.created", "payload": {}}).encode(),
            json.dumps({"event": "payment.authorized"}).encode(),
            json.dumps({"event": "payment.authorized", "payload": "garbage"}).encode(),
            json.dumps({"event": None}).encode(),
            json.dumps({"event": 5}).encode(),
            json.dumps({}).encode(),
            json.dumps({"event": "PAYMENT.CAPTURED"}).encode(),
        ]
        for body in bodies:
            with patch("core.payments.verify_and_grant") as grant, self.assertNumQueries(0):
                response = self.deliver(body)
            self.assertEqual(response.status_code, 200, body)
            self.assertEqual(response.json(), {"status": "ignored"}, body)
            grant.assert_not_called()
        self.assert_untouched()

    def test_captured_event_with_captured_status_continues_and_verifies(self):
        self.assertEqual(self.deliver(captured_body("order_A1")).status_code, 200)
        self.assertEqual(self.order_state(self.order)[0], OrderStatus.VERIFIED)
        self.assertEqual(Enrollment.objects.count(), 1)

    def test_captured_event_with_any_other_or_missing_status_is_400_and_grants_nothing(self):
        for status in ("authorized", "created", "failed", "refunded", "CAPTURED",
                       "captured ", "", None, True, 1):
            response = self.deliver(captured_body("order_A1", status=status))
            self.assertEqual(response.status_code, 400, status)
            self.assertEqual(response.json(), {"error": "invalid_payload"}, status)
        self.assert_untouched()

    def test_captured_event_with_a_missing_or_malformed_entity_is_400_not_a_traceback(self):
        def event(**payload):
            return json.dumps({"event": "payment.captured", **payload}).encode()

        no_status = {"id": "pay_1", "order_id": "order_A1", "amount": 149900, "currency": "INR"}
        bodies = [
            event(),
            event(payload=None),
            event(payload=[]),
            event(payload={}),
            event(payload={"payment": None}),
            event(payload={"payment": []}),
            event(payload={"payment": {}}),
            event(payload={"payment": {"entity": None}}),
            event(payload={"payment": {"entity": []}}),
            event(payload={"payment": {"entity": {}}}),
            event(payload={"payment": {"entity": no_status}}),
        ]
        for body in bodies:
            response = self.deliver(body)
            self.assertEqual(response.status_code, 400, body)
            self.assertEqual(response.json(), {"error": "invalid_payload"}, body)
            self.assertEqual(response.headers["Content-Type"], "application/json")
        self.assert_untouched()

    def test_unusable_order_or_payment_ids_are_400(self):
        for order_id, payment_id in ((None, "pay_1"), ("", "pay_1"), (5, "pay_1"),
                                     ("order_A1", None), ("order_A1", ""), ("order_A1", 9),
                                     ("order_A1", "p" * 65), ("o" * 65, "pay_1")):
            body = captured_body(order_id, payment_id)
            response = self.deliver(body)
            self.assertEqual(response.status_code, 400, (order_id, payment_id))
        self.assert_untouched()

    def test_an_unknown_order_id_is_400_and_touches_nothing_else(self):
        other = self.make_order("order_OTHER", student=self.bob)
        other_before = self.order_state(other)
        response = self.deliver(captured_body("order_DOES_NOT_EXIST"))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "unknown_order"})
        self.assertEqual(self.order_state(other), other_before)
        self.assert_untouched()

    def test_the_lookup_is_by_order_id_never_by_payment_id(self):
        settled = self.make_order("order_SETTLED", status=OrderStatus.VERIFIED,
                                  payment_id="pay_SETTLED")
        before = self.order_state(settled)
        # A payment id that exists locally, attached to an order id that does not.
        response = self.deliver(captured_body("order_NOPE", payment_id="pay_SETTLED"))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "unknown_order"})
        self.assertEqual(self.order_state(settled), before)
        # And a never-seen payment id still verifies through its order id.
        self.assertEqual(self.deliver(captured_body("order_A1", payment_id="pay_NEW")).status_code, 200)
        self.assertEqual(Order.objects.get(razorpay_order_id="order_A1").razorpay_payment_id, "pay_NEW")

    def test_the_only_grant_call_is_reached_for_captured_events_alone(self):
        with patch("core.payments.verify_and_grant", return_value=payments.VERIFIED) as grant:
            self.deliver(captured_body("order_A1", event="payment.authorized"))
            grant.assert_not_called()
            self.deliver(captured_body("order_A1", status="authorized"))
            grant.assert_not_called()
            self.deliver(captured_body("order_A1"))
            grant.assert_called_once_with("order_A1", "pay_TEST1", 149900, "INR")


class WebhookAmountCurrencyTests(PaymentBase):
    def setUp(self):
        super().setUp()
        self.order = self.make_order("order_A1")
        self.before = self.order_state(self.order)

    def test_an_amount_that_differs_in_any_way_is_400_and_grants_nothing(self):
        for amount in (149899, 149901, 100, 0, 149900 * 100, -149900, "149900", 149900.0,
                       149900.5, True, None, [149900]):
            response = self.deliver(captured_body("order_A1", amount=amount))
            self.assertEqual(response.status_code, 400, amount)
            self.assertEqual(response.json(), {"error": "amount_mismatch"}, amount)
        self.assertEqual(self.order_state(self.order), self.before)
        self.assert_nothing_granted()

    def test_the_amount_is_the_orders_own_value_not_the_current_plan_price(self):
        order = self.create_via_api("course", self.plan, "order_FROZEN")
        self.assertEqual(order.amount_paise, 149900)
        self.plan.price_paise = 99900   # admin edits the price after checkout started
        self.plan.save()
        stale = self.deliver(captured_body("order_FROZEN", payment_id="pay_NEWPRICE", amount=99900))
        self.assertEqual(stale.status_code, 400)
        self.assertEqual(self.order_state(order)[0], OrderStatus.CREATED)
        original = self.deliver(captured_body("order_FROZEN", payment_id="pay_OLDPRICE", amount=149900))
        self.assertEqual(original.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.VERIFIED)
        self.assertEqual(order.amount_paise, 149900)

    def test_a_different_currency_is_400_and_is_never_normalized(self):
        for currency in ("USD", "inr", "Inr", "INR ", "", None, "INRR", 356):
            response = self.deliver(captured_body("order_A1", currency=currency))
            self.assertEqual(response.status_code, 400, currency)
            self.assertEqual(response.json(), {"error": "currency_mismatch"}, currency)
        self.assertEqual(self.order_state(self.order), self.before)
        self.assert_nothing_granted()

    def test_a_mismatch_writes_nothing_at_all(self):
        with self.capture_writes() as captured:
            self.deliver(captured_body("order_A1", amount=1))
            self.deliver(captured_body("order_A1", currency="USD"))
        self.assertEqual(write_statements(captured), [])


# --------------------------------------------------------------------------
# the one entitlement path (REQ-PAY-03, TESTING.md 47/48)
# --------------------------------------------------------------------------
class VerifiedGrantTests(PaymentBase):
    def test_first_course_payment_end_to_end_with_no_admin_action(self):
        order = self.create_via_api("course", self.plan, "order_C1")
        self.assert_nothing_granted()
        self.assertEqual(self.get("my_courses").content.decode().count("PSU Finance"), 0)

        response = self.deliver(captured_body("order_C1", payment_id="pay_C1"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "verified"})

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.VERIFIED)
        self.assertEqual(order.razorpay_payment_id, "pay_C1")
        self.assertIsNotNone(order.verified_at)
        self.assertLess(timezone.now() - order.verified_at, timedelta(seconds=30))

        enrollment = Enrollment.objects.get()
        self.assertEqual(enrollment.student, self.alice)
        self.assertEqual(enrollment.course, self.course)
        self.assertEqual(enrollment.pricing_plan, self.plan)
        self.assertEqual(enrollment.order, order)
        self.assertEqual(enrollment.expires_at, payments.add_months(order.verified_at, 6))
        self.assertEqual(MockTestEnrollment.objects.count(), 0)

        self.assertTrue(can_access_course(self.alice, self.course))
        self.assertFalse(can_access_course(self.bob, self.course))
        self.assertIn("PSU Finance", self.get("my_courses").content.decode())
        self.assertIn("PSU Finance", self.get("dashboard").content.decode())

    def test_first_standalone_mock_payment_end_to_end_grants_only_the_mock_test(self):
        order = self.create_via_api("mock_test", self.mock_plan, "order_M1")
        self.assertFalse(can_access_mock_test(self.alice, self.mock))

        response = self.deliver(captured_body("order_M1", payment_id="pay_M1", amount=49900))
        self.assertEqual(response.status_code, 200)

        order.refresh_from_db()
        self.assertEqual((order.status, order.razorpay_payment_id),
                         (OrderStatus.VERIFIED, "pay_M1"))
        grant = MockTestEnrollment.objects.get()
        self.assertEqual((grant.student, grant.mock_test, grant.order),
                         (self.alice, self.mock, order))
        self.assertEqual(Enrollment.objects.count(), 0)
        self.assertTrue(can_access_mock_test(self.alice, self.mock))
        self.assertFalse(can_access_mock_test(self.bob, self.mock))
        self.assertIn("Standalone Mock", self.get("my_mock_tests").content.decode())

    def test_a_course_purchase_unlocks_included_mock_tests_without_standalone_rows(self):
        included = make_mock_test("Included Mock")
        CourseMockTest.objects.create(course=self.course, mock_test=included)
        self.create_via_api("course", self.plan, "order_C2")
        self.assertFalse(can_access_mock_test(self.alice, included))
        self.deliver(captured_body("order_C2"))
        self.assertTrue(can_access_mock_test(self.alice, included))
        self.assertEqual(MockTestEnrollment.objects.count(), 0)

    def test_a_failed_order_may_still_verify_from_a_valid_captured_webhook(self):
        order = self.make_order("order_F1", status=OrderStatus.FAILED)
        self.assertEqual(self.deliver(captured_body("order_F1")).status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.VERIFIED)
        self.assertEqual(Enrollment.objects.count(), 1)

    def test_an_impossible_target_state_fails_closed_without_writes(self):
        # The DB CheckConstraint makes this unreachable; the branch is defensive.
        order = self.make_order("order_X1")
        before = self.order_state(order)
        with patch("core.payments._plan_target", return_value=None):
            response = self.deliver(captured_body("order_X1"))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "invalid_target"})
        self.assertEqual(self.order_state(order), before)
        self.assert_nothing_granted()


class CourseValidityTests(PaymentBase):
    """IMPLEMENTATION DETAIL: calendar months with a deterministic month-end clamp."""

    def test_add_months_is_calendar_based_and_clamps_the_day(self):
        utc = dt_timezone.utc
        cases = [
            (datetime(2026, 1, 15, 9, 30, 5, 123, tzinfo=utc), 3, datetime(2026, 4, 15, 9, 30, 5, 123, tzinfo=utc)),
            (datetime(2026, 1, 31, 10, 0, tzinfo=utc), 1, datetime(2026, 2, 28, 10, 0, tzinfo=utc)),
            (datetime(2028, 1, 31, 10, 0, tzinfo=utc), 1, datetime(2028, 2, 29, 10, 0, tzinfo=utc)),
            (datetime(2026, 8, 31, 23, 59, 59, tzinfo=utc), 6, datetime(2027, 2, 28, 23, 59, 59, tzinfo=utc)),
            (datetime(2026, 3, 31, 0, 0, tzinfo=utc), 3, datetime(2026, 6, 30, 0, 0, tzinfo=utc)),
            (datetime(2026, 11, 30, 8, 0, tzinfo=utc), 3, datetime(2027, 2, 28, 8, 0, tzinfo=utc)),
            (datetime(2026, 12, 15, 8, 0, tzinfo=utc), 12, datetime(2027, 12, 15, 8, 0, tzinfo=utc)),
            (datetime(2027, 8, 31, 8, 0, tzinfo=utc), 6, datetime(2028, 2, 29, 8, 0, tzinfo=utc)),
            (datetime(2026, 2, 28, 8, 0, tzinfo=utc), 12, datetime(2027, 2, 28, 8, 0, tzinfo=utc)),
            (datetime(2028, 2, 29, 8, 0, tzinfo=utc), 12, datetime(2029, 2, 28, 8, 0, tzinfo=utc)),
        ]
        for start, months, expected in cases:
            self.assertEqual(payments.add_months(start, months), expected, (start, months))
            self.assertEqual(payments.add_months(start, months).tzinfo, start.tzinfo)

    def test_a_month_end_purchase_expires_on_the_clamped_day(self):
        plan = PricingPlan.objects.create(course=self.course, duration_months=6, price_paise=149900)
        self.make_order("order_END", plan=plan)
        moment = datetime(2026, 8, 31, 10, 15, tzinfo=dt_timezone.utc)
        with server_time(moment):
            self.deliver(captured_body("order_END", amount=149900))
        enrollment = Enrollment.objects.get()
        self.assertEqual(enrollment.expires_at, datetime(2027, 2, 28, 10, 15, tzinfo=dt_timezone.utc))
        self.assertEqual(Order.objects.get().verified_at, moment)

    def test_each_plan_length_uses_its_own_duration(self):
        for months in (3, 6, 12):
            course = make_course(f"Course {months}")
            plan = PricingPlan.objects.create(course=course, duration_months=months, price_paise=1000)
            self.make_order(f"order_M{months}", plan=plan)
            moment = datetime(2026, 5, 20, 12, 0, tzinfo=dt_timezone.utc)
            with server_time(moment):
                self.deliver(captured_body(f"order_M{months}", payment_id=f"pay_M{months}", amount=1000))
            self.assertEqual(Enrollment.objects.get(course=course).expires_at,
                             payments.add_months(moment, months))


# --------------------------------------------------------------------------
# idempotency, conflicts, integrity, rollback (D11.1, TESTING.md 49/50, task 31-39)
# --------------------------------------------------------------------------
class IdempotencyTests(PaymentBase):
    def setUp(self):
        super().setUp()
        self.order = self.make_order("order_A1")
        self.body = captured_body("order_A1", payment_id="pay_A")

    def test_a_duplicate_delivery_is_200_and_writes_nothing(self):
        first_time = datetime(2026, 5, 1, 9, 0, tzinfo=dt_timezone.utc)
        with server_time(first_time):
            self.assertEqual(self.deliver(self.body).json(), {"status": "verified"})
        verified = self.order_state(self.order)
        enrollment = Enrollment.objects.values().get()

        with server_time(first_time + timedelta(days=30)), \
                patch.object(Order, "save") as order_save, \
                self.capture_writes() as captured:
            response = self.deliver(self.body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "duplicate"})
        order_save.assert_not_called()
        self.assertEqual(write_statements(captured), [])
        self.assertEqual(self.order_state(self.order), verified)
        self.assertEqual(verified[2], first_time)                     # verified_at not moved
        self.assertEqual(Enrollment.objects.count(), 1)
        self.assertEqual(Enrollment.objects.values().get(), enrollment)  # no extended validity
        self.assertEqual(MockTestEnrollment.objects.count(), 0)

    def test_many_duplicate_deliveries_still_leave_one_grant(self):
        for _ in range(5):
            self.assertEqual(self.deliver(self.body).status_code, 200)
        self.assertEqual(Enrollment.objects.count(), 1)
        self.assertEqual(Order.objects.get().razorpay_payment_id, "pay_A")

    def test_a_conflicting_payment_id_on_a_verified_order_changes_nothing_and_logs_an_anomaly(self):
        first_time = datetime(2026, 5, 1, 9, 0, tzinfo=dt_timezone.utc)
        with server_time(first_time):
            self.deliver(self.body)
        verified = self.order_state(self.order)
        enrollment = Enrollment.objects.values().get()

        with server_time(first_time + timedelta(days=3)), \
                self.capture_writes() as captured, \
                self.assertLogs("core.payments", "WARNING") as logs:
            response = self.deliver(captured_body("order_A1", payment_id="pay_B"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "conflict"})
        self.assertEqual(write_statements(captured), [])
        self.assertEqual(self.order_state(self.order), verified)
        self.assertEqual(verified[1], "pay_A")
        self.assertEqual(Enrollment.objects.values().get(), enrollment)
        joined = "\n".join(logs.output)
        self.assertIn(f"order_pk={self.order.pk}", joined)
        self.assertIn("pay_A", joined)
        self.assertIn("pay_B", joined)

    def test_the_same_payment_id_cannot_verify_a_second_order(self):
        first = self.order
        self.assertEqual(self.deliver(self.body).status_code, 200)
        second = self.make_order("order_A2")
        first_state = self.order_state(first)

        response = self.deliver(captured_body("order_A2", payment_id="pay_A"))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "integrity_error"})
        self.assertNotIn("pay_A", response.content.decode())          # no DB detail leaks

        self.assertEqual(self.order_state(second)[:3], (OrderStatus.CREATED, None, None))
        self.assertFalse(Enrollment.objects.filter(order=second).exists())
        self.assertEqual(Enrollment.objects.count(), 1)
        self.assertEqual(self.order_state(first), first_state)
        # The unique constraint was not weakened, and a fresh payment id still works.
        self.assertEqual(self.deliver(captured_body("order_A2", payment_id="pay_A2")).status_code, 200)
        self.assertEqual(Enrollment.objects.count(), 2)


class RollbackTests(PaymentBase):
    """The Order update and its entitlement are one atomic unit (D10, API.md §2)."""

    def assert_rolled_back(self, order):
        row = Order.objects.get(pk=order.pk)
        self.assertEqual(row.status, OrderStatus.CREATED)
        self.assertIsNone(row.razorpay_payment_id)
        self.assertIsNone(row.verified_at)
        self.assert_nothing_granted()

    def test_a_failure_creating_the_course_enrollment_rolls_the_order_back(self):
        order = self.make_order("order_R1")
        crashing = Client(raise_request_exception=False)
        with patch.object(Enrollment.objects, "create", side_effect=RuntimeError("boom")):
            response = self.deliver(captured_body("order_R1"), client=crashing)
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("boom", response.content.decode())
        self.assert_rolled_back(order)
        # Razorpay's retry then succeeds normally.
        self.assertEqual(self.deliver(captured_body("order_R1")).status_code, 200)
        self.assertEqual(Order.objects.get().status, OrderStatus.VERIFIED)
        self.assertEqual(Enrollment.objects.count(), 1)

    def test_an_integrity_failure_creating_the_mock_enrollment_rolls_the_order_back(self):
        order = self.make_order("order_R2", mock_plan=self.mock_plan)
        with patch.object(MockTestEnrollment.objects, "create",
                          side_effect=IntegrityError("duplicate key")):
            response = self.deliver(captured_body("order_R2", amount=49900))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "integrity_error"})
        self.assertNotIn("duplicate key", response.content.decode())
        self.assert_rolled_back(order)

    def test_a_failure_saving_the_order_grants_nothing(self):
        order = self.make_order("order_R3")
        with patch.object(Order, "save", side_effect=DatabaseError("write failed")):
            response = self.deliver(captured_body("order_R3"),
                                    client=Client(raise_request_exception=False))
        self.assertEqual(response.status_code, 500)
        self.assert_rolled_back(order)

    def test_entitlement_creation_is_inside_the_same_atomic_block_as_the_order_update(self):
        source = (ROOT / "core" / "payments.py").read_text(encoding="utf-8")
        body = source[source.index("def verify_and_grant"):]
        atomic = body.index("with transaction.atomic():")
        for marker in ("select_for_update()", "order.save(", "Enrollment.objects.create(",
                       "MockTestEnrollment.objects.create("):
            self.assertGreater(body.index(marker), atomic, marker)
        self.assertEqual(body.count("transaction.atomic()"), 1)


# --------------------------------------------------------------------------
# browser success is not verification (TESTING.md 53)
# --------------------------------------------------------------------------
class BrowserSuccessGrantsNothingTests(PaymentBase):
    def test_a_browser_success_with_no_webhook_leaves_the_order_created_and_grants_nothing(self):
        order = self.create_via_api("course", self.plan, "order_B1")
        # Everything a browser can do after Razorpay Checkout's success callback:
        # poll the read-only status URL, reload pages.
        for _ in range(3):
            self.assertEqual(self.client.get(
                reverse("checkout_order_status", args=["order_B1"])).json(),
                {"status": "created"})
        self.get("dashboard")
        self.get("my_courses")
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CREATED)
        self.assertIsNone(order.razorpay_payment_id)
        self.assert_nothing_granted()

    def test_a_student_cannot_forge_the_webhook_from_the_browser(self):
        order = self.create_via_api("course", self.plan, "order_B2")
        body = captured_body("order_B2", payment_id="pay_FORGED")
        # The browser callback hands back a payment id and a signature of its own;
        # neither is the webhook secret's HMAC, so neither can verify anything.
        for signature in ("", "razorpay_signature_from_checkout", sign(body, KEY_SECRET),
                          sign(body, "guess")):
            response = self.deliver(body, signature=signature, client=self.client)
            self.assertEqual(response.status_code, 400)
        self.assertEqual(Order.objects.get().status, OrderStatus.CREATED)
        self.assert_nothing_granted()

    def test_no_route_besides_the_signed_webhook_can_verify_or_grant(self):
        self.assertEqual(
            sorted(p.name for p in project_urls.urlpatterns
                   if isinstance(p, URLPattern) and p.name and p.name.startswith("checkout_")),
            ["checkout_create_order", "checkout_order_status", "checkout_webhook"],
        )

        def modules(pattern):
            return {
                path.name for path in (ROOT / "core").glob("*.py")
                if path.name not in ("models.py", "admin.py")
                and re.search(pattern, path.read_text(encoding="utf-8"))
            }

        # One module creates entitlement rows, one module marks an Order verified, and
        # the grant function has exactly one caller: the signature-verified webhook view.
        self.assertEqual(
            modules(r"\b(MockTest)?Enrollment\.objects\.(create|get_or_create|update_or_create|bulk_create)"),
            {"payments.py"})
        self.assertEqual(modules(r"\b(MockTest)?Enrollment\("), set())
        self.assertEqual(modules(r"OrderStatus\.VERIFIED"), {"payments.py"})
        self.assertEqual(modules(r"verified_at\s*="), {"payments.py"})
        self.assertEqual(modules(r"payments\.verify_and_grant\("), {"payment_views.py"})

    def test_the_success_handler_makes_no_request_and_never_touches_payment_ids_or_signatures(self):
        script = inline_script()
        handler = script[script.index("handler:"):script.index("modal:")]
        self.assertRegex(handler, r"handler:\s*function\s*\(\s*\)")      # ignores its argument
        self.assertNotIn("fetch(", handler)
        self.assertNotIn("XMLHttpRequest", handler)
        self.assertIn("Payment received. Verifying payment...", handler)
        self.assertIn("pollStatus(", handler)
        for forbidden in ("razorpay_payment_id", "razorpay_signature"):
            self.assertNotIn(forbidden, script)

    def test_the_script_only_ever_creates_an_order_and_reads_status(self):
        script = inline_script()
        self.assertEqual(script.count("fetch("), 2)
        self.assertEqual(script.count('method: "POST"'), 1)
        self.assertIn("fetch(button.dataset.createOrderUrl", script)
        for forbidden in ("grant", "/verify", "enroll", "XMLHttpRequest", "sendBeacon", "setInterval"):
            self.assertNotIn(forbidden, script)
        self.assertNotIn("webhook", script.lower())

    def test_polling_is_bounded_and_then_falls_back_to_a_safe_message(self):
        script = inline_script()
        self.assertRegex(script, r"var MAX_POLLS = \d+;")
        self.assertLessEqual(int(re.search(r"var MAX_POLLS = (\d+);", script).group(1)), 30)
        self.assertIn("attempt + 1 >= MAX_POLLS", script)
        self.assertIn("giveUp()", script)
        self.assertIn("window.setTimeout(", script)
        self.assertIn("We are still confirming your payment", script)
        # Verified is the only state that navigates away, and only to the dashboard.
        self.assertEqual(script.count("window.location.assign("), 1)
        self.assertIn('data && data.status === "verified"', script)
        self.assertIn("button.dataset.dashboardUrl", script)


# --------------------------------------------------------------------------
# optional order-status endpoint (implementation detail)
# --------------------------------------------------------------------------
class OrderStatusTests(PaymentBase):
    def status_url(self, order_id):
        return reverse("checkout_order_status", args=[order_id])

    def test_login_is_required(self):
        self.make_order("order_S1")
        response = self.client.get(self.status_url("order_S1"))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(reverse("account_login")))

    def test_the_owner_sees_the_real_status_and_only_the_status(self):
        order = self.make_order("order_S1")
        self.login(self.alice)
        for status in (OrderStatus.CREATED, OrderStatus.FAILED, OrderStatus.VERIFIED):
            Order.objects.filter(pk=order.pk).update(
                status=status, razorpay_payment_id="pay_SECRETISH", amount_paise=149900)
            response = self.client.get(self.status_url("order_S1"))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"status": status})
            for leak in ("pay_SECRETISH", "149900", KEY_SECRET, WEBHOOK_SECRET, "order_S1"):
                self.assertNotIn(leak, response.content.decode())

    def test_another_student_and_a_guess_are_indistinguishable(self):
        self.make_order("order_S1", student=self.alice, status=OrderStatus.VERIFIED,
                        payment_id="pay_S1")
        self.login(self.bob)
        theirs = self.client.get(self.status_url("order_S1"))
        guessed = self.client.get(self.status_url("order_NOPE"))
        self.assertEqual(theirs.status_code, 404)
        self.assertEqual((theirs.status_code, theirs.content), (guessed.status_code, guessed.content))
        self.assertNotIn("verified", theirs.content.decode())

    def test_the_response_is_private_no_store(self):
        self.make_order("order_S1")
        self.login(self.alice)
        control = self.client.get(self.status_url("order_S1")).headers["Cache-Control"]
        self.assertIn("no-store", control)
        self.assertIn("private", control)

    def test_it_is_read_only_and_cannot_grant_or_verify(self):
        order = self.make_order("order_S1")
        self.login(self.alice)
        before = (self.counts(), self.order_state(order))
        with self.capture_writes() as captured:
            for _ in range(3):
                self.client.get(self.status_url("order_S1"))
            self.client.get(self.status_url("order_S1") + "?status=verified&razorpay_payment_id=pay_X")
        self.assertEqual(write_statements(captured), [])
        self.assertEqual((self.counts(), self.order_state(order)), before)
        for method in ("post", "put", "delete", "patch"):
            self.assertEqual(getattr(self.client, method)(self.status_url("order_S1")).status_code,
                             405, method)
        self.assertEqual(self.order_state(order)[0], OrderStatus.CREATED)


# --------------------------------------------------------------------------
# checkout UI (TESTING.md 52) - server-rendered contract; JS is source-pinned above
# --------------------------------------------------------------------------
class CourseCheckoutUiTests(PaymentBase):
    def test_a_signed_in_student_gets_a_checkout_cta_wired_to_the_documented_routes(self):
        self.login(self.alice)
        html = self.get("course_detail", self.course.pk).content.decode()
        button = re.search(r"<button[^>]*data-checkout-button[^>]*>", html, re.S).group(0)
        self.assertIn('data-enroll-state="checkout"', button)
        self.assertIn('data-target-type="course"', button)
        self.assertIn(f'data-create-order-url="{reverse("checkout_create_order")}"', button)
        self.assertIn('data-status-url-template="/checkout/order/ORDER_ID/status/"', button)
        self.assertRegex(button, r'data-csrf-token="[^"]{20,}"')
        self.assertIn('src="https://checkout.razorpay.com/v1/checkout.js"', html)
        self.assertEqual(len(re.findall(r"<button[^>]*data-checkout-button", html)), 1)
        self.assertIn("Enroll Now", html)
        self.assertNotRegex(html, r"<button[^>]*\bdisabled\b[^>]*>Enroll Now</button>")
        for hidden in ("Online enrollment is not open yet.", KEY_SECRET, WEBHOOK_SECRET):
            self.assertNotIn(hidden, html)

    def test_each_plan_is_a_radio_carrying_its_own_id_and_the_script_submits_the_checked_one(self):
        second = PricingPlan.objects.create(course=self.course, duration_months=12, price_paise=249900)
        self.login(self.alice)
        html = self.get("course_detail", self.course.pk).content.decode()
        for plan in (self.plan, second):
            self.assertRegex(
                html, rf'<input[^>]*type="radio"[^>]*name="plan"[^>]*value="{plan.pk}"')
        script = inline_script()
        self.assertIn("""#plan-selector input[name="plan"]:checked""", script)
        self.assertIn("pricing_plan_id: planId", script)
        self.assertIn("target_type: button.dataset.targetType", script)
        # The plan is read at click time, so changing the selection changes what is sent.
        self.assertLess(script.index("function selectedPlanId"), script.index('addEventListener("click"'))
        self.assertIn("var planId = selectedPlanId();", script)

    def test_viewing_the_page_never_creates_an_order(self):
        self.client_class().get(reverse("course_detail", args=[self.course.pk]))
        self.login(self.alice)
        self.client.get(reverse("course_detail", args=[self.course.pk]))
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_anonymous_students_still_go_through_login_and_signup_first(self):
        html = self.get("course_detail", self.course.pk).content.decode()
        detail = reverse("course_detail", args=[self.course.pk])
        self.assertIn(f'href="{reverse("account_login")}?next={detail}">Enroll Now</a>', html)
        self.assertIn(f'href="{reverse("account_signup")}?next={detail}"', html)
        for absent in ("data-checkout-button", "checkout.razorpay.com", "data-csrf-token",
                       "csrfmiddlewaretoken"):
            self.assertNotIn(absent, html)

    def test_a_course_without_plans_offers_no_checkout(self):
        empty = make_course("No Plans")
        self.login(self.alice)
        html = self.get("course_detail", empty.pk).content.decode()
        self.assertNotIn("data-checkout-button", html)
        self.assertNotIn("checkout.razorpay.com", html)
        self.assertRegex(html, r"<button[^>]*\bdisabled\b[^>]*>Enroll Now</button>")

    def test_the_signed_in_detail_page_is_never_cacheable(self):
        self.login(self.alice)
        control = self.get("course_detail", self.course.pk).headers["Cache-Control"]
        self.assertIn("no-store", control)
        self.assertIn("private", control)


class MockTestCheckoutUiTests(PaymentBase):
    def test_a_signed_in_student_gets_a_checkout_cta_bound_to_the_mock_test_plan(self):
        self.login(self.alice)
        html = self.get("mock_test_detail", self.mock.pk).content.decode()
        button = re.search(r"<button[^>]*data-checkout-button[^>]*>", html, re.S).group(0)
        self.assertIn('data-target-type="mock_test"', button)
        self.assertIn(f'data-create-order-url="{reverse("checkout_create_order")}"', button)
        self.assertRegex(
            html, rf'<input[^>]*type="radio"[^>]*name="plan"[^>]*value="{self.mock_plan.pk}"[^>]* checked')
        self.assertIn("₹499", html)
        self.assertIn('src="https://checkout.razorpay.com/v1/checkout.js"', html)
        self.assertNotIn(KEY_SECRET, html)

    def test_several_plans_each_carry_their_own_id(self):
        extra = MockTestPricingPlan.objects.create(mock_test=self.mock, price_paise=79900)
        self.login(self.alice)
        html = self.get("mock_test_detail", self.mock.pk).content.decode()
        for plan in (self.mock_plan, extra):
            self.assertRegex(html, rf'<input[^>]*name="plan"[^>]*value="{plan.pk}"')
        radios = re.findall(r'<input[^>]*type="radio"[^>]*>', html)
        self.assertEqual(len(radios), 2)
        self.assertEqual(sum(" checked" in radio for radio in radios), 1)

    def test_viewing_the_page_never_creates_an_order(self):
        self.client_class().get(reverse("mock_test_detail", args=[self.mock.pk]))
        self.login(self.alice)
        self.client.get(reverse("mock_test_detail", args=[self.mock.pk]))
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_anonymous_students_see_the_prices_and_the_login_prompt_only(self):
        html = self.get("mock_test_detail", self.mock.pk).content.decode()
        self.assertIn("₹499", html)
        self.assertIn(reverse("account_login"), html)
        for absent in ("data-checkout-button", "checkout.razorpay.com", 'type="radio"',
                       "csrfmiddlewaretoken", "data-csrf-token"):
            self.assertNotIn(absent, html)

    def test_a_mock_test_without_plans_offers_no_checkout(self):
        bare = make_mock_test("Bare Mock")
        self.login(self.alice)
        html = self.get("mock_test_detail", bare.pk).content.decode()
        self.assertNotIn("data-checkout-button", html)
        self.assertIn("Pricing is not available yet.", html)


# --------------------------------------------------------------------------
# csrf_exempt scope, secrets, logging, config (TESTING.md 55-59, task 57/63)
# --------------------------------------------------------------------------
class PaymentHygieneTests(PaymentBase):
    def test_csrf_exempt_is_used_by_the_razorpay_webhook_alone(self):
        offenders = {
            str(p.relative_to(ROOT))
            for folder in ("core", "config", "templates")
            for p in (ROOT / folder).rglob("*")
            if p.suffix in {".py", ".html"} and "migrations" not in p.parts
            and "csrf_exempt" in p.read_text(encoding="utf-8")
        }
        self.assertEqual(offenders, {str(Path("core") / "payment_views.py")})
        source = (ROOT / "core" / "payment_views.py").read_text(encoding="utf-8")
        self.assertEqual(source.count("@csrf_exempt"), 1)
        self.assertRegex(source, r"@csrf_exempt\s+@never_cache\s+@require_POST\s+def razorpay_webhook")
        exempt = {
            p.name for p in project_urls.urlpatterns
            if isinstance(p, URLPattern) and getattr(p.callback, "csrf_exempt", False)
        }
        self.assertEqual(exempt, {"checkout_webhook"})

    def test_settings_expose_exactly_the_frozen_variable_names(self):
        for name in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET"):
            self.assertTrue(hasattr(settings, name), name)
        settings_source = (ROOT / "config" / "settings.py").read_text(encoding="utf-8")
        for invented in ("RAZORPAY_SECRET", "RAZORPAY_API_KEY", "PAYMENT_SECRET", "rzp_live_"):
            for path in [ROOT / "config" / "settings.py", *(ROOT / "core").glob("*.py")]:
                self.assertNotIn(invented, path.read_text(encoding="utf-8"), (invented, path.name))
        self.assertIn('os.environ.get("RAZORPAY_KEY_ID", "")', settings_source)

    def test_no_secret_or_payload_ever_reaches_the_logs(self):
        self.make_order("order_L1")
        self.login(self.alice)
        body = captured_body("order_L1", payment_id="pay_L1")
        with self.assertLogs("core", "DEBUG") as logs:
            # provider failure whose message carries secrets
            with razorpay_api(error=Exception(f"{KEY_SECRET} {SENSITIVE_MARKER} {WEBHOOK_SECRET}")):
                self.post_create({"target_type": "course", "pricing_plan_id": self.plan.pk})
            # malformed provider response
            with razorpay_api(response={"id": ""}):
                self.post_create({"target_type": "course", "pricing_plan_id": self.plan.pk})
            # missing credentials
            with self.settings(RAZORPAY_KEY_ID=""):
                self.post_create({"target_type": "course", "pricing_plan_id": self.plan.pk})
            # missing webhook secret
            with self.settings(RAZORPAY_WEBHOOK_SECRET=""):
                self.deliver(body)
            # settle then conflict (anomaly log), integrity conflict, invalid target
            self.deliver(body)
            self.deliver(captured_body("order_L1", payment_id="pay_L2"))
            self.make_order("order_L2")
            self.deliver(captured_body("order_L2", payment_id="pay_L1"))
        joined = "\n".join(logs.output)
        for forbidden in (KEY_SECRET, WEBHOOK_SECRET, SENSITIVE_MARKER, body.decode(),
                          "X-Razorpay-Signature", '"email"', "149900"):
            self.assertNotIn(forbidden, joined, forbidden)
        self.assertIn("Conflicting payment", joined)
        self.assertTrue(all(record.startswith(("WARNING:", "ERROR:")) for record in logs.output))

    def test_the_signature_is_hmac_sha256_compared_in_constant_time(self):
        source = (ROOT / "core" / "payments.py").read_text(encoding="utf-8")
        verify = source[source.index("def verify_webhook_signature"):source.index("def add_months")]
        for required in ("hmac.new(", "hashlib.sha256", "hmac.compare_digest("):
            self.assertIn(required, verify)
        self.assertNotRegex(verify, r"(==|!=)\s*signature|signature\s*(==|!=)")

    def test_the_network_guard_makes_any_socket_connect_fail_loudly(self):
        import socket
        with self.assertRaises(AssertionError):
            socket.create_connection(("127.0.0.1", 9), timeout=1)

    def test_the_razorpay_sdk_is_mocked_at_the_one_construction_point(self):
        source = (ROOT / "core" / "payments.py").read_text(encoding="utf-8")
        self.assertEqual(source.count("razorpay.Client("), 1)
        for other in ("import requests", "import httpx", "urllib.request"):
            for path in (ROOT / "core").glob("payment*.py"):
                self.assertNotIn(other, path.read_text(encoding="utf-8"), path.name)

    def test_no_dependency_or_env_example_change_is_needed(self):
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        for name in ("RAZORPAY_KEY_ID=", "RAZORPAY_KEY_SECRET=", "RAZORPAY_WEBHOOK_SECRET="):
            self.assertIn(name, env_example)
        self.assertEqual(len(re.findall(r"^RAZORPAY_", env_example, re.M)), 3)
