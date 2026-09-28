"""LinkedIn adapter — posts as the member personally.

The simplest of the three: one authorization, one destination, no page picker.

Identity comes from OpenID Connect (``/v2/userinfo``), which is what LinkedIn
moved to for profile reads; the ``sub`` claim is the member id and the publish
author is ``urn:li:person:{sub}``. That URN is stashed in ``provider_meta`` at
connect time so the publisher never has to re-derive it.

Requires the "Sign In with LinkedIn using OpenID Connect" and "Share on
LinkedIn" products on the developer app. ``w_member_social`` is review-gated:
before approval the flow works only for accounts listed on the app itself.

Access tokens last about 60 days. Refresh tokens are issued only to apps
approved for them, so ``refresh_token`` is frequently empty and reconnecting is
the normal recovery — hence the ``expired`` status rather than a silent failure.
"""

from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.utils import timezone

from social.providers.base import (
    ConnectedAccount,
    OAuthDenied,
    ProviderAuthError,
    ProviderError,
    ProviderNotConfigured,
    PublishedPost,
    fetch_bytes,
    get_json,
    post_json,
    post_raw,
    put_bytes,
)

KEY = "linkedin"
PROVIDES = ("linkedin",)
LABEL = "LinkedIn"

SCOPES = ("openid", "profile", "email", "w_member_social")

AUTHORIZE_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
REVOKE_URL = "https://www.linkedin.com/oauth/v2/revoke"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"


def _credentials():
    client_id = getattr(settings, "LINKEDIN_CLIENT_ID", "")
    client_secret = getattr(settings, "LINKEDIN_CLIENT_SECRET", "")
    if not (client_id and client_secret):
        raise ProviderNotConfigured("LinkedIn is not configured on this server.")
    return client_id, client_secret


def is_configured() -> bool:
    return bool(
        getattr(settings, "LINKEDIN_CLIENT_ID", "")
        and getattr(settings, "LINKEDIN_CLIENT_SECRET", "")
    )


def authorize_url(state: str, redirect_uri: str) -> str:
    client_id, _ = _credentials()
    return f"{AUTHORIZE_URL}?" + urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": " ".join(SCOPES),
        }
    )


def exchange_code(code: str, redirect_uri: str) -> list:
    client_id, client_secret = _credentials()

    token = post_json(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        what="exchange the LinkedIn authorization code",
    )
    access_token = token.get("access_token")
    if not access_token:
        raise OAuthDenied("LinkedIn did not return an access token.")

    profile = get_json(
        USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        what="read the LinkedIn profile",
    )
    sub = profile.get("sub")
    if not sub:
        raise OAuthDenied("LinkedIn did not identify the member.")

    expires_at = None
    if token.get("expires_in"):
        expires_at = timezone.now() + timedelta(seconds=int(token["expires_in"]))

    account = ConnectedAccount(
        provider=KEY,
        provider_account_id=str(sub),
        access_token=access_token,
        refresh_token=token.get("refresh_token", "") or "",
        display_name=profile.get("name", ""),
        # LinkedIn has no public @handle on the API; the email local-part is the
        # closest recognisable label, and the UI only uses this for display.
        handle=(profile.get("email") or "").split("@")[0],
        avatar_url=profile.get("picture", "") or "",
        scopes=token.get("scope", " ".join(SCOPES)),
        meta={"author_urn": f"urn:li:person:{sub}"},
    )
    account.expires_at = expires_at
    return [account]


def revoke(account) -> None:
    """Best effort. LinkedIn 400s on an already-invalid token; that is fine."""
    client_id, client_secret = _credentials()
    post_json(
        REVOKE_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "token": account.access_token,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        what="revoke the LinkedIn token",
    )


# --------------------------------------------------------------------------- #
# Publishing
# --------------------------------------------------------------------------- #

POSTS_URL = "https://api.linkedin.com/rest/posts"
IMAGES_URL = "https://api.linkedin.com/rest/images"

#: LinkedIn versions its REST API by month and rejects a call with no version
#: header. Pinned in settings rather than hardcoded because it has to be moved
#: forward roughly yearly — an unpinned "latest" would break on their schedule
#: instead of ours.
DEFAULT_API_VERSION = "202609"


def _api_version() -> str:
    return getattr(settings, "LINKEDIN_API_VERSION", "") or DEFAULT_API_VERSION


def _headers(account):
    return {
        "Authorization": f"Bearer {account.access_token}",
        "LinkedIn-Version": _api_version(),
        "X-Restli-Protocol-Version": "2.0.0",
        "Content-Type": "application/json",
    }


def _author_urn(account) -> str:
    """The URN we post as.

    Stashed at connect time; re-derived from the account id if an older row
    predates that. Posting as a member, never a company page — the app is not
    approved for organization shares.
    """
    urn = (account.provider_meta or {}).get("author_urn")
    return urn or f"urn:li:person:{account.provider_account_id}"


def publish(account, content) -> PublishedPost:
    """Share ``content`` on the member's own feed.

    The image is optional and degrades on purpose: if the upload fails we still
    post the text. A caption that went out without its picture is a worse post;
    a caption that never went out is a broken feature.
    """
    author = _author_urn(account)
    body = {
        "author": author,
        "commentary": content.caption,
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }

    if content.image_url:
        image_urn = _upload_image(account, author, content.image_url)
        if image_urn:
            body["content"] = {"media": {"id": image_urn}}

    resp = post_raw(
        POSTS_URL,
        json=body,
        headers=_headers(account),
        what="publish the LinkedIn post",
    )

    # 201 with an empty body; the URN is only in the header.
    urn = resp.headers.get("x-restli-id") or resp.headers.get("X-RestLi-Id") or ""
    if not urn:
        raise ProviderError("LinkedIn accepted the post but did not return its id.")
    return PublishedPost(
        provider_post_id=urn,
        permalink=f"https://www.linkedin.com/feed/update/{urn}/",
    )


def _upload_image(account, author, image_url):
    """Register an upload slot, PUT the bytes, return the image URN.

    Unlike Meta, LinkedIn will not fetch an image by URL — it hands out a
    single-use upload URL and wants the bytes. Returns ``None`` on any failure
    so ``publish`` can fall back to a text post; an auth failure is re-raised,
    because that one is not going to work for the text either.
    """
    try:
        initialized = post_json(
            f"{IMAGES_URL}?action=initializeUpload",
            json={"initializeUploadRequest": {"owner": author}},
            headers=_headers(account),
            what="prepare the LinkedIn image upload",
        )
        slot = initialized.get("value") or {}
        upload_url, image_urn = slot.get("uploadUrl"), slot.get("image")
        if not (upload_url and image_urn):
            return None

        content, content_type = fetch_bytes(image_url)
        put_bytes(
            upload_url,
            content=content,
            content_type=content_type,
            headers={"Authorization": f"Bearer {account.access_token}"},
            what="upload the image to LinkedIn",
        )
        return image_urn
    except ProviderAuthError:
        raise
    except ProviderError:
        return None
