from urllib.parse import urlsplit

from django.conf import settings


def support_link(request):
    """REQ-WEB-02: expose the configured Support/Chat URL, https only.

    An unset, non-https, host-less, or whitespace-containing value yields
    None so templates render no link rather than a placeholder destination.
    """
    url = settings.WHATSAPP_SUPPORT_URL
    parts = urlsplit(url)
    safe = (
        parts.scheme == "https"
        and bool(parts.hostname)
        and not any(ch.isspace() for ch in url)
    )
    return {"support_url": url if safe else None}
