"""Phase 7: per-chapter Practice MCQs (REQ-COURSE-05, API.md §6, DECISIONS.md D11.8).

Stateless, untimed, immediate feedback: no Attempt/AttemptAnswer is ever written.
Django tests cannot run browser JavaScript, so the page script is only inspected
statically here (and probed under Node outside the repo).
"""

import json

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from core.models import Attempt, AttemptAnswer, Question
from tests.mcq_support import (
    enroll, formal_question, make_course, make_library, make_mock_test, post_json,
    practice_question,
)
from tests.test_student import StudentTestCase

NO_STORE = ("private", "no-store")


class PracticeBase(StudentTestCase):
    """One course with two chapters; 2 practice questions on the first."""

    def setUp(self):
        super().setUp()
        self.course = make_course("Practice Course")
        self.chapter, self.other_chapter = make_library(self.course, books=1, chapters=2)
        self.q1 = practice_question(self.chapter, 1, correct="B")
        self.q2 = practice_question(self.chapter, 2, correct="D", explanation=False)
        self.other_q = practice_question(self.other_chapter, 9, correct="A")
        self.list_url = reverse("practice_mcqs", args=[self.chapter.pk])
        self.page_url = reverse("practice_page", args=[self.chapter.pk])

    def check_url(self, question, chapter=None):
        return reverse("practice_check", args=[(chapter or self.chapter).pk, question.pk])

    def check(self, question, option, chapter=None, client=None):
        return post_json(client or self.client, self.check_url(question, chapter),
                         {"selected_option": option})


class PracticeAccessTests(PracticeBase):
    def urls(self):
        return [self.list_url, self.page_url]

    def test_anonymous_is_sent_to_login_for_every_practice_route(self):
        for url in self.urls():
            response = self.client.get(url)
            self.assertRedirects(response, f"{reverse('account_login')}?next={url}",
                                 fetch_redirect_response=False)
        response = self.check(self.q1, "B")
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("account_login"), response["Location"])

    def test_entitled_student_fetches_questions_in_a_stable_order(self):
        enroll(self.alice, self.course)
        self.login(self.alice)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        questions = response.json()["questions"]
        self.assertEqual([q["id"] for q in questions], [self.q1.pk, self.q2.pk])
        self.assertEqual(set(questions[0]),
                         {"id", "text", "option_a", "option_b", "option_c", "option_d"})
        self.assertEqual(questions[0]["text"], self.q1.text)
        self.assertEqual(self.client.get(self.page_url).status_code, 200)

    def test_no_enrollment_expired_enrollment_and_someone_elses_are_all_denied(self):
        enroll(self.bob, self.course)                    # not alice's
        self.login(self.alice)
        self.assert_all_denied()
        enroll(self.alice, self.course, days=-1)         # alice's, but expired
        self.assert_all_denied()

    def assert_all_denied(self):
        self.assertEqual(self.client.get(self.list_url).status_code, 403)
        self.assertEqual(self.client.get(self.page_url).status_code, 403)
        response = self.check(self.q1, "B")
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("correct_option", response.content.decode())

    def test_enrollment_in_another_course_does_not_cover_this_chapter(self):
        enroll(self.alice, make_course("Elsewhere"))
        self.login(self.alice)
        self.assert_all_denied()

    def test_unknown_chapter_gets_the_same_403_as_a_denied_one(self):
        enroll(self.alice, self.course)
        other_course = make_course("Not mine")
        denied_chapter = make_library(other_course, books=1, chapters=1)[0]
        practice_question(denied_chapter, 1)
        self.login(self.alice)
        unknown = self.client.get(reverse("practice_mcqs", args=[denied_chapter.pk + 9999]))
        denied = self.client.get(reverse("practice_mcqs", args=[denied_chapter.pk]))
        self.assertEqual((unknown.status_code, denied.status_code), (403, 403))
        self.assertEqual(unknown.content, denied.content)

    def test_wrong_methods_are_rejected(self):
        enroll(self.alice, self.course)
        self.login(self.alice)
        self.assertEqual(self.client.post(self.list_url).status_code, 405)
        self.assertEqual(self.client.get(self.check_url(self.q1)).status_code, 405)


