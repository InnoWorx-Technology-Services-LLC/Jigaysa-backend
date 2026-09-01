"""The contract every network adapter implements, plus shared HTTP helpers.

Isolates network-specific concerns — endpoints, scope strings, token exchange,
the shape of a profile response — so views and serializers stay about
connections. Mirrors ``payments.gateway``: typed errors, an ``is_configured()``
that makes a missing credential a clean 503 rather than a crash, and no SDK.

An adapter is a **module**, not a class. There is exactly one of each and they
hold no state, so a class would only add ceremony.

Adapters expose::

    KEY               str                  provider key, or the primary one
    PROVIDES          tuple[str, ...]      every provider key this flow yields
    SCOPES            tuple[str, ...]
    is_configured()   -> bool
    authorize_url(state, redirect_uri)     -> str
    exchange_code(code, redirect_uri)      -> list[ConnectedAccount]
    publish(account, PostContent)          -> PublishedPost
    revoke(account)                        -> None   (best effort)

``exchange_code`` returns a **list** because one authorization does not always
mean one destination: a single Meta consent yields every Page the trainer
admins plus the Instagram account linked to each.
"""

from dataclasses import dataclass, field

import requests

#: Every outbound call is capped. A network that hangs must not hold a request
#: thread — or, in the publisher, stall the whole cron run behind one account.
TIMEOUT = 15

#: Ceiling on an image pulled in for upload. Our own course thumbnails, so this
#: is a guard against a mistake rather than against hostile input.
MAX_IMAGE_BYTES = 8 * 1024 * 1024


class ProviderNotConfigured(RuntimeError):
    """No client credentials for this network on this deployment."""


