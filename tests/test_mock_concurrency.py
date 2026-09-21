"""Phase 7 Tier-1: real PostgreSQL row-lock races on the Attempt row
(REQ-MOCK-04, API.md §5, DECISIONS.md D11.2, TESTING.md "Mock Test Deadline & Concurrency").

TestCase wraps every test in one transaction on one connection, so it cannot
model two requests contending for a row lock. These tests are TransactionTestCase:
each request runs in its own thread with its own PostgreSQL connection (closed
when the thread ends), and the outcome is asserted from the database itself.

Ordering is deterministic, without sleeps:

* The first request is paused *while it holds the Attempt lock* by patching
  `core.mock_views.can_access_mock_test`, which the frozen order runs only
  after the lock is taken (lock -> owner -> entitlement -> ...).
* The test then starts the second request and polls `pg_stat_activity` until
  PostgreSQL reports a backend waiting on a lock (a state check with a
  timeout, not a sleep-and-hope), and only then releases the first request.
"""

import copy
import threading
import time
from datetime import timedelta
from unittest.mock import patch

from django.db import connection, connections, transaction
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse

from core import attempts, mock_views
from core.models import Attempt, AttemptAnswer
from tests.mcq_support import (
    buy_mock_test, formal_question, make_mock_test, post_json,
)
from tests.test_student import make_user

TIMEOUT = 30  # seconds; only ever reached if a test is failing


class Worker(threading.Thread):
    """Runs `fn` on its own thread (so its own DB connection) and keeps the outcome."""

    def __init__(self, name, fn):
        super().__init__(name=name, daemon=True)
        self.fn, self.result, self.error = fn, None, None

    def run(self):
        try:
            self.result = self.fn()
        except BaseException as exc:  # surfaced by the test, never swallowed
            self.error = exc
        finally:
            connections.close_all()


class Gate:
    """Pauses the thread named `holder` inside can_access_mock_test (lock already held)."""

    def __init__(self, holder):
        self.holder = holder
        self.entered = threading.Event()
        self.proceed = threading.Event()
        self.real = mock_views.can_access_mock_test

    def __call__(self, user, mock_test):
        if threading.current_thread().name == self.holder:
            self.entered.set()
            if not self.proceed.wait(TIMEOUT):
                raise AssertionError("gate was never released")
        return self.real(user, mock_test)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class RaceBase(TransactionTestCase):
    def setUp(self):
        self.alice = make_user("alice")
        self.bob = make_user("bob")
        self.mock_test = make_mock_test("Race Test")
        self.q1, self.q2, self.q3 = (
            formal_question(self.mock_test, 1, "A"),
            formal_question(self.mock_test, 2, "B"),
            formal_question(self.mock_test, 3, "C"),
        )
        buy_mock_test(self.alice, self.mock_test)
        self.login_client = Client()
        self.login_client.force_login(self.alice)
        self.workers = []
        self.addCleanup(self.join_all)

    # -- plumbing -----------------------------------------------------------
    def join_all(self):
        for worker in self.workers:
            worker.join(TIMEOUT)

    def session_client(self):
        """A new Client on alice's ONE session (a second login would displace it)."""
        client = Client()
        client.cookies = copy.deepcopy(self.login_client.cookies)
        return client

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
        """Wait for `event`, but fail at once (with its error) if `worker` dies first."""
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
        patcher = patch.object(mock_views, "can_access_mock_test", gate)
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
        self.fail("no request ever queued behind the lock")

    # -- data ---------------------------------------------------------------
    def new_attempt(self, test=None, client=None):
        response = (client or self.login_client).post(
            reverse("mock_test_start", args=[(test or self.mock_test).pk]))
        self.assertEqual(response.status_code, 200, response.content)
        return Attempt.objects.get(pk=response.json()["attempt_id"])

    def save_answer(self, attempt, question, option, client=None):
        return post_json(
            client or self.login_client,
            reverse("attempt_autosave", args=[attempt.pk]),
            {"question_id": question.pk, "selected_option": option},
        )

    def submit_attempt(self, attempt, client=None):
        return post_json(client or self.login_client,
                         reverse("attempt_submit", args=[attempt.pk]))

    def stored(self, attempt):
        return (
            Attempt.objects.get(pk=attempt.pk),
            dict(AttemptAnswer.objects.filter(attempt=attempt)
                 .values_list("question_id", "selected_option")),
        )


