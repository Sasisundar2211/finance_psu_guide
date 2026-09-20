"""Phase 6: protected chapter PDF reader
(REQ-COURSE-01..04, REQ-DASH-01, API.md §3/§7, DECISIONS.md D10 item 8, D11.4, D11.5, D15).

R2 is never contacted: `core.r2.boto3.client` is mocked in every test that could
reach it, and the R2 settings are fake placeholders. Django tests cannot execute
browser JavaScript, so the client loading model is pinned by inspecting the
template's script source (ClientLoadingContractTests); it is not run here.
"""

import re
from datetime import timedelta
from unittest.mock import MagicMock, call, patch

from botocore.exceptions import ClientError, NoCredentialsError
from django import forms
from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection, models
from django.test import SimpleTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from core import r2
from core.forms import MAX_CHAPTER_PDF_BYTES, ChapterAdminForm
from core.models import Book, Chapter, Enrollment, UserChapterProgress
from core.student_views import course_progress, recently_accessed_course
from tests.test_student import CONFLICT_URL, StudentTestCase, enroll, make_course, make_library, touch

FAKE_R2 = {
    "R2_BUCKET_NAME": "test-bucket",
    "R2_ENDPOINT_URL": "https://example-account.r2.invalid",
    "R2_ACCESS_KEY_ID": "test-access-key-id",
    "R2_SECRET_ACCESS_KEY": "test-secret-access-key",
}
OBJECT_KEY = "chapters/private-object-key.pdf"
SIGNED_URL = "https://example-account.r2.invalid/test-bucket/opaque?X-Amz-Signature=sig123"
PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<< >>\nendobj\n%%EOF\n"
TEMPLATE = settings.BASE_DIR / "templates" / "student" / "chapter_viewer.html"


def visible(response):
    """Rendered HTML without the random CSRF tokens and the SRI hashes.

    Both are long random-looking base64 strings, so word scans over the raw
    HTML could match them by chance.
    """
    html = response.content.decode()
    html = re.sub(r'value="[^"]{64}"', "", html)
    return re.sub(r'integrity="[^"]*"', "", html)


class ReaderTestCase(StudentTestCase):
    """A one-book course with a PDF-backed chapter, and a mocked R2 client."""

    def setUp(self):
        super().setUp()
        overrides = override_settings(**FAKE_R2)
        overrides.enable()
        self.addCleanup(overrides.disable)
        boto = patch("core.r2.boto3.client")
        self.boto = boto.start()
        self.addCleanup(boto.stop)
        self.r2 = self.boto.return_value
        self.r2.generate_presigned_url.return_value = SIGNED_URL

        self.course = make_course("Reader Course")
        self.chapters = make_library(self.course, books=1, chapters=2, key=OBJECT_KEY)
        self.chapter = self.chapters[0]
        self.signed_url = reverse("chapter_signed_url", args=[self.chapter.pk])
        self.viewer_url = reverse("chapter_viewer", args=[self.chapter.pk])

    def progress_rows(self, **filters):
        return UserChapterProgress.objects.filter(**filters)


class SignedUrlAuthorizationTests(ReaderTestCase):
    def test_anonymous_is_sent_to_the_allauth_login_and_r2_is_untouched(self):
        response = self.client.get(self.signed_url)
        self.assertRedirects(
            response,
            f"{reverse('account_login')}?next={self.signed_url}",
            fetch_redirect_response=False,
        )
        self.boto.assert_not_called()
        self.assertEqual(UserChapterProgress.objects.count(), 0)

    def test_active_enrollment_gets_a_url(self):
        enroll(self.alice, self.course)
        self.login(self.alice)
        response = self.client.get(self.signed_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["url"], SIGNED_URL)

    def test_no_enrollment_or_only_someone_elses_gets_403_calls_no_r2_and_writes_no_progress(self):
        enroll(self.bob, self.course)  # another student's enrollment does not help alice
        self.login(self.alice)
        response = self.client.get(self.signed_url)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"error": "access_denied"})
        self.boto.assert_not_called()
        self.assertEqual(UserChapterProgress.objects.count(), 0)

    def test_expired_enrollment_on_this_course_gets_403(self):
        enroll(self.alice, self.course, days=-1)
        self.login(self.alice)
        response = self.client.get(self.signed_url)
        self.assertEqual(response.status_code, 403)
        self.boto.assert_not_called()
        self.assertEqual(UserChapterProgress.objects.count(), 0)

    def test_expiry_boundary_is_exclusive(self):
        enrollment = enroll(self.alice, self.course)
        frozen = timezone.now()
        enrollment.expires_at = frozen
        enrollment.save()
        self.login(self.alice)
        with patch("django.utils.timezone.now", return_value=frozen):
            self.assertEqual(self.client.get(self.signed_url).status_code, 403)

    def test_enrollment_in_a_different_course_does_not_cover_the_chapter(self):
        other = make_course("Other Course")
        make_library(other, books=1, chapters=1)
        enroll(self.alice, other)
        self.login(self.alice)
        self.assertEqual(self.client.get(self.signed_url).status_code, 403)
        self.boto.assert_not_called()

    def test_unknown_chapter_gets_the_same_403_and_reveals_nothing(self):
        enroll(self.alice, self.course)
        self.login(self.alice)
        known_but_denied = make_course("Denied Course")
        denied_chapter = make_library(known_but_denied, books=1, chapters=1, key=OBJECT_KEY)[0]
        unknown = self.client.get(reverse("chapter_signed_url", args=[denied_chapter.pk + 9999]))
        denied = self.client.get(reverse("chapter_signed_url", args=[denied_chapter.pk]))
        self.assertEqual(unknown.status_code, 403)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(unknown.content, denied.content)
        for leaked in (OBJECT_KEY, "Denied Course", "url"):
            self.assertNotIn(leaked, unknown.content.decode())
        self.boto.assert_not_called()

    def test_only_get_is_allowed_so_head_and_post_cannot_touch_progress(self):
        enroll(self.alice, self.course)
        self.login(self.alice)
        self.assertEqual(self.client.head(self.signed_url).status_code, 405)
        self.assertEqual(self.client.post(self.signed_url).status_code, 405)
        self.boto.assert_not_called()
        self.assertEqual(UserChapterProgress.objects.count(), 0)

    def test_a_displaced_session_gets_the_conflict_redirect_not_a_url(self):
        enroll(self.alice, self.course)
        first, second = self.client_class(), self.client_class()
        first.force_login(self.alice)
        second.force_login(self.alice)
        response = first.get(self.signed_url)
        self.assertRedirects(response, CONFLICT_URL, fetch_redirect_response=False)
        self.boto.assert_not_called()
        self.assertEqual(UserChapterProgress.objects.count(), 0)


