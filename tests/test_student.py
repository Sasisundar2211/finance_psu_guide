"""Phase 5: enrollment access + student dashboard
(REQ-DASH-01..04, REQ-COURSE-01/04, REQ-MOCK-05, DECISIONS.md D11.5).

Enrollment / MockTestEnrollment / Order fixtures are built directly: real
entitlement creation is the verified Razorpay webhook (Phase 8).
"""

import itertools
import re
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import AnonymousUser, User
from django.db import connection
from django.shortcuts import resolve_url
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

import core.models
from core.access import (
    accessible_mock_tests,
    can_access_course,
    can_access_mock_test,
)
from core.models import (
    Attempt,
    Book,
    Chapter,
    Course,
    CourseMockTest,
    Enrollment,
    MockTest,
    MockTestEnrollment,
    MockTestPricingPlan,
    Order,
    PricingPlan,
    UserChapterProgress,
    UserProfile,
)
from core.student_views import course_progress, recently_accessed_course

CONFLICT_URL = "/accounts/login/?error=session_conflict"
_seq = itertools.count(1)


def make_user(name="student"):
    return User.objects.create_user(name, f"{name}@example.com", "x")


def make_course(title="Course A", published=True):
    return Course.objects.create(
        title=title,
        short_description=f"Short {title}",
        full_description=f"Full {title}",
        is_published=published,
    )


def make_mock_test(title="Test A", published=True):
    return MockTest.objects.create(title=title, is_published=published, duration_minutes=60)


def make_library(course, books=2, chapters=2, key="private/secret-key.pdf"):
    """books x chapters, all titled '<course> B<i> C<j>' so they are greppable."""
    made = []
    for i in range(books):
        book = Book.objects.create(course=course, title=f"{course.title} B{i}", order=i)
        for j in range(chapters):
            made.append(
                Chapter.objects.create(
                    book=book,
                    title=f"{course.title} B{i} C{j}",
                    order=j,
                    pdf_object_key=key,
                )
            )
    return made


def enroll(user, course, days=30):
    n = next(_seq)
    plan = PricingPlan.objects.create(course=course, duration_months=3, price_paise=100000)
    order = Order.objects.create(
        student=user,
        course_pricing_plan=plan,
        razorpay_order_id=f"order_{n}",
        amount_paise=100000,
    )
    return Enrollment.objects.create(
        student=user,
        course=course,
        pricing_plan=plan,
        expires_at=timezone.now() + timedelta(days=days),
        order=order,
    )


def buy_mock_test(user, mock_test):
    n = next(_seq)
    plan = MockTestPricingPlan.objects.create(mock_test=mock_test, price_paise=50000)
    order = Order.objects.create(
        student=user,
        mock_test_pricing_plan=plan,
        razorpay_order_id=f"order_{n}",
        amount_paise=50000,
    )
    return MockTestEnrollment.objects.create(student=user, mock_test=mock_test, order=order)


def touch(user, chapter, completed=False, days_ago=0):
    """A progress fixture with a chosen last_accessed_at (auto_now needs update())."""
    row = UserChapterProgress.objects.create(user=user, chapter=chapter, is_completed=completed)
    UserChapterProgress.objects.filter(pk=row.pk).update(
        last_accessed_at=timezone.now() - timedelta(days=days_ago)
    )
    return row


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class StudentTestCase(TestCase):
    def setUp(self):
        super().setUp()
        self.alice = make_user("alice")
        self.bob = make_user("bob")

    def login(self, user):
        self.client.force_login(user)

    def get(self, name, *args):
        return self.client.get(reverse(name, args=args))


