import importlib.util

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import User as StockUser
from django.test import TestCase
from django.urls import resolve, reverse


class DatabaseConfigTests(TestCase):
    def test_default_engine_is_postgresql(self):
        self.assertEqual(
            settings.DATABASES["default"]["ENGINE"],
            "django.db.backends.postgresql",
        )

    def test_no_sqlite_fallback(self):
        for alias, config in settings.DATABASES.items():
            self.assertNotIn("sqlite3", config["ENGINE"], f"alias={alias}")

    def test_conn_max_age_is_zero(self):
        self.assertEqual(settings.DATABASES["default"]["CONN_MAX_AGE"], 0)

    def test_connect_timeout_is_15(self):
        self.assertEqual(
            settings.DATABASES["default"]["OPTIONS"]["connect_timeout"], 15
        )


class StockUserModelTests(TestCase):
    def test_auth_user_model_is_stock(self):
        self.assertEqual(settings.AUTH_USER_MODEL, "auth.User")

    def test_get_user_model_is_stock_user(self):
        self.assertIs(get_user_model(), StockUser)


class AllauthInstalledAppsTests(TestCase):
    def test_allauth_apps_installed(self):
        for app in (
            "allauth",
            "allauth.account",
            "allauth.socialaccount",
            "allauth.socialaccount.providers.google",
        ):
            self.assertTrue(apps.is_installed(app), f"{app} not installed")

    def test_no_other_social_provider_installed(self):
        provider_apps = [
            app
            for app in settings.INSTALLED_APPS
            if app.startswith("allauth.socialaccount.providers.")
        ]
        self.assertEqual(
            provider_apps, ["allauth.socialaccount.providers.google"]
        )


class AccountPolicyTests(TestCase):
    def test_login_methods_is_username_only(self):
        self.assertEqual(settings.ACCOUNT_LOGIN_METHODS, {"username"})

    def test_signup_fields_present(self):
        required = {"username*", "email*", "password1*", "password2*"}
        self.assertTrue(required.issubset(set(settings.ACCOUNT_SIGNUP_FIELDS)))

    def test_email_verification_mandatory(self):
        self.assertEqual(settings.ACCOUNT_EMAIL_VERIFICATION, "mandatory")


class GoogleProviderSettingsTests(TestCase):
    def test_login_on_get_is_false(self):
        self.assertFalse(settings.SOCIALACCOUNT_LOGIN_ON_GET)

    def test_store_tokens_is_false(self):
        self.assertFalse(settings.SOCIALACCOUNT_STORE_TOKENS)

    def test_auto_connect_is_false(self):
        self.assertFalse(settings.SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT)

    def test_no_global_email_authentication_setting(self):
        self.assertFalse(getattr(settings, "SOCIALACCOUNT_EMAIL_AUTHENTICATION", False))

    def test_google_email_authentication_is_true(self):
        self.assertTrue(
            settings.SOCIALACCOUNT_PROVIDERS["google"]["EMAIL_AUTHENTICATION"]
        )

    def test_google_scopes_are_profile_and_email(self):
        self.assertEqual(
            settings.SOCIALACCOUNT_PROVIDERS["google"]["SCOPE"],
            ["profile", "email"],
        )

    def test_google_access_type_is_online(self):
        self.assertEqual(
            settings.SOCIALACCOUNT_PROVIDERS["google"]["AUTH_PARAMS"]["access_type"],
            "online",
        )

    def test_google_pkce_enabled(self):
        self.assertTrue(
            settings.SOCIALACCOUNT_PROVIDERS["google"]["OAUTH_PKCE_ENABLED"]
        )


class RouteResolutionTests(TestCase):
    def test_local_login_url_resolves(self):
        url = reverse("account_login")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_signup_url_resolves(self):
        url = reverse("account_signup")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_password_reset_url_resolves(self):
        url = reverse("account_reset_password")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_google_login_initiation_url_resolves(self):
        url = reverse("google_login")
        match = resolve(url)
        self.assertIsNotNone(match)

    def test_google_callback_url_resolves(self):
        url = reverse("google_callback")
        match = resolve(url)
        self.assertIsNotNone(match)


class HomeTemplateTests(TestCase):
    def test_anonymous_home_links_to_auth_pages_without_google_action(self):
        # SECURITY.md §9: the anonymous home is public/edge-cacheable, so it
        # carries no CSRF form. "Continue with Google" (REQ-ACC-04/REQ-WEB-07)
        # lives on the login/signup pages only (POST-only, see test_auth.py);
        # home just links to those named routes.
        response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn(f'href="{reverse("account_login")}"', content)
        self.assertIn(f'href="{reverse("account_signup")}"', content)
        self.assertNotIn(reverse("google_login"), content)
        self.assertNotIn("<form", content)
        self.assertNotIn("csrfmiddlewaretoken", content)


class DevelopmentEmailBackendTests(TestCase):
    def test_console_backend_when_debug(self):
        # Django's test runner overrides settings.DEBUG (to False) and
        # settings.EMAIL_BACKEND (to locmem) for the whole test run via
        # setup_test_environment(), so the live django.conf.settings object
        # can no longer show what config/settings.py actually computed.
        # Re-execute the settings module directly, independent of Django's
        # settings singleton, against the same environment (DJANGO_DEBUG is
        # still set for this process), to verify the real branch taken.
        settings_path = settings.BASE_DIR / "config" / "settings.py"
        spec = importlib.util.spec_from_file_location(
            "_settings_under_test", settings_path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(module.DEBUG)
        self.assertEqual(
            module.EMAIL_BACKEND,
            "django.core.mail.backends.console.EmailBackend",
        )
