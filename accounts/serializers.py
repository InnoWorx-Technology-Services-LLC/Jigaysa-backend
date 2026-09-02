from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from accounts.models import Role, TrainerProfile

User = get_user_model()

# Roles a self-service registrant may pick. Privileged roles are gated and
# can only be assigned by an admin (e.g. via the admin site / future API).
SELF_REGISTRABLE_ROLES = {Role.STUDENT, Role.TRAINER}


class UserSerializer(serializers.ModelSerializer):
    """Read/update the current user's profile. Role is read-only here."""

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
