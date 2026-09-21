"""Phase 7: formal mock-test attempts (REQ-MOCK-01/04/05, API.md §4-§5,
DECISIONS.md D10, D11.2).

Deadline tests freeze the server clock instead of sleeping. Real Postgres
row-lock races live in tests/test_mock_concurrency.py.
"""

import re
from datetime import datetime, timedelta

from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from core import attempts
from core.models import (
    Attempt, AttemptAnswer, CourseMockTest, Enrollment, MockTestEnrollment, Order, Question,
)
from tests.mcq_support import (
    MockBase, buy_mock_test, enroll, formal_question, make_course, make_library,
    make_mock_test, post_json, practice_question, server_time,
)
from unittest.mock import patch

NO_STORE = ("private", "no-store")


def without_tokens(response):
    """The response body with the random per-request CSRF tokens removed."""
    return re.sub(r'value="[^"]{64}"', "", response.content.decode())
PUBLIC_QUESTION_KEYS = {"id", "text", "option_a", "option_b", "option_c", "option_d"}


def write_queries(queries):
    return [q["sql"] for q in queries
            if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]


class StartTests(MockBase):
    def test_anonymous_is_sent_to_login_and_nothing_is_created(self):
        response = self.client.post(self.start_url())
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("account_login"), response["Location"])
        self.assertEqual(Attempt.objects.count(), 0)

    def test_standalone_purchase_grants_start_with_the_frozen_response_shape(self):
        self.login(self.alice)
        response = self.client.post(self.start_url())
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(set(data), {"attempt_id", "time_limit_minutes", "questions"})
        self.assertEqual(data["time_limit_minutes"], 60)
        self.assertEqual([q["id"] for q in data["questions"]], [q.pk for q in self.questions])
        attempt = Attempt.objects.get(pk=data["attempt_id"])
        self.assertEqual(attempt.student, self.alice)
        self.assertEqual(attempt.mock_test, self.mock_test)
        self.assertIsNone(attempt.submitted_at)
        self.assertIsNone(attempt.score)

    def test_active_course_enrollment_with_a_coursemocktest_link_grants_start(self):
        course = make_course("Includes Test")
        test = make_mock_test("Included")
        formal_question(test, 1)
        CourseMockTest.objects.create(course=course, mock_test=test)
        enroll(self.bob, course)
        self.login(self.bob)
        self.assertEqual(self.client.post(self.start_url(test)).status_code, 200)
        self.assertEqual(Attempt.objects.get().student, self.bob)

    def test_expired_course_enrollment_does_not_grant_start(self):
        course = make_course("Lapsed")
        test = make_mock_test("Lapsed Test")
        formal_question(test, 1)
        CourseMockTest.objects.create(course=course, mock_test=test)
        enroll(self.bob, course, days=-1)
        self.login(self.bob)
        response = self.client.post(self.start_url(test))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Attempt.objects.count(), 0)

    def test_no_entitlement_and_cross_student_are_403_and_unknown_test_looks_identical(self):
        self.login(self.bob)  # alice owns the purchase, bob owns nothing
        denied = self.client.post(self.start_url())
        unknown = self.client.post(reverse("mock_test_start", args=[self.mock_test.pk + 999]))
        self.assertEqual((denied.status_code, unknown.status_code), (403, 403))
        self.assertEqual(without_tokens(denied), without_tokens(unknown))
        self.assertNotIn("Formal question", denied.content.decode())
        self.assertEqual(Attempt.objects.count(), 0)

    def test_course_link_without_an_enrollment_for_that_course_does_not_help(self):
        course = make_course("Linked")
        CourseMockTest.objects.create(course=course, mock_test=self.mock_test)
        self.login(self.bob)  # bob has no Enrollment in `course`
        self.assertEqual(self.client.post(self.start_url()).status_code, 403)

    def test_start_withholds_correct_option_and_explanation(self):
        self.login(self.alice)
        response = self.client.post(self.start_url())
        for question in response.json()["questions"]:
            self.assertEqual(set(question), PUBLIC_QUESTION_KEYS)
        body = response.content.decode()
        for leaked in ("correct_option", "explanation", "SECRET-FORMAL"):
            self.assertNotIn(leaked, body)

    def test_only_this_tests_formal_questions_are_included_never_chapter_practice(self):
        chapter = make_library(make_course("Has Practice"), books=1, chapters=1)[0]
        practice = practice_question(chapter, 1)
        other_test_q = formal_question(make_mock_test("Another"), 7)
        self.login(self.alice)
        ids = [q["id"] for q in self.client.post(self.start_url()).json()["questions"]]
        self.assertEqual(ids, [q.pk for q in self.questions])
        self.assertNotIn(practice.pk, ids)
        self.assertNotIn(other_test_q.pk, ids)

    def test_time_limit_is_copied_at_start_and_a_later_edit_never_changes_the_attempt(self):
        self.mock_test.duration_minutes = 45
        self.mock_test.save()
        attempt = self.begin()
        self.assertEqual(attempt.time_limit_minutes, 45)
        self.mock_test.duration_minutes = 5
        self.mock_test.save()
        resumed = self.client.post(self.start_url()).json()
        self.assertEqual(resumed["attempt_id"], attempt.pk)
        self.assertEqual(resumed["time_limit_minutes"], 45)
        attempt.refresh_from_db()
        self.assertEqual(attempt.time_limit_minutes, 45)
        self.assertEqual(self.deadline(attempt), attempt.started_at + timedelta(minutes=45))

    def test_a_new_attempt_after_finalization_takes_the_current_duration(self):
        first = self.begin()
        self.submit(first)
        self.mock_test.duration_minutes = 30
        self.mock_test.save()
        second = self.client.post(self.start_url()).json()
        self.assertNotEqual(second["attempt_id"], first.pk)
        self.assertEqual(second["time_limit_minutes"], 30)

    def test_start_creates_no_entitlement_or_order_rows(self):
        models = (MockTestEnrollment, Enrollment, Order)
        before = [m.objects.count() for m in models]
        self.begin()
        self.assertEqual([m.objects.count() for m in models], before)

    def test_start_while_unfinished_resumes_the_same_attempt(self):
        first = self.begin()
        for _ in range(2):
            self.assertEqual(self.client.post(self.start_url()).json()["attempt_id"], first.pk)
        self.assertEqual(Attempt.objects.count(), 1)

    def test_starting_is_per_student_and_per_test(self):
        mine = self.begin()
        buy_mock_test(self.bob, self.mock_test)
        theirs = self.begin(user=self.bob)
        self.assertNotEqual(mine.pk, theirs.pk)
        self.assertEqual(theirs.student, self.bob)

    def test_a_test_with_no_questions_is_refused_without_creating_an_attempt(self):
        empty = make_mock_test("Empty")
        buy_mock_test(self.alice, empty)
        self.login(self.alice)
        response = self.client.post(self.start_url(empty))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"error": "no_questions"})
        self.assertEqual(Attempt.objects.count(), 0)

    def test_publication_is_presentation_not_authorization(self):
        draft = make_mock_test("Draft", published=False)
        formal_question(draft, 1)
        buy_mock_test(self.alice, draft)
        self.login(self.alice)
        self.assertEqual(self.client.post(self.start_url(draft)).status_code, 200)

    def test_a_browser_form_post_is_redirected_to_the_attempt_page(self):
        self.login(self.alice)
        response = self.client.post(self.start_url(), HTTP_ACCEPT="text/html,application/xhtml+xml")
        attempt = Attempt.objects.get()
        self.assertRedirects(response, self.page_url(attempt), fetch_redirect_response=False)

    def test_only_post_is_allowed(self):
        self.login(self.alice)
        self.assertEqual(self.client.get(self.start_url()).status_code, 405)
        self.assertEqual(Attempt.objects.count(), 0)

    def test_start_is_private_no_store_including_the_denial(self):
        self.login(self.alice)
        ok = self.client.post(self.start_url())
        self.login(self.bob)
        denied = self.client.post(self.start_url())
        for response in (ok, denied):
            for directive in NO_STORE:
                self.assertIn(directive, response.headers["Cache-Control"])