class AuthAndCacheTests(StudentTestCase):
    def protected_urls(self):
        course = make_course()
        enroll(self.alice, course)
        return [
            reverse("dashboard"),
            reverse("my_courses"),
            reverse("my_mock_tests"),
            reverse("profile"),
            reverse("course_library", args=[course.pk]),
        ]

    def test_every_student_page_requires_login_via_allauth_route(self):
        for url in self.protected_urls():
            response = self.client.get(url)
            self.assertRedirects(
                response,
                f"{reverse('account_login')}?next={url}",
                fetch_redirect_response=False,
            )

    def test_anonymous_profile_post_redirects_and_changes_nothing(self):
        response = self.client.post(reverse("profile"), {"phone_number": "999"})
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("account_login"), response["Location"])
        self.assertIsNone(UserProfile.objects.get(user=self.alice).phone_number)

    def test_student_pages_are_private_no_store(self):
        urls = self.protected_urls()
        self.login(self.alice)
        for url in urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            control = response.headers["Cache-Control"]
            for directive in ("private", "no-store"):
                self.assertIn(directive, control, url)

    def test_access_denied_response_is_also_no_store(self):
        course = make_course()
        self.login(self.bob)
        response = self.get("course_library", course.pk)
        self.assertEqual(response.status_code, 403)
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_read_only_pages_reject_post(self):
        self.login(self.alice)
        for name in ("dashboard", "my_courses", "my_mock_tests"):
            self.assertEqual(self.client.post(reverse(name)).status_code, 405, name)


class CourseAccessHelperTests(StudentTestCase):
    def setUp(self):
        super().setUp()
        self.course = make_course()

    def test_active_enrollment_grants_access(self):
        enroll(self.alice, self.course)
        self.assertTrue(can_access_course(self.alice, self.course))

    def test_expired_enrollment_does_not(self):
        enroll(self.alice, self.course, days=-1)
        self.assertFalse(can_access_course(self.alice, self.course))

    def test_expiry_boundary_is_exclusive(self):
        enrollment = enroll(self.alice, self.course)
        Enrollment.objects.filter(pk=enrollment.pk).update(expires_at=timezone.now())
        self.assertFalse(can_access_course(self.alice, self.course))

    def test_no_enrollment_does_not(self):
        self.assertFalse(can_access_course(self.alice, self.course))

    def test_other_students_enrollment_does_not(self):
        enroll(self.alice, self.course)
        self.assertFalse(can_access_course(self.bob, self.course))

    def test_enrollment_in_a_different_course_does_not(self):
        enroll(self.alice, make_course("Other"))
        self.assertFalse(can_access_course(self.alice, self.course))

    def test_anonymous_never_has_access(self):
        enroll(self.alice, self.course)
        self.assertFalse(can_access_course(AnonymousUser(), self.course))


class MockAccessHelperTests(StudentTestCase):
    def setUp(self):
        super().setUp()
        self.course = make_course()
        self.test = make_mock_test()

    def link(self, course=None, test=None):
        return CourseMockTest.objects.create(course=course or self.course, mock_test=test or self.test)

    def test_standalone_purchase_grants_access(self):
        buy_mock_test(self.alice, self.test)
        self.assertTrue(can_access_mock_test(self.alice, self.test))

    def test_active_course_with_linked_test_grants_access(self):
        self.link()
        enroll(self.alice, self.course)
        self.assertTrue(can_access_mock_test(self.alice, self.test))

    def test_expired_course_link_does_not_grant_access(self):
        self.link()
        enroll(self.alice, self.course, days=-1)
        self.assertFalse(can_access_mock_test(self.alice, self.test))

    def test_unrelated_enrollment_does_not_grant_access(self):
        other = make_course("Other")
        self.link()  # test belongs to self.course, alice is only in `other`
        enroll(self.alice, other)
        buy_mock_test(self.alice, make_mock_test("Another test"))
        self.assertFalse(can_access_mock_test(self.alice, self.test))

    def test_other_students_purchase_or_enrollment_does_not_grant_access(self):
        self.link()
        enroll(self.alice, self.course)
        buy_mock_test(self.alice, self.test)
        self.assertFalse(can_access_mock_test(self.bob, self.test))

    def test_anonymous_never_has_access(self):
        buy_mock_test(self.alice, self.test)
        self.assertFalse(can_access_mock_test(AnonymousUser(), self.test))

    def test_both_paths_collapse_to_one_accessible_test(self):
        self.link()
        enroll(self.alice, self.course)
        buy_mock_test(self.alice, self.test)
        self.assertEqual(list(accessible_mock_tests(self.alice)), [self.test])

    def test_two_courses_linking_the_same_test_collapse_to_one(self):
        second = make_course("Second")
        self.link()
        self.link(course=second)
        enroll(self.alice, self.course)
        enroll(self.alice, second)
        self.assertEqual(accessible_mock_tests(self.alice).count(), 1)


