"""Meta adapter — Facebook Pages and the Instagram accounts linked to them.

**One adapter for two provider keys.** Facebook and Instagram are separate cards
in the UI but a single authorization underneath: the same consent returns every
Page the trainer admins and, for each Page, the Instagram Business account
attached to it. So ``exchange_code`` returns a list that routinely mixes
``facebook`` and ``instagram`` destinations, and connecting either card can
light up both. The UI should say so, or it reads as a bug.

Two constraints worth remembering when reading this:

* **Posting happens as a Page, never a personal profile** — that capability is
  gone from the API. A trainer with no Page cannot publish to Facebook at all,
  which is a real dead end and is reported as such rather than as an error.
* **An Instagram account must be Business or Creator and linked to a Page.**
  A personal IG account is invisible to this call. It simply won't appear.

Token lifetimes work out well here: the short-lived user token is exchanged for
a long-lived one, and the Page tokens derived from it do not expire. The
Instagram destination borrows its Page's token.
"""

from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.utils import timezone

from social.providers.base import (
    ConnectedAccount,
    OAuthDenied,
    ProviderError,
    ProviderNotConfigured,
    PublishedPost,
    get_json,
    post_json,
)

KEY = "facebook"
PROVIDES = ("facebook", "instagram")
LABEL = "Facebook & Instagram"

SCOPES = (
    "pages_show_list",
    "pages_read_engagement",
    "pages_manage_posts",
    "instagram_basic",
    "instagram_content_publish",
)


def _version() -> str:
    return getattr(settings, "META_GRAPH_VERSION", "") or "v21.0"


def _graph(path: str) -> str:
    return f"https://graph.facebook.com/{_version()}/{path.lstrip('/')}"


def _credentials():
    app_id = getattr(settings, "META_APP_ID", "")
    app_secret = getattr(settings, "META_APP_SECRET", "")
    if not (app_id and app_secret):
        raise ProviderNotConfigured(
            "Facebook and Instagram are not configured on this server."
        )
    return app_id, app_secret


def is_configured() -> bool:
    return bool(
        getattr(settings, "META_APP_ID", "")
        and getattr(settings, "META_APP_SECRET", "")
    )


def authorize_url(state: str, redirect_uri: str) -> str:
    app_id, _ = _credentials()
    return f"https://www.facebook.com/{_version()}/dialog/oauth?" + urlencode(
        {
            "client_id": app_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "response_type": "code",
            "scope": ",".join(SCOPES),
        }
    )


def exchange_code(code: str, redirect_uri: str) -> list:
    app_id, app_secret = _credentials()

    short = get_json(
        _graph("oauth/access_token"),
        params={
            "client_id": app_id,
            "client_secret": app_secret,
            "redirect_uri": redirect_uri,
            "code": code,
        },
        what="exchange the Facebook authorization code",
    )
    if not short.get("access_token"):
        raise OAuthDenied("Facebook did not return an access token.")

    # Swap the ~1-hour token for a ~60-day one before deriving Page tokens.
    # Page tokens inherit their durability from the user token they came from,
    # so skipping this would hand us credentials that die within the hour.
    long_lived = get_json(
        _graph("oauth/access_token"),
        params={
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": short["access_token"],
        },
        what="extend the Facebook access token",
    )
    user_token = long_lived.get("access_token") or short["access_token"]
    user_expires_at = None
    if long_lived.get("expires_in"):
        user_expires_at = timezone.now() + timedelta(
            seconds=int(long_lived["expires_in"])
        )

    pages = get_json(
        _graph("me/accounts"),
        params={
            "fields": "id,name,username,access_token,picture{url}",
            "access_token": user_token,
        },
        what="list your Facebook Pages",
    ).get("data", [])

    if not pages:
        raise ProviderError(
            "No Facebook Page found on this account. Posting to a personal "
            "profile isn't supported by Facebook — create a Page, or grant "
            "this app access to one, then connect again."
        )

    accounts = []
    for page in pages:
        page_token = page.get("access_token")
        if not page_token:
            continue
        accounts.append(_page_account(page, page_token, user_token, user_expires_at))
        instagram = _instagram_for_page(page, page_token)
        if instagram is not None:
            accounts.append(instagram)
    return accounts


def _page_account(page, page_token, user_token, user_expires_at):
    return ConnectedAccount(
        provider="facebook",
        provider_account_id=str(page["id"]),
        access_token=page_token,
        display_name=page.get("name", ""),
        handle=page.get("username", "") or "",
        avatar_url=(page.get("picture") or {}).get("data", {}).get("url", ""),
        scopes=",".join(SCOPES),
        meta={
            "page_id": str(page["id"]),
            # Kept so a future refresh can re-derive Page tokens without
            # sending the trainer back through consent.
            "user_access_token": user_token,
            "user_token_expires_at": (
                user_expires_at.isoformat() if user_expires_at else None
            ),
        },
    )


