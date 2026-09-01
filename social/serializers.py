"""Serializers for the connect page and the Promote wizard.

Tokens are never serialized — not write-only, not hinted, not present. Unlike a
gateway key an admin might paste in, an OAuth token is only ever written by the
callback, so there is no input to accept and no reason for one to leave the
server.
"""

from django.utils import timezone
from rest_framework import serializers

from courses.models import Course
from social import promotions, providers
from social.models import Campaign, CampaignPost, Provider, SocialAccount


class SocialAccountSerializer(serializers.ModelSerializer):
    """A connected destination, as the card and the account picker render it."""

    provider_label = serializers.CharField(source="get_provider_display", read_only=True)
    needs_reconnect = serializers.SerializerMethodField()

    class Meta:
        model = SocialAccount
        fields = (
            "id", "provider", "provider_label", "display_name", "handle",
            "avatar_url", "status", "needs_reconnect", "last_error",
            "token_expires_at", "created_at", "updated_at",
        )
        read_only_fields = fields

    def get_needs_reconnect(self, obj) -> bool:
        """One flag for the UI, so the card doesn't reimplement the status rules."""
        return not obj.is_usable


class ProviderSerializer(serializers.Serializer):
    """One card on the connect page: the network, plus this trainer's state.

    Deliberately returns every provider — including the ones with no adapter —
    so the page renders from a single response instead of merging a hardcoded
    list against a dynamic one. ``available`` and ``configured`` are separate
    because they fail differently: ``available`` false means we haven't built
    it, ``configured`` false means this deployment has no credentials. The card
    copy for those two is not the same.
    """

    provider = serializers.CharField()
    label = serializers.CharField()
    available = serializers.BooleanField()
    configured = serializers.BooleanField()
    accounts = SocialAccountSerializer(many=True)


class ConnectRequestSerializer(serializers.Serializer):
    """Body of ``POST /social/connect/{provider}/``.

    ``return_to`` lets the promote wizard resume where it left off after the
    round-trip. It is validated as a site-relative path here *and* again when
    the redirect is built — an open redirect carrying the platform's own domain
    is worth checking twice.
    """

    return_to = serializers.CharField(required=False, allow_blank=True, max_length=500)

    def validate_return_to(self, value):
        if value and (not value.startswith("/") or value.startswith("//")):
            raise serializers.ValidationError(
                "return_to must be a path on this site, e.g. /trainer/settings/social."
            )
        return value


class ConnectResponseSerializer(serializers.Serializer):
    """What the frontend needs to start the round-trip: send the browser here."""

    authorize_url = serializers.URLField()
    provider = serializers.ChoiceField(choices=Provider.choices)
    expires_in = serializers.IntegerField()


# --------------------------------------------------------------------------- #
# The Promote wizard — templates, campaigns, scheduling
#
# Most of the wizard's rules live in the validation below, and it is all one
# idea: **refuse what the network will refuse, before the trainer walks away.**
# A campaign scheduled for 2am that fails at 2am because Instagram had no image
# is a support ticket; the same campaign rejected at the Schedule button is a
# two-second fix.
# --------------------------------------------------------------------------- #

#: How far ahead a post may be scheduled. Not a platform limit — a guard
#: against a mistyped year silently parking a campaign in 2126.
MAX_SCHEDULE_DAYS = 365

#: Tolerance for a "publish at the time I picked" that arrives a moment late.
#: Round-trip latency and a browser clock a few seconds off should not turn
#: into a validation error the trainer cannot act on.
SCHEDULE_GRACE_SECONDS = 120


def _validate_schedule(when):
    """Shared by create and update: a schedule must be soon-ish and ahead.

    ``None`` is not an error — it is the "Publish now" radio, and both screens
    accept it.
    """
    if when is None:
        return None
    now = timezone.now()
    if (now - when).total_seconds() > SCHEDULE_GRACE_SECONDS:
        raise serializers.ValidationError(
            "Pick a time in the future, or choose Publish now."
        )
    if (when - now).days > MAX_SCHEDULE_DAYS:
        raise serializers.ValidationError(
            "That's more than a year away — check the date."
        )
    return when