class MyCoursesTests(StudentTestCase):
    def test_purchased_course_appears_with_continue_learning(self):
        course = make_course("Purchased")
        enroll(self.alice, course)
        self.login(self.alice)
        html = self.get("my_courses").content.decode()
        self.assertIn("Purchased", html)
        self.assertIn(f'href="{reverse("course_library", args=[course.pk])}"', html)
        self.assertIn("Continue Learning", html)

    def test_unpurchased_and_other_students_courses_never_appear(self):
        enroll(self.bob, make_course("Bobs Course"))
        make_course("Never Bought")
        self.login(self.alice)
        html = self.get("my_courses").content.decode()
        self.assertNotIn("Bobs Course", html)
        self.assertNotIn("Never Bought", html)
        self.assertIn("courses-empty", html)

    def test_expired_purchase_is_shown_as_expired_without_access_link(self):
        course = make_course("Lapsed")
        enroll(self.alice, course, days=-3)
        self.login(self.alice)
        html = self.get("my_courses").content.decode()
        self.assertIn("Lapsed", html)
        self.assertIn("Expired", html)
        self.assertNotIn("Continue Learning", html)
        self.assertNotIn(reverse("course_library", args=[course.pk]), html)

    def test_duplicate_enrollments_show_the_course_once(self):
        course = make_course("Dup")
        enroll(self.alice, course, days=-10)
        enroll(self.alice, course, days=20)
        enroll(self.alice, course, days=5)
        self.login(self.alice)
        response = self.get("my_courses")
        self.assertEqual(len(response.context["courses"]), 1)
        self.assertTrue(response.context["courses"][0]["is_active"])
        self.assertEqual(response.content.decode().count("Continue Learning"), 1)

    def test_active_courses_sort_before_expired_ones(self):
        enroll(self.alice, make_course("A Expired"), days=-1)
        enroll(self.alice, make_course("Z Active"))
        self.login(self.alice)
        titles = [c["course"].title for c in self.get("my_courses").context["courses"]]
        self.assertEqual(titles, ["Z Active", "A Expired"])