class AccessDelegationTests(MockBase):
    """Every formal route must ultimately rely on can_access_mock_test, nothing else."""

    def setUp(self):
        super().setUp()
        self.attempt = self.begin()

    def test_every_route_is_denied_when_the_helper_says_no(self):
        with patch("core.mock_views.can_access_mock_test", return_value=False):
            self.assertEqual(self.client.post(self.start_url()).status_code, 403)
            self.assertEqual(self.client.get(self.page_url(self.attempt)).status_code, 403)
            self.assertEqual(self.save(self.attempt, self.questions[0], "A").status_code, 403)
            self.assertEqual(self.submit(self.attempt).status_code, 403)
        self.attempt.refresh_from_db()
        self.assertIsNone(self.attempt.submitted_at)
        self.assertEqual(AttemptAnswer.objects.count(), 0)

    def test_every_route_follows_the_helper_when_it_says_yes(self):
        """Nothing else gates the routes: an owner passes whenever the helper does."""
        Enrollment.objects.all().delete()
        MockTestEnrollment.objects.all().delete()   # genuinely no entitlement any more
        self.assertEqual(self.save(self.attempt, self.questions[0], "A").status_code, 403)
        with patch("core.mock_views.can_access_mock_test", return_value=True):
            self.assertEqual(self.client.get(self.page_url(self.attempt)).status_code, 200)
            self.assertEqual(self.save(self.attempt, self.questions[0], "A").status_code, 200)
            self.assertEqual(self.submit(self.attempt).status_code, 200)

    def test_losing_entitlement_mid_attempt_blocks_further_writes(self):
        self.assertEqual(self.save(self.attempt, self.questions[0], "A").status_code, 200)
        MockTestEnrollment.objects.filter(student=self.alice).delete()
        self.assertEqual(self.save(self.attempt, self.questions[1], "B").status_code, 403)
        self.assertEqual(AttemptAnswer.objects.count(), 1)
        self.assertEqual(self.submit(self.attempt).status_code, 403)
        self.attempt.refresh_from_db()
        self.assertIsNone(self.attempt.submitted_at)


