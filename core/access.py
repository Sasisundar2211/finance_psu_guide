"""Centralized entitlement checks (REQ-COURSE-04, REQ-MOCK-05, DATA_MODEL.md `Enrollment`).

These helpers are the single authority for "may this student see this
course / mock test". Views never re-implement the queries. They only read
`Enrollment`, `MockTestEnrollment` and `CourseMockTest` rows; granting access
is Phase 8's job (verified webhook), not something done here.
"""

from django.db.models import Q
from django.utils import timezone

from .models import CourseMockTest, Enrollment, MockTest, MockTestEnrollment


def active_enrollments(user):
    """The user's course Enrollments whose `expires_at` is still in the future."""
    if not user.is_authenticated:
        return Enrollment.objects.none()
    return Enrollment.objects.filter(student=user, expires_at__gt=timezone.now())


def can_access_course(user, course):
    return active_enrollments(user).filter(course=course).exists()


def accessible_mock_tests(user):
    """MockTests the user may access: standalone purchase OR active course link."""
    if not user.is_authenticated:
        return MockTest.objects.none()
    standalone = MockTestEnrollment.objects.filter(student=user).values("mock_test_id")
    via_course = CourseMockTest.objects.filter(
        course__in=active_enrollments(user).values("course_id")
    ).values("mock_test_id")
    return MockTest.objects.filter(Q(pk__in=standalone) | Q(pk__in=via_course))


def can_access_mock_test(user, mock_test):
    return accessible_mock_tests(user).filter(pk=mock_test.pk).exists()
