from django.contrib.auth import logout
from django.http import HttpResponseRedirect
from django.urls import reverse

from .models import UserProfile


class SingleActiveSessionMiddleware:
    """REQ-ACC-02/03: UserProfile.active_session_key is the sole authority.

    A request whose session key differs from the stored key belongs to a
    session superseded by a newer login (local or Google) and is displaced.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            profile, _ = UserProfile.objects.get_or_create(user_id=request.user.pk)
            session_key = request.session.session_key
            if not session_key or profile.active_session_key != session_key:
                request.session.flush()
                logout(request)
                return HttpResponseRedirect(
                    f"{reverse('account_login')}?error=session_conflict"
                )
        return self.get_response(request)
