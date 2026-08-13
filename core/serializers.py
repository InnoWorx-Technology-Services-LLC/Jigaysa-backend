"""Serializers for the media-upload presign endpoints and platform settings."""

from rest_framework import serializers

from core.models import PlatformSetting
from core.storage import UPLOAD_PURPOSES


class PresignUploadSerializer(serializers.Serializer):
    filename = serializers.CharField(max_length=255)
    content_type = serializers.CharField(max_length=127)
    purpose = serializers.ChoiceField(choices=sorted(UPLOAD_PURPOSES))


class PresignDownloadSerializer(serializers.Serializer):
    key = serializers.CharField(max_length=1024)


# --- platform settings ------------------------------------------------------ #


def _tail(secret: str) -> str:
    """A hint that a secret is set, without being enough to use it."""
    return f"••••{secret[-4:]}" if len(secret) > 4 else ("••••" if secret else "")


class PlatformSettingSerializer(serializers.ModelSerializer):
    """Admin read/write of the whole settings row.

    The two secrets are **write-only**. A settings screen that reads its own
    secrets back turns one stolen admin session into a stolen payment gateway,
    and it buys nothing: an admin who needs to change a key is pasting a new one
    from the Razorpay dashboard, not reading the old one off the form. What the
    UI gets instead is ``*_set`` booleans and a last-four hint, which is enough
    to render "configured ••••a1b2" and a Change button.

    Sending ``""`` for a secret clears it; omitting the field leaves it alone —
    so a PATCH of the GST rate cannot wipe your gateway credentials by accident.
    """

    razorpay_key_secret = serializers.CharField(
        write_only=True, required=False, allow_blank=True, trim_whitespace=True
    )
    razorpay_webhook_secret = serializers.CharField(
        write_only=True, required=False, allow_blank=True, trim_whitespace=True
    )
    razorpay_key_secret_set = serializers.SerializerMethodField()
    razorpay_webhook_secret_set = serializers.SerializerMethodField()
    razorpay_key_secret_hint = serializers.SerializerMethodField()
    razorpay_key_source = serializers.SerializerMethodField()
    gateway_configured = serializers.SerializerMethodField()
    flags = serializers.SerializerMethodField()
    enforced_flags = serializers.SerializerMethodField()

    class Meta:
        model = PlatformSetting
        fields = (
            "platform_name", "support_email", "default_currency",
            "gst_percent", "platform_commission_percent",
            # key id is public — it ships to the browser to open Checkout
            "razorpay_key_id",
            "razorpay_key_secret", "razorpay_webhook_secret",
            "razorpay_key_secret_set", "razorpay_webhook_secret_set",
            "razorpay_key_secret_hint", "razorpay_key_source",
            "gateway_configured",
            "course_approval_required", "trainer_self_onboarding",
            "allow_coupon_codes", "smart_classroom_module", "ai_suggestions",
            "container_classrooms",
            "flags", "enforced_flags", "updated_at",
        )
        read_only_fields = ("updated_at",)

    def get_razorpay_key_secret_set(self, obj) -> bool:
        return bool(obj.razorpay_key_secret)

    def get_razorpay_webhook_secret_set(self, obj) -> bool:
        return bool(obj.razorpay_webhook_secret)

    def get_razorpay_key_secret_hint(self, obj) -> str:
        return _tail(obj.razorpay_key_secret)

    def get_razorpay_key_source(self, obj) -> str:
        """Where the keys in force come from: ``settings``, ``environment`` or
        ``none``.

        Without this, blank credential boxes on a platform that is happily
        taking payments would read as "not configured" and invite an admin to
        "fix" working live keys.
        """
        if obj.razorpay_key_id and obj.razorpay_key_secret:
            return "settings"
        from payments import gateway  # lazy: avoids an app-load cycle

        return "environment" if gateway.is_configured() else "none"

    def get_gateway_configured(self, obj) -> bool:
        from payments import gateway  # lazy: avoids an app-load cycle

        return gateway.is_configured()

    def get_flags(self, obj) -> dict:
        return obj.flags()

    def get_enforced_flags(self, obj) -> list:
        """Which flags actually change behaviour — the rest are stored only."""
        return sorted(PlatformSetting.ENFORCED_FLAGS)


class PublicPlatformSettingSerializer(serializers.ModelSerializer):
    """The slice any caller may see, logged out included.

    Branding and feature flags only. Nothing financial: the GST rate and the
    commission split are the platform's commercial terms, and the gateway keys
    are credentials. A student needs to know whether the coupon box should
    render — not what margin the platform takes.
    """

    flags = serializers.SerializerMethodField()

    class Meta:
        model = PlatformSetting
        fields = ("platform_name", "support_email", "default_currency", "flags")

    def get_flags(self, obj) -> dict:
        return obj.flags()
