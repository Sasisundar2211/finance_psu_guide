"""Phase 8 Tier-1: real PostgreSQL row-lock races on the Order row
(REQ-PAY-02, API.md §2, SECURITY.md §4, DECISIONS.md D11.1, TESTING.md "Integration - Payment").

TestCase wraps a test in one transaction on one connection, so it cannot model two
webhook deliveries contending for a row lock. These are TransactionTestCase: each
delivery runs on its own thread with its own PostgreSQL connection (closed when the
thread ends) and the outcome is asserted from the database.

Ordering is deterministic and sleep-free, the same way as the Phase 7 races:

* The first delivery is paused *while it holds the Order lock*, by wrapping
  `core.payments._plan_target`, which `verify_and_grant` only reaches after
  `select_for_update()` and the amount/status checks, and before any write.
* The test then starts the second delivery and polls `pg_stat_activity` until
  PostgreSQL reports a backend waiting on a lock (a state check with a timeout),
  and only then releases the first.

Removing `select_for_update()` from the handler makes the second delivery run
straight through instead of queueing, so `wait_until_a_backend_waits_on_a_lock`
fails: these tests are not satisfied by sequential calls.
"""

import threading
import time
from unittest.mock import patch

from django.db import DatabaseError, connection, transaction
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse

from core import payments
from core.models import (
    Enrollment,
    MockTestEnrollment,
    MockTestPricingPlan,
    Order,
    OrderStatus,
    PricingPlan,
)
from tests.payment_support import (
    KEY_ID, KEY_SECRET, WEBHOOK_SECRET, captured_body, sign,
)
from tests.test_mock_concurrency import TIMEOUT, Worker
from tests.test_student import make_course, make_mock_test, make_user


class Gate:
    """Pauses the thread named `holder` inside `_plan_target` (Order lock already held)."""

    def __init__(self, holder):
        self.holder = holder
        self.entered = threading.Event()
        self.proceed = threading.Event()
        self.real = payments._plan_target

    def __call__(self, order):
        if threading.current_thread().name == self.holder:
            self.entered.set()
            if not self.proceed.wait(TIMEOUT):
                raise AssertionError("gate was never released")
        return self.real(order)


@override_settings(
    RAZORPAY_KEY_ID=KEY_ID,
    RAZORPAY_KEY_SECRET=KEY_SECRET,
    RAZORPAY_WEBHOOK_SECRET=WEBHOOK_SECRET,
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class WebhookRaceBase(TransactionTestCase):
    def setUp(self):
        self.alice = make_user("alice")
        self.course = make_course("Race Course")
        self.plan = PricingPlan.objects.create(
            course=self.course, duration_months=6, price_paise=149900)
        self.mock = make_mock_test("Race Mock")
        self.mock_plan = MockTestPricingPlan.objects.create(
            mock_test=self.mock, price_paise=49900)
        self.order = self.make_order("order_RACE")
        self.workers = []
        self.addCleanup(self.join_all)

    # -- data -----------------------------------------------------------------
    def make_order(self, razorpay_order_id, mock=False):
        target = ({"mock_test_pricing_plan": self.mock_plan} if mock
                  else {"course_pricing_plan": self.plan})
        return Order.objects.create(
            student=self.alice, razorpay_order_id=razorpay_order_id,
            amount_paise=(self.mock_plan if mock else self.plan).price_paise,
            currency="INR", **target)

    def body(self, payment_id, order_id="order_RACE", mock=False):
        return captured_body(order_id, payment_id, amount=49900 if mock else 149900)

    def deliver(self, body):
        """One webhook delivery on the calling thread (its own connection)."""
        response = Client().post(
            reverse("checkout_webhook"), data=body, content_type="application/json",
            HTTP_X_RAZORPAY_SIGNATURE=sign(body))
        return response.status_code, response.json()

    # -- plumbing (same shape as the Phase 7 race base) ---------------------------
    def join_all(self):
        for worker in self.workers:
            worker.join(TIMEOUT)

    def spawn(self, name, fn):
        worker = Worker(name, fn)
        self.workers.append(worker)
        worker.start()
        return worker

    def finish(self, *workers):
        for worker in workers:
            worker.join(TIMEOUT)
            self.assertFalse(worker.is_alive(), f"{worker.name} is stuck")
            if worker.error is not None:
                raise worker.error
        return [worker.result for worker in workers]

    def await_event(self, event, worker):
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            if event.wait(0.05):
                return
            if not worker.is_alive():
                self.fail(f"{worker.name} ended before the event: "
                          f"error={worker.error!r} result={worker.result!r}")
        self.fail(f"timed out waiting for {worker.name}")

    def gate(self, holder):
        gate = Gate(holder)
        patcher = patch.object(payments, "_plan_target", gate)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(gate.proceed.set)   # never leave a thread parked on failure
        return gate

    def wait_until_a_backend_waits_on_a_lock(self):
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND pid <> pg_backend_pid() AND wait_event_type = 'Lock'"
                )
                if cursor.fetchone()[0]:
                    return
            time.sleep(0.01)
        self.fail("no delivery ever queued behind the Order lock")

    def race(self, first_body, second_body):
        """Hold the first delivery inside the lock, queue the second behind it, release."""
        gate = self.gate("first")
        first = self.spawn("first", lambda: self.deliver(first_body))
        self.await_event(gate.entered, first)
        second = self.spawn("second", lambda: self.deliver(second_body))
        self.wait_until_a_backend_waits_on_a_lock()
        self.assertTrue(second.is_alive(), "second delivery did not block on the lock")
        self.assertIsNone(second.result)
        gate.proceed.set()
        return self.finish(first, second)


