"""Connected social accounts for the trainer panel (PRD §3.3 promotion).

One row per publishing **destination**, not per network: a trainer who admins
two Facebook Pages gets two rows, and the Instagram account linked to a Page is
a row of its own. That is what lets the account picker in the promote wizard
offer real choices rather than a network name.

A connection is only half the story. :class:`Campaign` is one pass through the
Promote wizard, and :class:`CampaignPost` is the row it fans out into per
destination — kept apart because a campaign succeeds or fails per network, and
one Instagram rejection must not roll back a LinkedIn post that already landed.
"""

from django.conf import settings
from django.db import models

from core.models import TimeStampedModel
from social.crypto import EncryptedTextField
from social.promotions import TEMPLATE_CHOICES


class Provider(models.TextChoices):
    """Every network the UI draws a card for.

    ``X`` and ``YOUTUBE`` are listed with no adapter behind them. Keeping them
    in the enum means their cards render "not connected" through the same code
    path as everything else, instead of being special-cased in the frontend —
    and adding an adapter later is the only change needed to light them up.
    """

    LINKEDIN = "linkedin", "LinkedIn"
    FACEBOOK = "facebook", "Facebook"
    INSTAGRAM = "instagram", "Instagram"
    X = "x", "X"
    YOUTUBE = "youtube", "YouTube"


class SocialAccount(TimeStampedModel):
    """A trainer's authorization to publish to one destination."""

    class Status(models.TextChoices):
        CONNECTED = "connected", "Connected"
        EXPIRED = "expired", "Token expired"
        REVOKED = "revoked", "Revoked upstream"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="social_accounts",
    )
    provider = models.CharField(max_length=20, choices=Provider.choices)

    #: The network's own id for this destination — a Page id, an IG user id, a
    #: LinkedIn member ``sub``. Together with (user, provider) it is what makes
    #: reconnecting update the existing row instead of duplicating it.
    provider_account_id = models.CharField(max_length=255)

    display_name = models.CharField(max_length=255, blank=True)
    handle = models.CharField(max_length=255, blank=True)
    avatar_url = models.URLField(max_length=1024, blank=True)

    # Credentials. Ciphertext at rest — see social.crypto.
    access_token = EncryptedTextField(blank=True, default="")
    refresh_token = EncryptedTextField(blank=True, default="")
    token_expires_at = models.DateTimeField(null=True, blank=True)
    scopes = models.TextField(blank=True)

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.CONNECTED
    )
    #: Why publishing last failed, shown next to a "Reconnect" prompt.
    last_error = models.TextField(blank=True)

    #: Network-specific facts the publisher needs and nothing else should know
    #: about: the LinkedIn person URN, the Page a token belongs to, the Page an
    #: Instagram account hangs off.
    provider_meta = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["provider", "display_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "provider", "provider_account_id"],
                name="uniq_social_account_per_destination",
            )
        ]
        indexes = [models.Index(fields=["user", "provider"])]

    def __str__(self):
        return f"{self.get_provider_display()}<{self.handle or self.display_name}>"

    @property
    def is_usable(self) -> bool:
        """True when this account can be published to right now."""
        return self.status == self.Status.CONNECTED and bool(self.access_token)

    def mark_unusable(self, status, error=""):
        """Record that the credential stopped working.

        Called from the publisher on an auth failure so the connect page can
        show "Reconnect" instead of silently failing again on the next run.
        """
        self.status = status
        self.last_error = error
        self.save(update_fields=["status", "last_error", "updated_at"])