class CourseLibraryTests(StudentTestCase):
    def setUp(self):
        super().setUp()
        self.course = make_course("Library Course")
        self.chapters = make_library(self.course, books=2, chapters=2)

    def test_active_purchase_shows_books_and_their_chapters(self):
        enroll(self.alice, self.course)
        self.login(self.alice)
        response = self.get("course_library", self.course.pk)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        for chapter in self.chapters:
            self.assertIn(chapter.title, html)
        for book in ("Library Course B0", "Library Course B1"):
            self.assertIn(book, html)
        self.assertLess(html.index("B0 C0"), html.index("B0 C1"))
        self.assertLess(html.index("B0 C1"), html.index("B1 C0"))

    def test_chapter_is_the_leaf_and_no_topic_model_exists(self):
        enroll(self.alice, self.course)
        self.login(self.alice)
        html = self.get("course_library", self.course.pk).content.decode()
        self.assertFalse(hasattr(core.models, "Topic"))
        self.assertNotIn("Topic", html)

    def test_expired_purchase_cannot_open_the_library(self):
        enroll(self.alice, self.course, days=-1)
        self.login(self.alice)
        response = self.get("course_library", self.course.pk)
        self.assertEqual(response.status_code, 403)
        html = response.content.decode()
        for leaked in ("Library Course", "B0", "C0"):
            self.assertNotIn(leaked, html)

    def test_unpurchased_course_gets_the_same_403_as_an_unknown_id(self):
        self.login(self.alice)
        denied = self.get("course_library", self.course.pk)
        unknown = self.get("course_library", self.course.pk + 9999)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(unknown.status_code, 403)
        # The masked CSRF token in the nav logout form differs per render.
        strip = lambda r: re.sub(r'value="[^"]{64}"', "", r.content.decode())
        self.assertEqual(strip(denied), strip(unknown))

    def test_other_students_purchase_does_not_open_the_library(self):
        enroll(self.alice, self.course)
        self.login(self.bob)
        self.assertEqual(self.get("course_library", self.course.pk).status_code, 403)

    def test_pdf_object_key_and_signed_urls_never_appear(self):
        enroll(self.alice, self.course)
        self.login(self.alice)
        html = self.get("course_library", self.course.pk).content.decode()
        for forbidden in ("private/secret-key.pdf", "pdf_object_key", "signed-url", "X-Amz"):
            self.assertNotIn(forbidden, html)

    def test_browsing_writes_no_progress(self):
        enroll(self.alice, self.course)
        touch(self.alice, self.chapters[0], completed=True, days_ago=2)
        before = list(UserChapterProgress.objects.values_list("pk", "is_completed", "last_accessed_at"))
        self.login(self.alice)
        for _ in range(2):
            self.get("course_library", self.course.pk)
            self.get("dashboard")
            self.get("my_courses")
        after = list(UserChapterProgress.objects.values_list("pk", "is_completed", "last_accessed_at"))
        self.assertEqual(before, after)

    def test_completed_chapters_are_marked_and_counted(self):
        enroll(self.alice, self.course)
        touch(self.alice, self.chapters[0], completed=True)
        self.login(self.alice)
        response = self.get("course_library", self.course.pk)
        html = response.content.decode()
        self.assertEqual(html.count("Completed</span>"), 1)
        self.assertIn("1 of 4 chapters completed (25%)", html)

    def test_included_published_mock_tests_are_listed_and_unpublished_hidden(self):
        shown, hidden = make_mock_test("Included Shown"), make_mock_test("Included Hidden", published=False)
        CourseMockTest.objects.create(course=self.course, mock_test=shown)
        CourseMockTest.objects.create(course=self.course, mock_test=hidden)
        enroll(self.alice, self.course)
        self.login(self.alice)
        html = self.get("course_library", self.course.pk).content.decode()
        self.assertIn("Included Shown", html)
        self.assertNotIn("Included Hidden", html)

    def test_query_count_does_not_grow_with_books_and_chapters(self):
        enroll(self.alice, self.course)
        self.login(self.alice)
        url = reverse("course_library", args=[self.course.pk])
        with CaptureQueriesContext(connection) as small:
            self.client.get(url)
        make_library(make_course("Filler"), books=1, chapters=1)  # unrelated
        for i in range(5):
            book = Book.objects.create(course=self.course, title=f"Extra {i}", order=10 + i)
            for j in range(3):
                Chapter.objects.create(book=book, title=f"Extra {i}.{j}", order=j)
        with CaptureQueriesContext(connection) as large:
            self.client.get(url)
        self.assertEqual(len(small), len(large))


class ProgressTests(StudentTestCase):
    def setUp(self):
        super().setUp()
        self.course = make_course()

    def percent(self, user=None):
        return course_progress(user or self.alice, [self.course.pk])[self.course.pk]

    def test_completed_count_and_percent(self):
        chapters = make_library(self.course, books=1, chapters=4)
        touch(self.alice, chapters[0], completed=True)
        touch(self.alice, chapters[1], completed=True)
        touch(self.alice, chapters[2], completed=False)  # opened, not completed
        self.assertEqual(self.percent(), {"completed": 2, "total": 4, "percent": 50})

    def test_percent_is_floored_and_hits_100_only_when_complete(self):
        chapters = make_library(self.course, books=1, chapters=3)
        touch(self.alice, chapters[0], completed=True)
        self.assertEqual(self.percent()["percent"], 33)
        touch(self.alice, chapters[1], completed=True)
        self.assertEqual(self.percent()["percent"], 66)
        touch(self.alice, chapters[2], completed=True)
        self.assertEqual(self.percent()["percent"], 100)

    def test_total_spans_all_books_of_the_course(self):
        make_library(self.course, books=3, chapters=2)
        self.assertEqual(self.percent()["total"], 6)

    def test_zero_chapter_course_is_zero_percent(self):
        Book.objects.create(course=self.course, title="Empty book")
        self.assertEqual(self.percent(), {"completed": 0, "total": 0, "percent": 0})

    def test_another_users_progress_is_ignored(self):
        chapters = make_library(self.course, books=1, chapters=2)
        touch(self.bob, chapters[0], completed=True)
        touch(self.bob, chapters[1], completed=True)
        self.assertEqual(self.percent(), {"completed": 0, "total": 2, "percent": 0})

    def test_other_courses_chapters_are_not_counted(self):
        make_library(self.course, books=1, chapters=2)
        other = make_library(make_course("Other"), books=1, chapters=2)
        touch(self.alice, other[0], completed=True)
        self.assertEqual(self.percent()["completed"], 0)

    def test_dashboard_and_my_courses_show_progress_for_active_courses(self):
        chapters = make_library(self.course, books=1, chapters=4)
        touch(self.alice, chapters[0], completed=True)
        enroll(self.alice, self.course)
        self.login(self.alice)
        for name in ("dashboard", "my_courses"):
            html = self.get(name).content.decode()
            self.assertIn("1 of 4 chapters completed (25%)", html, name)
        self.assertIn("Chapters completed in active courses: 1", self.get("dashboard").content.decode())