class TemplateSerializer(serializers.Serializer):
    """One card in step 1, pre-rendered for step 2.

    ``hint`` is not an error — it says which fallback the copy used, so the
    trainer knows why the testimonial template reads like a promise when the
    course has no reviews yet.
    """

    key = serializers.CharField()
    badge = serializers.CharField()
    title = serializers.CharField()
    blurb = serializers.CharField()
    caption = serializers.CharField()
    hashtags = serializers.CharField()
    hint = serializers.CharField(allow_blank=True)


class ImageOptionSerializer(serializers.Serializer):
    """The Image row in step 2. ``available`` mirrors the connect page's idiom."""

    value = serializers.CharField()
    label = serializers.CharField()
    available = serializers.BooleanField()
    url = serializers.CharField(allow_blank=True)
    note = serializers.CharField(allow_blank=True)


class PromoteContextSerializer(serializers.Serializer):
    """Everything steps 1 and 2 need, in one call."""

    course = serializers.DictField()
    templates = TemplateSerializer(many=True)
    image_options = ImageOptionSerializer(many=True)


class RewriteRequestSerializer(serializers.Serializer):
    """Body of ``POST /social/rewrite/`` — the Rewrite button in step 2."""

    caption = serializers.CharField(max_length=6000)
    provider = serializers.ChoiceField(choices=Provider.choices)
    hashtags = serializers.CharField(
        required=False, allow_blank=True, max_length=255
    )
    link_url = serializers.URLField(required=False, allow_blank=True)


class RewriteResponseSerializer(serializers.Serializer):
    """``caption`` is the editable text; ``preview`` is what would actually post.

    They differ because the link and the hashtags are appended at publish time,
    per network — the preview pane needs to show that, the textarea must not
    swallow it.
    """

    provider = serializers.CharField()
    caption = serializers.CharField()
    preview = serializers.CharField()
    limit = serializers.IntegerField()


class CampaignPostSerializer(serializers.ModelSerializer):
    """One destination's outcome, as the wizard's result list renders it."""

    provider_label = serializers.CharField(
        source="get_provider_display", read_only=True
    )
    can_retry = serializers.BooleanField(read_only=True)

    class Meta:
        model = CampaignPost
        fields = (
            "id", "account", "provider", "provider_label", "account_label",
            "status", "published_caption", "provider_post_id", "permalink",
            "error", "attempts", "can_retry", "published_at",
        )
        read_only_fields = fields


class CampaignSerializer(serializers.ModelSerializer):
    """A campaign and everything it fanned out into."""

    course = serializers.SlugRelatedField(slug_field="slug", read_only=True)
    course_title = serializers.CharField(source="course.title", read_only=True)
    posts = CampaignPostSerializer(many=True, read_only=True)
    can_edit = serializers.SerializerMethodField()

    class Meta:
        model = Campaign
        fields = (
            "id", "course", "course_title", "template", "caption", "hashtags",
            "image_source", "image_url", "link_url", "scheduled_for", "status",
            "published_at", "can_edit", "posts", "created_at", "updated_at",
        )
        read_only_fields = fields

    def get_can_edit(self, obj) -> bool:
        """Once anything has reached a network the copy is public; editing the
        row after that would only make our record disagree with what people can
        already read."""
        return obj.status in Campaign.EDITABLE_STATUSES


