"""Phase 3: authentication, UserProfile lifecycle, single active session.

REQ-ACC-01..04, DECISIONS.md D14 and D17. Google is exercised through
allauth's real initiation -> state -> callback flow; only outbound HTTP
(`requests.Session.request`) is stubbed, so no test touches the network.
"""

import copy
import json
import re
from contextlib import contextmanager
from unittest import mock
from urllib.parse import parse_qs, urlparse

from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount, SocialToken
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from core.models import UserProfile

User = get_user_model()

CONFLICT_URL = "/accounts/login/?error=session_conflict"
PASSWORD = "correct-horse-battery-staple-9"
NEW_PASSWORD = "another-long-passphrase-42"

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"


def google_providers(client_id="test-client-id", secret="test-client-secret"):
    providers = copy.deepcopy(settings.SOCIALACCOUNT_PROVIDERS)
    providers["google"]["APP"] = {"client_id": client_id, "secret": secret, "key": ""}
    return providers


def make_local_user(username="student", email="student@example.com", verified=True):
    user = User.objects.create_user(username=username, email=email, password=PASSWORD)
    EmailAddress.objects.create(user=user, email=email, primary=True, verified=verified)
    return user


def local_login(client, username="student", password=PASSWORD):
    return client.post(
        reverse("account_login"), {"login": username, "password": password}
    )


def is_authenticated(client):
    # /accounts/email/ is an allauth login-required view.
    return client.get(reverse("account_email")).status_code == 200


def session_key(client):
    return client.cookies[settings.SESSION_COOKIE_NAME].value


def authenticated_user_id(client):
    return client.session.get("_auth_user_id")


def link_from_outbox(path_fragment):
    for message in mail.outbox:
        for path in re.findall(r"http://testserver(/\S+)", message.body):
            if path_fragment in path:
                return path
    raise AssertionError(f"no link containing {path_fragment!r} in mail.outbox")


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.headers = {"content-type": "application/json"}
        self.text = json.dumps(payload)
        self.content = self.text.encode()

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._payload


@contextmanager
def stubbed_google(*, sub, email, verified, token_status=200):
    userinfo = {
        "id": sub,
        "email": email,
        "verified_email": verified,
        "name": "Test Student",
        "given_name": "Test",
        "family_name": "Student",
    }

    def fake_request(method, url, **kwargs):
        if url == GOOGLE_TOKEN_URL:
            data = kwargs.get("data") or {}
            if token_status != 200 or not data.get("client_id") or not data.get(
                "client_secret"
            ):
                status = token_status if token_status != 200 else 401
                return FakeResponse(status, {"error": "invalid_client"})
            return FakeResponse(
                200,
                {"access_token": "stub-access", "token_type": "Bearer", "expires_in": 3599},
            )
        if url == GOOGLE_USERINFO_URL:
            return FakeResponse(200, userinfo)
        raise AssertionError(f"unexpected outbound HTTP request: {method} {url}")

    with mock.patch("requests.sessions.Session.request", side_effect=fake_request) as stub:
        yield stub


def start_google_login(client):
    return client.post(reverse("google_login"), {"process": "login"})


def state_from(initiation_response):
    return parse_qs(urlparse(initiation_response["Location"]).query)["state"][0]


def google_login(client, *, sub, email, verified=True, **stub_kwargs):
    """Run initiation + callback; return the callback response."""
    with stubbed_google(sub=sub, email=email, verified=verified, **stub_kwargs):
        initiation = start_google_login(client)
        assert initiation.status_code == 302, initiation.status_code
        assert initiation["Location"].startswith(GOOGLE_AUTHORIZE_URL)
        return client.get(
            reverse("google_callback"),
            {"code": "stub-code", "state": state_from(initiation)},
        )


class AuthTestCase(TestCase):
    """allauth rate limits (login, confirm_email) live in the shared cache."""

    def setUp(self):
        super().setUp()
        cache.clear()


FAST_HASHER = override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"]
)


