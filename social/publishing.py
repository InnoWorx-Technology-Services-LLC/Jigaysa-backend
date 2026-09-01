"""Turning a campaign into posts on real networks.

Two callers, one path. ``POST /social/campaigns/`` with no ``publish_at`` runs
:func:`run_campaign` inline so the wizard can show what landed; the cron sweep
runs :func:`run_due` for everything scheduled. Both funnel into
:func:`publish_post`, so an inline publish and a scheduled one cannot drift.

**Every post is claimed before it is sent.** The claim is a conditional UPDATE
(``pending`` → ``publishing``) whose row count decides whether this worker owns
the post. That is deliberately not ``select_for_update``: it costs one
statement, behaves identically on SQLite and MySQL, and — unlike a lock — it
still holds when the sweep overlaps an inline publish of the same campaign,
which is exactly the case that would otherwise post twice.

Retries are bounded and typed. A network having a bad minute goes back to
``pending`` for the next sweep; a credential that has stopped working goes
straight to ``failed`` and marks the account, so the connect page shows
"Reconnect". Retrying a dead token is not resilience, it is a loop.
"""

import logging

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from social import promotions, providers
from social.models import Campaign, CampaignPost, SocialAccount
from social.providers.base import (
    PostContent,
    ProviderAuthError,
    ProviderError,
    ProviderNotConfigured,
)

logger = logging.getLogger(__name__)

#: How many posts one sweep will attempt. Each is a network round-trip capped
#: at ``providers.base.TIMEOUT``, so this bounds a cron run's worst case.
SWEEP_LIMIT = 50


# --------------------------------------------------------------------------- #
# Fan-out
# --------------------------------------------------------------------------- #


def build_posts(campaign, accounts):
    """Create one pending post per destination.

    ``provider`` and ``account_label`` are copied onto the row rather than read
    through the FK, so a campaign still reads sensibly after the trainer
    disconnects the account it went out through.
    """
    return CampaignPost.objects.bulk_create(
        [
            CampaignPost(
                campaign=campaign,
                account=account,
                provider=account.provider,
                account_label=account.display_name or account.handle,
            )
            for account in accounts
        ]
    )


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #


def run_campaign(campaign) -> Campaign:
    """Attempt every pending post on one campaign, then settle its status."""
    posts = campaign.posts.filter(
        status=CampaignPost.Status.PENDING
    ).select_related("account")
    for post in posts:
        publish_post(post)
    return refresh_status(campaign)


def due_filter():
    """``scheduled_for`` is null (publish now) or already past."""
    return Q(campaign__scheduled_for__isnull=True) | Q(
        campaign__scheduled_for__lte=timezone.now()
    )


def run_due(limit=SWEEP_LIMIT):
    """Publish everything whose time has come. Returns the posts attempted.

    Picks up two things: campaigns scheduled for a past time, and campaigns
    with no schedule at all whose inline publish never finished — a request
    that timed out mid-fan-out leaves pending rows, and this is what makes
    those recoverable rather than lost.
    """
    due = (
        CampaignPost.objects.filter(
            status=CampaignPost.Status.PENDING,
            campaign__status__in=(
                Campaign.Status.SCHEDULED,
                Campaign.Status.PUBLISHING,
            ),
        )
        .filter(due_filter())
        .select_related("account", "campaign", "campaign__course")[:limit]
    )

    attempted = []
    touched = set()
    for post in due:
        attempted.append(post)
        touched.add(post.campaign_id)
        publish_post(post)

    for campaign in Campaign.objects.filter(pk__in=touched).select_related("course"):
        refresh_status(campaign)
    return attempted


def publish_post(post) -> bool:
    """Send one post. Returns True when it reached the network.

    Never raises: a campaign fans out to several destinations and one network
    refusing must not stop the others. The reason is recorded on the row, which
    is what the wizard shows next to a Retry.
    """
    if not _claim(post):
        return False  # another worker owns it

    try:
        result = _send(post)
    except ProviderAuthError as exc:
        _fail(post, str(exc), permanent=True)
        _mark_account_unusable(post, str(exc))
        return False
    except (ProviderNotConfigured, ProviderError) as exc:
        _fail(post, str(exc))
        return False
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("Unexpected error publishing campaign post %s", post.pk)
        _fail(post, "Unexpected error: {}".format(exc))
        return False

    post.status = CampaignPost.Status.PUBLISHED
    post.provider_post_id = result.provider_post_id
    post.permalink = result.permalink
    post.published_at = timezone.now()
    post.error = ""
    post.save(
        update_fields=[
            "status", "provider_post_id", "permalink", "published_at",
            "error", "updated_at",
        ]
    )
    return True


def _send(post):
    """Compose for this network, then hand it to the adapter."""
    account = post.account
    if account is None:
        raise ProviderError(
            "The account this post was going to has been disconnected."
        )
    if not account.is_usable:
        raise ProviderAuthError(
            account.last_error
            or "This account needs to be reconnected before it can post."
        )

    adapter = providers.adapter_for(post.provider)
    if adapter is None or not hasattr(adapter, "publish"):
        raise ProviderError(
            "Publishing to {} isn't supported yet.".format(
                post.get_provider_display()
            )
        )
    if not adapter.is_configured():
        raise ProviderNotConfigured(
            "{} is not configured on this server.".format(
                post.get_provider_display()
            )
        )

    campaign = post.campaign
    caption = promotions.compose(
        campaign.caption, campaign.hashtags, campaign.link_url, post.provider
    )
    post.published_caption = caption
    post.save(update_fields=["published_caption", "updated_at"])

    return adapter.publish(
        account,
        PostContent(
            caption=caption,
            image_url=campaign.image_url,
            link_url=campaign.link_url,
        ),
    )


