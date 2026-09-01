"""OAuth ``state`` and redirect-URI plumbing.

The callback is a browser redirect target, so it cannot carry a JWT and must be
``AllowAny``. **``state`` is the only thing authenticating it.** Everything in
this module exists to make that one parameter trustworthy.

It is a signed, timestamped payload — ``TimestampSigner`` over compact JSON —
rather than a random string in a table. Signing gives us three properties at
once with no storage and no session (this API is stateless by design):

* it names the user, so the callback knows whose account to attach without
  trusting anything the browser sent;
* it cannot be forged without ``SECRET_KEY``, which is what stops an attacker
  from grafting their own social account onto someone else's login;
* it expires on its own, so an authorize URL left in a tab overnight is dead.

The trade-off against a database row is that a state cannot be marked used, so a
replay inside the TTL is possible. That is acceptable here: replaying it only
re-runs the same connect for the same user, and the authorization code itself is
single-use at the network.
"""

import json

from django.conf import settings
from django.core import signing
from django.urls import reverse

SALT = "social.oauth.state"

#: How long an authorize URL stays valid. Long enough to log into the network
#: and read a consent screen, short enough that a stale tab is not a key.
STATE_MAX_AGE = 600  # seconds

_signer = signing.TimestampSigner(salt=SALT)


class InvalidState(Exception):
    """The state parameter was missing, tampered with, or too old."""


def make_state(user_id: int, provider: str, return_to: str = "") -> str:
    """Sign the facts the callback will need back."""
    payload = json.dumps(
        {"u": int(user_id), "p": provider, "r": return_to or ""},
        separators=(",", ":"),
    )
    return _signer.sign(signing.b64_encode(payload.encode()).decode())


def read_state(state: str, max_age: int = STATE_MAX_AGE) -> dict:
    """Verify and unpack a state parameter.

    Returns ``{"user_id", "provider", "return_to"}``. Raises :class:`InvalidState`
    for anything that isn't a signature we produced within ``max_age``.
    """
    if not state:
        raise InvalidState("Missing state parameter.")
    try:
        raw = _signer.unsign(state, max_age=max_age)
        payload = json.loads(signing.b64_decode(raw.encode()).decode())
    except signing.SignatureExpired as exc:
        raise InvalidState(
            "This sign-in link expired. Start the connection again."
        ) from exc
    except (signing.BadSignature, ValueError, TypeError) as exc:
        raise InvalidState("The sign-in link was invalid.") from exc

    return {
        "user_id": payload.get("u"),
        "provider": payload.get("p", ""),
        "return_to": payload.get("r", ""),
    }


def callback_url(request, provider: str) -> str:
    """The redirect URI for a provider — must match the network's console exactly.

    Prefers ``SOCIAL_OAUTH_REDIRECT_BASE`` over the request's own host because
    the value registered with LinkedIn and Meta is fixed, while the incoming
    host is not: behind a proxy, on an alternate domain, or over plain HTTP in
    dev, deriving it from the request produces a URI the network rejects.
    """
    path = reverse("social:callback", kwargs={"provider": provider})
    base = (getattr(settings, "SOCIAL_OAUTH_REDIRECT_BASE", "") or "").rstrip("/")
    if base:
        return f"{base}{path}"
    return request.build_absolute_uri(path)


#: Where the trainer lands after the round-trip, when the caller didn't say.
DEFAULT_RETURN_TO = "/trainer/settings/social"


def frontend_origin() -> str:
    """The scheme and host of the web app, with any path stripped.

    ``FRONTEND_URL`` is documented as the *student* app and on this deployment
    carries a path (``https://lms.jigyaasaa.com/student``). The trainer panel
    lives at the host root, so concatenating onto that value would land every
    OAuth round-trip on ``/student/trainer/settings/social`` — a 404 at the very
    end of a flow that otherwise worked. Take the origin and nothing else.
    """
    from urllib.parse import urlsplit

    parts = urlsplit(settings.FRONTEND_URL)
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return settings.FRONTEND_URL.rstrip("/")


def frontend_redirect(return_to: str, **params) -> str:
    """Build the frontend URL to bounce back to, with an outcome in the query.

    ``return_to`` is echoed from state, which the browser could have influenced
    at connect time, so only a site-relative path is honoured — anything
    absolute or protocol-relative falls back to the default. Without that check
    this is an open redirect wearing the platform's own domain.
    """
    from urllib.parse import urlencode

    if (
        not return_to
        or not return_to.startswith("/")
        or return_to.startswith("//")
    ):
        return_to = DEFAULT_RETURN_TO

    base = frontend_origin()
    query = urlencode({k: v for k, v in params.items() if v})
    return f"{base}{return_to}?{query}" if query else f"{base}{return_to}"