@FAST_HASHER
class LocalAuthTests(AuthTestCase):
    signup_data = {
        "username": "newstudent",
        "email": "new@example.com",
        "password1": PASSWORD,
        "password2": PASSWORD,
    }

    def test_local_signup_works_and_requires_email_verification(self):
        response = self.client.post(reverse("account_signup"), self.signup_data)
        self.assertRedirects(
            response,
            reverse("account_email_verification_sent"),
            fetch_redirect_response=False,
        )
        user = User.objects.get(username="newstudent")
        self.assertFalse(EmailAddress.objects.get(user=user).verified)
        self.assertEqual(len(mail.outbox), 1)
        self.assertFalse(is_authenticated(self.client))

    def test_unverified_local_user_cannot_authenticate(self):
        self.client.post(reverse("account_signup"), self.signup_data)
        response = local_login(self.client, "newstudent")
        self.assertRedirects(
            response,
            reverse("account_email_verification_sent"),
            fetch_redirect_response=False,
        )
        self.assertFalse(is_authenticated(self.client))
        profile = UserProfile.objects.get(user__username="newstudent")
        self.assertIsNone(profile.active_session_key)

    def test_email_confirmation_then_login_works(self):
        self.client.post(reverse("account_signup"), self.signup_data)
        link = link_from_outbox("/accounts/confirm-email/")
        self.assertEqual(self.client.get(link).status_code, 200)
        self.assertEqual(self.client.post(link).status_code, 302)
        self.assertTrue(
            EmailAddress.objects.get(user__username="newstudent").verified
        )
        self.assertEqual(local_login(self.client, "newstudent").status_code, 302)
        self.assertTrue(is_authenticated(self.client))

    def test_verified_local_user_can_authenticate(self):
        make_local_user()
        self.assertEqual(local_login(self.client).status_code, 302)
        self.assertTrue(is_authenticated(self.client))

    def test_wrong_password_does_not_authenticate(self):
        user = make_local_user()
        response = local_login(self.client, password="not-the-password")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(is_authenticated(self.client))
        self.assertIsNone(UserProfile.objects.get(user=user).active_session_key)

    def test_local_logout_works(self):
        make_local_user()
        local_login(self.client)
        self.assertTrue(is_authenticated(self.client))
        response = self.client.post(reverse("account_logout"))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(is_authenticated(self.client))

    def test_password_reset_remains_functional(self):
        make_local_user()
        response = self.client.post(
            reverse("account_reset_password"), {"email": "student@example.com"}
        )
        self.assertRedirects(
            response, reverse("account_reset_password_done"), fetch_redirect_response=False
        )
        link = link_from_outbox("/accounts/password/reset/key/")
        landing = self.client.get(link, follow=True)
        set_password_url = landing.redirect_chain[-1][0]
        done = self.client.post(
            set_password_url, {"password1": NEW_PASSWORD, "password2": NEW_PASSWORD}
        )
        self.assertEqual(done.status_code, 302)
        self.assertEqual(local_login(self.client, password=PASSWORD).status_code, 200)
        self.assertFalse(is_authenticated(self.client))
        self.assertEqual(local_login(self.client, password=NEW_PASSWORD).status_code, 302)
        self.assertTrue(is_authenticated(self.client))

    def test_login_signup_pages_offer_local_form_and_csrf_post_for_google(self):
        for name in ("account_login", "account_signup"):
            with self.subTest(page=name):
                html = self.client.get(reverse(name)).content.decode()
                google_action = f'action="{reverse("google_login")}?process=login"'
                self.assertIn(google_action, html)
                self.assertIn("Continue with Google", html)
                self.assertNotIn(f'href="{reverse("google_login")}', html)
                self.assertGreaterEqual(html.count("csrfmiddlewaretoken"), 2)
                self.assertIn(f'action="{reverse(name)}"', html)

    def test_login_page_explains_session_conflict(self):
        plain = self.client.get(reverse("account_login")).content.decode()
        self.assertNotIn("session-conflict-notice", plain)
        html = self.client.get(CONFLICT_URL).content.decode()
        self.assertIn("session-conflict-notice", html)
        self.assertIn("another device or browser", html)

    def test_no_login_alias_route_exists(self):
        self.assertEqual(self.client.get("/login/").status_code, 404)