class RecentlyAccessedTests(StudentTestCase):
    def setUp(self):
        super().setUp()
        self.a, self.b = make_course("Course A"), make_course("Course B")
        self.chapter_a = make_library(self.a, 1, 1)[0]
        self.chapter_b = make_library(self.b, 1, 1)[0]

    def test_latest_active_course_wins(self):
        enroll(self.alice, self.a)
        enroll(self.alice, self.b)
        touch(self.alice, self.chapter_a, days_ago=5)
        touch(self.alice, self.chapter_b, days_ago=1)
        self.assertEqual(recently_accessed_course(self.alice), self.b)

    def test_newer_expired_course_is_excluded(self):
        enroll(self.alice, self.a)
        enroll(self.alice, self.b, days=-1)
        touch(self.alice, self.chapter_a, days_ago=5)
        touch(self.alice, self.chapter_b, days_ago=1)
        self.assertEqual(recently_accessed_course(self.alice), self.a)

    def test_all_expired_gives_none(self):
        enroll(self.alice, self.a, days=-1)
        enroll(self.alice, self.b, days=-1)
        touch(self.alice, self.chapter_a)
        touch(self.alice, self.chapter_b)
        self.assertIsNone(recently_accessed_course(self.alice))

    def test_no_progress_gives_none(self):
        enroll(self.alice, self.a)
        self.assertIsNone(recently_accessed_course(self.alice))

    def test_progress_without_any_enrollment_gives_none(self):
        touch(self.alice, self.chapter_a)
        self.assertIsNone(recently_accessed_course(self.alice))

    def test_other_students_progress_is_ignored(self):
        enroll(self.alice, self.a)
        enroll(self.alice, self.b)
        touch(self.alice, self.chapter_a, days_ago=5)
        touch(self.bob, self.chapter_b, days_ago=0)
        self.assertEqual(recently_accessed_course(self.alice), self.a)

    def test_dashboard_renders_the_recent_course_and_the_empty_state(self):
        enroll(self.alice, self.a)
        self.login(self.alice)
        self.assertIn("recent-empty", self.get("dashboard").content.decode())
        touch(self.alice, self.chapter_a)
        html = self.get("dashboard").content.decode()
        self.assertIn('id="recent-course">Course A', html)
        self.assertIn(reverse("course_library", args=[self.a.pk]), html)