class SignedUrlContractTests(ReaderTestCase):
    def setUp(self):
        super().setUp()
        enroll(self.alice, self.course)
        self.login(self.alice)

    def test_response_is_exactly_the_url_and_a_60_second_expiry(self):
        response = self.client.get(self.signed_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"url": SIGNED_URL, "expires_in": 60})
        self.assertEqual(r2.PRESIGNED_URL_EXPIRY_SECONDS, 60)

    def test_presign_is_a_get_object_for_the_configured_bucket_and_chapter_key(self):
        self.client.get(self.signed_url)
        self.r2.generate_presigned_url.assert_called_once_with(
            "get_object",
            Params={"Bucket": "test-bucket", "Key": OBJECT_KEY},
            ExpiresIn=60,
        )

    def test_client_is_built_from_the_frozen_settings_only(self):
        self.client.get(self.signed_url)
        self.boto.assert_called_once()
        args, kwargs = self.boto.call_args
        self.assertEqual(args, ("s3",))
        self.assertEqual(kwargs["endpoint_url"], FAKE_R2["R2_ENDPOINT_URL"])
        self.assertEqual(kwargs["aws_access_key_id"], FAKE_R2["R2_ACCESS_KEY_ID"])
        self.assertEqual(kwargs["aws_secret_access_key"], FAKE_R2["R2_SECRET_ACCESS_KEY"])
        self.assertEqual(kwargs["region_name"], "auto")

    def test_the_object_key_is_not_exposed_separately_and_no_secret_appears(self):
        # A real presigned URL necessarily embeds the bucket/key path and the
        # (non-secret) access key id; nothing else about them is ever returned.
        text = self.client.get(self.signed_url).content.decode()
        for secret in (OBJECT_KEY, "pdf_object_key", FAKE_R2["R2_SECRET_ACCESS_KEY"]):
            self.assertNotIn(secret, text)

    def test_response_is_private_no_store_json(self):
        response = self.client.get(self.signed_url)
        control = response.headers["Cache-Control"]
        for directive in ("private", "no-store"):
            self.assertIn(directive, control)
        self.assertEqual(response.headers["Content-Type"], "application/json")

    def test_django_never_fetches_or_streams_pdf_bytes(self):
        response = self.client.get(self.signed_url)
        self.assertFalse(response.streaming)
        self.assertNotIn("pdf", response.headers["Content-Type"])
        # The only thing ever asked of R2 is to sign a URL: no object read.
        self.assertEqual(
            self.r2.method_calls,
            [
                call.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": "test-bucket", "Key": OBJECT_KEY},
                    ExpiresIn=60,
                )
            ],
        )

    def test_success_logs_nothing_so_no_url_can_leak_into_logs(self):
        with self.assertNoLogs(level="INFO"):
            self.client.get(self.signed_url)

    def test_the_url_is_not_stored_anywhere(self):
        self.client.get(self.signed_url)
        self.assertEqual(Chapter.objects.get(pk=self.chapter.pk).pdf_object_key, OBJECT_KEY)
        self.assertNotIn("X-Amz", self.client.get(self.viewer_url).content.decode())

    def test_query_count_is_bounded_and_independent_of_library_size(self):
        with CaptureQueriesContext(connection) as small:
            self.client.get(self.signed_url)
        big_course = make_course("Big Course")
        big = make_library(big_course, books=4, chapters=8, key=OBJECT_KEY)
        enroll(self.alice, big_course)
        with CaptureQueriesContext(connection) as large:
            self.client.get(reverse("chapter_signed_url", args=[big[-1].pk]))
        self.assertEqual(len(large), len(small))
        self.assertLessEqual(len(large), 12)