class AutosaveVersusSubmitRaceTests(RaceBase):
    def test_a_waiting_autosave_sees_the_finalized_attempt_and_writes_nothing(self):
        attempt = self.new_attempt()
        self.assertEqual(self.save_answer(attempt, self.q1, "A").status_code, 200)  # persisted
        gate = self.gate("holder")

        submitter = self.spawn("holder", lambda: self.submit_attempt(attempt, self.session_client()))
        self.await_event(gate.entered, submitter)              # submit holds the lock
        saver = self.spawn("waiter", lambda: self.save_answer(attempt, self.q2, "B", self.session_client()))
        self.wait_until_a_backend_waits_on_a_lock()            # autosave is queued behind it
        gate.proceed.set()
        submit_response, save_response = self.finish(submitter, saver)

        self.assertEqual(submit_response.status_code, 200)
        self.assertEqual(submit_response.json(), {"score": 1, "total_questions": 3})
        self.assertEqual(save_response.status_code, 409)
        self.assertEqual(save_response.json()["error"], "attempt_finalized")
        row, answers = self.stored(attempt)
        self.assertEqual(answers, {self.q1.pk: "A"})           # the late answer was NOT written
        self.assertIsNotNone(row.submitted_at)
        self.assertEqual((row.score, row.total_questions), (1, 3))

    def test_a_waiting_submit_scores_the_answer_the_autosave_wrote_first(self):
        attempt = self.new_attempt()
        gate = self.gate("holder")

        saver = self.spawn("holder", lambda: self.save_answer(attempt, self.q2, "B", self.session_client()))
        self.await_event(gate.entered, saver)                  # autosave holds the lock
        submitter = self.spawn("waiter", lambda: self.submit_attempt(attempt, self.session_client()))
        self.wait_until_a_backend_waits_on_a_lock()
        gate.proceed.set()
        save_response, submit_response = self.finish(saver, submitter)

        self.assertEqual(save_response.status_code, 200)
        self.assertEqual(submit_response.json(), {"score": 1, "total_questions": 3})
        row, answers = self.stored(attempt)
        self.assertEqual(answers, {self.q2.pk: "B"})
        self.assertEqual((row.score, row.total_questions), (1, 3))

    def test_two_waiting_autosaves_for_one_question_leave_exactly_one_row(self):
        attempt = self.new_attempt()
        gate = self.gate("holder")
        first = self.spawn("holder", lambda: self.save_answer(attempt, self.q1, "A", self.session_client()))
        self.await_event(gate.entered, first)
        second = self.spawn("waiter", lambda: self.save_answer(attempt, self.q1, "D", self.session_client()))
        self.wait_until_a_backend_waits_on_a_lock()
        gate.proceed.set()
        results = self.finish(first, second)

        self.assertEqual([r.status_code for r in results], [200, 200])   # no IntegrityError/500
        _, answers = self.stored(attempt)
        self.assertEqual(answers, {self.q1.pk: "D"})                     # the later writer wins, one row


class SimultaneousSubmitTests(RaceBase):
    def test_two_simultaneous_submits_produce_one_score_and_identical_responses(self):
        attempt = self.new_attempt()
        self.save_answer(attempt, self.q1, "A")
        self.save_answer(attempt, self.q2, "B")
        gate = self.gate("holder")

        with patch.object(mock_views, "finalize", wraps=attempts.finalize) as spy:
            first = self.spawn("holder", lambda: self.submit_attempt(attempt, self.session_client()))
            self.await_event(gate.entered, first)
            second = self.spawn("waiter", lambda: self.submit_attempt(attempt, self.session_client()))
            self.wait_until_a_backend_waits_on_a_lock()
            gate.proceed.set()
            r1, r2 = self.finish(first, second)

        self.assertEqual((r1.status_code, r2.status_code), (200, 200))
        self.assertEqual(r1.json(), {"score": 2, "total_questions": 3})
        self.assertEqual(r2.json(), r1.json())
        self.assertEqual(spy.call_count, 1)                    # only the lock winner scored
        row, answers = self.stored(attempt)
        self.assertEqual((row.score, row.total_questions), (2, 3))
        self.assertEqual(len(answers), 2)

    def test_two_simultaneous_starts_open_one_attempt(self):
        fresh = make_mock_test("Fresh Race Test")
        formal_question(fresh, 1)
        buy_mock_test(self.alice, fresh)
        start = reverse("mock_test_start", args=[fresh.pk])
        gate = self.gate("holder")

        first = self.spawn("holder", lambda: self.session_client().post(start))
        self.await_event(gate.entered, first)                  # holds the per-student lock
        second = self.spawn("waiter", lambda: self.session_client().post(start))
        self.wait_until_a_backend_waits_on_a_lock()
        gate.proceed.set()
        r1, r2 = self.finish(first, second)

        self.assertEqual((r1.status_code, r2.status_code), (200, 200))
        self.assertEqual(r1.json()["attempt_id"], r2.json()["attempt_id"])
        self.assertEqual(Attempt.objects.filter(mock_test=fresh).count(), 1)