def _instagram_for_page(page, page_token):
    """The IG Business account linked to a Page, or ``None``.

    A Page without one is the common case, not an error — most trainers will
    connect Facebook and simply not have Instagram appear.
    """
    try:
        linked = get_json(
            _graph(str(page["id"])),
            params={
                "fields": "instagram_business_account{id,username,profile_picture_url}",
                "access_token": page_token,
            },
            what="check for a linked Instagram account",
        )
    except ProviderError:
        # One Page failing this lookup must not lose the whole connection —
        # the Facebook destinations are still perfectly good.
        return None

    ig = linked.get("instagram_business_account")
    if not ig or not ig.get("id"):
        return None

    return ConnectedAccount(
        provider="instagram",
        provider_account_id=str(ig["id"]),
        # Publishing to Instagram is authorised by the linked Page's token.
        access_token=page_token,
        display_name=ig.get("username", "") or page.get("name", ""),
        handle=ig.get("username", "") or "",
        avatar_url=ig.get("profile_picture_url", "") or "",
        scopes=",".join(SCOPES),
        meta={
            "ig_user_id": str(ig["id"]),
            "page_id": str(page["id"]),
            "page_name": page.get("name", ""),
        },
    )


def revoke(account) -> None:
    """Best effort: drop this app's permissions for the account.

    Only meaningful for a Facebook Page row — an Instagram row shares its Page's
    token, and revoking through it would silently disconnect the Page too.
    """
    if account.provider != "facebook":
        return
    import requests

    from social.providers.base import TIMEOUT

    try:
        requests.delete(
            _graph(f"{account.provider_account_id}/permissions"),
            params={"access_token": account.access_token},
            timeout=TIMEOUT,
        )
    except requests.RequestException:
        pass  # the row is going away regardless; upstream cleanup is a courtesy


# --------------------------------------------------------------------------- #
# Publishing
# --------------------------------------------------------------------------- #


def publish(account, content) -> PublishedPost:
    """Publish to a Page or an Instagram Business account.

    One entry point for two provider keys, because that is how the connection
    works too. The two flows share nothing but the Graph host: Facebook is a
    single call, Instagram is a container that has to be created and then
    published.
    """
    if account.provider == "instagram":
        return _publish_instagram(account, content)
    return _publish_facebook(account, content)


def _page_id(account) -> str:
    return (account.provider_meta or {}).get("page_id") or account.provider_account_id


def _publish_facebook(account, content) -> PublishedPost:
    """Post to the Page feed, as a photo when there is one.

    ``/photos`` takes the image by URL — Graph fetches it itself, so there is
    no upload step. Its response carries both a photo id and the feed
    ``post_id``; the second is the one a permalink is built from.
    """
    page_id = _page_id(account)

    if content.image_url:
        created = post_json(
            _graph(f"{page_id}/photos"),
            data={
                "url": content.image_url,
                "caption": content.caption,
                "access_token": account.access_token,
            },
            what="publish the Facebook photo post",
        )
        post_id = created.get("post_id") or created.get("id", "")
    else:
        payload = {"message": content.caption, "access_token": account.access_token}
        if content.link_url:
            payload["link"] = content.link_url
        created = post_json(
            _graph(f"{page_id}/feed"),
            data=payload,
            what="publish the Facebook post",
        )
        post_id = created.get("id", "")

    if not post_id:
        raise ProviderError("Facebook accepted the post but did not return its id.")
    return PublishedPost(
        provider_post_id=str(post_id),
        permalink=f"https://www.facebook.com/{post_id}",
    )


def _publish_instagram(account, content) -> PublishedPost:
    """Create a media container, then publish it.

    Instagram has no text-only post — the image is the post — so a missing
    image is a refusal here rather than a degraded one. ``image_url`` must be
    reachable by Meta's servers: a signed or private URL fails at their end
    with a message about downloading the media, not at ours.
    """
    ig_user_id = (account.provider_meta or {}).get("ig_user_id") or (
        account.provider_account_id
    )

    if not content.image_url:
        raise ProviderError(
            "Instagram posts must include an image. Add a course thumbnail, or "
            "drop Instagram from this campaign."
        )

    container = post_json(
        _graph(f"{ig_user_id}/media"),
        data={
            "image_url": content.image_url,
            "caption": content.caption,
            "access_token": account.access_token,
        },
        what="prepare the Instagram post",
    )
    creation_id = container.get("id")
    if not creation_id:
        raise ProviderError("Instagram did not return a media container.")

    published = post_json(
        _graph(f"{ig_user_id}/media_publish"),
        data={"creation_id": creation_id, "access_token": account.access_token},
        what="publish the Instagram post",
    )
    media_id = published.get("id")
    if not media_id:
        raise ProviderError("Instagram accepted the post but did not return its id.")

    return PublishedPost(
        provider_post_id=str(media_id),
        permalink=_permalink(media_id, account.access_token),
    )


def _permalink(media_id, token) -> str:
    """Best effort. Instagram only tells you the URL if you ask separately, and
    a post that published is not worth failing over a missing link."""
    try:
        return get_json(
            _graph(str(media_id)),
            params={"fields": "permalink", "access_token": token},
            what="read the Instagram post link",
        ).get("permalink", "")
    except ProviderError:
        return ""