def _claim(post) -> bool:
    """Take ownership of a pending post, or report that someone else has it.

    A conditional UPDATE, not a lock — see the module docstring. The attempt
    counter moves with the claim so a worker that dies mid-call still burns a
    try, which is what stops a post that reliably crashes us from retrying for
    ever.
    """
    claimed = CampaignPost.objects.filter(
        pk=post.pk, status=CampaignPost.Status.PENDING
    ).update(
        status=CampaignPost.Status.PUBLISHING,
        attempts=F("attempts") + 1,
        updated_at=timezone.now(),
    )
    if not claimed:
        return False
    post.refresh_from_db(fields=["status", "attempts"])
    return True


def _fail(post, error, permanent=False):
    """Record why, and decide whether the next sweep should try again."""
    exhausted = permanent or post.attempts >= CampaignPost.MAX_ATTEMPTS
    post.status = (
        CampaignPost.Status.FAILED if exhausted else CampaignPost.Status.PENDING
    )
    post.error = error[:1000]
    post.save(update_fields=["status", "error", "updated_at"])


def _mark_account_unusable(post, error):
    """Light up "Reconnect" on the connect page for a credential that died.

    An Instagram row shares its Page's token, so the Page is marked too —
    otherwise the trainer fixes one card and the other keeps failing on a token
    that was never separately broken.
    """
    account = post.account
    if account is None:
        return
    account.mark_unusable(SocialAccount.Status.EXPIRED, error[:1000])

    page_id = (account.provider_meta or {}).get("page_id")
    if page_id:
        SocialAccount.objects.filter(
            user_id=account.user_id, provider_account_id=page_id
        ).exclude(pk=account.pk).update(
            status=SocialAccount.Status.EXPIRED,
            last_error=error[:1000],
            updated_at=timezone.now(),
        )


# --------------------------------------------------------------------------- #
# Status roll-up
# --------------------------------------------------------------------------- #


def refresh_status(campaign) -> Campaign:
    """Recompute the campaign's status from its posts.

    ``partial`` is a real outcome, not a rounding of "failed": a launch that
    reached LinkedIn and not Instagram is half-shipped, and telling the trainer
    it failed would have them post the LinkedIn copy a second time.
    """
    states = list(campaign.posts.values_list("status", flat=True))
    if not states:
        return campaign

    published = states.count(CampaignPost.Status.PUBLISHED)
    cancelled = states.count(CampaignPost.Status.CANCELLED)
    outstanding = sum(
        1
        for s in states
        if s in (CampaignPost.Status.PENDING, CampaignPost.Status.PUBLISHING)
    )

    if outstanding:
        status = (
            Campaign.Status.PUBLISHING
            if campaign.is_due
            else Campaign.Status.SCHEDULED
        )
    elif published and published + cancelled == len(states):
        status = Campaign.Status.PUBLISHED
    elif published:
        status = Campaign.Status.PARTIAL
    elif cancelled == len(states):
        status = Campaign.Status.CANCELLED
    else:
        status = Campaign.Status.FAILED

    settled = status in (
        Campaign.Status.PUBLISHED,
        Campaign.Status.PARTIAL,
        Campaign.Status.FAILED,
    )
    fields = ["status", "updated_at"]
    if settled and campaign.published_at is None:
        campaign.published_at = timezone.now()
        fields.append("published_at")

    was = campaign.status
    campaign.status = status
    campaign.save(update_fields=fields)

    if settled and was != status:
        transaction.on_commit(lambda: _notify_outcome(campaign))
    return campaign


def _notify_outcome(campaign):
    """Tell the trainer how a scheduled campaign ended.

    Only worth sending for something they are not watching: a "publish now" is
    answered by the response they are already looking at, and a second ping for
    it is noise.
    """
    if campaign.scheduled_for is None:
        return

    from notifications.models import NotificationCategory
    from notifications.services import notify

    titles = {
        Campaign.Status.PUBLISHED: "Your promo post is live",
        Campaign.Status.PARTIAL: "Your promo post went out partly",
        Campaign.Status.FAILED: "Your promo post didn't go out",
    }
    bodies = {
        Campaign.Status.PUBLISHED: "Scheduled post for “{}” published.",
        Campaign.Status.PARTIAL: (
            "Scheduled post for “{}” reached some networks but not "
            "all. Open the Promotions tab to retry the rest."
        ),
        Campaign.Status.FAILED: (
            "Scheduled post for “{}” could not be published. "
            "Open the Promotions tab to see why."
        ),
    }
    title = titles.get(campaign.status)
    if not title:
        return

    notify(
        campaign.trainer,
        NotificationCategory.SYSTEM,
        title=title,
        body=bodies[campaign.status].format(campaign.course.title),
        link="/trainer/courses/{}/edit/promotions".format(campaign.course.slug),
    )
