"""Phase 5 student area: dashboard, My Courses, course library, My Mock Tests, Profile.

REQ-DASH-01..04, REQ-COURSE-01/04, REQ-MOCK-05, DECISIONS.md D11.5.

Everything here is per-student, so every response is private/no-store
(SECURITY.md §9) and every course/mock-test decision goes through
`core.access`. These views only *read* Enrollment, MockTestEnrollment,
UserChapterProgress and CourseMockTest; nothing here grants access, records
progress, or starts an attempt (Phases 6-8).
"""

from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.db.models.query import Prefetch
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.cache import add_never_cache_headers
from django.views.decorators.http import require_http_methods

from .access import accessible_mock_tests, active_enrollments, can_access_course
from .forms import ProfileForm
from .models import (
    Chapter,
    Course,
    CourseMockTest,
    Enrollment,
    MockTestEnrollment,
    UserChapterProgress,
)


def student_page(methods=("GET", "HEAD")):
    """Login-required, method-restricted, never-cacheable student view."""

    def decorator(view):
        @wraps(view)
        @login_required
        @require_http_methods(methods)
        def wrapper(request, *args, **kwargs):
            response = view(request, *args, **kwargs)
            add_never_cache_headers(response)
            return response

        return wrapper

    return decorator


def purchased_courses(user):
    """One entry per purchased course, with progress for the active ones.

    Historical or admin-created duplicates collapse deterministically: the
    Enrollment with the latest `expires_at` (then highest pk) represents the
    course, so a course is "active" whenever any of its enrollments is.
    Expired courses stay listed as expired but carry no progress figures.
    """
    now = timezone.now()
    latest = {}
    enrollments = (
        Enrollment.objects.filter(student=user)
        .select_related("course")
        .order_by("-expires_at", "-pk")
    )
    for enrollment in enrollments:
        latest.setdefault(enrollment.course_id, enrollment)

    active_ids = [c for c, e in latest.items() if e.expires_at > now]
    progress = course_progress(user, active_ids)

    entries = []
    for course_id, enrollment in latest.items():
        entry = {
            "course": enrollment.course,
            "expires_at": enrollment.expires_at,
            "is_active": course_id in progress,
        }
        entry.update(progress.get(course_id, {}))
        entries.append(entry)
    entries.sort(key=lambda e: (not e["is_active"], e["course"].title, e["course"].pk))
    return entries


def course_progress(user, course_ids):
    """{course_id: {completed, total, percent}} for the given courses.

    percent is the integer floor of completed/total*100 (so 100 only when
    every chapter is complete); a course with no chapters is 0%.
    """
    totals = dict(
        Chapter.objects.filter(book__course_id__in=course_ids)
        .order_by()
        .values_list("book__course_id")
        .annotate(n=Count("pk"))
    )
    done = dict(
        UserChapterProgress.objects.filter(
            user=user, is_completed=True, chapter__book__course_id__in=course_ids
        )
        .order_by()
        .values_list("chapter__book__course_id")
        .annotate(n=Count("pk"))
    )
    result = {}
    for course_id in course_ids:
        total = totals.get(course_id, 0)
        completed = done.get(course_id, 0)
        result[course_id] = {
            "completed": completed,
            "total": total,
            "percent": completed * 100 // total if total else 0,
        }
    return result


def recently_accessed_course(user):
    """D11.5: latest-accessed course among those with an ACTIVE enrollment."""
    row = (
        UserChapterProgress.objects.filter(
            user=user,
            chapter__book__course_id__in=active_enrollments(user).values("course_id"),
        )
        .select_related("chapter__book__course")
        .order_by("-last_accessed_at", "-pk")
        .first()
    )
    return row.chapter.book.course if row else None


def standalone_mock_tests(user):
    """The user's standalone purchases, one per mock test (My Mock Tests).

    Listed whether or not the test is still published: it is the student's
    own purchase, and the template withholds the public link when it is not.
    """
    latest = {}
    rows = (
        MockTestEnrollment.objects.filter(student=user)
        .select_related("mock_test")
        .order_by("-purchased_at", "-pk")
    )
    for row in rows:
        latest.setdefault(row.mock_test_id, row)
    return sorted(latest.values(), key=lambda r: (r.mock_test.title, r.mock_test_id))


@student_page()
def dashboard(request):
    courses = purchased_courses(request.user)
    active = [c for c in courses if c["is_active"]]
    return render(
        request,
        "student/dashboard.html",
        {
            "courses": courses,
            "recent_course": recently_accessed_course(request.user),
            "chapters_completed": sum(c["completed"] for c in active),
            "standalone_tests": standalone_mock_tests(request.user),
            "available_tests": accessible_mock_tests(request.user)
            .filter(is_published=True)
            .order_by("title", "pk"),
        },
    )


@student_page()
def my_courses(request):
    return render(
        request,
        "student/my_courses.html",
        {"courses": purchased_courses(request.user)},
    )


@student_page()
def course_library(request, pk):
    """REQ-COURSE-01/04: Course -> Books -> Chapters, only with active access.

    An unknown id and an un-entitled id get the identical 403 page, so the
    response reveals nothing about the course or its hierarchy. Chapters are
    loaded without `pdf_object_key`; PDF access is Phase 6.
    """
    course = Course.objects.filter(pk=pk).first()
    if course is None or not can_access_course(request.user, course):
        return render(request, "student/access_denied.html", status=403)

    books = course.books.prefetch_related(
        Prefetch(
            "chapters",
            queryset=Chapter.objects.only("id", "title", "order", "book_id").order_by(
                "order", "pk"
            ),
        )
    ).order_by("order", "pk")
    completed_ids = set(
        UserChapterProgress.objects.filter(
            user=request.user, is_completed=True, chapter__book__course=course
        ).values_list("chapter_id", flat=True)
    )
    mock_tests = (
        CourseMockTest.objects.filter(course=course, mock_test__is_published=True)
        .select_related("mock_test")
        .order_by("order", "pk")
    )
    return render(
        request,
        "student/course_library.html",
        {
            "course": course,
            "books": books,
            "completed_ids": completed_ids,
            "progress": course_progress(request.user, [course.pk])[course.pk],
            "mock_tests": mock_tests,
        },
    )


@student_page()
def my_mock_tests(request):
    return render(
        request,
        "student/my_mock_tests.html",
        {"enrollments": standalone_mock_tests(request.user)},
    )


@student_page(("GET", "HEAD", "POST"))
def profile(request):
    """REQ-DASH-04: only `request.user.profile` is ever read or written."""
    if request.method == "POST":
        form = ProfileForm(request.POST, instance=request.user.profile)
        if form.is_valid():
            form.save()
            messages.success(request, "Profile updated.")
            return redirect("profile")
    else:
        form = ProfileForm(instance=request.user.profile)
    return render(request, "student/profile.html", {"form": form})