class MyMockTestsTests(StudentTestCase):
    def test_only_own_standalone_purchases_appear(self):
        buy_mock_test(self.alice, make_mock_test("Alice Test"))
        buy_mock_test(self.bob, make_mock_test("Bob Test"))
        make_mock_test("Public Only")
        self.login(self.alice)
        html = self.get("my_mock_tests").content.decode()
        self.assertIn("Alice Test", html)
        self.assertNotIn("Bob Test", html)
        self.assertNotIn("Public Only", html)

    def test_course_included_test_is_not_listed_without_a_standalone_purchase(self):
        course, included = make_course(), make_mock_test("Included Only")
        CourseMockTest.objects.create(course=course, mock_test=included)
        enroll(self.alice, course)
        self.login(self.alice)
        self.assertNotIn("Included Only", self.get("my_mock_tests").content.decode())

    def test_course_included_test_with_standalone_purchase_is_listed_once(self):
        course, test = make_course(), make_mock_test("Both Paths")
        CourseMockTest.objects.create(course=course, mock_test=test)
        enroll(self.alice, course)
        buy_mock_test(self.alice, test)
        self.login(self.alice)
        self.assertEqual(len(self.get("my_mock_tests").context["enrollments"]), 1)

    def test_unpublished_purchase_is_kept_but_not_linked_and_not_leaked(self):
        test = make_mock_test("Retired", published=False)
        buy_mock_test(self.alice, test)
        self.login(self.alice)
        html = self.get("my_mock_tests").content.decode()
        self.assertIn("Retired", html)
        self.assertIn("Currently unavailable", html)
        self.assertNotIn(reverse("mock_test_detail", args=[test.pk]), html)
        self.assertEqual(self.get("mock_test_detail", test.pk).status_code, 404)
        self.client.logout()
        self.login(self.bob)
        self.assertNotIn("Retired", self.get("my_mock_tests").content.decode())

    def test_listing_creates_no_attempts(self):
        buy_mock_test(self.alice, make_mock_test())
        self.login(self.alice)
        self.get("my_mock_tests")
        self.assertEqual(Attempt.objects.count(), 0)


class DashboardTests(StudentTestCase):
    def test_new_student_sees_empty_states(self):
        self.login(self.alice)
        response = self.get("dashboard")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        for marker in ("recent-empty", "courses-empty", "purchased-tests-empty", "available-empty"):
            self.assertIn(marker, html)

    def test_shows_purchases_and_expired_state(self):
        enroll(self.alice, make_course("Live Course"))
        enroll(self.alice, make_course("Old Course"), days=-2)
        buy_mock_test(self.alice, make_mock_test("Own Test"))
        self.login(self.alice)
        html = self.get("dashboard").content.decode()
        for expected in ("Live Course", "Old Course", "Own Test", "Expired"):
            self.assertIn(expected, html)
        self.assertEqual(html.count("Continue Learning"), 1)

    def test_available_tests_use_access_truth_and_collapse_duplicates(self):
        course = make_course()
        both, via_course, standalone = (
            make_mock_test("Both Paths"),
            make_mock_test("Course Only"),
            make_mock_test("Standalone Only"),
        )
        draft, unrelated = make_mock_test("Draft", published=False), make_mock_test("Not Mine")
        for t in (both, via_course, draft):
            CourseMockTest.objects.create(course=course, mock_test=t)
        enroll(self.alice, course)
        buy_mock_test(self.alice, both)
        buy_mock_test(self.alice, standalone)
        self.login(self.alice)
        available = [t.title for t in self.get("dashboard").context["available_tests"]]
        self.assertEqual(available, ["Both Paths", "Course Only", "Standalone Only"])
        self.assertNotIn(unrelated.title, available)

    def test_expired_course_mock_tests_are_not_available(self):
        course, test = make_course(), make_mock_test("Lapsed Test")
        CourseMockTest.objects.create(course=course, mock_test=test)
        enroll(self.alice, course, days=-1)
        self.login(self.alice)
        self.assertEqual(list(self.get("dashboard").context["available_tests"]), [])

    def test_no_invented_analytics(self):
        enroll(self.alice, make_course())
        self.login(self.alice)
        html = self.get("dashboard").content.decode().lower()
        for invented in ("streak", "badge", "rank", "leaderboard", "₹", "revenue"):
            self.assertNotIn(invented, html)

    def test_query_count_does_not_grow_with_courses(self):
        def add_course(i):
            course = make_course(f"C{i}")
            chapters = make_library(course, books=2, chapters=2)
            touch(self.alice, chapters[0], completed=True)
            enroll(self.alice, course)
            CourseMockTest.objects.create(course=course, mock_test=make_mock_test(f"T{i}"))

        add_course(0)
        self.login(self.alice)
        with CaptureQueriesContext(connection) as one:
            self.get("dashboard")
        for i in range(1, 6):
            add_course(i)
        with CaptureQueriesContext(connection) as many:
            self.get("dashboard")
        self.assertEqual(len(one), len(many))