class SignedUrlFailureTests(ReaderTestCase):
    def setUp(self):
        super().setUp()
        enroll(self.alice, self.course)
        self.login(self.alice)

    def assert_generic_failure(self, response, status, error):
        self.assertEqual(response.status_code, status)
        self.assertEqual(response.json(), {"error": error})
        text = response.content.decode()
        for secret in (OBJECT_KEY, "X-Amz", "botocore", "ClientError", *FAKE_R2.values()):
            self.assertNotIn(secret, text)
        self.assertEqual(UserChapterProgress.objects.count(), 0)
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_chapter_without_a_pdf_is_404_and_does_not_presign_or_record_progress(self):
        Chapter.objects.filter(pk=self.chapter.pk).update(pdf_object_key="")
        response = self.client.get(self.signed_url)
        self.assert_generic_failure(response, 404, "not_available")
        self.boto.assert_not_called()

    def test_a_presign_failure_fails_closed_without_progress_or_leaks(self):
        leak = ClientError(
            {"Error": {"Code": "Boom", "Message": f"{OBJECT_KEY} {FAKE_R2['R2_SECRET_ACCESS_KEY']}"}},
            "GetObject",
        )
        for failure in (leak, NoCredentialsError()):
            self.r2.generate_presigned_url.side_effect = failure
            with self.assertLogs("core.student_views", level="ERROR") as logs:
                response = self.client.get(self.signed_url)
            self.assert_generic_failure(response, 503, "unavailable")
            logged = "\n".join(logs.output)
            for secret in (OBJECT_KEY, SIGNED_URL, *FAKE_R2.values()):
                self.assertNotIn(secret, logged)

    def test_missing_r2_configuration_fails_closed_and_builds_no_client(self):
        for name in FAKE_R2:
            with self.subTest(missing=name), override_settings(**{name: ""}):
                with self.assertLogs("core.student_views", level="ERROR"):
                    response = self.client.get(self.signed_url)
                self.assert_generic_failure(response, 503, "unavailable")
        self.boto.assert_not_called()


class ProgressTests(ReaderTestCase):
    def setUp(self):
        super().setUp()
        enroll(self.alice, self.course)
        self.login(self.alice)

    def test_first_successful_request_creates_one_completed_row(self):
        self.assertEqual(self.client.get(self.signed_url).status_code, 200)
        rows = self.progress_rows(user=self.alice, chapter=self.chapter)
        self.assertEqual(rows.count(), 1)
        self.assertTrue(rows.get().is_completed)
        self.assertEqual(UserChapterProgress.objects.count(), 1)

    def test_repeat_requests_keep_one_row_and_advance_last_accessed_at(self):
        first_at = timezone.now() + timedelta(hours=1)
        second_at = first_at + timedelta(minutes=10)
        with patch("django.utils.timezone.now", return_value=first_at):
            self.client.get(self.signed_url)
        self.assertEqual(self.progress_rows().get().last_accessed_at, first_at)
        with patch("django.utils.timezone.now", return_value=second_at):
            self.client.get(self.signed_url)
        self.assertEqual(self.progress_rows().count(), 1)
        row = self.progress_rows().get()
        self.assertEqual(row.last_accessed_at, second_at)
        self.assertTrue(row.is_completed)

    def test_completed_never_flips_back_and_an_incomplete_row_becomes_completed(self):
        done = touch(self.alice, self.chapter, completed=True, days_ago=3)
        self.client.get(self.signed_url)
        done.refresh_from_db()
        self.assertTrue(done.is_completed)
        self.assertGreater(done.last_accessed_at, timezone.now() - timedelta(minutes=1))

        pending = touch(self.alice, self.chapters[1], completed=False, days_ago=3)
        self.client.get(reverse("chapter_signed_url", args=[self.chapters[1].pk]))
        pending.refresh_from_db()
        self.assertTrue(pending.is_completed)
        self.assertEqual(self.progress_rows(user=self.alice).count(), 2)

    def test_another_users_progress_is_untouched(self):
        enroll(self.bob, self.course)
        bobs = touch(self.bob, self.chapter, completed=False, days_ago=5)
        before = (bobs.pk, bobs.is_completed, self.progress_rows(pk=bobs.pk).get().last_accessed_at)
        self.client.get(self.signed_url)
        after = self.progress_rows(pk=bobs.pk).values_list("pk", "is_completed", "last_accessed_at").get()
        self.assertEqual(before, after)
        self.assertEqual(self.progress_rows(user=self.bob).count(), 1)

    def test_browsing_and_opening_the_viewer_page_record_no_progress(self):
        for _ in range(2):
            self.get("my_courses")
            self.get("dashboard")
            self.get("course_library", self.course.pk)
            self.assertEqual(self.client.get(self.viewer_url).status_code, 200)
        self.assertEqual(UserChapterProgress.objects.count(), 0)
        self.boto.assert_not_called()

    def test_every_failure_path_records_no_progress(self):
        self.client.logout()
        self.login(self.bob)  # not entitled
        self.client.get(self.signed_url)
        self.client.logout()
        self.login(self.alice)
        Chapter.objects.filter(pk=self.chapter.pk).update(pdf_object_key="")
        self.client.get(self.signed_url)
        Chapter.objects.filter(pk=self.chapter.pk).update(pdf_object_key=OBJECT_KEY)
        self.r2.generate_presigned_url.side_effect = NoCredentialsError()
        with self.assertLogs("core.student_views", level="ERROR"):
            self.client.get(self.signed_url)
        self.assertEqual(UserChapterProgress.objects.count(), 0)

    def test_real_access_feeds_the_dashboard_progress_and_recent_course(self):
        self.client.get(self.signed_url)
        self.assertEqual(course_progress(self.alice, [self.course.pk])[self.course.pk]["completed"], 1)
        self.assertEqual(recently_accessed_course(self.alice), self.course)
        html = self.get("dashboard").content.decode()
        self.assertIn("Reader Course", html)

    def test_recent_course_still_requires_active_enrollment_d11_5(self):
        self.client.get(self.signed_url)
        Enrollment.objects.filter(student=self.alice).update(expires_at=timezone.now() - timedelta(days=1))
        self.assertIsNone(recently_accessed_course(self.alice))
        self.assertEqual(self.client.get(self.signed_url).status_code, 403)