class UserProfileLifecycleTests(AuthTestCase):
    def test_created_user_gets_exactly_one_profile(self):
        user = User.objects.create_user("plain", "plain@example.com", "pw-not-used-1")
        self.assertEqual(UserProfile.objects.filter(user=user).count(), 1)

    def test_resaving_user_does_not_duplicate_profile(self):
        user = User.objects.create_user("plain", "plain@example.com", "pw-not-used-1")
        user.first_name = "Changed"
        user.save()
        user.save()
        self.assertEqual(UserProfile.objects.filter(user=user).count(), 1)

    def test_superuser_creation_gets_a_profile(self):
        admin = User.objects.create_superuser("root", "root@example.com", "pw-not-used-1")
        self.assertEqual(UserProfile.objects.filter(user=admin).count(), 1)

    def test_profile_is_not_recreated_or_reset_on_later_saves(self):
        user = User.objects.create_user("plain", "plain@example.com", "pw-not-used-1")
        UserProfile.objects.filter(user=user).update(active_session_key="keep-me")
        user.save()
        self.assertEqual(UserProfile.objects.get(user=user).active_session_key, "keep-me")

    @FAST_HASHER
    def test_existing_user_without_profile_gets_one_at_login(self):
        user = make_local_user()
        UserProfile.objects.filter(user=user).delete()
        self.assertEqual(local_login(self.client).status_code, 302)
        profile = UserProfile.objects.get(user=user)
        self.assertEqual(profile.active_session_key, session_key(self.client))
        self.assertTrue(is_authenticated(self.client))

    @FAST_HASHER
    def test_authenticated_user_missing_profile_is_handled_safely(self):
        user = make_local_user()
        local_login(self.client)
        UserProfile.objects.filter(user=user).delete()
        response = self.client.get(reverse("account_email"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], CONFLICT_URL)
        self.assertEqual(UserProfile.objects.filter(user=user).count(), 1)
        self.assertFalse(is_authenticated(self.client))