class DeadlinePassesWhileWaitingTests(RaceBase):
    """D11.2: a request that queued behind the lock is judged by the clock AFTER it gets the lock."""

    def scenario(self, deadline_passes_while_waiting):
        attempt = self.new_attempt()
        self.assertEqual(self.save_answer(attempt, self.q1, "A").status_code, 200)  # in time, correct
        deadline = attempt.started_at + timedelta(minutes=attempt.time_limit_minutes)
        clock = {"now": attempt.started_at + timedelta(minutes=10)}
        with patch("django.utils.timezone.now", side_effect=lambda: clock["now"]):
            locked, release = threading.Event(), threading.Event()

            def hold_the_lock():
                with transaction.atomic():
                    Attempt.objects.select_for_update().get(pk=attempt.pk)
                    locked.set()
                    release.wait(TIMEOUT)

            holder = self.spawn("holder", hold_the_lock)
            self.await_event(locked, holder)
            waiter = self.spawn("waiter", lambda: self.save_answer(attempt, self.q2, "B", self.session_client()))
            self.wait_until_a_backend_waits_on_a_lock()
            if deadline_passes_while_waiting:
                clock["now"] = deadline + timedelta(seconds=1)   # time passes while it waits
            release.set()
            _, response = self.finish(holder, waiter)
        return attempt, deadline, response

    def test_an_autosave_that_gets_the_lock_after_the_deadline_is_rejected_and_finalizes(self):
        attempt, deadline, response = self.scenario(deadline_passes_while_waiting=True)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(),
                         {"error": "deadline_passed", "score": 1, "total_questions": 3})
        row, answers = self.stored(attempt)
        self.assertEqual(answers, {self.q1.pk: "A"})           # the late answer was not written
        self.assertEqual(row.submitted_at, deadline)
        self.assertEqual((row.score, row.total_questions), (1, 3))

    def test_the_same_wait_without_the_deadline_passing_is_accepted(self):
        """Control: proves the scenario harness itself is not what rejects the answer."""
        attempt, _, response = self.scenario(deadline_passes_while_waiting=False)
        self.assertEqual(response.status_code, 200)
        row, answers = self.stored(attempt)
        self.assertEqual(answers, {self.q1.pk: "A", self.q2.pk: "B"})
        self.assertIsNone(row.submitted_at)


class LockScopeTests(RaceBase):
    def test_one_students_attempt_lock_never_blocks_another_student_on_the_same_test(self):
        """The lock is on the Attempt row alone, not the shared MockTest row."""
        attempt = self.new_attempt()
        buy_mock_test(self.bob, self.mock_test)
        bob_client = Client()
        bob_client.force_login(self.bob)
        locked, release = threading.Event(), threading.Event()

        def hold_the_lock():
            with transaction.atomic():
                Attempt.objects.select_for_update().get(pk=attempt.pk)
                locked.set()
                release.wait(TIMEOUT)

        def bob_takes_the_test():
            started = self.new_attempt(client=bob_client)
            saved = self.save_answer(started, self.q1, "A", bob_client)
            submitted = self.submit_attempt(started, bob_client)
            return saved.status_code, submitted.json()

        holder = self.spawn("holder", hold_the_lock)
        self.await_event(locked, holder)
        bob = self.spawn("bob", bob_takes_the_test)
        try:
            bob.join(TIMEOUT)
            self.assertFalse(bob.is_alive(), "bob was blocked by alice's row lock")
        finally:
            release.set()
        self.finish(holder, bob)
        self.assertEqual(bob.result, (200, {"score": 1, "total_questions": 3}))
