"""Per-chapter Practice MCQs (REQ-COURSE-05, API.md §6, DECISIONS.md D11.8).

Stateless and untimed: nothing here writes anything, and in particular no
`Attempt` or `AttemptAnswer` row is ever created. Entitlement is exactly the
chapter PDF's (`student_views.entitled_chapter`, i.e. an active Enrollment
covering the chapter's course), so the two can never drift apart.

The answer key stays server-side until a specific answer is checked: the list
comes from `.values()` over the public columns only.
"""

from django.http import JsonResponse
from django.shortcuts import render

from .attempts import QUESTION_PUBLIC_FIELDS
from .mock_views import VALID_OPTIONS, read_json_object
from .models import Question
from .student_views import entitled_chapter, student_page


@student_page()
def practice_page(request, chapter_id):
    """The practice UI shell; its script loads questions from `practice_mcqs`."""
    chapter = entitled_chapter(request.user, chapter_id)
    if chapter is None:
        return render(request, "student/access_denied.html", status=403)
    return render(
        request,
        "student/practice.html",
        {"chapter": chapter, "book": chapter.book, "course": chapter.book.course},
    )


@student_page(("GET",))
def practice_mcqs(request, chapter_id):
    chapter = entitled_chapter(request.user, chapter_id)
    if chapter is None:
        return JsonResponse({"error": "access_denied"}, status=403)
    questions = (
        Question.objects.filter(chapter=chapter)
        .order_by("pk")
        .values(*QUESTION_PUBLIC_FIELDS)
    )
    return JsonResponse({"questions": list(questions)})


@student_page(("POST",))
def practice_check(request, chapter_id, question_id):
    chapter = entitled_chapter(request.user, chapter_id)
    if chapter is None:
        return JsonResponse({"error": "access_denied"}, status=403)

    data = read_json_object(request)
    selected = data.get("selected_option") if data else None
    if not (isinstance(selected, str) and selected in VALID_OPTIONS):
        return JsonResponse({"error": "invalid_request"}, status=400)

    # The question must be one of THIS chapter's practice questions. A formal
    # mock-test question (chapter is NULL) or another chapter's question is
    # simply not found here, however its id was guessed.
    question = Question.objects.filter(pk=question_id, chapter=chapter).first()
    if question is None:
        return JsonResponse({"error": "not_found"}, status=404)

    return JsonResponse(
        {
            "correct": selected == question.correct_option,
            "correct_option": question.correct_option,
            "explanation": question.explanation or None,
        }
    )
