"""Phase 4: public site (REQ-WEB-01..07, REQ-MOCK-02 public discovery).

Server-rendered, read-only pages. Nothing here may grant access, write
enrollments/orders/attempts, or expose chapters, PDFs, or questions.
"""

import re
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import connection
from django.template import Context, Template
from django.test import RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from core.context_processors import support_link
from core.models import (
    Attempt,
    BlogPost,
    Book,
    Chapter,
    Course,
    CourseMockTest,
    Enrollment,
    MockTest,
    MockTestPricingPlan,
    Order,
    PricingPlan,
    Question,
)

CONFLICT_URL = "/accounts/login/?error=session_conflict"
SUPPORT_URL = "https://support.example.com/chat-test"


def make_course(title="Course A", published=True, **kwargs):
    return Course.objects.create(
        title=title,
        short_description=kwargs.pop("short_description", f"Short {title}"),
        full_description=kwargs.pop("full_description", f"Full {title}"),
        is_published=published,
        **kwargs,
    )


def make_mock_test(title="Test A", published=True, **kwargs):
    kwargs.setdefault("duration_minutes", 60)
    return MockTest.objects.create(title=title, is_published=published, **kwargs)


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"]
)
class PublicTestCase(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.author = User.objects.create_user("author", "author@example.com", "x")

    def make_post(self, title="Post", slug=None, published_at=None, body="Body"):
        return BlogPost.objects.create(
            title=title,
            slug=slug or title.lower().replace(" ", "-"),
            body=body,
            published_at=published_at,
            author=self.author,
        )

    def get(self, name, *args):
        return self.client.get(reverse(name, args=args))


class InrFilterTests(TestCase):
    def render(self, value):
        return Template("{% load public_extras %}{{ v|inr }}").render(Context({"v": value}))

    def test_whole_rupees(self):
        self.assertEqual(self.render(149900), "₹1,499")

    def test_paise_remainder_is_kept(self):
        self.assertEqual(self.render(150050), "₹1,500.50")
        self.assertEqual(self.render(5), "₹0.05")

    def test_zero_and_none(self):
        self.assertEqual(self.render(0), "₹0")
        self.assertEqual(self.render(None), "")


class HomeTests(PublicTestCase):
    def test_home_returns_200_with_shared_navigation(self):
        response = self.get("home")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("<nav", html)
        for name in ("home", "course_list", "mock_test_list", "blog_list"):
            self.assertIn(f'href="{reverse(name)}"', html, name)

    def test_published_content_appears(self):
        make_course("Published Course")
        make_mock_test("Published Mock")
        self.make_post("Published Post", published_at=timezone.now())
        html = self.get("home").content.decode()
        self.assertIn("Published Course", html)
        self.assertIn("Published Mock", html)
        self.assertIn("Published Post", html)

    def test_unpublished_and_draft_content_does_not_appear(self):
        make_course("Hidden Course", published=False)
        make_mock_test("Hidden Mock", published=False)
        self.make_post("Draft Post", published_at=None)
        self.make_post("Future Post", published_at=timezone.now() + timedelta(days=1))
        html = self.get("home").content.decode()
        for hidden in ("Hidden Course", "Hidden Mock", "Draft Post", "Future Post"):
            self.assertNotIn(hidden, html)

    def test_empty_state_renders_cleanly(self):
        response = self.get("home")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        for marker in ("courses-empty", "mock-tests-empty", "blog-empty"):
            self.assertIn(marker, html)

    def test_home_lists_at_most_six_courses(self):
        for i in range(8):
            make_course(f"Course {i}")
        html = self.get("home").content.decode()
        self.assertEqual(html.count('class="card h-100"'), 6)

    def test_anonymous_home_offers_login_signup_links_and_no_csrf_form(self):
        html = self.get("home").content.decode()
        self.assertIn(f'href="{reverse("account_signup")}"', html)
        self.assertIn(f'href="{reverse("account_login")}"', html)
        self.assertNotIn(reverse("google_login"), html)
        self.assertNotIn("csrfmiddlewaretoken", html)


class CourseTests(PublicTestCase):
    def test_listing_shows_only_published_courses(self):
        make_course(
            "Live Course",
            short_description="Live summary",
            subjects_covered=["Cost Accounting", "Auditing"],
        )
        make_course("Draft Course", published=False)
        response = self.get("course_list")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Live Course", html)
        self.assertIn("Live summary", html)
        self.assertIn("Cost Accounting", html)
        self.assertNotIn("Draft Course", html)

    def test_listing_empty_state(self):
        self.assertIn("courses-empty", self.get("course_list").content.decode())

    def test_detail_renders_published_course(self):
        course = make_course(
            "Live Course",
            full_description="Line one\n\nLine two",
            subjects_covered=["Taxation"],
        )
        response = self.get("course_detail", course.pk)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Live Course", html)
        self.assertIn("Line one", html)
        self.assertIn("Taxation", html)

    def test_unpublished_detail_is_404(self):
        course = make_course("Draft Course", published=False)
        self.assertEqual(self.get("course_detail", course.pk).status_code, 404)
        self.assertEqual(self.get("course_detail", 999999).status_code, 404)

    def test_pricing_renders_from_paise_in_duration_order(self):
        course = make_course()
        PricingPlan.objects.create(course=course, duration_months=12, price_paise=250000)
        PricingPlan.objects.create(course=course, duration_months=3, price_paise=99900)
        PricingPlan.objects.create(course=course, duration_months=6, price_paise=150050)
        html = self.get("course_detail", course.pk).content.decode()
        self.assertIn("₹999", html)
        self.assertIn("₹1,500.50", html)
        self.assertIn("₹2,500", html)
        self.assertNotIn("99900", html)
        self.assertLess(html.index("3 months"), html.index("6 months"))
        self.assertLess(html.index("6 months"), html.index("12 months"))
        listing = self.get("course_list").content.decode()
        self.assertIn("From ₹999", listing)
        self.assertIn("3 / 6 / 12 months", listing)

    def test_course_without_plans_says_pricing_unavailable(self):
        course = make_course()
        self.assertIn(
            "Pricing is not available yet.",
            self.get("course_detail", course.pk).content.decode(),
        )
        self.assertNotIn("From ₹", self.get("course_list").content.decode())

    def test_no_protected_chapter_or_pdf_content_leaks(self):
        course = make_course()
        book = Book.objects.create(course=course, title="Secret Book Title")
        chapter = Chapter.objects.create(
            book=book, title="Secret Chapter Title", pdf_object_key="chapters/secret-42.pdf"
        )
        Question.objects.create(
            chapter=chapter,
            text="Secret practice question?",
            option_a="a", option_b="b", option_c="c", option_d="d",
            correct_option="A",
            explanation="Secret explanation",
        )
        for response in (self.get("course_list"), self.get("course_detail", course.pk)):
            html = response.content.decode()
            for secret in (
                "Secret Book Title",
                "Secret Chapter Title",
                "secret-42",
                ".pdf",
                "Secret practice question",
                "Secret explanation",
            ):
                self.assertNotIn(secret, html)

    def test_counts_are_aggregates_and_ignore_unpublished_mock_tests(self):
        course = make_course()
        book = Book.objects.create(course=course, title="B")
        chapters = [Chapter.objects.create(book=book, title=f"C{i}") for i in range(2)]
        for chapter in chapters:
            for _ in range(2):
                Question.objects.create(
                    chapter=chapter, text="q", option_a="a", option_b="b",
                    option_c="c", option_d="d", correct_option="A",
                )
        live, hidden = make_mock_test("Live Mock"), make_mock_test("Hidden Mock", published=False)
        CourseMockTest.objects.create(course=course, mock_test=live)
        CourseMockTest.objects.create(course=course, mock_test=hidden)
        html = self.get("course_detail", course.pk).content.decode()
        self.assertIn("2 chapters", html)
        self.assertIn("4 practice MCQs", html)
        self.assertIn("1 mock test<", html)
        self.assertNotIn("Hidden Mock", html)

    def test_listing_query_count_does_not_grow_with_courses(self):
        def count_queries():
            with CaptureQueriesContext(connection) as ctx:
                self.get("course_list")
            return len(ctx)

        course = make_course("First")
        PricingPlan.objects.create(course=course, duration_months=3, price_paise=1000)
        baseline = count_queries()
        for i in range(5):
            extra = make_course(f"Extra {i}")
            PricingPlan.objects.create(course=extra, duration_months=6, price_paise=2000)
        self.assertEqual(count_queries(), baseline)

    def test_anonymous_detail_routes_to_login_and_signup_with_next(self):
        course = make_course()
        html = self.get("course_detail", course.pk).content.decode()
        next_param = f"?next={reverse('course_detail', args=[course.pk])}"
        self.assertIn(f'href="{reverse("account_login")}{next_param}"', html)
        self.assertIn(f'href="{reverse("account_signup")}{next_param}"', html)

    def test_authenticated_detail_without_plans_keeps_enroll_cta_disabled_without_login_prompts(self):
        # Phase 8: a course with no PricingPlan has nothing to buy, so the CTA stays disabled.
        course = make_course()
        self.client.force_login(User.objects.create_user("student", "s@example.com", "x"))
        html = self.get("course_detail", course.pk).content.decode()
        self.assertNotIn("Create an account", html)
        self.assertNotIn(f'href="{reverse("account_login")}', html)
        self.assertEqual(html.count("Enroll Now"), 1)
        self.assertIn('data-enroll-state="unavailable"', html)
        self.assertRegex(html, r"<button[^>]*\bdisabled\b[^>]*>Enroll Now</button>")

    def test_html_in_course_fields_is_escaped(self):
        course = make_course(
            "<script>alert(1)</script>",
            short_description="<b>bold</b>",
            subjects_covered=["<i>x</i>"],
        )
        for response in (self.get("course_list"), self.get("course_detail", course.pk)):
            html = response.content.decode()
            self.assertNotIn("<script>alert(1)</script>", html)
            self.assertNotIn("<i>x</i>", html)
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)


