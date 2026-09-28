import re

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from accounts.models import Role, TrainerProfile, UserProfile

User = get_user_model()

HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
MAX_SKILLS = 30
MAX_SKILL_LENGTH = 50

# Roles a self-service registrant may pick. Privileged roles are gated and
# can only be assigned by an admin (e.g. via the admin site / future API).
SELF_REGISTRABLE_ROLES = {Role.STUDENT, Role.TRAINER}


class UserProfileSerializer(serializers.ModelSerializer):
    """The public-facing profile, same shape for every role."""

    class Meta:
        model = UserProfile
        fields = (
            "headline",
            "bio",
            "avatar",
            "location",
            "website",
            "github_url",
            "linkedin_url",
            "language",
            "timezone",
            "skills",
            "cover_color",
        )

    def validate_skills(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("Skills must be a list.")
        cleaned = []
        for skill in value:
            if not isinstance(skill, str):
                raise serializers.ValidationError("Each skill must be text.")
            skill = skill.strip()
            if not skill:
                continue
            if len(skill) > MAX_SKILL_LENGTH:
                raise serializers.ValidationError(
                    f"'{skill[:20]}…' is longer than {MAX_SKILL_LENGTH} characters."
                )
            # Case-insensitive dedupe, first spelling wins.
            if skill.casefold() not in {s.casefold() for s in cleaned}:
                cleaned.append(skill)
        if len(cleaned) > MAX_SKILLS:
            raise serializers.ValidationError(f"At most {MAX_SKILLS} skills.")
        return cleaned

    def validate_cover_color(self, value):
        value = value.strip()
        if value and not HEX_COLOR.match(value):
            raise serializers.ValidationError(
                "Use a hex colour such as #8FD14F."
            )
        return value


class UserSerializer(serializers.ModelSerializer):
    """Read/update the current user's account and profile.

    The profile page saves its whole form in one PATCH, so the profile is
    nested here rather than sitting behind a second endpoint the client would
    have to keep in step.
    """

    profile = UserProfileSerializer(required=False)

    class Meta:
        model = User
        fields = (
            "id",
            "email",
            "full_name",
            "role",
            "phone",
            "phone_verified",
            "organization",
            "is_active",
            "date_joined",
            "profile",
        )
        read_only_fields = (
            "id",
            "email",
            "role",
            "phone_verified",
            "organization",
            "is_active",
            "date_joined",
        )

    date_joined = serializers.DateTimeField(source="created_at", read_only=True)

    @staticmethod
    def _profile_for(user):
        """The profile row, with the instance's cached relation refreshed.

        ``request.user`` is a long-lived object that can carry a relation cached
        before this request — reading straight through ``user.profile`` then
        serialises a stale row, or reports one that has since been deleted.
        """
        profile, _ = UserProfile.objects.get_or_create(user=user)
        user.profile = profile
        return profile

    def to_representation(self, instance):
        self._profile_for(instance)
        return super().to_representation(instance)

    def update(self, instance, validated_data):
        profile_data = validated_data.pop("profile", None)
        user = super().update(instance, validated_data)
        if profile_data is not None:
            profile = self._profile_for(user)
            for field, value in profile_data.items():
                setattr(profile, field, value)
            profile.save()
        return user


class RegisterSerializer(serializers.ModelSerializer):
    """Email/password registration with Django password validation."""

    password = serializers.CharField(write_only=True, style={"input_type": "password"})
    role = serializers.ChoiceField(
        choices=[(r.value, r.label) for r in SELF_REGISTRABLE_ROLES],
        default=Role.STUDENT,
    )

    class Meta:
        model = User
        fields = ("id", "email", "full_name", "role", "phone", "password")

    def validate_password(self, value):
        validate_password(value)
        return value

    def validate_role(self, value):
        """Trainer signup is gated by the ``trainer_self_onboarding`` flag.

        Turned off, a platform onboards trainers by hand: an admin creates the
        account (or promotes a student), so nobody can list themselves as a
        mentor by picking a role at the signup form. Students are unaffected.
        """
        from core.models import PlatformSetting  # local: avoid an app-load cycle

        if value == Role.TRAINER and not (
            PlatformSetting.get_solo().trainer_self_onboarding
        ):
            raise serializers.ValidationError(
                "Trainer self-registration is disabled. Contact support to be "
                "onboarded as a trainer."
            )
        return value

    def create(self, validated_data):
        password = validated_data.pop("password")
        return User.objects.create_user(password=password, **validated_data)


class TokenPairSerializer(TokenObtainPairSerializer):
    """JWT login serializer that embeds role + id into the token claims."""

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["role"] = user.role
        token["email"] = user.email
        return token

    def validate(self, attrs):
        data = super().validate(attrs)
        data["user"] = UserSerializer(self.user).data
        return data


class LogoutSerializer(serializers.Serializer):
    """Accepts a refresh token to blacklist on logout."""

    refresh = serializers.CharField()


class PasswordResetRequestSerializer(serializers.Serializer):
    """Request a reset link/token for an email (PRD §3.1 password reset)."""

    email = serializers.EmailField()


class PasswordResetConfirmSerializer(serializers.Serializer):
    """Confirm a reset with the uid+token pair and a new password."""

    uid = serializers.CharField()
    token = serializers.CharField()
    new_password = serializers.CharField(
        write_only=True, style={"input_type": "password"}
    )

    def validate_new_password(self, value):
        validate_password(value)
        return value


class OTPRequestSerializer(serializers.Serializer):
    """Request a login OTP for a mobile number (PRD §3.1 mobile OTP login)."""

    phone = serializers.CharField(max_length=20)


class OTPVerifySerializer(serializers.Serializer):
    """Verify a mobile OTP and log in."""

    phone = serializers.CharField(max_length=20)
    code = serializers.CharField(max_length=6)


class TrainerProfileSerializer(serializers.ModelSerializer):
    """A trainer's teaching profile.

    ``is_approved`` is read-only here on purpose — approval is an admin decision
    made through the dedicated actions, not something a trainer can grant
    themselves by PATCHing their own profile.
    """

    user_id = serializers.IntegerField(source="user.id", read_only=True)
    full_name = serializers.CharField(source="user.full_name", read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)
    effective_revenue_share_pct = serializers.DecimalField(
        max_digits=5,
        decimal_places=2,
        read_only=True,
        help_text=(
            "The cut in force: the trainer's own rate, or the platform default "
            "when ``revenue_share_pct`` is null. Display this, not the raw field."
        ),
    )

    class Meta:
        model = TrainerProfile
        fields = (
            "id",
            "user_id",
            "full_name",
            "email",
            "expertise",
            "years_experience",
            "hourly_rate",
            "reviewed_at",
            "review_note",
            "rating_avg",
            "rating_count",
            "is_approved",
            "revenue_share_pct",
            "effective_revenue_share_pct",
            "created_at",
        )
        read_only_fields = (
            "rating_avg",
            "rating_count",
            "is_approved",
            "reviewed_at",
            "review_note",
            "revenue_share_pct",
            "created_at",
        )


# --------------------------------------------------------------------------- #
# Admin console — the Users page
# --------------------------------------------------------------------------- #


class AdminUserSerializer(serializers.ModelSerializer):
    """One row in the admin roster.

    Everything here is read-only. Role and suspension change through the
    dedicated actions, which is what lets them refuse the moves that would lock
    the platform out of itself — a plain PATCH has nowhere to put that rule.
    """

    role_label = serializers.CharField(source="get_role_display", read_only=True)
    date_joined = serializers.DateTimeField(source="created_at", read_only=True)
    status = serializers.SerializerMethodField()
    organization_name = serializers.CharField(
        source="organization.name", read_only=True, default=""
    )

    class Meta:
        model = User
        fields = (
            "id", "email", "full_name", "role", "role_label", "status",
            "is_active", "phone", "phone_verified", "organization",
            "organization_name", "date_joined", "last_login",
        )
        read_only_fields = fields

    def get_status(self, obj) -> str:
        """The word the table prints. One field, so the client doesn't reinvent
        the mapping from ``is_active`` and get it inconsistent between screens."""
        return "active" if obj.is_active else "suspended"


class UserStatsSerializer(serializers.Serializer):
    """The counters above the table."""

    total_users = serializers.IntegerField()
    trainers = serializers.IntegerField()
    students = serializers.IntegerField()
    institutions = serializers.IntegerField()
    admins = serializers.IntegerField()
    suspended = serializers.IntegerField()


class RoleChangeSerializer(serializers.Serializer):
    """Body of ``PATCH /admin/users/{id}/role/``."""

    role = serializers.ChoiceField(choices=Role.choices)


class SuspendSerializer(serializers.Serializer):
    """Body of ``POST /admin/users/{id}/suspend/``.

    ``reason`` is optional and is shown to the suspended user verbatim, so an
    admin who writes one is writing user-facing copy. Blank falls back to
    something neutral rather than an empty notification.
    """

    reason = serializers.CharField(
        required=False, allow_blank=True, max_length=500
    )


# --------------------------------------------------------------------------- #
# Account security: password change, sessions, two-factor
# --------------------------------------------------------------------------- #


class ChangePasswordSerializer(serializers.Serializer):
    """Body of ``POST /auth/change-password/``.

    The current password is required and is the whole point: without it, a
    stolen access token would be enough to take an account permanently.
    """

    current_password = serializers.CharField(
        write_only=True, style={"input_type": "password"}
    )
    new_password = serializers.CharField(
        write_only=True, style={"input_type": "password"}
    )

    def validate_new_password(self, value):
        validate_password(value)
        return value


class ActiveSessionSerializer(serializers.Serializer):
    """One live refresh token — as close to "a session" as a JWT API has.

    No device or IP: nothing records them when a token is issued, and inventing
    a "Chrome on Windows" label we cannot substantiate would be worse than
    leaving the column out.
    """

    id = serializers.IntegerField()
    created_at = serializers.DateTimeField()
    expires_at = serializers.DateTimeField()


class TwoFactorStatusSerializer(serializers.Serializer):
    """The state of two-factor on this account.

    ``can_enable`` is false without a phone number on the profile, which is the
    only prerequisite — surface it rather than letting the button fail.
    """

    enabled = serializers.BooleanField()
    method = serializers.CharField(allow_blank=True)
    phone_hint = serializers.CharField(allow_blank=True)
    can_enable = serializers.BooleanField()


class TwoFactorToggleSerializer(serializers.Serializer):
    """Body of ``DELETE /auth/2fa/`` — the password, not a code.

    Asking for a code to switch this off would be exactly wrong: a lost phone is
    when someone most needs to turn it off.
    """

    password = serializers.CharField(
        write_only=True, style={"input_type": "password"}
    )


class TwoFactorConfirmSerializer(serializers.Serializer):
    """Body of ``POST /auth/2fa/confirm/``."""

    code = serializers.CharField(min_length=4, max_length=8)
