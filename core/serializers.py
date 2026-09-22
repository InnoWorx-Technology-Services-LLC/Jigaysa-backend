"""Serializers for the media-upload presign endpoints and platform settings."""

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from core.models import Organization, PlatformSetting
from core.storage import UPLOAD_PURPOSES

User = get_user_model()


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


# --- admin console: institutions -------------------------------------------- #


class OrganizationSerializer(serializers.ModelSerializer):
    """One row of the Institutions table.

    ``member_count`` and ``course_count`` are annotated by the view, not
    computed here — a serializer that queries per row turns a 20-row page into
    41 queries, and this table exists to be scanned.
    """

    member_count = serializers.IntegerField(read_only=True)
    course_count = serializers.IntegerField(read_only=True)
    type_label = serializers.CharField(source="get_type_display", read_only=True)
    status = serializers.SerializerMethodField()

    class Meta:
        model = Organization
        fields = (
            "id", "name", "slug", "type", "type_label", "is_active", "status",
            "member_count", "course_count", "created_at", "updated_at",
        )
        read_only_fields = fields

    def get_status(self, obj) -> str:
        """The word the table prints, so two screens can't disagree about what
        to call the same row."""
        return "active" if obj.is_active else "inactive"


class OrganizationWriteSerializer(serializers.ModelSerializer):
    """Body of create and update.

    ``slug`` is deliberately absent: it is derived from the name on create and
    then **frozen**. It appears in URLs, so re-slugging on a rename would break
    every link already shared — and a rename is a label change, not a new
    tenant.
    """

    class Meta:
        model = Organization
        fields = ("name", "type", "is_active")

    def validate_name(self, value):
        name = value.strip()
        if not name:
            raise serializers.ValidationError("Give the institution a name.")
        clash = Organization.objects.filter(name__iexact=name)
        if self.instance:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError(
                "An organization with that name already exists."
            )
        return name


class OrganizationOnboardSerializer(serializers.Serializer):
    """Body of ``POST /admin/organizations/onboard/``.

    The three-call flow (create org, promote a user to ``institution``, attach
    them) needs an existing account to promote. This is the shortcut for the
    common case where the institution's admin has no account yet: one call
    that creates the org, creates their login, and links them — atomically, so
    a failure partway never leaves an org with no admin or a login with no
    org. Attaching a *second* admin, or one who already registered, is still
    the three-call flow — this endpoint always creates a brand-new user.
    """

    name = serializers.CharField(max_length=255)
    type = serializers.ChoiceField(
        choices=Organization.OrgType.choices,
        default=Organization.OrgType.INSTITUTION,
    )
    admin_email = serializers.EmailField()
    admin_full_name = serializers.CharField(max_length=255)
    admin_password = serializers.CharField(
        write_only=True, style={"input_type": "password"}
    )
    admin_phone = serializers.CharField(
        max_length=20, required=False, allow_blank=True
    )

    def validate_name(self, value):
        name = value.strip()
        if not name:
            raise serializers.ValidationError("Give the institution a name.")
        if Organization.objects.filter(name__iexact=name).exists():
            raise serializers.ValidationError(
                "An organization with that name already exists."
            )
        return name

    def validate_admin_email(self, value):
        email = value.strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError(
                "A user with that email already exists — attach them with "
                "the existing members/add/ flow instead."
            )
        return email

    def validate_admin_password(self, value):
        validate_password(value)
        return value


class OrganizationMemberSerializer(serializers.ModelSerializer):
    """A person inside an institution.

    Read-only on purpose. Membership is changed through the organisation's own
    add/remove actions, and role through the users endpoint — neither is a
    field somebody should be able to flip by PATCHing a member row.
    """

    role_label = serializers.CharField(source="get_role_display", read_only=True)

    class Meta:
        model = User
        fields = (
            "id", "email", "full_name", "role", "role_label", "is_active",
        )
        read_only_fields = fields
