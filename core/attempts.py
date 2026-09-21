"""Formal mock-test attempt rules (REQ-MOCK-01/04, API.md §5, DECISIONS.md D10/D11.2).

Pure domain helpers shared by the attempt views and the student list pages.
None of them takes a lock or opens a transaction: the mutating views in
`core.mock_views` acquire the `Attempt` row lock first and only then call
`finalize`, so everything here runs under that lock.
"""

from datetime import timedelta

from django.db.models import Count, F

from .models import Attempt, Question

# Only these columns ever leave the database for an unfinished attempt or a
# practice list. `correct_option` and `explanation` are deliberately absent.
QUESTION_PUBLIC_FIELDS = ("id", "text", "option_a", "option_b", "option_c", "option_d")


def deadline_of(attempt):
    """The server-side deadline. Derived from stored fields only (never a column)."""
    return attempt.started_at + timedelta(minutes=attempt.time_limit_minutes)


def finalize(attempt, submitted_at):
    """Store the one final result for `attempt`.

    The caller must hold the Attempt row lock and must already have seen
    `submitted_at is None` under that lock. Only `AttemptAnswer` rows that are
    already persisted are scored, and only questions of the attempt's own
    mock test count (`question__mock_test_id`); an unanswered question (no row,
    or a NULL selection) never equals `correct_option`.
    """
    attempt.submitted_at = submitted_at
    attempt.score = attempt.answers.filter(
        question__mock_test_id=attempt.mock_test_id,
        selected_option=F("question__correct_option"),
    ).count()
    attempt.total_questions = Question.objects.filter(
        mock_test_id=attempt.mock_test_id
    ).count()
    attempt.save(update_fields=["submitted_at", "score", "total_questions"])


def final_state(attempt):
    return {"score": attempt.score, "total_questions": attempt.total_questions}


def formal_questions(mock_test_id):
    """Formal (mock_test-linked) questions in a stable order, without the answer key."""
    return (
        Question.objects.filter(mock_test_id=mock_test_id)
        .order_by("pk")
        .values(*QUESTION_PUBLIC_FIELDS)
    )


def latest_attempts(user, mock_test_ids):
    """{mock_test_id: the user's newest Attempt} in one query (DISTINCT ON)."""
    rows = (
        Attempt.objects.filter(student=user, mock_test_id__in=mock_test_ids)
        .order_by("mock_test_id", "-started_at", "-pk")
        .distinct("mock_test_id")
    )
    return {attempt.mock_test_id: attempt for attempt in rows}


def question_counts(mock_test_ids):
    """{mock_test_id: number of formal questions}; absent ids have none."""
    return dict(
        Question.objects.filter(mock_test_id__in=mock_test_ids)
        .order_by()
        .values_list("mock_test_id")
        .annotate(n=Count("pk"))
    )