class AutosaveTests(MockBase):
    def setUp(self):
        super().setUp()
        self.attempt = self.begin()
        self.q1, self.q2, self.q3 = self.questions

    def test_a_valid_answer_before_the_deadline_is_saved(self):
        response = self.save(self.attempt, self.q1, "A")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {"saved_at"})
        saved_at = datetime.fromisoformat(response.json()["saved_at"])
        self.assertIsNotNone(saved_at.tzinfo)
        row = AttemptAnswer.objects.get()
        self.assertEqual((row.attempt, row.question, row.selected_option),
                         (self.attempt, self.q1, "A"))
        self.assertEqual(row.saved_at, saved_at)

    def test_all_four_letters_are_accepted(self):
        for letter in "ABCD":
            self.assertEqual(self.save(self.attempt, self.q1, letter).status_code, 200)
            self.assertEqual(AttemptAnswer.objects.get().selected_option, letter)

    def test_null_clears_the_answer_without_deleting_or_duplicating_the_row(self):
        self.save(self.attempt, self.q1, "C")
        self.assertEqual(self.save(self.attempt, self.q1, None).status_code, 200)
        row = AttemptAnswer.objects.get()
        self.assertIsNone(row.selected_option)

    def test_repeat_saves_update_one_row(self):
        for letter in ("A", "B", "D", "A"):
            self.save(self.attempt, self.q1, letter)
        self.assertEqual(AttemptAnswer.objects.filter(attempt=self.attempt).count(), 1)
        self.assertEqual(AttemptAnswer.objects.get().selected_option, "A")

    def test_a_repeat_save_persists_a_fresh_saved_at_and_reports_the_stored_value(self):
        first, second = (self.attempt.started_at + timedelta(minutes=m) for m in (1, 2))
        with server_time(first):
            self.save(self.attempt, self.q1, "A")
        with server_time(second):
            response = self.save(self.attempt, self.q1, "B")
        row = AttemptAnswer.objects.get()
        self.assertEqual(row.saved_at, second)                      # written to the database
        self.assertEqual(datetime.fromisoformat(response.json()["saved_at"]), second)
        self.assertEqual(row.selected_option, "B")

    def test_invalid_bodies_are_400_and_write_nothing(self):
        url = self.autosave_url(self.attempt)
        bad_payloads = [
            {"question_id": self.q1.pk, "selected_option": "E"},
            {"question_id": self.q1.pk, "selected_option": "a"},
            {"question_id": self.q1.pk, "selected_option": ""},
            {"question_id": self.q1.pk, "selected_option": 1},
            {"question_id": self.q1.pk, "selected_option": ["A"]},
            {"question_id": self.q1.pk},                       # selected_option missing
            {"selected_option": "A"},                          # question_id missing
            {"question_id": str(self.q1.pk), "selected_option": "A"},
            {"question_id": True, "selected_option": "A"},
            {"question_id": None, "selected_option": "A"},
        ]
        for payload in bad_payloads:
            self.assertEqual(post_json(self.client, url, payload).status_code, 400, payload)
        for raw in (b"", b"nope", b"[]", b"3"):
            response = self.client.post(url, data=raw, content_type="application/json")
            self.assertEqual(response.status_code, 400, raw)
        self.assertEqual(AttemptAnswer.objects.count(), 0)

    def test_a_question_of_another_mock_test_is_rejected(self):
        foreign = formal_question(make_mock_test("Other Test"), 1)
        response = self.save(self.attempt, foreign, "A")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "invalid_question"})
        self.assertEqual(AttemptAnswer.objects.count(), 0)

    def test_a_chapter_practice_question_cannot_be_autosaved_into_a_formal_attempt(self):
        chapter = make_library(make_course("Practice"), books=1, chapters=1)[0]
        practice = practice_question(chapter, 1)
        response = self.save(self.attempt, practice, "B")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(AttemptAnswer.objects.count(), 0)

    def test_unknown_and_out_of_range_question_ids_are_400_not_500(self):
        for question_id in (self.q3.pk + 5000, 10 ** 30):
            response = post_json(self.client, self.autosave_url(self.attempt),
                                 {"question_id": question_id, "selected_option": "A"})
            self.assertEqual(response.status_code, 400, question_id)

    def test_saved_answers_survive_a_reload_of_the_attempt_page(self):
        self.save(self.attempt, self.q1, "B")
        self.save(self.attempt, self.q2, "D")
        self.save(self.attempt, self.q3, "C")
        self.save(self.attempt, self.q3, None)   # cleared again
        html = self.client.get(self.page_url(self.attempt)).content.decode()

        def checked(question, letter):
            tag = re.search(rf'<input[^>]*id="q{question.pk}{letter}"[^>]*>', html).group(0)
            return " checked" in tag

        self.assertTrue(checked(self.q1, "B"))
        self.assertTrue(checked(self.q2, "D"))
        self.assertEqual([checked(self.q1, l) for l in "ACD"], [False] * 3)
        self.assertEqual([checked(self.q3, l) for l in "ABCD"], [False] * 4)

    def test_another_students_attempt_cannot_be_changed_or_probed(self):
        buy_mock_test(self.bob, self.mock_test)
        self.login(self.bob)
        denied = self.save(self.attempt, self.q1, "A")
        unknown = post_json(self.client, reverse("attempt_autosave", args=[self.attempt.pk + 999]),
                            {"question_id": self.q1.pk, "selected_option": "A"})
        self.assertEqual((denied.status_code, unknown.status_code), (403, 403))
        self.assertEqual(without_tokens(denied), without_tokens(unknown))
        self.assertEqual(AttemptAnswer.objects.count(), 0)

    def test_client_supplied_times_are_never_read(self):
        with server_time(self.deadline(self.attempt) + timedelta(seconds=1)):
            response = post_json(
                self.client, self.autosave_url(self.attempt),
                {"question_id": self.q1.pk, "selected_option": "A", "timestamp": "2000-01-01T00:00:00Z",
                 "deadline": "2999-01-01T00:00:00Z", "now": 0, "submitted_at": None,
                 "started_at": "2999-01-01T00:00:00Z", "time_limit_minutes": 99999},
                HTTP_DATE="Sat, 01 Jan 2000 00:00:00 GMT", HTTP_X_CLIENT_TIME="0",
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(AttemptAnswer.objects.count(), 0)

    def test_autosave_responses_are_private_no_store_and_carry_no_answer_key(self):
        responses = [
            self.save(self.attempt, self.q1, "A"),
            self.save(self.attempt, self.q1, "Z"),   # 400
            self.submit(self.attempt),
        ]
        responses.append(self.save(self.attempt, self.q2, "A"))   # 409: finalized
        self.login(self.bob)
        responses.append(self.save(self.attempt, self.q1, "A"))   # 403
        for response in responses:
            for directive in NO_STORE:
                self.assertIn(directive, response.headers["Cache-Control"], response.status_code)
            for leaked in ("correct_option", "explanation", "SECRET-FORMAL"):
                self.assertNotIn(leaked, response.content.decode())

    def test_wrong_method_is_405(self):
        self.assertEqual(self.client.get(self.autosave_url(self.attempt)).status_code, 405)
        self.assertEqual(self.client.get(self.submit_url(self.attempt)).status_code, 405)

    def test_mutations_lock_only_the_attempt_row_before_anything_else(self):
        for do in (lambda: self.save(self.attempt, self.q1, "A"), lambda: self.submit(self.attempt)):
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(do().status_code, 200)
            sql = [q["sql"] for q in queries]
            locking = [i for i, s in enumerate(sql) if "FOR UPDATE" in s]
            # The Attempt lock comes first. (update_or_create later locks the
            # answer row itself, which is fine: we already hold the attempt.)
            self.assertIn('FOR UPDATE OF "core_attempt"', sql[locking[0]])  # never the MockTest row
            first_use = [i for i, s in enumerate(sql) if "core_attemptanswer" in s or 'UPDATE "core_attempt"' in s]
            self.assertTrue(all(i > locking[0] for i in first_use), sql)


class FinalizedAttemptTests(MockBase):
    def setUp(self):
        super().setUp()
        self.attempt = self.begin()
        self.q1, self.q2, self.q3 = self.questions
        self.save(self.attempt, self.q1, "A")
        self.submit(self.attempt)

    def test_autosave_after_finalization_is_409_with_the_stored_state_and_no_write(self):
        before = list(AttemptAnswer.objects.values_list("question_id", "selected_option", "saved_at"))
        response = self.save(self.attempt, self.q2, "B")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"error": "attempt_finalized", "score": 1, "total_questions": 3})
        changed = self.save(self.attempt, self.q1, "D")   # cannot overwrite a finalized answer
        self.assertEqual(changed.status_code, 409)
        after = list(AttemptAnswer.objects.values_list("question_id", "selected_option", "saved_at"))
        self.assertEqual(after, before)