class MockTestPublicTests(PublicTestCase):
    def test_listing_shows_only_published_tests_with_public_metadata(self):
        live = make_mock_test("Live Mock", description="About it", duration_minutes=90)
        MockTestPricingPlan.objects.create(mock_test=live, price_paise=49900)
        make_mock_test("Hidden Mock", published=False)
        response = self.get("mock_test_list")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Live Mock", html)
        self.assertIn("About it", html)
        self.assertIn("90 minutes", html)
        self.assertIn("₹499", html)
        self.assertNotIn("Hidden Mock", html)

    def test_listing_empty_state(self):
        self.assertIn("mock-tests-empty", self.get("mock_test_list").content.decode())

    def test_detail_published_and_unpublished(self):
        live = make_mock_test("Live Mock", description="Detail text", duration_minutes=45)
        MockTestPricingPlan.objects.create(mock_test=live, price_paise=10000)
        hidden = make_mock_test("Hidden Mock", published=False)
        response = self.get("mock_test_detail", live.pk)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        for expected in ("Live Mock", "Detail text", "45 minutes", "₹100"):
            self.assertIn(expected, html)
        self.assertEqual(self.get("mock_test_detail", hidden.pk).status_code, 404)

    def test_questions_are_never_exposed_and_no_attempt_is_created(self):
        mock = make_mock_test("Live Mock")
        Question.objects.create(
            mock_test=mock, text="Secret exam question?", option_a="Alpha secret",
            option_b="b", option_c="c", option_d="d", correct_option="A",
            explanation="Secret reasoning",
        )
        for response in (self.get("mock_test_list"), self.get("mock_test_detail", mock.pk)):
            html = response.content.decode()
            for secret in ("Secret exam question", "Alpha secret", "Secret reasoning"):
                self.assertNotIn(secret, html)
        self.assertEqual(Attempt.objects.count(), 0)