class ViewerPageTests(ReaderTestCase):
    def setUp(self):
        super().setUp()
        enroll(self.alice, self.course)

    def test_viewer_requires_login(self):
        response = self.client.get(self.viewer_url)
        self.assertRedirects(
            response,
            f"{reverse('account_login')}?next={self.viewer_url}",
            fetch_redirect_response=False,
        )

    def test_viewer_requires_active_entitlement_and_denies_uniformly(self):
        expired = make_course("Expired Course")
        expired_chapter = make_library(expired, books=1, chapters=1)[0]
        enroll(self.alice, expired, days=-1)
        self.login(self.bob)
        denied = self.client.get(self.viewer_url)
        self.login(self.alice)
        expired_response = self.client.get(reverse("chapter_viewer", args=[expired_chapter.pk]))
        unknown = self.client.get(reverse("chapter_viewer", args=[expired_chapter.pk + 9999]))
        for response in (denied, expired_response, unknown):
            self.assertEqual(response.status_code, 403)
            self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(visible(expired_response), visible(unknown))
        self.assertEqual(self.client.get(self.viewer_url).status_code, 200)
        self.boto.assert_not_called()

    def test_viewer_is_private_no_store_and_read_only(self):
        self.login(self.alice)
        response = self.client.get(self.viewer_url)
        for directive in ("private", "no-store"):
            self.assertIn(directive, response.headers["Cache-Control"])
        self.assertEqual(self.client.post(self.viewer_url).status_code, 405)

    def test_titles_render_and_are_escaped(self):
        book = Book.objects.create(course=self.course, title="Book <b>One</b>", order=9)
        chapter = Chapter.objects.create(
            book=book, title="Tax & <i>Law</i>", order=1, pdf_object_key=OBJECT_KEY
        )
        self.login(self.alice)
        html = self.client.get(reverse("chapter_viewer", args=[chapter.pk])).content.decode()
        for text in ("Tax &amp; &lt;i&gt;Law&lt;/i&gt;", "Book &lt;b&gt;One&lt;/b&gt;", "Reader Course"):
            self.assertIn(text, html)
        self.assertNotIn("<i>Law</i>", html)

    def test_no_object_key_and_no_r2_or_presigned_url_in_the_html(self):
        self.login(self.alice)
        html = self.client.get(self.viewer_url).content.decode()
        for forbidden in (
            OBJECT_KEY,
            "pdf_object_key",
            "X-Amz",
            SIGNED_URL,
            FAKE_R2["R2_ENDPOINT_URL"],
            *FAKE_R2.values(),
        ):
            self.assertNotIn(forbidden, html)
        self.assertNotRegex(html, r"\.pdf\b")

    def test_every_link_is_a_local_route_and_none_points_at_a_pdf_or_r2(self):
        self.login(self.alice)
        html = self.client.get(self.viewer_url).content.decode()
        hrefs = re.findall(r'<a\b[^>]*\bhref="([^"]*)"', html)
        self.assertTrue(hrefs)
        for href in hrefs:
            self.assertTrue(href.startswith(("/", "#")), href)
        self.assertNotRegex(html, r"<(embed|object|iframe)\b")
        self.assertNotRegex(html, r"<a\b[^>]*\bdownload\b")

    def test_no_normal_download_or_save_control(self):
        self.login(self.alice)
        html = visible(self.client.get(self.viewer_url)).lower()
        self.assertNotIn("download", html)
        self.assertNotRegex(html, r"\bsave\b")

    def test_navigation_zoom_fullscreen_and_print_controls_exist(self):
        self.login(self.alice)
        html = self.client.get(self.viewer_url).content.decode()
        for control_id, label in (
            ("pdf-prev", "Previous"),
            ("pdf-next", "Next"),
            ("pdf-zoom-in", "Zoom in"),
            ("pdf-zoom-out", "Zoom out"),
            ("pdf-fullscreen", "Fullscreen"),
            ("pdf-print", "Print"),
        ):
            self.assertRegex(html, rf'<button[^>]*id="{control_id}"[^>]*>{label}</button>')
        for indicator in ("pdf-page-num", "pdf-page-count"):
            self.assertIn(f'id="{indicator}"', html)
        self.assertIn('id="pdf-canvas"', html)

    def test_the_page_points_the_script_at_the_django_endpoint_only(self):
        self.login(self.alice)
        html = self.client.get(self.viewer_url).content.decode()
        self.assertIn(f'data-signed-url="{self.signed_url}"', html)

    def test_no_text_layer_is_rendered(self):
        self.login(self.alice)
        html = self.client.get(self.viewer_url).content.decode().lower()
        for marker in ("textlayer", "text-layer", "text_layer", "gettextcontent"):
            self.assertNotIn(marker, html)

    def test_zoom_is_not_clamped_to_the_container_on_screen(self):
        # Found in a real browser: an on-screen `max-width: 100%` on the canvas made
        # Zoom in a no-op past fit-width. Shrink-to-fit belongs to printing only.
        self.login(self.alice)
        html = self.client.get(self.viewer_url).content.decode()
        css = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
        on_screen, _, printing = css.partition("@media print")
        self.assertNotIn("max-width", on_screen)
        self.assertIn("max-width", printing)

    def test_pdfjs_is_pinned_https_sri_and_only_on_the_viewer(self):
        self.login(self.alice)
        html = self.client.get(self.viewer_url).content.decode()
        srcs = re.findall(r'<script\b[^>]*\bsrc="([^"]*pdfjs[^"]*)"', html)
        self.assertEqual(len(srcs), 1)
        self.assertRegex(srcs[0], r"^https://cdn\.jsdelivr\.net/npm/pdfjs-dist@\d+\.\d+\.\d+/build/pdf\.min\.mjs$")
        self.assertRegex(html, r'data-worker-src="https://cdn\.jsdelivr\.net/npm/pdfjs-dist@\d+\.\d+\.\d+/build/pdf\.worker\.min\.mjs"')
        self.assertRegex(html, r'src="[^"]*pdfjs[^"]*"\s+integrity="sha384-[A-Za-z0-9+/=]+"\s+crossorigin="anonymous"')
        self.assertNotIn("@latest", html)
        # Everything else stays free of PDF.js.
        for name, args in (("dashboard", ()), ("my_courses", ()), ("course_library", (self.course.pk,))):
            self.assertNotIn("pdfjs", self.get(name, *args).content.decode(), name)

    def test_a_chapter_with_no_pdf_shows_a_notice_and_loads_no_reader(self):
        Chapter.objects.filter(pk=self.chapter.pk).update(pdf_object_key="")
        self.login(self.alice)
        html = self.client.get(self.viewer_url).content.decode()
        self.assertIn('id="pdf-unavailable"', html)
        self.assertNotIn("pdf-viewer", html)
        self.assertNotIn("pdfjs", html)