class ProviderError(RuntimeError):
    """An upstream call failed. Carries a message safe to show a trainer.

    ``status_code`` and ``payload`` are attached so callers can tell a network
    having a bad minute from a credential that is finished. The publisher needs
    that distinction: one is worth retrying on the next sweep, the other should
    stop immediately and light up "Reconnect" on the connect page.
    """

    def __init__(self, message, *, status_code=None, payload=None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload if isinstance(payload, dict) else {}


class OAuthDenied(ProviderError):
    """The user declined consent, or the network rejected the exchange."""


class ProviderAuthError(ProviderError):
    """The stored credential is no longer accepted.

    Raised for anything that will keep failing until the trainer reconnects: a
    revoked token, an expired one, a permission removed from the app. Retrying
    these is not resilience, it is a loop.
    """


@dataclass
class ConnectedAccount:
    """One publishing destination discovered during token exchange.

    A plain value object rather than a model instance: exchange_code should not
    decide what is persisted or how conflicts resolve. The view upserts.
    """

    provider: str
    provider_account_id: str
    access_token: str
    display_name: str = ""
    handle: str = ""
    avatar_url: str = ""
    refresh_token: str = ""
    expires_at = None
    scopes: str = ""
    meta: dict = field(default_factory=dict)


@dataclass
class PostContent:
    """What a campaign wants published, before any network sees it.

    Already composed for the target network by ``social.promotions.compose`` —
    the adapter's job is the API call, not the copywriting.
    """

    caption: str
    image_url: str = ""
    link_url: str = ""


@dataclass
class PublishedPost:
    """What came back. ``permalink`` is best effort; some networks make you ask."""

    provider_post_id: str
    permalink: str = ""


def get_json(url, *, params=None, headers=None, what="request"):
    """GET returning parsed JSON, with upstream errors turned into ProviderError."""
    return _request("GET", url, params=params, headers=headers, what=what)


def put_bytes(url, *, content, content_type, headers=None, what="upload the image"):
    """PUT raw bytes to an upload slot a network handed out."""
    sent = {"Content-Type": content_type or "application/octet-stream"}
    sent.update(headers or {})
    try:
        resp = requests.put(url, data=content, headers=sent, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise ProviderError(f"Could not reach the network to {what}.") from exc
    if resp.status_code >= 400:
        raise ProviderError(f"{what.capitalize()} failed: HTTP {resp.status_code}")
    return resp


def post_json(url, *, data=None, json=None, params=None, headers=None, what="request"):
    """POST returning parsed JSON. ``data`` is form-encoded, ``json`` is a body."""
    return _request(
        "POST", url, data=data, json=json, params=params, headers=headers, what=what
    )


def post_raw(url, *, data=None, json=None, params=None, headers=None, what="request"):
    """POST returning the whole response, for networks that answer in a header.

    LinkedIn's ``/rest/posts`` returns ``201`` with an empty body and the new
    post's URN in ``x-restli-id``. Without the response object there is no way
    to build a permalink, and a published post the trainer cannot open is only
    half a feature.
    """
    resp, _ = _send(
        "POST", url, data=data, json=json, params=params, headers=headers, what=what
    )
    return resp


def _request(method, url, *, data=None, json=None, params=None, headers=None,
             what="request"):
    _, payload = _send(
        method, url, data=data, json=json, params=params, headers=headers, what=what
    )
    return payload


def _send(method, url, *, data=None, json=None, params=None, headers=None,
          what="request"):
    try:
        resp = requests.request(
            method, url, data=data, json=json, params=params, headers=headers,
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise ProviderError(f"Could not reach the network to {what}.") from exc

    try:
        payload = resp.json()
    except ValueError:
        payload = {}

    if resp.status_code >= 400:
        message = f"{what.capitalize()} failed: {_error_text(payload, resp)}"
        error = ProviderAuthError if _is_auth_failure(resp, payload) else ProviderError
        raise error(message, status_code=resp.status_code, payload=payload)
    return resp, payload


#: Meta error codes that mean "this token is done" rather than "try later".
#: 190 covers expired/invalid/revoked; 102 is a dead session; 463 and 467 are
#: the expired and invalid variants Graph returns for long-lived tokens.
META_AUTH_CODES = {102, 190, 463, 467}


def _is_auth_failure(resp, payload) -> bool:
    """True when the credential is the problem, not the request or the network."""
    if resp.status_code in (401, 403):
        return True
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("code") in META_AUTH_CODES:
            return True
    return False


def fetch_bytes(url: str, what="download the image"):
    """GET raw bytes plus a content type — for networks that want an upload.

    LinkedIn will not take an image by URL the way Meta does; the bytes have to
    be PUT to a slot it hands out. Capped by the same ``TIMEOUT`` as everything
    else and by ``MAX_IMAGE_BYTES``, because the source here is a course
    thumbnail on our own CDN and a surprise there should fail fast rather than
    stream a video into memory.
    """
    try:
        resp = requests.get(url, timeout=TIMEOUT, stream=True)
        resp.raise_for_status()
        content = resp.raw.read(MAX_IMAGE_BYTES + 1, decode_content=True)
    except requests.RequestException as exc:
        raise ProviderError(f"Could not {what}.") from exc

    if not content:
        raise ProviderError(f"Could not {what}: the file was empty.")
    if len(content) > MAX_IMAGE_BYTES:
        raise ProviderError(
            f"Could not {what}: it is larger than "
            f"{MAX_IMAGE_BYTES // (1024 * 1024)}MB."
        )
    return content, resp.headers.get("Content-Type", "image/jpeg")


def _error_text(payload, resp) -> str:
    """Pull a human-readable reason out of whatever the network returned.

    Networks disagree on the shape of an error and some return prose. Truncated
    because this string reaches a trainer-facing message and an upstream stack
    trace is neither useful to them nor ours to display.
    """
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            text = error.get("message") or error.get("error_user_msg")
            if text:
                return str(text)[:300]
        for key in ("error_description", "message", "error"):
            if payload.get(key):
                return str(payload[key])[:300]
    return f"HTTP {resp.status_code}"