class BlogTests(PublicTestCase):
    def test_listing_only_published_non_future_posts_newest_first(self):
        now = timezone.now()
        self.make_post("Older Post", published_at=now - timedelta(days=5))
        self.make_post("Newer Post", published_at=now - timedelta(days=1))
        self.make_post("Draft Post", published_at=None)
        self.make_post("Future Post", published_at=now + timedelta(days=1))
        response = self.get("blog_list")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertNotIn("Draft Post", html)
        self.assertNotIn("Future Post", html)
        self.assertLess(html.index("Newer Post"), html.index("Older Post"))

    def test_listing_shows_excerpt_not_full_body(self):
        body = " ".join(f"word{i}" for i in range(100))
        self.make_post("Long Post", published_at=timezone.now(), body=body)
        html = self.get("blog_list").content.decode()
        self.assertIn("word0", html)
        self.assertNotIn("word99", html)

    def test_detail_for_published_post(self):
        post = self.make_post(
            "Live Post", published_at=timezone.now() - timedelta(days=1), body="First\n\nSecond"
        )
        response = self.get("blog_detail", post.slug)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Live Post", html)
        self.assertIn("First", html)
        self.assertIn("Second", html)
        self.assertIn(post.published_at.strftime("%Y-%m-%d"), html)

    def test_draft_future_and_unknown_detail_are_404(self):
        draft = self.make_post("Draft Post", published_at=None)
        future = self.make_post("Future Post", published_at=timezone.now() + timedelta(days=1))
        for slug in (draft.slug, future.slug, "no-such-post"):
            self.assertEqual(self.get("blog_detail", slug).status_code, 404, slug)

    def test_html_in_title_and_body_is_escaped(self):
        post = self.make_post(
            "<script>alert(1)</script>",
            slug="xss",
            published_at=timezone.now(),
            body="<img src=x onerror=alert(2)>",
        )
        for response in (self.get("blog_list"), self.get("blog_detail", post.slug)):
            html = response.content.decode()
            self.assertNotIn("<script>alert(1)</script>", html)
            self.assertNotIn("<img src=x", html)
        detail = self.get("blog_detail", post.slug).content.decode()
        self.assertIn("&lt;img src=x onerror=alert(2)&gt;", detail)