class DeadlineTests(MockBase):
    def setUp(self):
        super().setUp()
        self.attempt = self.begin()
        self.q1, self.q2, self.q3 = self.questions
        self.deadline_at = self.deadline(self.attempt)
        self.assertEqual(self.deadline_at - self.attempt.started_at, timedelta(minutes=60))

    def test_answer_just_before_the_deadline_is_accepted(self):
        with server_time(self.deadline_at - timedelta(microseconds=1)):
            self.assertEqual(self.save(self.attempt, self.q1, "A").status_code, 200)
        self.assertEqual(AttemptAnswer.objects.get().selected_option, "A")
        self.attempt.refresh_from_db()
        self.assertIsNone(self.attempt.submitted_at)

    def test_answer_exactly_at_the_deadline_is_rejected(self):
        with server_time(self.deadline_at):
            response = self.save(self.attempt, self.q1, "A")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "deadline_passed")
        self.assertEqual(AttemptAnswer.objects.count(), 0)

    def test_answer_after_the_deadline_is_rejected_and_never_written(self):
        self.save(self.attempt, self.q1, "A")           # persisted in time
        with server_time(self.deadline_at + timedelta(minutes=5)):
            response = self.save(self.attempt, self.q2, "B")   # would have been correct
        self.assertEqual(response.status_code, 409)
        self.assertEqual(list(AttemptAnswer.objects.values_list("question_id", flat=True)), [self.q1.pk])

    def test_a_late_autosave_finalizes_the_attempt_at_the_deadline_from_stored_answers_only(self):
        self.save(self.attempt, self.q1, "A")           # correct, in time
        self.save(self.attempt, self.q2, "A")           # wrong, in time
        with server_time(self.deadline_at + timedelta(hours=3)):
            response = self.save(self.attempt, self.q3, "C")   # correct but late
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(),
                         {"error": "deadline_passed", "score": 1, "total_questions": 3})
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.submitted_at, self.deadline_at)
        self.assertEqual((self.attempt.score, self.attempt.total_questions), (1, 3))
        self.assertEqual(AttemptAnswer.objects.count(), 2)

    def test_the_finalization_is_committed_not_rolled_back_with_the_409(self):
        with server_time(self.deadline_at + timedelta(seconds=1)):
            self.save(self.attempt, self.q1, "A")
        self.assertIsNotNone(Attempt.objects.get(pk=self.attempt.pk).submitted_at)

    def test_nothing_finalizes_an_expired_attempt_until_a_request_arrives(self):
        """No worker: time passing alone changes nothing (only the frozen clock moved)."""
        with server_time(self.deadline_at + timedelta(days=2)):
            self.attempt.refresh_from_db()
            self.assertIsNone(self.attempt.submitted_at)
            self.assertIsNone(self.attempt.score)

    def test_submit_after_the_deadline_uses_the_deadline_as_submitted_at(self):
        self.save(self.attempt, self.q1, "A")
        with server_time(self.deadline_at + timedelta(minutes=30)):
            response = self.submit(self.attempt)
        self.assertEqual(response.json(), {"score": 1, "total_questions": 3})
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.submitted_at, self.deadline_at)

    def test_submit_before_the_deadline_uses_the_server_now(self):
        moment = self.attempt.started_at + timedelta(minutes=12, seconds=5)
        with server_time(moment):
            self.submit(self.attempt)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.submitted_at, moment)

    def test_submit_at_the_deadline_is_clamped_to_it(self):
        with server_time(self.deadline_at):
            self.submit(self.attempt)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.submitted_at, self.deadline_at)

    def test_the_submit_body_cannot_smuggle_in_answers(self):
        self.save(self.attempt, self.q1, "A")
        response = self.submit(self.attempt, {
            "answers": {str(self.q2.pk): "B", str(self.q3.pk): "C"},
            "question_id": self.q2.pk, "selected_option": "B",
        })
        self.assertEqual(response.json(), {"score": 1, "total_questions": 3})
        self.assertEqual(AttemptAnswer.objects.count(), 1)

    def test_the_page_never_finalizes_it_only_shows_zero_time_left(self):
        with server_time(self.deadline_at + timedelta(minutes=1)):
            html = self.client.get(self.page_url(self.attempt)).content.decode()
        self.assertIn('data-remaining-seconds="0"', html)
        self.assertNotIn("attempt-result", html)
        self.attempt.refresh_from_db()
        self.assertIsNone(self.attempt.submitted_at)

    def test_remaining_seconds_come_from_the_server_clock(self):
        with server_time(self.attempt.started_at + timedelta(minutes=10, seconds=30)):
            html = self.client.get(self.page_url(self.attempt)).content.decode()
        self.assertIn(f'data-remaining-seconds="{49 * 60 + 30}"', html)