class WebhookRaceTests(WebhookRaceBase):
    def test_the_order_row_is_really_locked_between_the_checks_and_the_grant(self):
        gate = self.gate("first")
        first = self.spawn("first", lambda: self.deliver(self.body("pay_A")))
        self.await_event(gate.entered, first)
        # The handler has read the Order and is about to write; nobody else can lock it.
        with transaction.atomic():
            with self.assertRaises(DatabaseError):
                Order.objects.select_for_update(nowait=True).get(razorpay_order_id="order_RACE")
        self.assertEqual(Order.objects.get().status, OrderStatus.CREATED)  # nothing committed yet
        gate.proceed.set()
        self.finish(first)
        self.assertEqual(Order.objects.get().status, OrderStatus.VERIFIED)

    def test_two_simultaneous_identical_deliveries_verify_once_and_grant_once(self):
        body = self.body("pay_A")
        (code1, json1), (code2, json2) = self.race(body, body)
        self.assertEqual((code1, json1), (200, {"status": "verified"}))
        self.assertEqual((code2, json2), (200, {"status": "duplicate"}))
        order = Order.objects.get()
        self.assertEqual((order.status, order.razorpay_payment_id),
                         (OrderStatus.VERIFIED, "pay_A"))
        self.assertIsNotNone(order.verified_at)
        self.assertEqual(Enrollment.objects.count(), 1)
        self.assertEqual(Enrollment.objects.get().order, order)
        self.assertEqual(MockTestEnrollment.objects.count(), 0)

    def test_two_simultaneous_captures_with_different_payment_ids_grant_once(self):
        (code1, json1), (code2, json2) = self.race(self.body("pay_A"), self.body("pay_B"))
        self.assertEqual((code1, json1), (200, {"status": "verified"}))
        self.assertEqual((code2, json2), (200, {"status": "conflict"}))
        order = Order.objects.get()
        self.assertEqual(order.razorpay_payment_id, "pay_A")   # the lock winner, never replaced
        self.assertEqual(order.status, OrderStatus.VERIFIED)
        self.assertEqual(Enrollment.objects.count(), 1)
        self.assertEqual(Enrollment.objects.get().order, order)

    def test_the_same_race_on_a_standalone_mock_order_grants_one_mock_enrollment(self):
        mock_order = self.make_order("order_RACE_MOCK", mock=True)
        first, second = (self.body("pay_M1", "order_RACE_MOCK", mock=True),
                         self.body("pay_M2", "order_RACE_MOCK", mock=True))
        results = self.race(first, second)
        self.assertEqual([r[0] for r in results], [200, 200])
        self.assertEqual([r[1]["status"] for r in results], ["verified", "conflict"])
        mock_order.refresh_from_db()
        self.assertEqual(mock_order.razorpay_payment_id, "pay_M1")
        self.assertEqual(MockTestEnrollment.objects.count(), 1)
        self.assertEqual(MockTestEnrollment.objects.get().order, mock_order)
        self.assertEqual(Enrollment.objects.count(), 0)

    def test_a_crowd_of_identical_deliveries_yields_one_verification(self):
        for round_number in range(3):
            order_id, payment_id = f"order_CROWD{round_number}", f"pay_CROWD{round_number}"
            self.make_order(order_id)
            body = self.body(payment_id, order_id)
            barrier = threading.Barrier(8)

            def hit():
                barrier.wait(TIMEOUT)
                return self.deliver(body)

            workers = [self.spawn(f"crowd-{round_number}-{i}", hit) for i in range(8)]
            results = self.finish(*workers)
            self.assertEqual({code for code, _ in results}, {200})
            self.assertEqual(sorted(payload["status"] for _, payload in results),
                             ["duplicate"] * 7 + ["verified"])
            order = Order.objects.get(razorpay_order_id=order_id)
            self.assertEqual((order.status, order.razorpay_payment_id),
                             (OrderStatus.VERIFIED, payment_id))
            self.assertEqual(Enrollment.objects.filter(order=order).count(), 1)

    def test_a_crowd_of_different_payment_ids_stores_exactly_one_of_them(self):
        barrier = threading.Barrier(8)

        def hit(payment_id):
            def run():
                barrier.wait(TIMEOUT)
                return payment_id, self.deliver(self.body(payment_id))
            return run

        workers = [self.spawn(f"crowd-{n}", hit(f"pay_{n}")) for n in range(8)]
        results = self.finish(*workers)
        self.assertEqual({code for _, (code, _) in results}, {200})
        outcome = {payment_id: payload["status"] for payment_id, (_, payload) in results}
        winners = [payment_id for payment_id, status in outcome.items() if status == "verified"]
        self.assertEqual(len(winners), 1)
        self.assertEqual(sorted(status for status in outcome.values() if status != "verified"),
                         ["conflict"] * 7)
        order = Order.objects.get()
        self.assertEqual((order.status, order.razorpay_payment_id),
                         (OrderStatus.VERIFIED, winners[0]))
        self.assertEqual(Enrollment.objects.count(), 1)
