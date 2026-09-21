"""Shared fixtures for the Phase 7 tests (practice MCQs, formal attempts, races).

No test methods live here on purpose: unittest would otherwise collect the base
class again in every module that imports it.
"""

import json
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.urls import reverse

from core.models import Attempt, Question
from tests.test_student import (
    StudentTestCase,
    buy_mock_test,
    enroll,
    make_course,
    make_library,
    make_mock_test,
)

__all__ = [
    "MockBase", "buy_mock_test", "enroll", "formal_question", "make_course",
    "make_library", "make_mock_test", "post_json", "practice_question", "server_time",
]


def formal_question(mock_test, n=1, correct="A", explanation=True):
    return Question.objects.create(
        mock_test=mock_test,
        text=f"Formal question {n} of {mock_test.pk}",
        option_a=f"F{n} option alpha",
        option_b=f"F{n} option bravo",
        option_c=f"F{n} option charlie",
        option_d=f"F{n} option delta",
        correct_option=correct,
        explanation=f"SECRET-FORMAL-EXPLANATION-{n}" if explanation else "",
    )


def practice_question(chapter, n=1, correct="B", explanation=True):
    return Question.objects.create(
        chapter=chapter,
        text=f"Practice question {n} of {chapter.pk}",
        option_a=f"P{n} option alpha",
        option_b=f"P{n} option bravo",
        option_c=f"P{n} option charlie",
        option_d=f"P{n} option delta",
        correct_option=correct,
        explanation=f"SECRET-PRACTICE-EXPLANATION-{n}" if explanation else "",
    )


def post_json(client, url, payload=None, **extra):
    return client.post(url, data=json.dumps({} if payload is None else payload),
                       content_type="application/json", **extra)


@contextmanager
def server_time(moment):
    """Freeze the server clock (every `timezone.now()` caller) at `moment`."""
    with patch("django.utils.timezone.now", return_value=moment):
        yield


class MockBase(StudentTestCase):
    """alice owns a standalone purchase of a 60-minute test with 3 formal questions
    (correct answers A, B, C); bob owns nothing."""

    def setUp(self):
        super().setUp()
        self.mock_test = make_mock_test("Formal Test")
        self.questions = [
            formal_question(self.mock_test, 1, "A"),
            formal_question(self.mock_test, 2, "B"),
            formal_question(self.mock_test, 3, "C"),
        ]
        buy_mock_test(self.alice, self.mock_test)

    # -- URLs ---------------------------------------------------------------
    def start_url(self, test=None):
        return reverse("mock_test_start", args=[(test or self.mock_test).pk])

    def page_url(self, attempt):
        return reverse("attempt_page", args=[attempt.pk])

    def autosave_url(self, attempt):
        return reverse("attempt_autosave", args=[attempt.pk])

    def submit_url(self, attempt):
        return reverse("attempt_submit", args=[attempt.pk])

    # -- actions ------------------------------------------------------------
    def begin(self, user=None, test=None):
        """Log `user` (default alice) in, start an attempt, return the Attempt."""
        self.login(user or self.alice)
        response = self.client.post(self.start_url(test))
        self.assertEqual(response.status_code, 200, response.content)
        return Attempt.objects.get(pk=response.json()["attempt_id"])

    def save(self, attempt, question, option, **extra):
        return post_json(
            self.client, self.autosave_url(attempt),
            {"question_id": question.pk, "selected_option": option}, **extra,
        )

    def submit(self, attempt, payload=None):
        return post_json(self.client, self.submit_url(attempt), payload)

    def deadline(self, attempt):
        attempt.refresh_from_db()
        return attempt.started_at + timedelta(minutes=attempt.time_limit_minutes)

    def other_user(self, name="carol"):
        return User.objects.create_user(name, f"{name}@example.com", "x")