class ScoringAndIdempotencyTests(MockBase):
    def setUp(self):
        super().setUp()
        self.attempt = self.begin()
        self.q1, self.q2, self.q3 = self.questions

    def test_correct_wrong_and_unanswered_are_scored_and_stored(self):
        self.save(self.attempt, self.q1, "A")     # correct
        self.save(self.attempt, self.q2, "A")     # wrong (key is B)
        # q3 unanswered
        response = self.submit(self.attempt)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"score": 1, "total_questions": 3})
        self.attempt.refresh_from_db()
        self.assertEqual((self.attempt.score, self.attempt.total_questions), (1, 3))
        self.assertIsNotNone(self.attempt.submitted_at)

    def test_a_cleared_answer_scores_nothing(self):
        self.save(self.attempt, self.q1, "A")
        self.save(self.attempt, self.q1, None)
        self.assertEqual(self.submit(self.attempt).json(), {"score": 0, "total_questions": 3})

    def test_a_perfect_and_a_zero_paper(self):
        for question, letter in zip(self.questions, "ABC"):
            self.save(self.attempt, question, letter)
        self.assertEqual(self.submit(self.attempt).json(), {"score": 3, "total_questions": 3})
        second = self.begin(user=self.alice)   # finalized, so this is a NEW attempt
        self.assertNotEqual(second.pk, self.attempt.pk)
        self.assertEqual(self.submit(second).json(), {"score": 0, "total_questions": 3})

    def test_total_questions_counts_only_this_tests_formal_questions(self):
        chapter = make_library(make_course("Practice"), books=1, chapters=1)[0]
        practice_question(chapter, 1)
        practice_question(chapter, 2)
        formal_question(make_mock_test("Other"), 1)
        self.assertEqual(self.submit(self.attempt).json()["total_questions"], 3)

    def test_an_answer_row_for_a_foreign_question_is_never_scored(self):
        """Defence in depth: even a row injected past the view cannot add to the score."""
        chapter = make_library(make_course("Practice"), books=1, chapters=1)[0]
        practice = practice_question(chapter, 1, correct="A")
        foreign = formal_question(make_mock_test("Other"), 1, correct="A")
        AttemptAnswer.objects.create(attempt=self.attempt, question=practice, selected_option="A")
        AttemptAnswer.objects.create(attempt=self.attempt, question=foreign, selected_option="A")
        self.save(self.attempt, self.q1, "A")
        self.assertEqual(self.submit(self.attempt).json(), {"score": 1, "total_questions": 3})

    def test_submitting_twice_returns_the_identical_stored_result_with_no_rescoring(self):
        self.save(self.attempt, self.q1, "A")
        first = self.submit(self.attempt)
        stored = Attempt.objects.get(pk=self.attempt.pk)
        # Everything that could change a recomputation is changed afterwards.
        Question.objects.filter(pk=self.q1.pk).update(correct_option="D")
        formal_question(self.mock_test, 4, "A")
        AttemptAnswer.objects.filter(attempt=self.attempt).update(selected_option="D")
        with CaptureQueriesContext(connection) as queries:
            second = self.submit(self.attempt)
        self.assertEqual(first.json(), {"score": 1, "total_questions": 3})
        self.assertEqual(second.json(), first.json())
        self.assertEqual(write_queries(queries), [])
        after = Attempt.objects.get(pk=self.attempt.pk)
        self.assertEqual((after.submitted_at, after.score, after.total_questions),
                         (stored.submitted_at, stored.score, stored.total_questions))

    def test_a_finalized_score_is_computed_exactly_once(self):
        with patch("core.mock_views.finalize", wraps=attempts.finalize) as spy:
            for _ in range(3):
                self.submit(self.attempt)
        self.assertEqual(spy.call_count, 1)

    def test_a_test_whose_questions_were_removed_scores_zero_of_zero_without_error(self):
        Question.objects.filter(mock_test=self.mock_test).delete()
        self.assertEqual(self.submit(self.attempt).json(), {"score": 0, "total_questions": 0})