class Campaign(TimeStampedModel):
    """One pass through the Promote wizard: a course, some copy, some destinations.

    The wizard's four steps land in three groups of fields — ``template``,
    then ``caption``/``hashtags``/``image_source``, then the ``posts`` fanned
    out from the chosen accounts, then ``scheduled_for``. Nothing is stored
    until the trainer presses Publish or Schedule; an abandoned wizard leaves
    no row, the same way an abandoned consent screen doesn't.

    ``scheduled_for`` being ``NULL`` means "publish now" and is not the same as
    a scheduled time that happens to be in the past — the sweep treats a null
    as due immediately, which is what makes an inline publish that timed out
    recoverable instead of lost.
    """

    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        PUBLISHING = "publishing", "Publishing"
        PUBLISHED = "published", "Published"
        PARTIAL = "partial", "Partly published"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    class ImageSource(models.TextChoices):
        THUMBNAIL = "thumbnail", "Course thumbnail"
        PROMO_CARD = "promo_card", "Auto promo card"
        NONE = "none", "No image"

    #: Statuses a trainer is still allowed to edit or cancel. Once a single
    #: post has reached a network the copy is public and editing the row would
    #: only make our record disagree with what people can already read.
    EDITABLE_STATUSES = (Status.SCHEDULED,)

    trainer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="social_campaigns",
    )
    course = models.ForeignKey(
        "courses.Course",
        on_delete=models.CASCADE,
        related_name="social_campaigns",
    )

    template = models.CharField(max_length=32, choices=TEMPLATE_CHOICES)
    caption = models.TextField()
    hashtags = models.CharField(max_length=255, blank=True)

    image_source = models.CharField(
        max_length=20, choices=ImageSource.choices, default=ImageSource.THUMBNAIL
    )
    #: Resolved once at create time. The course thumbnail can be replaced after
    #: a campaign is scheduled, and a post should go out with the art the
    #: trainer previewed rather than whatever the course happens to hold when
    #: the cron fires.
    image_url = models.URLField(max_length=1024, blank=True)
    link_url = models.URLField(max_length=1024, blank=True)

    #: ``NULL`` = publish immediately. See the class docstring.
    scheduled_for = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.SCHEDULED
    )
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["trainer", "-created_at"]),
            models.Index(fields=["status", "scheduled_for"]),
        ]

    def __str__(self):
        return f"{self.course} · {self.template} ({self.status})"

    @property
    def is_due(self) -> bool:
        """True when the sweep should be publishing this now."""
        from django.utils import timezone

        if self.status not in (self.Status.SCHEDULED, self.Status.PUBLISHING):
            return False
        return self.scheduled_for is None or self.scheduled_for <= timezone.now()


class CampaignPost(TimeStampedModel):
    """One campaign's attempt at one destination.

    Statuses live here rather than only on the campaign because networks fail
    independently: Instagram rejecting an image says nothing about whether the
    LinkedIn post went out, and the wizard has to show the trainer exactly
    which destination needs another try.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PUBLISHING = "publishing", "Publishing"
        PUBLISHED = "published", "Published"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    #: Give up after this many tries. A network that has refused three times is
    #: not having a bad minute, and a post that keeps retrying past its moment
    #: is worse than one that stops and says so.
    MAX_ATTEMPTS = 3

    campaign = models.ForeignKey(
        Campaign, on_delete=models.CASCADE, related_name="posts"
    )
    #: ``SET_NULL``, not ``CASCADE``: disconnecting an account must not erase
    #: the record of what was already published through it.
    account = models.ForeignKey(
        SocialAccount,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="campaign_posts",
    )
    #: Denormalised so a row still reads sensibly after its account is gone.
    provider = models.CharField(max_length=20, choices=Provider.choices)
    account_label = models.CharField(max_length=255, blank=True)

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    #: What was actually sent — composed per network at publish time, so this
    #: is the only record of the Instagram variant of a LinkedIn caption.
    published_caption = models.TextField(blank=True)
    provider_post_id = models.CharField(max_length=255, blank=True)
    permalink = models.URLField(max_length=1024, blank=True)
    error = models.TextField(blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["provider", "id"]
        indexes = [models.Index(fields=["status", "campaign"])]
        constraints = [
            models.UniqueConstraint(
                fields=["campaign", "account"],
                name="uniq_campaign_post_per_account",
            )
        ]

    def __str__(self):
        return f"{self.campaign_id} → {self.account_label or self.provider}"

    @property
    def can_retry(self) -> bool:
        return (
            self.status == self.Status.FAILED
            and self.attempts < self.MAX_ATTEMPTS
        )
