"""Formal mock tests: start, attempt page, autosave, submit
(REQ-MOCK-01/04/05, API.md §4-§5, DECISIONS.md D10, D11.2).

Rules every route here follows:

* Access is decided only by `core.access.can_access_mock_test`; no route
  re-implements it. Enrollment rows are only read, never created (Phase 8).
* Each mutation (start, autosave, submit) runs in `transaction.atomic()` and
  takes its row lock BEFORE it reads `submitted_at`, reads the server clock,
  or accepts/scores anything. The clock is read after the lock is held, so a
  request that waited on the lock is judged at the time it actually runs.
* The deadline is `started_at + time_limit_minutes` on the server clock
  (`core.attempts.deadline_of`). Nothing the client sends is ever read as a time.
* A view returns from INSIDE the atomic block when it has written something
  (a late-autosave finalization), so the write commits together with the 409.
* Answer keys are never selected for an unfinished attempt: questions come from
  `core.attempts.formal_questions`, which cannot return them.
* An unknown attempt and someone else's attempt get the identical 403.
"""

import json
import math

from django.contrib.auth import get_user_model
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone

from .access import can_access_mock_test
from .attempts import deadline_of, final_state, finalize, formal_questions
from .models import Attempt, MockTest, Question
from .student_views import student_page

VALID_OPTIONS = frozenset({"A", "B", "C", "D"})


def read_json_object(request):
    """The request body as a JSON object, or None if it is not one."""
    try:
        data = json.loads(request.body or b"")
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _denied():
    return JsonResponse({"error": "access_denied"}, status=403)


def _finalized(error, attempt):
    """409 carrying the stored final state so the page can move to the result."""
    return JsonResponse({"error": error, **final_state(attempt)}, status=409)


def _lock_attempt(user, attempt_id):
    """The user's own Attempt with its row locked, or None (unknown or not theirs).

    `of=("self",)` locks only the attempt row; a bare select_for_update() with
    select_related would also lock the shared MockTest row and serialise every
    student taking the same test.
    """
    return (
        Attempt.objects.select_for_update(of=("self",))
        .select_related("mock_test")
        .filter(pk=attempt_id, student=user)
        .first()
    )


def _wants_html(request):
    """A browser form post asks for HTML; an API caller (or fetch) gets JSON."""
    return "text/html" in request.headers.get("Accept", "")


@student_page(("POST",))
def start(request, mock_test_id):
    """API.md §5: begin (or resume) an attempt.

    Implementation detail, not a frozen rule: a student has at most one
    unfinished attempt per mock test, and `start` returns it instead of opening
    a second one. The per-student lock below makes a double click safe.
    `time_limit_minutes` is copied from the test here, so later edits to the
    test never change a running attempt.
    """
    mock_test = MockTest.objects.filter(pk=mock_test_id).first()
    if mock_test is None:
        return _denied()

    with transaction.atomic():
        # NO KEY UPDATE still serialises this student's starts, but unlike a
        # plain FOR UPDATE it does not block inserts that merely reference the
        # user (e.g. UserChapterProgress).
        get_user_model().objects.select_for_update(no_key=True).get(pk=request.user.pk)
        if not can_access_mock_test(request.user, mock_test):
            return _denied()

        attempt = (
            Attempt.objects.filter(
                student=request.user, mock_test=mock_test, submitted_at__isnull=True
            )
            .order_by("-started_at", "-pk")
            .first()
        )
        questions = list(formal_questions(mock_test.pk))
        if attempt is None:
            if not questions:
                return JsonResponse({"error": "no_questions"}, status=409)
            attempt = Attempt.objects.create(
                student=request.user,
                mock_test=mock_test,
                time_limit_minutes=mock_test.duration_minutes,
            )

    if _wants_html(request):
        return redirect("attempt_page", attempt_id=attempt.pk)
    return JsonResponse(
        {
            "attempt_id": attempt.pk,
            "time_limit_minutes": attempt.time_limit_minutes,
            "questions": questions,
        }
    )


@student_page()
def attempt_page(request, attempt_id):
    """The attempt itself: a running attempt to answer, or its stored result.

    A plain read: it never finalizes. If the deadline has already passed the
    page starts at 00:00 and its script submits at once, which finalizes
    through the locked submit route like any other expiry.
    """
    attempt = (
        Attempt.objects.select_related("mock_test")
        .filter(pk=attempt_id, student=request.user)
        .first()
    )
    if attempt is None or not can_access_mock_test(request.user, attempt.mock_test):
        return render(request, "student/mock_access_denied.html", status=403)

    context = {"attempt": attempt, "mock_test": attempt.mock_test}
    if attempt.submitted_at is not None:
        return render(request, "student/attempt.html", context)

    saved = dict(attempt.answers.values_list("question_id", "selected_option"))
    questions = list(formal_questions(attempt.mock_test_id))
    for question in questions:
        question["selected"] = saved.get(question["id"])
        question["options"] = [
            (letter, question[f"option_{letter.lower()}"]) for letter in "ABCD"
        ]
    seconds_left = (deadline_of(attempt) - timezone.now()).total_seconds()
    context.update(
        questions=questions,
        remaining_seconds=max(0, math.ceil(seconds_left)),
    )
    return render(request, "student/attempt.html", context)


def _parse_autosave(request):
    """(question_id, selected_option) from a well-formed body, else None."""
    data = read_json_object(request)
    if data is None or "selected_option" not in data:
        return None
    question_id, selected = data.get("question_id"), data["selected_option"]
    if type(question_id) is not int:  # bool is an int subclass; reject it too
        return None
    if selected is not None and not (isinstance(selected, str) and selected in VALID_OPTIONS):
        return None
    return question_id, selected


@student_page(("POST",))
def autosave(request, attempt_id):
    """API.md §5 autosave, in the frozen order, all under the Attempt row lock."""
    parsed = _parse_autosave(request)
    if parsed is None:
        return JsonResponse({"error": "invalid_request"}, status=400)
    question_id, selected = parsed

    with transaction.atomic():
        attempt = _lock_attempt(request.user, attempt_id)
        if attempt is None or not can_access_mock_test(request.user, attempt.mock_test):
            return _denied()

        # The clock is read only now, with the lock held (D11.2).
        now = timezone.now()
        if attempt.submitted_at is not None:
            return _finalized("attempt_finalized", attempt)

        deadline = deadline_of(attempt)
        if now >= deadline:
            # Late: the answer is rejected, the attempt is finalized at the
            # deadline from what is already stored, and that commits with the 409.
            finalize(attempt, deadline)
            return _finalized("deadline_passed", attempt)

        # The answer must belong to THIS attempt's test: a chapter practice
        # question or another test's question is never accepted.
        if not Question.objects.filter(
            pk=question_id, mock_test_id=attempt.mock_test_id
        ).exists():
            return JsonResponse({"error": "invalid_question"}, status=400)

        answer, _ = attempt.answers.update_or_create(
            question_id=question_id, defaults={"selected_option": selected}
        )
        return JsonResponse({"saved_at": answer.saved_at.isoformat()})


@student_page(("POST",))
def submit(request, attempt_id):
    """API.md §5 submit: idempotent, scored once, under the Attempt row lock.

    The request body is ignored: answers must already have been autosaved.
    """
    with transaction.atomic():
        attempt = _lock_attempt(request.user, attempt_id)
        if attempt is None or not can_access_mock_test(request.user, attempt.mock_test):
            return _denied()

        if attempt.submitted_at is None:
            now = timezone.now()
            finalize(attempt, min(now, deadline_of(attempt)))
        return JsonResponse(final_state(attempt))