class CsrfTests(MockBase):
    def csrf_client(self, user):
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)
        return client

    def test_start_autosave_and_submit_all_require_a_csrf_token(self):
        attempt = self.begin()
        client = self.csrf_client(self.alice)
        page = client.get(self.page_url(attempt))
        token = client.cookies["csrftoken"].value
        self.assertIn("csrfmiddlewaretoken", page.content.decode())
        payload = {"question_id": self.questions[0].pk, "selected_option": "A"}

        self.assertEqual(client.post(self.start_url()).status_code, 403)
        self.assertEqual(post_json(client, self.autosave_url(attempt), payload).status_code, 403)
        self.assertEqual(post_json(client, self.submit_url(attempt)).status_code, 403)
        self.assertEqual(AttemptAnswer.objects.count(), 0)
        attempt.refresh_from_db()
        self.assertIsNone(attempt.submitted_at)

        header = {"HTTP_X_CSRFTOKEN": token}
        self.assertEqual(client.post(self.start_url(), **header).status_code, 200)
        self.assertEqual(post_json(client, self.autosave_url(attempt), payload, **header).status_code, 200)
        self.assertEqual(post_json(client, self.submit_url(attempt), **header).status_code, 200)

    def test_start_form_carries_a_csrf_token(self):
        self.login(self.alice)
        html = self.get("my_mock_tests").content.decode()
        form = re.search(rf'<form[^>]*action="{re.escape(self.start_url())}".*?</form>', html, re.S).group(0)
        self.assertIn("csrfmiddlewaretoken", form)


