from allauth.account.signals import password_changed, password_set
from django.contrib.auth.signals import user_logged_in
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import UserProfile


@receiver(post_save, sender="auth.User")
def create_user_profile(sender, instance, created, raw=False, **kwargs):
    if created and not raw:
        UserProfile.objects.get_or_create(user=instance)


def record_active_session(request, user):
    if not request.session.session_key:
        request.session.save()
    UserProfile.objects.update_or_create(
        user=user, defaults={"active_session_key": request.session.session_key}
    )


@receiver(user_logged_in)
def record_session_on_login(sender, request, user, **kwargs):
    record_active_session(request, user)


# allauth's password change/set flows call update_session_auth_hash(), which
# rotates the session key without a new login. Re-record it so the session that
# just changed its own password is not displaced. Both signals are emitted only
# from authenticated views, so the session is already the authoritative one.
@receiver(password_changed)
@receiver(password_set)
def record_session_on_password_change(sender, request, user, **kwargs):
    record_active_session(request, user)