class CampaignCreateSerializer(serializers.Serializer):
    """Body of ``POST /social/campaigns/`` — the whole wizard, submitted.

    ``publish_at`` absent or null is "Publish now"; a timestamp is "Schedule for
    later". Those are the two radio buttons in step 4 and nothing else
    distinguishes them.
    """

    course = serializers.SlugRelatedField(
        slug_field="slug", queryset=Course.objects.all()
    )
    template = serializers.ChoiceField(choices=promotions.TEMPLATE_CHOICES)
    caption = serializers.CharField(max_length=6000)
    hashtags = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=255
    )
    image_source = serializers.ChoiceField(
        choices=Campaign.ImageSource.choices,
        required=False,
        default=Campaign.ImageSource.THUMBNAIL,
    )
    accounts = serializers.PrimaryKeyRelatedField(
        queryset=SocialAccount.objects.all(), many=True, allow_empty=False
    )
    publish_at = serializers.DateTimeField(
        required=False, allow_null=True, default=None
    )

    def validate_course(self, course):
        user = self.context["request"].user
        if getattr(user, "role", None) != "admin" and course.trainer_id != user.id:
            raise serializers.ValidationError("That isn't your course.")
        return course

    def validate_caption(self, caption):
        if not caption.strip():
            raise serializers.ValidationError("Write something to post.")
        return caption.strip()

    def validate_image_source(self, source):
        if (
            source == Campaign.ImageSource.PROMO_CARD
            and not promotions.PROMO_CARD_AVAILABLE
        ):
            raise serializers.ValidationError(
                "Auto promo cards aren't available on this server yet. Use the "
                "course thumbnail, or post without an image."
            )
        return source

    def validate_accounts(self, accounts):
        """Only your own destinations, only ones that can actually post today."""
        user = self.context["request"].user
        seen = set()
        for account in accounts:
            if account.user_id != user.id:
                raise serializers.ValidationError(
                    "One of those accounts isn't connected to your login."
                )
            if account.pk in seen:
                raise serializers.ValidationError(
                    "The same account is listed twice."
                )
            seen.add(account.pk)

            adapter = providers.adapter_for(account.provider)
            if adapter is None or not hasattr(adapter, "publish"):
                raise serializers.ValidationError(
                    "Publishing to {} isn't supported yet.".format(
                        account.get_provider_display()
                    )
                )
            if not account.is_usable:
                raise serializers.ValidationError(
                    "{} needs to be reconnected before it can post.".format(
                        account.display_name or account.get_provider_display()
                    )
                )
        return accounts

    def validate_publish_at(self, when):
        return _validate_schedule(when)

    def validate(self, attrs):
        """Cross-field rules the networks would otherwise enforce for us."""
        facts = promotions.course_facts(attrs["course"])
        image_url = promotions.resolve_image(facts, attrs["image_source"])

        wants_instagram = any(
            a.provider == Provider.INSTAGRAM for a in attrs["accounts"]
        )
        if wants_instagram and not image_url:
            raise serializers.ValidationError(
                {
                    "accounts": "Instagram requires an image on every post. Add a "
                    "course thumbnail, or drop Instagram from this campaign."
                }
            )
        if attrs["image_source"] == Campaign.ImageSource.THUMBNAIL and not image_url:
            raise serializers.ValidationError(
                {"image_source": "This course has no cover image to post."}
            )

        attrs["_image_url"] = image_url
        attrs["_link_url"] = facts.link_url
        return attrs


class CampaignUpdateSerializer(serializers.Serializer):
    """Body of ``PATCH /social/campaigns/{id}/`` — edit or move a scheduled post.

    Destinations are not editable here on purpose. Changing them means
    reconciling posts that may already have been claimed by a sweep, and
    "cancel and recreate" is both clearer to the trainer and impossible to get
    subtly wrong.
    """

    caption = serializers.CharField(required=False, max_length=6000)
    hashtags = serializers.CharField(
        required=False, allow_blank=True, max_length=255
    )
    publish_at = serializers.DateTimeField(required=False, allow_null=True)

    def validate_caption(self, caption):
        if not caption.strip():
            raise serializers.ValidationError("Write something to post.")
        return caption.strip()

    def validate_publish_at(self, when):
        return _validate_schedule(when)