class AttemptPageTests(MockBase):
    def setUp(self):
        super().setUp()
        self.attempt = self.begin()
        self.q1, self.q2, self.q3 = self.questions

    def test_page_requires_login(self):
        self.client.logout()
        url = self.page_url(self.attempt)
        self.assertRedirects(self.client.get(url), f"{reverse('account_login')}?next={url}",
                             fetch_redirect_response=False)

    def test_only_the_owner_with_entitlement_may_open_it(self):
        buy_mock_test(self.bob, self.mock_test)   # entitled, but not the owner
        self.login(self.bob)
        response = self.client.get(self.page_url(self.attempt))
        unknown = self.client.get(reverse("attempt_page", args=[self.attempt.pk + 999]))
        self.assertEqual((response.status_code, unknown.status_code), (403, 403))
        self.assertEqual(without_tokens(response), without_tokens(unknown))
        for private in ("Formal question", self.mock_test.title):
            self.assertNotIn(private, response.content.decode())

    def test_a_running_attempt_renders_questions_the_countdown_and_the_urls_only(self):
        response = self.client.get(self.page_url(self.attempt))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        match = re.search(r'data-remaining-seconds="(\d+)"', html)
        self.assertTrue(59 * 60 + 50 <= int(match.group(1)) <= 60 * 60)
        self.assertIn(f'data-autosave-url="{self.autosave_url(self.attempt)}"', html)
        self.assertIn(f'data-submit-url="{self.submit_url(self.attempt)}"', html)
        for question in self.questions:
            self.assertIn(question.text, html)
            self.assertIn(question.option_c, html)
        self.assertEqual(html.count('type="radio"'), 12)
        self.assertIn('id="attempt-timer"', html)

    def test_no_answer_key_or_explanation_reaches_the_browser(self):
        html = self.client.get(self.page_url(self.attempt)).content.decode()
        for leaked in ("correct_option", "explanation", "SECRET-FORMAL"):
            self.assertNotIn(leaked, html)

    def test_the_page_script_keeps_the_timer_display_only(self):
        html = self.client.get(self.page_url(self.attempt)).content.decode()
        script = html[html.index("<script>"):html.index("</script>")]
        script = "\n".join(l for l in script.splitlines() if not l.strip().startswith("//"))
        self.assertIn("performance.now", script)          # monotonic, immune to clock changes
        for forbidden in ("localStorage", "sessionStorage", "Date.now", "new Date", "deadline"):
            self.assertNotIn(forbidden, script)
        self.assertIn("X-CSRFToken", script)
        self.assertIn("submit(true)", script)             # zero time -> ask the server to finalize
        self.assertIn("status === 409", script)
        self.assertIn("Not saved", script)                # a failed save is never shown as Saved

    def test_finalized_attempt_shows_the_stored_result_and_no_editable_controls(self):
        self.save(self.attempt, self.q1, "A")
        self.save(self.attempt, self.q2, "A")
        self.submit(self.attempt)
        html = self.client.get(self.page_url(self.attempt)).content.decode()
        self.assertIn("Formal Test", html)
        self.assertIn("Score: 1 / 3", html)
        self.assertIn("finalized", html.lower())
        self.assertEqual(re.findall(r'<input(?![^>]*type="hidden")[^>]*>', html), [])
        for editable in ("attempt-root", "attempt-submit", "data-autosave-url",
                         "<textarea", "Submit test"):
            self.assertNotIn(editable, html)
        for leaked in ("correct_option", "SECRET-FORMAL", "Formal question"):
            self.assertNotIn(leaked, html)

    def test_a_lapsed_owner_cannot_see_even_a_finalized_result(self):
        self.submit(self.attempt)
        MockTestEnrollment.objects.filter(student=self.alice).delete()
        self.assertEqual(self.client.get(self.page_url(self.attempt)).status_code, 403)

    def test_page_and_denial_are_private_no_store(self):
        ok = self.client.get(self.page_url(self.attempt))
        self.login(self.bob)
        denied = self.client.get(self.page_url(self.attempt))
        for response in (ok, denied):
            for directive in NO_STORE:
                self.assertIn(directive, response.headers["Cache-Control"])

    def test_html_in_question_text_is_escaped(self):
        Question.objects.filter(pk=self.q1.pk).update(text="<script>alert(1)</script>")
        html = self.client.get(self.page_url(self.attempt)).content.decode()
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)