@FAST_HASHER
class SingleActiveSessionTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_local_user()
        self.first = Client()
        self.second = Client()

    def test_first_login_records_its_session_key(self):
        local_login(self.first)
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.active_session_key, session_key(self.first))

    def test_second_login_overwrites_active_session_key(self):
        local_login(self.first)
        local_login(self.second)
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.active_session_key, session_key(self.second))
        self.assertNotEqual(session_key(self.first), session_key(self.second))

    def test_first_browser_is_displaced_to_canonical_conflict_url(self):
        local_login(self.first)
        local_login(self.second)
        response = self.first.get(reverse("account_email"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], CONFLICT_URL)
        self.assertEqual(reverse("account_login") + "?error=session_conflict", CONFLICT_URL)

    def test_displaced_request_does_not_reach_the_protected_view(self):
        local_login(self.first)
        local_login(self.second)
        with mock.patch("allauth.account.views.EmailView.dispatch") as view:
            response = self.first.get(reverse("account_email"))
        view.assert_not_called()
        self.assertEqual(response["Location"], CONFLICT_URL)

    def test_displaced_browser_becomes_unauthenticated(self):
        local_login(self.first)
        local_login(self.second)
        self.first.get(reverse("account_email"))
        follow_up = self.first.get(reverse("account_email"))
        self.assertEqual(follow_up.status_code, 302)
        self.assertTrue(follow_up["Location"].startswith(reverse("account_login")))
        self.assertNotIn("session_conflict", follow_up["Location"])
        self.assertIsNone(authenticated_user_id(self.first))

    def test_conflict_redirect_lands_on_login_page_with_notice(self):
        local_login(self.first)
        local_login(self.second)
        response = self.first.get(reverse("account_email"), follow=True)
        self.assertEqual(response.redirect_chain[-1][0], CONFLICT_URL)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "another device or browser")

    def test_newest_browser_remains_authenticated(self):
        local_login(self.first)
        local_login(self.second)
        self.first.get(reverse("account_email"))
        self.assertTrue(is_authenticated(self.second))
        self.assertTrue(is_authenticated(self.second))

    def test_two_different_users_do_not_invalidate_each_other(self):
        make_local_user("other", "other@example.com")
        local_login(self.first, "student")
        local_login(self.second, "other")
        self.assertTrue(is_authenticated(self.first))
        self.assertTrue(is_authenticated(self.second))
        third = Client()
        local_login(third, "student")
        self.assertFalse(is_authenticated(self.first))
        self.assertTrue(is_authenticated(self.second))
        self.assertTrue(is_authenticated(third))

    def test_stale_session_logout_cannot_erase_newer_key(self):
        local_login(self.first)
        local_login(self.second)
        newest = session_key(self.second)
        self.first.post(reverse("account_logout"))
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.active_session_key, newest)
        self.assertTrue(is_authenticated(self.second))

    def test_anonymous_requests_are_untouched(self):
        response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 200)

    def test_changing_own_password_does_not_displace_the_session(self):
        local_login(self.first)
        response = self.first.post(
            reverse("account_change_password"),
            {
                "oldpassword": PASSWORD,
                "password1": NEW_PASSWORD,
                "password2": NEW_PASSWORD,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(is_authenticated(self.first))
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.active_session_key, session_key(self.first))

    def test_middleware_runs_after_authentication_middleware(self):
        middleware = settings.MIDDLEWARE
        self.assertGreater(
            middleware.index("core.middleware.SingleActiveSessionMiddleware"),
            middleware.index("django.contrib.auth.middleware.AuthenticationMiddleware"),
        )


@FAST_HASHER
@override_settings(SOCIALACCOUNT_PROVIDERS=google_providers())
class GoogleAuthTests(AuthTestCase):
    def test_get_does_not_initiate_oauth(self):
        with stubbed_google(sub="x", email="x@example.com", verified=True) as stub:
            response = self.client.get(reverse("google_login"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Location", response)
        self.assertIn('method="post"', response.content.decode().lower())
        stub.assert_not_called()

    def test_post_initiation_redirects_to_google_with_frozen_parameters(self):
        response = start_google_login(self.client)
        self.assertEqual(response.status_code, 302)
        target = urlparse(response["Location"])
        self.assertEqual(f"{target.scheme}://{target.netloc}{target.path}", GOOGLE_AUTHORIZE_URL)
        params = parse_qs(target.query)
        self.assertEqual(params["client_id"], ["test-client-id"])
        self.assertEqual(set(params["scope"][0].split()), {"profile", "email"})
        self.assertEqual(params["access_type"], ["online"])
        self.assertEqual(params["code_challenge_method"], ["S256"])
        self.assertIn("code_challenge", params)
        self.assertTrue(params["redirect_uri"][0].endswith("/accounts/google/login/callback/"))

    def test_post_initiation_is_csrf_protected(self):
        strict = Client(enforce_csrf_checks=True)
        response = strict.post(reverse("google_login"), {"process": "login"})
        self.assertEqual(response.status_code, 403)

    def test_case_a_verified_email_authenticates_existing_user_without_duplicate(self):
        user = make_local_user()
        response = google_login(self.client, sub="g-1", email="student@example.com")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(is_authenticated(self.client))
        self.assertEqual(authenticated_user_id(self.client), str(user.pk))
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(UserProfile.objects.filter(user=user).count(), 1)
        self.assertEqual(SocialAccount.objects.count(), 0)
        self.assertEqual(SocialToken.objects.count(), 0)

    def test_case_b_new_google_signup_creates_user_profile_and_socialaccount(self):
        response = google_login(self.client, sub="g-new", email="new@example.com")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(is_authenticated(self.client))
        user = User.objects.get(email="new@example.com")
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(UserProfile.objects.filter(user=user).count(), 1)
        account = SocialAccount.objects.get()
        self.assertEqual((account.user, account.provider, account.uid), (user, "google", "g-new"))
        self.assertEqual(SocialToken.objects.count(), 0)

    def test_google_login_records_the_session_key(self):
        google_login(self.client, sub="g-new", email="new@example.com")
        profile = UserProfile.objects.get(user__email="new@example.com")
        self.assertEqual(profile.active_session_key, session_key(self.client))

    def test_unverified_google_email_never_authenticates_or_links_existing_user(self):
        existing = make_local_user()
        google_login(
            self.client, sub="g-attacker", email="student@example.com", verified=False
        )
        self.assertFalse(is_authenticated(self.client))
        self.assertIsNone(authenticated_user_id(self.client))
        self.assertFalse(SocialAccount.objects.filter(user=existing).exists())
        existing.profile.refresh_from_db()
        self.assertIsNone(existing.profile.active_session_key)

    def test_unverified_google_email_for_new_identity_is_not_authenticated(self):
        google_login(self.client, sub="g-unverified", email="fresh@example.com", verified=False)
        self.assertFalse(is_authenticated(self.client))

    def test_local_login_remains_functional_with_google_enabled(self):
        make_local_user()
        self.assertEqual(local_login(self.client).status_code, 302)
        self.assertTrue(is_authenticated(self.client))

    def test_cancelled_consent_does_not_authenticate_or_create_rows(self):
        with stubbed_google(sub="x", email="x@example.com", verified=True) as stub:
            state = state_from(start_google_login(self.client))
            response = self.client.get(
                reverse("google_callback"), {"error": "access_denied", "state": state}
            )
        self.assertRedirects(
            response, reverse("socialaccount_login_cancelled"), fetch_redirect_response=False
        )
        stub.assert_not_called()
        self.assertFalse(is_authenticated(self.client))
        self.assertEqual(
            (User.objects.count(), UserProfile.objects.count(), SocialAccount.objects.count()),
            (0, 0, 0),
        )

    def test_provider_error_does_not_authenticate_and_local_login_still_works(self):
        make_local_user()
        google_login(
            self.client, sub="g-1", email="student@example.com", token_status=500
        )
        self.assertFalse(is_authenticated(self.client))
        self.assertEqual(SocialAccount.objects.count(), 0)
        self.assertEqual(local_login(self.client).status_code, 302)
        self.assertTrue(is_authenticated(self.client))

    def test_invalid_oauth_state_is_rejected_before_token_exchange(self):
        with stubbed_google(sub="g-1", email="new@example.com", verified=True) as stub:
            start_google_login(self.client)
            response = self.client.get(
                reverse("google_callback"), {"code": "stub-code", "state": "tampered"}
            )
        self.assertNotEqual(response.status_code, 302)
        stub.assert_not_called()
        self.assertFalse(is_authenticated(self.client))
        self.assertEqual((User.objects.count(), SocialAccount.objects.count()), (0, 0))

    @override_settings(SOCIALACCOUNT_PROVIDERS=google_providers(client_id="", secret=""))
    def test_missing_credentials_fail_closed(self):
        google_login(self.client, sub="g-1", email="new@example.com")
        self.assertFalse(is_authenticated(self.client))
        self.assertEqual((User.objects.count(), SocialAccount.objects.count()), (0, 0))

    def test_repeated_google_login_creates_no_duplicates_and_displaces_previous(self):
        clients = [Client(), Client(), Client()]
        for client in clients:
            response = google_login(client, sub="g-repeat", email="repeat@example.com")
            self.assertEqual(response.status_code, 302)
            self.assertEqual((User.objects.count(), SocialAccount.objects.count()), (1, 1))
            self.assertEqual(UserProfile.objects.count(), 1)
        self.assertEqual(SocialToken.objects.count(), 0)
        self.assertFalse(is_authenticated(clients[0]))
        self.assertFalse(is_authenticated(clients[1]))
        self.assertTrue(is_authenticated(clients[2]))


@FAST_HASHER
@override_settings(SOCIALACCOUNT_PROVIDERS=google_providers())
class CrossAuthSingleSessionTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_local_user()
        self.first = Client()
        self.second = Client()

    def test_local_then_google_invalidates_local_session(self):
        local_login(self.first)
        google_login(self.second, sub="g-1", email="student@example.com")
        self.assertEqual(authenticated_user_id(self.second), str(self.user.pk))
        self.assertFalse(is_authenticated(self.first))
        self.assertTrue(is_authenticated(self.second))

    def test_google_then_local_invalidates_google_session(self):
        google_login(self.first, sub="g-1", email="student@example.com")
        local_login(self.second)
        self.assertFalse(is_authenticated(self.first))
        self.assertTrue(is_authenticated(self.second))

    def test_google_then_google_invalidates_first_google_session(self):
        google_login(self.first, sub="g-1", email="student@example.com")
        google_login(self.second, sub="g-1", email="student@example.com")
        self.assertFalse(is_authenticated(self.first))
        self.assertTrue(is_authenticated(self.second))
        self.assertEqual(User.objects.count(), 1)

    def test_displaced_google_session_gets_the_canonical_redirect(self):
        google_login(self.first, sub="g-1", email="student@example.com")
        local_login(self.second)
        response = self.first.get(reverse("account_email"))
        self.assertEqual(response["Location"], CONFLICT_URL)
