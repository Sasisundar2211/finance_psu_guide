"""Phase 4 public site: server-rendered, read-only, no access decisions.

Nothing here grants entitlement, creates enrollments/attempts, or exposes
chapters, PDFs, or questions (REQ-WEB-03/04/05, REQ-MOCK-02).
"""

from functools import wraps

from django.db.models import Count, Min, Prefetch, Q
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.cache import add_never_cache_headers
from django.views.decorators.http import require_safe

from .models import BlogPost, Course, MockTest, PricingPlan

HOME_COURSE_LIMIT = 6
HOME_MOCK_TEST_LIMIT = 3
HOME_BLOG_LIMIT = 3


def public_page(view):
    """Read-only public view (GET/HEAD only).

    The shared nav is personalized for signed-in users (SECURITY.md §9), so
    their copy of a public page is never edge-cacheable.
    """

    @wraps(view)
    @require_safe
    def wrapper(request, *args, **kwargs):
        response = view(request, *args, **kwargs)
        if request.user.is_authenticated:
            add_never_cache_headers(response)
        return response

    return wrapper


def published_courses():
    """Published courses with the aggregate counts REQ-WEB-03/04 call for.

    Counts are aggregates only; mock tests are counted when published so an
    unpublished test's existence is not disclosed.
    """
    return (
        Course.objects.filter(is_published=True)
        .annotate(
            starting_price_paise=Min("pricing_plans__price_paise"),
            chapter_count=Count("books__chapters", distinct=True),
            mcq_count=Count("books__chapters__questions", distinct=True),
            mock_test_count=Count(
                "course_mock_tests",
                filter=Q(course_mock_tests__mock_test__is_published=True),
                distinct=True,
            ),
        )
        .prefetch_related(
            Prefetch(
                "pricing_plans",
                queryset=PricingPlan.objects.order_by("duration_months"),
            )
        )
    )


def published_mock_tests():
    return MockTest.objects.filter(is_published=True).annotate(
        starting_price_paise=Min("pricing_plans__price_paise")
    )


def published_posts():
    return BlogPost.objects.filter(
        published_at__isnull=False, published_at__lte=timezone.now()
    ).order_by("-published_at")


@public_page
def home(request):
    return render(
        request,
        "home.html",
        {
            "courses": published_courses().order_by("title")[:HOME_COURSE_LIMIT],
            "mock_tests": published_mock_tests().order_by("title")[
                :HOME_MOCK_TEST_LIMIT
            ],
            "posts": published_posts()[:HOME_BLOG_LIMIT],
        },
    )


@public_page
def course_list(request):
    return render(
        request,
        "courses/course_list.html",
        {"courses": published_courses().order_by("title")},
    )


@public_page
def course_detail(request, pk):
    course = get_object_or_404(published_courses(), pk=pk)
    return render(request, "courses/course_detail.html", {"course": course})


@public_page
def mock_test_list(request):
    return render(
        request,
        "mock_tests/mock_test_list.html",
        {"mock_tests": published_mock_tests().order_by("title")},
    )


@public_page
def mock_test_detail(request, pk):
    mock_test = get_object_or_404(published_mock_tests(), pk=pk)
    return render(
        request, "mock_tests/mock_test_detail.html", {"mock_test": mock_test}
    )


@public_page
def blog_list(request):
    return render(request, "blog/blog_list.html", {"posts": published_posts()})


@public_page
def blog_detail(request, slug):
    post = get_object_or_404(published_posts(), slug=slug)
    return render(request, "blog/blog_detail.html", {"post": post})