class StudentListIntegrationTests(MockBase):
    def course_with_test(self):
        course = make_course("Course With Test")
        included = make_mock_test("Included Test")
        formal_question(included, 1)
        CourseMockTest.objects.create(course=course, mock_test=included)
        return course, included

    def test_standalone_purchase_shows_start_then_resume_then_result_and_new_attempt(self):
        self.login(self.alice)
        html = self.get("my_mock_tests").content.decode()
        self.assertIn(f'action="{self.start_url()}"', html)
        self.assertIn("Start test", html)
        attempt = self.begin()

        html = self.get("my_mock_tests").content.decode()
        self.assertIn(f'href="{self.page_url(attempt)}"', html)
        self.assertIn("Resume attempt", html)
        self.assertNotIn(f'action="{self.start_url()}"', html)

        self.save(attempt, self.questions[0], "A")
        self.submit(attempt)
        html = self.get("my_mock_tests").content.decode()
        self.assertIn("View result (1/3)", html)
        self.assertIn("Start a new attempt", html)
        self.assertIn(f'action="{self.start_url()}"', html)
        self.assertNotIn("Resume attempt", html)

    def test_a_course_included_test_is_startable_from_inside_the_purchased_course(self):
        course, included = self.course_with_test()
        enroll(self.bob, course)
        self.login(self.bob)
        html = self.get("course_library", course.pk).content.decode()
        self.assertIn(f'action="{reverse("mock_test_start", args=[included.pk])}"', html)
        self.assertIn("Start test", html)
        attempt = self.begin(user=self.bob, test=included)
        html = self.get("course_library", course.pk).content.decode()
        self.assertIn(f'href="{self.page_url(attempt)}"', html)
        self.assertIn("Resume attempt", html)

    def test_course_included_tests_are_not_duplicated_into_my_mock_tests(self):
        course, included = self.course_with_test()
        enroll(self.bob, course)
        self.login(self.bob)
        html = self.get("my_mock_tests").content.decode()
        self.assertNotIn("Included Test", html)
        self.assertNotIn(reverse("mock_test_start", args=[included.pk]), html)

    def test_no_coming_soon_placeholder_remains_anywhere_in_the_student_area(self):
        course, included = self.course_with_test()
        enroll(self.alice, course)
        attempt = self.begin()
        for name, args in (("my_mock_tests", ()), ("course_library", (course.pk,)),
                           ("dashboard", ()), ("attempt_page", (attempt.pk,))):
            html = self.get(name, *args).content.decode().lower()
            self.assertNotIn("coming soon", html, name)
            self.assertNotIn("taking a mock test is", html, name)

    def test_a_test_without_questions_offers_no_start_button(self):
        empty = make_mock_test("Empty Test")
        buy_mock_test(self.alice, empty)
        self.login(self.alice)
        html = self.get("my_mock_tests").content.decode()
        self.assertIn("No questions have been added yet.", html)
        self.assertNotIn(reverse("mock_test_start", args=[empty.pk]), html)

    def test_an_unpublished_test_keeps_its_unavailable_label_and_offers_no_new_start(self):
        draft = make_mock_test("Draft Test", published=False)
        formal_question(draft, 1)
        buy_mock_test(self.alice, draft)
        self.login(self.alice)
        html = self.get("my_mock_tests").content.decode()
        self.assertIn("Currently unavailable", html)
        self.assertNotIn(reverse("mock_test_start", args=[draft.pk]), html)

    def test_list_pages_stay_read_only_and_use_a_constant_number_of_queries(self):
        self.begin()
        url = reverse("my_mock_tests")
        with CaptureQueriesContext(connection) as small:
            response = self.client.get(url)
        self.assertEqual(write_queries(small), [])
        for n in range(4):
            extra = make_mock_test(f"Extra {n}")
            formal_question(extra, 1)
            buy_mock_test(self.alice, extra)
        with CaptureQueriesContext(connection) as large:
            self.client.get(url)
        self.assertEqual(len(large), len(small))
        self.assertEqual(Attempt.objects.count(), 1)