class PracticeAnswerKeyTests(PracticeBase):
    def setUp(self):
        super().setUp()
        enroll(self.alice, self.course)
        self.login(self.alice)

    def test_initial_list_and_page_never_carry_the_key_or_explanation(self):
        listing = self.client.get(self.list_url)
        for question in listing.json()["questions"]:
            self.assertNotIn("correct_option", question)
            self.assertNotIn("explanation", question)
        self.assertNotIn("SECRET-PRACTICE", listing.content.decode())
        # The page script names the fields it will RECEIVE after a check, so the
        # words appear; what must never appear is any question data, key or text.
        page = self.client.get(self.page_url).content.decode()
        self.assertNotIn("SECRET-PRACTICE", page)
        self.assertNotIn(self.q1.text, page)
        self.assertNotIn(self.q1.option_b, page)

    def test_correct_answer_is_correct_and_reveals_key_and_explanation_only_now(self):
        response = self.check(self.q1, "B")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "correct": True, "correct_option": "B",
            "explanation": "SECRET-PRACTICE-EXPLANATION-1",
        })

    def test_incorrect_answer_is_incorrect_but_still_reveals_the_right_one(self):
        self.assertEqual(self.check(self.q1, "A").json(), {
            "correct": False, "correct_option": "B",
            "explanation": "SECRET-PRACTICE-EXPLANATION-1",
        })

    def test_missing_explanation_is_null_not_empty_string(self):
        self.assertIsNone(self.check(self.q2, "D").json()["explanation"])

    def test_every_valid_letter_is_accepted_and_judged(self):
        for letter in "ABCD":
            self.assertEqual(self.check(self.q1, letter).json()["correct"], letter == "B")

    def test_invalid_selected_option_is_rejected_without_revealing_anything(self):
        for bad in ("E", "a", "", "AB", None, 1, ["A"], {"x": 1}):
            response = self.check(self.q1, bad)
            self.assertEqual(response.status_code, 400, repr(bad))
            self.assertNotIn("correct_option", response.content.decode())
        for body in (b"", b"not json", b"[]", b'"A"', b"{}"):
            response = self.client.post(self.check_url(self.q1), data=body,
                                        content_type="application/json")
            self.assertEqual(response.status_code, 400, body)


class PracticeQuestionTargetTests(PracticeBase):
    def setUp(self):
        super().setUp()
        enroll(self.alice, self.course)
        self.login(self.alice)

    def test_a_question_of_another_chapter_cannot_be_checked_by_guessing_its_id(self):
        # alice is entitled to BOTH chapters (same course), yet the URL's chapter rules.
        response = self.check(self.other_q, "A", chapter=self.chapter)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("correct_option", response.content.decode())
        self.assertEqual(self.check(self.other_q, "A", chapter=self.other_chapter).status_code, 200)

    def test_a_question_of_a_chapter_in_another_course_is_not_found_either(self):
        foreign_chapter = make_library(make_course("Foreign"), books=1, chapters=1)[0]
        foreign_q = practice_question(foreign_chapter, 1)
        self.assertEqual(self.check(foreign_q, "A").status_code, 404)

    def test_a_formal_mock_test_question_cannot_be_checked_through_practice(self):
        formal = formal_question(make_mock_test("Formal"), 1, "A")
        response = self.check(formal, "A")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("SECRET-FORMAL", response.content.decode())
        self.assertNotIn("correct", response.content.decode())

    def test_formal_questions_never_appear_in_the_practice_list(self):
        formal_question(make_mock_test("Formal"), 1, "A")
        ids = [q["id"] for q in self.client.get(self.list_url).json()["questions"]]
        self.assertEqual(ids, [self.q1.pk, self.q2.pk])

    def test_nonexistent_and_out_of_range_ids_are_404_not_500(self):
        missing = Question(pk=self.other_q.pk + 5000)
        self.assertEqual(self.check(missing, "A").status_code, 404)
        huge = Question(pk=10 ** 30)
        self.assertEqual(self.check(huge, "A").status_code, 404)