class CrossStudentIsolationTests(StudentTestCase):
    def test_a_purchase_is_visible_only_to_its_owner_everywhere(self):
        course, test = make_course("Alice Only Course"), make_mock_test("Alice Only Test")
        make_library(course)
        CourseMockTest.objects.create(course=course, mock_test=test)
        enroll(self.alice, course)
        buy_mock_test(self.alice, make_mock_test("Alice Standalone"))
        self.login(self.bob)
        for name in ("dashboard", "my_courses", "my_mock_tests"):
            html = self.get(name).content.decode()
            for private in ("Alice Only Course", "Alice Only Test", "Alice Standalone"):
                self.assertNotIn(private, html, name)
        self.assertEqual(self.get("course_library", course.pk).status_code, 403)
        self.assertFalse(can_access_mock_test(self.bob, test))
        self.assertEqual(list(self.get("dashboard").context["available_tests"]), [])

    def test_changing_the_id_in_the_url_never_reaches_someone_elses_course(self):
        mine, theirs = make_course("Mine"), make_course("Theirs")
        make_library(theirs)
        enroll(self.alice, mine)
        enroll(self.bob, theirs)
        self.login(self.alice)
        self.assertEqual(self.get("course_library", mine.pk).status_code, 200)
        response = self.get("course_library", theirs.pk)
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("Theirs", response.content.decode())