class LibraryLinkTests(ReaderTestCase):
    def test_entitled_chapters_link_to_the_viewer_and_nothing_else_about_pdfs(self):
        enroll(self.alice, self.course)
        self.login(self.alice)
        html = self.get("course_library", self.course.pk).content.decode()
        for chapter in self.chapters:
            self.assertIn(f'href="{reverse("chapter_viewer", args=[chapter.pk])}"', html)
        for forbidden in ("signed-url", OBJECT_KEY, "pdf_object_key", "X-Amz"):
            self.assertNotIn(forbidden, html)
        self.assertNotIn("coming soon", html.split('id="included-tests-heading"')[0])
        self.boto.assert_not_called()

    def test_expired_and_unentitled_students_get_no_links_and_no_access_by_guessing(self):
        enroll(self.alice, self.course, days=-1)
        self.login(self.alice)
        html = self.get("course_library", self.course.pk).content.decode()
        self.assertNotIn(self.viewer_url, html)
        self.assertEqual(self.client.get(self.viewer_url).status_code, 403)
        self.login(self.bob)
        self.assertEqual(self.client.get(self.viewer_url).status_code, 403)
        self.assertEqual(self.client.get(self.signed_url).status_code, 403)


class ClientLoadingContractTests(SimpleTestCase):
    """The D11.4 loading model, pinned by inspecting the script source (not executed here)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        source = TEMPLATE.read_text(encoding="utf-8")
        cls.js = source.split("{% verbatim %}", 1)[1].split("{% endverbatim %}", 1)[0]

    def body(self, name):
        match = re.search(rf"function {name}\([^)]*\) \{{\n(.*?)\n\}}\n", self.js, re.S)
        self.assertIsNotNone(match, name)
        return match.group(1)

    def test_fetches_the_signed_url_from_the_django_endpoint_only(self):
        self.assertIn("fetch(viewer.dataset.signedUrl", self.js)
        self.assertEqual(len(re.findall(r"\bfetch\(", self.js)), 2)  # endpoint + the one full GET

    def test_full_get_reads_the_whole_body_into_memory_after_checking_ok(self):
        body = self.body("fetchPdfBytes")
        self.assertEqual(self.js.count("arrayBuffer()"), 1)
        self.assertIn("await response.arrayBuffer()", body)
        self.assertLess(body.index("response.ok"), body.index("arrayBuffer"))
        self.assertNotIn("Range", self.js)
        self.assertNotIn(".body.getReader", self.js)

    def test_pdfjs_is_given_the_bytes_never_the_url(self):
        self.assertRegex(self.js, r"getDocument\(\{\s*data:\s*bytes\s*\}\)")
        self.assertEqual(self.js.count("getDocument("), 1)
        self.assertNotRegex(self.js, r"getDocument\(\s*(?!\{)")
        self.assertNotRegex(self.js, r"getDocument\(\{[^}]*\burl\b")

    def test_retry_is_exactly_one_fresh_signed_url_and_one_more_full_get(self):
        body = self.body("loadPdfBytes")
        self.assertEqual(len(re.findall(r"await requestSignedUrl\(\)", body)), 2)
        self.assertEqual(len(re.findall(r"await fetchPdfBytes\(", body)), 2)
        self.assertEqual(len(re.findall(r"\btry\b", body)), 1)
        self.assertEqual(len(re.findall(r"\bcatch\b", body)), 1)
        for looping in (r"\bfor\b", r"\bwhile\b", r"\bdo\b", "setTimeout", "setInterval", r"loadPdfBytes\("):
            self.assertNotRegex(body, looping)
        # The other call sites in the file cannot add attempts.
        self.assertEqual(len(re.findall(r"\bloadPdfBytes\(\)", self.js)), 2)  # definition + one use
        self.assertEqual(len(re.findall(r"\brequestSignedUrl\(\)", self.js)), 3)  # definition + two retries

    def test_a_failed_endpoint_call_is_not_retried_and_the_r2_error_is_not_parsed(self):
        body = self.body("requestSignedUrl")
        self.assertNotIn("fetchPdfBytes", body)
        self.assertNotIn("requestSignedUrl", body)
        # A non-2xx answer from R2 is never read as a body of any kind.
        fetch_body = self.body("fetchPdfBytes")
        for reading in (".json(", ".text(", ".blob(", ".formData("):
            self.assertNotIn(reading, fetch_body)

    def test_pages_are_rendered_from_the_in_memory_document(self):
        self.assertIn("pdfDoc.getPage(", self.js)
        self.assertIn("page.render(", self.js)
        self.assertIn("page.getViewport(", self.js)
        # No refresh of the signed URL after the bytes are loaded.
        for name in ("renderPage", "goToPage", "zoomBy"):
            self.assertNotIn("requestSignedUrl", self.body(name))
            self.assertNotIn("fetch(", self.body(name))

    def test_renders_never_overlap(self):
        body = self.body("renderPage")
        self.assertIn("if (rendering)", body)
        self.assertIn("renderAgain", body)
        self.assertIn("finally", body)

    def test_print_and_fullscreen_actions_use_the_standard_browser_apis(self):
        self.assertIn("window.print()", self.js)
        self.assertIn("viewer.requestFullscreen()", self.js)
        self.assertIn("document.exitFullscreen()", self.js)
        self.assertIn('typeof viewer.requestFullscreen !== "function"', self.js)

    def test_printing_is_never_intercepted_d15(self):
        for forbidden in ("beforeprint", "afterprint", "matchMedia", "keydown", "keyup", "keypress", "contextmenu", "preventDefault"):
            self.assertNotIn(forbidden, self.js, forbidden)

    def test_no_text_layer_no_direct_file_urls_and_no_url_persistence(self):
        for forbidden in (
            "TextLayer",
            "textLayer",
            "getTextContent",
            "createElement",
            "createObjectURL",
            "window.open",
            "location.",
            "innerHTML",
            "localStorage",
            "sessionStorage",
            "document.cookie",
            "console.",
            "download",
        ):
            self.assertNotIn(forbidden, self.js, forbidden)
        self.assertNotRegex(self.js, r"\.pdf\b")  # no file name; "pdfjsLib" is fine

    def test_script_is_a_static_block_with_no_django_syntax(self):
        self.assertNotIn("{{", self.js)
        self.assertNotIn("{%", self.js)


class R2HelperTests(ReaderTestCase):
    def test_object_key_is_deterministic(self):
        self.assertEqual(r2.chapter_object_key(7), "chapters/7.pdf")
        self.assertEqual(r2.chapter_object_key(7), r2.chapter_object_key(7))

    def test_each_missing_setting_fails_closed_before_any_client_is_built(self):
        for name in FAKE_R2:
            with self.subTest(missing=name), override_settings(**{name: ""}):
                with self.assertRaises(r2.R2Error) as caught:
                    r2.presign_pdf_get(OBJECT_KEY)
                self.assertNotIn(OBJECT_KEY, str(caught.exception))
                with self.assertRaises(r2.R2Error):
                    r2.upload_chapter_pdf(1, MagicMock())
        self.boto.assert_not_called()

    def test_errors_carry_only_the_exception_class_never_r2_text(self):
        self.r2.generate_presigned_url.side_effect = ClientError(
            {"Error": {"Code": "X", "Message": FAKE_R2["R2_SECRET_ACCESS_KEY"]}}, "GetObject"
        )
        with self.assertRaises(r2.R2Error) as caught:
            r2.presign_pdf_get(OBJECT_KEY)
        self.assertEqual(str(caught.exception), "presign failed: ClientError")
        self.assertIsNone(caught.exception.__cause__)

    def test_upload_targets_the_private_bucket_at_the_deterministic_key(self):
        fileobj = MagicMock()
        key = r2.upload_chapter_pdf(42, fileobj)
        self.assertEqual(key, "chapters/42.pdf")
        self.r2.upload_fileobj.assert_called_once_with(
            fileobj, "test-bucket", "chapters/42.pdf", ExtraArgs={"ContentType": "application/pdf"}
        )
        self.assertNotIn("ACL", str(self.r2.upload_fileobj.call_args))

    def test_the_r2_settings_are_exactly_the_frozen_names(self):
        for name in FAKE_R2:
            self.assertTrue(hasattr(settings, name), name)
        for invented in ("R2_REGION", "AWS_ACCESS_KEY_ID", "AWS_S3_ENDPOINT_URL", "PDF_BUCKET", "STORAGE_URL"):
            self.assertFalse(hasattr(settings, invented), invented)


class ChapterAdminUploadTests(ReaderTestCase):
    def setUp(self):
        super().setUp()
        self.staff = User.objects.create_superuser("admin", "admin@example.com", "x")
        self.client.force_login(self.staff)
        self.book = self.chapter.book
        self.uploads = []

        def capture(fileobj, bucket, key, ExtraArgs=None):
            self.uploads.append((fileobj.read(), bucket, key, ExtraArgs))

        self.r2.upload_fileobj.side_effect = capture

    def payload(self, upload=None, **extra):
        data = {"book": self.book.pk, "title": "Uploaded chapter", "order": 5}
        data.update(extra)
        if upload is not None:
            data["pdf_file"] = upload
        return data

    def pdf(self, name="notes.pdf", content=PDF_BYTES, content_type="application/pdf"):
        return SimpleUploadedFile(name, content, content_type=content_type)

    def add(self, upload=None, **extra):
        return self.client.post(reverse("admin:core_chapter_add"), self.payload(upload, **extra))

    def change(self, chapter, upload=None, **extra):
        data = self.payload(upload, **{"title": chapter.title, "order": chapter.order, **extra})
        return self.client.post(reverse("admin:core_chapter_change", args=[chapter.pk]), data)

    def test_the_upload_is_a_plain_form_field_not_a_model_field(self):
        self.assertIsInstance(ChapterAdminForm.base_fields["pdf_file"], forms.FileField)
        self.assertNotIn("pdf_file", [f.name for f in Chapter._meta.get_fields()])
        self.assertFalse(any(isinstance(f, models.FileField) for f in Chapter._meta.get_fields()))
        self.assertIsInstance(Chapter._meta.get_field("pdf_object_key"), models.CharField)

    def test_the_admin_form_has_a_file_input_and_no_editable_raw_key(self):
        html = self.client.get(reverse("admin:core_chapter_add")).content.decode()
        self.assertRegex(html, r'<input[^>]*type="file"[^>]*name="pdf_file"|<input[^>]*name="pdf_file"[^>]*type="file"')
        self.assertNotIn('name="pdf_object_key"', html)
        change_html = self.client.get(reverse("admin:core_chapter_change", args=[self.chapter.pk])).content.decode()
        self.assertNotIn('name="pdf_object_key"', change_html)
        self.assertNotIn(OBJECT_KEY, change_html)

    def test_the_book_inline_does_not_expose_the_raw_key_either(self):
        html = self.client.get(reverse("admin:core_book_change", args=[self.book.pk])).content.decode()
        self.assertNotIn("pdf_object_key", html)
        self.assertNotIn(OBJECT_KEY, html)

    def test_an_accepted_pdf_is_uploaded_server_side_and_only_its_key_is_stored(self):
        response = self.add(self.pdf())
        self.assertEqual(response.status_code, 302)
        chapter = Chapter.objects.get(title="Uploaded chapter")
        self.assertEqual(chapter.pdf_object_key, f"chapters/{chapter.pk}.pdf")
        self.assertEqual(self.uploads, [(PDF_BYTES, "test-bucket", chapter.pdf_object_key, {"ContentType": "application/pdf"})])
        self.assertNotIn("://", chapter.pdf_object_key)
        self.assertEqual(self.r2.method_calls[0][0], "upload_fileobj")
        self.assertEqual(len(self.r2.method_calls), 1)  # nothing is presigned or read for admin

    def test_replacing_a_pdf_overwrites_the_same_key(self):
        Chapter.objects.filter(pk=self.chapter.pk).update(pdf_object_key=f"chapters/{self.chapter.pk}.pdf")
        for content in (PDF_BYTES, PDF_BYTES + b"second version\n"):
            self.assertEqual(self.change(self.chapter, self.pdf(content=content)).status_code, 302)
        keys = {upload[2] for upload in self.uploads}
        self.assertEqual(keys, {f"chapters/{self.chapter.pk}.pdf"})
        self.assertEqual(len(self.uploads), 2)
        self.assertEqual(self.uploads[1][0], PDF_BYTES + b"second version\n")
        self.chapter.refresh_from_db()
        self.assertEqual(self.chapter.pdf_object_key, f"chapters/{self.chapter.pk}.pdf")
        self.assertEqual(Chapter.objects.filter(title=self.chapter.title).count(), 1)

    def test_saving_without_a_file_uploads_nothing_and_keeps_the_key(self):
        self.assertEqual(self.change(self.chapter).status_code, 302)
        self.boto.assert_not_called()
        self.chapter.refresh_from_db()
        self.assertEqual(self.chapter.pdf_object_key, OBJECT_KEY)

    def assert_rejected(self, response, message):
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, message)
        self.assertEqual(self.uploads, [])
        self.boto.assert_not_called()
        self.assertFalse(Chapter.objects.filter(title="Uploaded chapter").exists())

    def test_wrong_extension_is_rejected(self):
        self.assert_rejected(self.add(self.pdf(name="notes.txt")), "must have a .pdf extension")

    def test_wrong_content_type_is_rejected(self):
        self.assert_rejected(self.add(self.pdf(content_type="text/plain")), "must be a PDF")

    def test_content_that_is_not_a_pdf_is_rejected_despite_name_and_type(self):
        self.assert_rejected(self.add(self.pdf(content=b"<html>not a pdf</html>")), "does not look like a valid PDF")

    def test_empty_file_is_rejected(self):
        self.assert_rejected(self.add(self.pdf(content=b"")), "empty")

    def test_oversized_file_is_rejected(self):
        with patch("core.forms.MAX_CHAPTER_PDF_BYTES", len(PDF_BYTES) - 1):
            self.assert_rejected(self.add(self.pdf()), "too large")

    def test_a_file_at_the_ceiling_is_accepted(self):
        with patch("core.forms.MAX_CHAPTER_PDF_BYTES", len(PDF_BYTES)):
            self.assertEqual(self.add(self.pdf()).status_code, 302)
        self.assertEqual(len(self.uploads), 1)

    def test_the_ceiling_is_one_central_constant_of_50_mib(self):
        self.assertEqual(MAX_CHAPTER_PDF_BYTES, 50 * 1024 * 1024)

    def test_an_upload_failure_saves_nothing_and_never_reports_success(self):
        self.r2.upload_fileobj.side_effect = ClientError({"Error": {"Code": "X", "Message": "m"}}, "PutObject")
        before = Chapter.objects.count()
        with self.assertLogs("core.admin", level="ERROR") as logs:
            response = self.add(self.pdf(), title="Uploaded chapter")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("admin:core_chapter_add"))
        self.assertEqual(Chapter.objects.count(), before)
        page = self.client.get(response["Location"])
        shown = [str(m) for m in page.context["messages"]]
        self.assertEqual(len(shown), 1)
        self.assertIn("nothing was saved", shown[0])
        self.assertNotIn("successfully", " ".join(shown))
        self.assertNotIn("test-secret-access-key", "\n".join(logs.output))

    def test_a_failed_replacement_leaves_the_existing_chapter_untouched(self):
        self.r2.upload_fileobj.side_effect = NoCredentialsError()
        with self.assertLogs("core.admin", level="ERROR"):
            response = self.change(self.chapter, self.pdf(), title="Renamed")
        self.assertEqual(response.status_code, 302)
        self.chapter.refresh_from_db()
        self.assertNotEqual(self.chapter.title, "Renamed")
        self.assertEqual(self.chapter.pdf_object_key, OBJECT_KEY)

    def test_unconfigured_r2_fails_the_upload_without_a_client_or_a_saved_row(self):
        with override_settings(R2_BUCKET_NAME=""), self.assertLogs("core.admin", level="ERROR"):
            response = self.add(self.pdf())
        self.assertEqual(response.status_code, 302)
        self.boto.assert_not_called()
        self.assertFalse(Chapter.objects.filter(title="Uploaded chapter").exists())

    def test_only_admin_staff_can_use_the_upload_path(self):
        self.client.force_login(self.alice)  # a student: not is_staff
        response = self.add(self.pdf())
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])
        self.boto.assert_not_called()
        self.assertFalse(Chapter.objects.filter(title="Uploaded chapter").exists())