class PracticeIsStatelessTests(PracticeBase):
    def test_practice_creates_no_attempt_and_no_attempt_answer(self):
        formal_question(make_mock_test("F"), 1)  # a bank that could tempt a shared write path
        enroll(self.alice, self.course)
        self.login(self.alice)
        before = (Attempt.objects.count(), AttemptAnswer.objects.count(), Question.objects.count())
        self.client.get(self.page_url)
        self.client.get(self.list_url)
        for q in (self.q1, self.q2):
            for letter in "ABCD":
                self.check(q, letter)
        self.check(self.other_q, "A", chapter=self.chapter)  # 404 path
        self.assertEqual(
            (Attempt.objects.count(), AttemptAnswer.objects.count(), Question.objects.count()),
            before,
        )
        self.assertEqual(before[:2], (0, 0))

    def test_a_check_writes_nothing_at_all(self):
        enroll(self.alice, self.course)
        self.login(self.alice)
        with CaptureQueriesContext(connection) as queries:
            self.check(self.q1, "B")
        writes = [q["sql"] for q in queries
                  if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
        self.assertEqual(writes, [])


class PracticeCacheAndCsrfTests(PracticeBase):
    def test_every_practice_response_is_private_no_store(self):
        enroll(self.alice, self.course)
        self.login(self.alice)
        responses = [
            self.client.get(self.page_url), self.client.get(self.list_url),
            self.check(self.q1, "B"), self.check(self.q1, "X"),
            self.check(self.other_q, "A", chapter=self.chapter),
        ]
        self.login(self.bob)  # not entitled: the denials are private too
        responses += [self.client.get(self.page_url), self.client.get(self.list_url),
                      self.check(self.q1, "B")]
        for response in responses:
            for directive in NO_STORE:
                self.assertIn(directive, response.headers["Cache-Control"], response.status_code)

    def test_answer_check_is_csrf_protected(self):
        enroll(self.alice, self.course)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.alice)
        page = client.get(self.page_url)
        token = client.cookies["csrftoken"].value
        self.assertIn("csrfmiddlewaretoken", page.content.decode())

        rejected = self.check(self.q1, "B", client=client)
        self.assertEqual(rejected.status_code, 403)
        self.assertNotIn("correct_option", rejected.content.decode())

        accepted = post_json(client, self.check_url(self.q1), {"selected_option": "B"},
                             HTTP_X_CSRFTOKEN=token)
        self.assertEqual(accepted.status_code, 200)
        self.assertTrue(accepted.json()["correct"])


class PracticeEmptyAndLibraryTests(PracticeBase):
    def test_chapter_without_questions_is_an_empty_list_not_an_error(self):
        enroll(self.alice, self.course)
        self.login(self.alice)
        empty = reverse("practice_mcqs", args=[self.other_chapter.pk])
        self.other_q.delete()
        self.assertEqual(self.client.get(empty).json(), {"questions": []})
        self.assertEqual(
            self.client.get(reverse("practice_page", args=[self.other_chapter.pk])).status_code,
            200,
        )

    def test_library_links_practice_only_for_chapters_that_have_questions(self):
        enroll(self.alice, self.course)
        self.login(self.alice)
        no_questions = make_library(self.course, books=1, chapters=1)[0]  # 3rd chapter
        html = self.get("course_library", self.course.pk).content.decode()
        self.assertIn(f'href="{self.page_url}"', html)
        self.assertIn("Practice MCQs (2)", html)
        self.assertNotIn(reverse("practice_page", args=[no_questions.pk]), html)

    def test_library_practice_path_is_not_exposed_to_unentitled_or_expired_students(self):
        enroll(self.bob, self.course)  # someone else's enrollment does not help alice
        self.login(self.alice)
        response = self.get("course_library", self.course.pk)
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(self.page_url, response.content.decode())
        enroll(self.alice, self.course, days=-1)
        response = self.get("course_library", self.course.pk)
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(self.page_url, response.content.decode())
        self.assertNotIn("Practice MCQs", response.content.decode())

    def test_library_query_count_does_not_grow_with_the_number_of_chapters(self):
        enroll(self.alice, self.course)
        self.login(self.alice)
        url = reverse("course_library", args=[self.course.pk])
        with CaptureQueriesContext(connection) as small:
            self.client.get(url)
        for chapter in make_library(self.course, books=2, chapters=4):
            practice_question(chapter, 1)
        with CaptureQueriesContext(connection) as large:
            self.client.get(url)
        self.assertEqual(len(large), len(small))


class PracticePageContractTests(PracticeBase):
    """The script cannot run here; pin the structure the reviewer must be able to rely on."""

    def test_page_script_shows_immediate_feedback_and_uses_textcontent_for_content(self):
        enroll(self.alice, self.course)
        self.login(self.alice)
        html = self.client.get(self.page_url).content.decode()
        self.assertIn(f'data-list-url="{self.list_url}"', html)
        self.assertIn("csrfmiddlewaretoken", html)
        self.assertIn("X-CSRFToken", html)
        self.assertIn('"/check/"', html)
        for shown in ("Correct", "Incorrect", "Correct answer: "):
            self.assertIn(shown, html)
        # Question/option/explanation text goes in via textContent, never innerHTML.
        self.assertNotIn("innerHTML", html)
        self.assertNotIn("Date.now", html)
        self.assertNotIn("setInterval", html)  # untimed: no timer of any kind
        self.assertNotIn("autosave", html.lower())