class NavigationTests(PublicTestCase):
    PUBLIC_ROUTES = (
        ("home", ()),
        ("course_list", ()),
        ("mock_test_list", ()),
        ("blog_list", ()),
    )

    def test_anonymous_sees_login_and_signup_but_no_logout(self):
        for name, args in self.PUBLIC_ROUTES:
            html = self.get(name, *args).content.decode()
            self.assertIn(f'href="{reverse("account_login")}"', html, name)
            self.assertIn(f'href="{reverse("account_signup")}"', html, name)
            self.assertNotIn(f'action="{reverse("account_logout")}"', html, name)

    def test_authenticated_sees_account_menu_not_anonymous_ctas(self):
        user = User.objects.create_user("student", "s@example.com", "x")
        self.client.force_login(user)
        for name, args in self.PUBLIC_ROUTES:
            html = self.get(name, *args).content.decode()
            self.assertNotIn(f'href="{reverse("account_login")}"', html, name)
            self.assertNotIn(f'href="{reverse("account_signup")}"', html, name)
            self.assertNotIn(reverse("google_login"), html, name)
            self.assertIn("student", html, name)
            self.assertIn(f'href="{reverse("account_email")}"', html, name)
            self.assertIn(f'href="{reverse("account_change_password")}"', html, name)
            # allauth logout is POST-only: it must be a CSRF-protected form.
            self.assertIn(f'<form method="post" action="{reverse("account_logout")}"', html, name)

    def test_logout_from_the_nav_form_works(self):
        self.client.force_login(User.objects.create_user("student", "s@example.com", "x"))
        self.client.post(reverse("account_logout"))
        self.assertIn(
            f'href="{reverse("account_login")}"', self.get("home").content.decode()
        )

    def test_templates_use_named_urls_not_hardcoded_auth_paths(self):
        pattern = re.compile(r"""(?:href|action)=["']/accounts/""")
        offenders = [
            str(path)
            for path in Path(settings.BASE_DIR, "templates").rglob("*.html")
            if pattern.search(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [])

    def test_nav_marks_the_current_section(self):
        html = self.get("course_list").content.decode()
        self.assertRegex(html, r'class="nav-link active"\s+href="/courses/"\s+aria-current="page"')
        self.assertNotIn('aria-current="page">Blog', html)

    def test_public_pages_are_read_only(self):
        course = make_course()
        for name, args in self.PUBLIC_ROUTES + (("course_detail", (course.pk,)),):
            self.assertEqual(self.client.post(reverse(name, args=args)).status_code, 405, name)
        self.assertEqual(Enrollment.objects.count() + Order.objects.count(), 0)

    def test_bootstrap_assets_are_pinned_with_integrity_hashes(self):
        html = self.get("home").content.decode()
        self.assertEqual(html.count('integrity="sha384-'), 2)
        self.assertIn("bootstrap@5.3.3", html)


class PublicCacheabilityTests(PublicTestCase):
    """SECURITY.md §9: anonymous public pages stay edge-cacheable; signed-in
    copies of the same pages (personalized nav) are private/no-store."""

    def seed_routes(self):
        course = make_course()
        PricingPlan.objects.create(course=course, duration_months=3, price_paise=99900)
        mock = make_mock_test()
        MockTestPricingPlan.objects.create(mock_test=mock, price_paise=49900)
        post = self.make_post("Live Post", published_at=timezone.now())
        return (
            ("home", ()),
            ("course_list", ()),
            ("course_detail", (course.pk,)),
            ("mock_test_list", ()),
            ("mock_test_detail", (mock.pk,)),
            ("blog_list", ()),
            ("blog_detail", (post.slug,)),
        )

    def test_anonymous_home_returns_200_and_sets_no_csrf_cookie(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(settings.CSRF_COOKIE_NAME, response.cookies)
        self.assertNotIn(settings.CSRF_COOKIE_NAME, self.client.cookies)
        self.assertNotIn("csrfmiddlewaretoken", response.content.decode())

    def test_anonymous_public_pages_are_cookie_free_and_not_private(self):
        for name, args in self.seed_routes():
            response = self.get(name, *args)
            self.assertEqual(response.status_code, 200, name)
            self.assertEqual(len(response.cookies), 0, f"{name} set {list(response.cookies)}")
            control = response.headers.get("Cache-Control", "")
            for directive in ("private", "no-store", "no-cache"):
                self.assertNotIn(directive, control, name)
            self.assertNotIn("csrfmiddlewaretoken", response.content.decode(), name)
        self.assertEqual(len(self.client.cookies), 0)

    def test_authenticated_public_pages_are_private_no_store(self):
        routes = self.seed_routes()
        self.client.force_login(User.objects.create_user("student", "s@example.com", "x"))
        for name, args in routes:
            control = self.get(name, *args).headers["Cache-Control"]
            self.assertIn("no-store", control, name)
            self.assertIn("private", control, name)


class PlanSelectorTests(PublicTestCase):
    """REQ-WEB-04: selecting a validity plan updates the displayed price.

    Django tests cannot run browser JS, so these pin the rendered selector /
    data contract that the (small, reviewable) inline script consumes.
    """

    RADIO = re.compile(
        r'<input class="form-check-input" type="radio" name="plan"\s+'
        r'id="plan-(\d+)" value="(\d+)"\s+'
        r'data-price-display="([^"]*)"( checked)?>'
    )

    def make_priced_course(self):
        course = make_course()
        plans = {
            12: PricingPlan.objects.create(course=course, duration_months=12, price_paise=250000),
            3: PricingPlan.objects.create(course=course, duration_months=3, price_paise=99900),
            6: PricingPlan.objects.create(course=course, duration_months=6, price_paise=150050),
        }
        return course, plans

    def detail_html(self, course):
        return self.get("course_detail", course.pk).content.decode()

    def test_one_radio_per_plan_in_duration_order_with_display_prices(self):
        course, plans = self.make_priced_course()
        other = make_course("Other")
        PricingPlan.objects.create(course=other, duration_months=3, price_paise=777700)
        html = self.detail_html(course)
        radios = self.RADIO.findall(html)
        self.assertEqual(
            [(pk, value, display) for pk, value, display, _ in radios],
            [
                (str(plans[m].pk), str(plans[m].pk), price)
                for m, price in ((3, "₹999"), (6, "₹1,500.50"), (12, "₹2,500"))
            ],
        )
        self.assertNotIn("₹7,777", html)

    def test_first_plan_is_selected_and_initial_price_matches_it(self):
        course, _ = self.make_priced_course()
        html = self.detail_html(course)
        checked = [r for r in self.RADIO.findall(html) if r[3]]
        self.assertEqual(len(checked), 1)
        self.assertEqual(checked[0][2], "₹999")
        initial = re.search(r'id="selected-price"[^>]*>([^<]*)<', html)
        self.assertEqual(initial.group(1), "₹999")

    def test_each_radio_has_a_label_with_duration_and_price(self):
        course, plans = self.make_priced_course()
        html = self.detail_html(course)
        for months, price in ((3, "₹999"), (6, "₹1,500.50"), (12, "₹2,500")):
            pk = plans[months].pk
            self.assertRegex(
                html,
                rf'for="plan-{pk}">\s*<span>{months} months</span>\s*'
                rf'<span class="fw-semibold">{re.escape(price)}</span>',
            )

    def test_display_values_come_from_paise_without_float_artifacts(self):
        course = make_course()
        for months, paise in ((3, 1), (6, 1999), (12, 10000000)):
            PricingPlan.objects.create(course=course, duration_months=months, price_paise=paise)
        html = self.detail_html(course)
        self.assertEqual(
            [display for _, _, display, _ in self.RADIO.findall(html)],
            ["₹0.01", "₹19.99", "₹100,000"],
        )
        self.assertNotIn("10000000", html)

    def test_inline_script_is_tiny_and_only_copies_the_preformatted_price(self):
        course, _ = self.make_priced_course()
        html = self.detail_html(course)
        scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
        self.assertEqual(len(scripts), 1)
        script = scripts[0]
        self.assertLessEqual(len(script.strip().splitlines()), 12)
        # Wired to the ids/attribute the markup actually provides.
        self.assertIn('getElementById("selected-price")', script)
        self.assertIn('#plan-selector input[name="plan"]', script)
        self.assertIn('addEventListener("change"', script)
        self.assertIn("radio.dataset.priceDisplay", script)
        for id_ in ("selected-price", "plan-selector"):
            self.assertIn(f'id="{id_}"', html)
        # No client-side money math or locale formatting.
        for banned in ("parseFloat", "parseInt", "toFixed", "toLocaleString", "Math.", "Number("):
            self.assertNotIn(banned, script)

    def test_course_without_plans_renders_no_selector(self):
        course = make_course()
        html = self.detail_html(course)
        for absent in ("plan-selector", "selected-price", 'name="plan"', "<script>"):
            self.assertNotIn(absent, html)
        self.assertIn("Pricing is not available yet.", html)

    def test_selector_adds_no_form_no_writes_and_no_query_growth(self):
        course, _ = self.make_priced_course()
        self.assertNotIn("<form", self.detail_html(course))

        def count_queries(target):
            with CaptureQueriesContext(connection) as ctx:
                self.get("course_detail", target.pk)
            return len(ctx)

        baseline = count_queries(course)
        many = make_course("Many Plans")
        for months in (3, 6, 12):
            PricingPlan.objects.create(course=many, duration_months=months, price_paise=months * 1000)
        self.assertEqual(count_queries(many), baseline)
        self.assertEqual(Order.objects.count() + Enrollment.objects.count(), 0)


class EnrollCtaTests(PublicTestCase):
    """REQ-WEB-03/04 render "Enroll Now"; Phase 4 implements no checkout.

    The full logged-out -> login -> checkout flow (REQ-WEB-07) is a later phase.
    """

    def login_next(self, course):
        return f'{reverse("account_login")}?next={reverse("course_detail", args=[course.pk])}'

    def test_anonymous_cards_offer_view_details_and_enroll_now_via_login(self):
        courses = [make_course("Alpha"), make_course("Beta")]
        for name in ("home", "course_list"):
            html = self.get(name).content.decode()
            self.assertEqual(html.count("Enroll Now"), 2, name)
            self.assertEqual(html.count(">View Details</a>"), 2, name)
            for course in courses:
                self.assertIn(f'href="{self.login_next(course)}">Enroll Now</a>', html, name)
                self.assertIn(
                    f'href="{reverse("course_detail", args=[course.pk])}">View Details</a>',
                    html,
                    name,
                )

    def test_anonymous_detail_enroll_now_routes_through_login_back_to_the_course(self):
        course = make_course()
        html = self.get("course_detail", course.pk).content.decode()
        self.assertEqual(html.count("Enroll Now"), 1)
        self.assertIn(f'href="{self.login_next(course)}">Enroll Now</a>', html)
        login = self.client.get(self.login_next(course)).content.decode()
        self.assertIn(f'value="{reverse("course_detail", args=[course.pk])}"', login)

    def test_authenticated_card_cta_is_a_link_to_the_detail_page_where_the_plan_is_chosen(self):
        # Phase 8: cards have no plan selector, so a signed-in student's Enroll Now
        # leads to the detail page (the only place checkout starts), never to checkout itself.
        course = make_course()
        self.client.force_login(User.objects.create_user("student", "s@example.com", "x"))
        detail = reverse("course_detail", args=[course.pk])
        for name in ("home", "course_list"):
            html = self.get(name).content.decode()
            self.assertIn('data-enroll-state="choose-plan"', html, name)
            self.assertRegex(html, rf'<a[^>]*href="{detail}"[^>]*>Enroll Now</a>', name)
            self.assertNotRegex(html, r"<button[^>]*>Enroll Now</button>", name)
            self.assertNotIn("data-checkout-button", html, name)

    def test_checkout_and_payment_stay_off_anonymous_pages_and_off_the_card_lists(self):
        course = make_course()
        PricingPlan.objects.create(course=course, duration_months=3, price_paise=99900)
        anonymous, student = self.client_class(), self.client_class()
        student.force_login(User.objects.create_user("student", "s@example.com", "x"))
        detail = reverse("course_detail", args=[course.pk])
        pages = [
            (anonymous, reverse("home")),
            (anonymous, reverse("course_list")),
            (anonymous, detail),
            (student, reverse("home")),
            (student, reverse("course_list")),
        ]
        for client, url in pages:
            html = client.get(url).content.decode().lower()
            for forbidden in ("checkout", "razorpay", "/orders", "payment"):
                self.assertNotIn(forbidden, html, url)
        # Viewing any page, including the signed-in detail page, only reads.
        student.get(detail)
        self.assertEqual(Order.objects.count() + Enrollment.objects.count(), 0)


class Phase3RegressionTests(PublicTestCase):
    def test_login_and_signup_keep_google_as_a_csrf_protected_post_form(self):
        google_action = f'{reverse("google_login")}?process=login'
        for name in ("account_login", "account_signup"):
            html = self.get(name).content.decode()
            form = re.search(
                rf'<form method="post" action="{re.escape(google_action)}"[^>]*>(.*?)</form>',
                html,
                re.S,
            )
            self.assertIsNotNone(form, name)
            self.assertIn("csrfmiddlewaretoken", form.group(1), name)
            self.assertIn("Continue with Google", form.group(1), name)
            self.assertNotIn(f'href="{reverse("google_login")}', html, name)

    def test_login_and_signup_still_render_with_the_new_base(self):
        for name in ("account_login", "account_signup"):
            response = self.get(name)
            self.assertEqual(response.status_code, 200, name)
            html = response.content.decode()
            self.assertIn("Finance PSU Guide", html)
            self.assertIn(f'action="{reverse("google_login")}?process=login"', html)
            self.assertIn(f'href="{reverse("course_list")}"', html)
            self.assertEqual(html.count("<main"), 1, name)

    def test_session_conflict_notice_survives_and_public_pages_trigger_displacement(self):
        user = User.objects.create_user("student", "s@example.com", "x")
        first, second = self.client_class(), self.client_class()
        first.force_login(user)
        second.force_login(user)

        response = first.get(reverse("course_list"))
        self.assertRedirects(response, CONFLICT_URL, fetch_redirect_response=False)
        html = first.get(CONFLICT_URL).content.decode()
        self.assertIn("session-conflict-notice", html)
        self.assertIn(f'href="{reverse("account_signup")}"', html)
        self.assertEqual(second.get(reverse("course_list")).status_code, 200)


class SupportLinkTests(PublicTestCase):
    def test_no_link_is_emitted_when_unconfigured(self):
        with override_settings(WHATSAPP_SUPPORT_URL=""):
            for name in ("home", "course_list", "blog_list"):
                html = self.get(name).content.decode()
                self.assertNotIn(">Support<", html, name)
                self.assertNotIn("whatsapp", html.lower(), name)
                self.assertNotIn("wa.me", html, name)

    def test_env_template_lists_the_setting_without_a_value(self):
        env_example = Path(settings.BASE_DIR, ".env.example").read_text(encoding="utf-8")
        self.assertIn("WHATSAPP_SUPPORT_URL=", env_example)
        self.assertNotRegex(env_example, r"WHATSAPP_SUPPORT_URL=\S")

    def test_configured_https_link_opens_in_a_new_tab(self):
        with override_settings(WHATSAPP_SUPPORT_URL=SUPPORT_URL):
            for name in ("home", "course_list", "mock_test_list", "blog_list"):
                html = self.get(name).content.decode()
                self.assertIn(
                    f'href="{SUPPORT_URL}" target="_blank" rel="noopener noreferrer">Support</a>',
                    html,
                    name,
                )

    def test_non_https_values_are_never_rendered(self):
        for bad in (
            "javascript:alert(1)",
            "http://support.example.com",
            "wa.me/123",
            "https://",
            "https:///no-host",
            "https://exa mple.com/x",
            "data:text/html,https://x.example",
        ):
            with override_settings(WHATSAPP_SUPPORT_URL=bad):
                self.assertIsNone(support_link(RequestFactory().get("/"))["support_url"], bad)
                html = self.get("home").content.decode()
                self.assertNotIn(f'href="{bad}"', html, bad)
                self.assertNotIn(">Support<", html, bad)

    def test_every_rendered_support_link_is_the_configured_value_and_hardened(self):
        # Nav, footer and the home "Need help?" block must all agree, for
        # anonymous and signed-in visitors alike.
        anchor = re.compile(r"<a\b[^>]*>")
        user = User.objects.create_user("student", "s@example.com", "x")
        with override_settings(WHATSAPP_SUPPORT_URL=SUPPORT_URL):
            for signed_in in (False, True):
                if signed_in:
                    self.client.force_login(user)
                html = self.get("home").content.decode()
                anchors = [a for a in anchor.findall(html) if SUPPORT_URL in a]
                self.assertEqual(len(anchors), 3, signed_in)  # nav, footer, help block
                for tag in anchors:
                    self.assertIn(f'href="{SUPPORT_URL}"', tag)
                    self.assertIn('target="_blank"', tag)
                    self.assertIn('rel="noopener noreferrer"', tag)

    def test_configured_value_is_html_escaped_in_the_attribute(self):
        tricky = 'https://support.example.com/x?a=1&b="><script>alert(1)</script>'
        with override_settings(WHATSAPP_SUPPORT_URL=tricky):
            html = self.get("home").content.decode()
        self.assertNotIn("<script>alert(1)</script>", html)