class ProfileTests(StudentTestCase):
    def setUp(self):
        super().setUp()
        UserProfile.objects.filter(user=self.alice).update(phone_number="9000000001")
        UserProfile.objects.filter(user=self.bob).update(phone_number="9000000002")

    def test_email_and_own_phone_are_displayed(self):
        self.login(self.alice)
        html = self.get("profile").content.decode()
        self.assertIn("alice@example.com", html)
        self.assertIn("9000000001", html)
        self.assertNotIn("9000000002", html)
        self.assertNotIn("bob@example.com", html)

    def test_valid_update_changes_only_own_profile(self):
        self.login(self.alice)
        response = self.client.post(reverse("profile"), {"phone_number": "9123456789"})
        self.assertRedirects(response, reverse("profile"))
        self.assertEqual(UserProfile.objects.get(user=self.alice).phone_number, "9123456789")
        self.assertEqual(UserProfile.objects.get(user=self.bob).phone_number, "9000000002")

    def test_update_confirmation_is_shown(self):
        self.login(self.alice)
        response = self.client.post(reverse("profile"), {"phone_number": "9123456789"}, follow=True)
        self.assertContains(response, "Profile updated.")

    def test_no_way_to_edit_another_users_profile(self):
        self.login(self.alice)
        self.client.post(
            reverse("profile"),
            {"phone_number": "9123456789", "user": self.bob.pk, "user_id": self.bob.pk, "id": UserProfile.objects.get(user=self.bob).pk},
        )
        self.assertEqual(UserProfile.objects.get(user=self.bob).phone_number, "9000000002")
        self.assertEqual(UserProfile.objects.get(user=self.bob).user_id, self.bob.pk)
        self.assertEqual(UserProfile.objects.get(user=self.alice).phone_number, "9123456789")
        self.assertEqual(reverse("profile"), "/profile/")  # no id/user parameter in the route

    def test_email_is_not_editable_here(self):
        self.login(self.alice)
        self.client.post(reverse("profile"), {"phone_number": "9123456789", "email": "evil@example.com"})
        self.assertEqual(User.objects.get(pk=self.alice.pk).email, "alice@example.com")

    def test_max_length_follows_the_model(self):
        self.login(self.alice)
        response = self.client.post(reverse("profile"), {"phone_number": "1" * 16})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(UserProfile.objects.get(user=self.alice).phone_number, "9000000001")
        ok = self.client.post(reverse("profile"), {"phone_number": "1" * 15})
        self.assertEqual(ok.status_code, 302)

    def test_blank_phone_clears_the_number(self):
        self.login(self.alice)
        self.client.post(reverse("profile"), {"phone_number": ""})
        self.assertIsNone(UserProfile.objects.get(user=self.alice).phone_number)

    def test_csrf_is_enforced(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.alice)
        response = client.post(reverse("profile"), {"phone_number": "9123456789"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(UserProfile.objects.get(user=self.alice).phone_number, "9000000001")
        self.assertIn("csrfmiddlewaretoken", client.get(reverse("profile")).content.decode())

    def test_password_change_uses_the_allauth_route(self):
        self.login(self.alice)
        html = self.get("profile").content.decode()
        self.assertIn(f'href="{reverse("account_change_password")}"', html)


class NavigationTests(StudentTestCase):
    STUDENT_NAMES = ("dashboard", "my_courses", "my_mock_tests", "profile")

    def test_anonymous_nav_is_unchanged(self):
        html = self.get("home").content.decode()
        for name in self.STUDENT_NAMES:
            self.assertNotIn(f'href="{reverse(name)}"', html, name)
        for name in ("course_list", "mock_test_list", "blog_list", "account_login", "account_signup"):
            self.assertIn(f'href="{reverse(name)}"', html, name)

    def test_authenticated_nav_exposes_student_destinations_and_keeps_public_ones(self):
        self.login(self.alice)
        html = self.get("dashboard").content.decode()
        for name in (*self.STUDENT_NAMES, "account_change_password", "account_logout",
                     "course_list", "mock_test_list", "blog_list"):
            self.assertIn(reverse(name), html, name)

    def test_login_lands_on_the_dashboard(self):
        self.assertEqual(resolve_url(settings.LOGIN_REDIRECT_URL), reverse("dashboard"))


class RegressionTests(StudentTestCase):
    def test_public_pages_stay_public_and_uncached_headers_untouched(self):
        course, test = make_course("Public Course"), make_mock_test("Public Test")
        for name, args in (("home", ()), ("course_list", ()), ("course_detail", (course.pk,)),
                           ("mock_test_list", ()), ("mock_test_detail", (test.pk,))):
            response = self.get(name, *args)
            self.assertEqual(response.status_code, 200, name)
            self.assertNotIn("no-store", response.headers.get("Cache-Control", ""), name)
        make_course("Hidden Course", published=False)
        self.assertNotIn("Hidden Course", self.get("course_list").content.decode())

    def test_single_active_session_still_displaces_the_first_session(self):
        first, second = self.client_class(), self.client_class()
        first.force_login(self.alice)
        second.force_login(self.alice)
        response = first.get(reverse("dashboard"))
        self.assertRedirects(response, CONFLICT_URL, fetch_redirect_response=False)
        self.assertEqual(second.get(reverse("dashboard")).status_code, 200)

    def test_student_gets_write_nothing_to_access_or_attempt_tables(self):
        course = make_course()
        chapters = make_library(course)
        test = make_mock_test()
        CourseMockTest.objects.create(course=course, mock_test=test)
        enroll(self.alice, course)
        buy_mock_test(self.alice, make_mock_test("Own"))
        touch(self.alice, chapters[0])
        models = (Enrollment, MockTestEnrollment, Order, Attempt, UserChapterProgress, UserProfile)
        before = [(m, m.objects.count()) for m in models]
        self.login(self.alice)
        for name, args in (("dashboard", ()), ("my_courses", ()), ("my_mock_tests", ()),
                           ("profile", ()), ("course_library", (course.pk,))):
            self.assertEqual(self.get(name, *args).status_code, 200, name)
        self.get("course_detail", course.pk)
        self.assertEqual([(m, m.objects.count()) for m in models], before)

    def test_public_enroll_cta_still_creates_no_enrollment(self):
        course = make_course()
        PricingPlan.objects.create(course=course, duration_months=3, price_paise=100000)
        self.login(self.alice)
        self.get("course_detail", course.pk)
        self.assertEqual(Enrollment.objects.count(), 0)
