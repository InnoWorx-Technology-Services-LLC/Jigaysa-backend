import random

from django.contrib.auth.tokens import default_token_generator
from django.core.cache import cache
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from rest_framework import generics, mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from accounts.models import LoginActivity, TrainerProfile, User
from accounts.providers import get_sms_provider
from accounts.serializers import (
    LogoutSerializer,
    OTPRequestSerializer,
    OTPVerifySerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    RegisterSerializer,
    TokenPairSerializer,
    TrainerProfileSerializer,
    UserSerializer,
)
from notifications.models import NotificationCategory
from notifications.services import notify


def _is_admin(user):
    return getattr(user, "role", None) == "admin"

# OTP settings (kept simple; move to settings for prod tuning).
OTP_TTL_SECONDS = 300
OTP_MAX_ATTEMPTS = 5


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


class RegisterView(generics.CreateAPIView):
    """POST /auth/register/ — email/password signup."""

    serializer_class = RegisterSerializer
    permission_classes = [AllowAny]
    api_roles = ("public",)


class LoginView(TokenObtainPairView):
    """POST /auth/login/ — JWT obtain; records a LoginActivity row."""

    serializer_class = TokenPairSerializer
    permission_classes = [AllowAny]
    api_roles = ("public",)

    def post(self, request, *args, **kwargs):
        email = request.data.get("email", "")
        common = {
            "email_attempted": email,
            "ip_address": _client_ip(request),
            "user_agent": request.META.get("HTTP_USER_AGENT", ""),
        }
        try:
            response = super().post(request, *args, **kwargs)
        except Exception:
            LoginActivity.objects.create(success=False, **common)
            raise

        from accounts.models import User

        user = User.objects.filter(email=email).first()
        LoginActivity.objects.create(user=user, success=True, **common)
        return response


class LogoutView(APIView):
    """POST /auth/logout/ — blacklist the supplied refresh token."""

    permission_classes = [IsAuthenticated]
    serializer_class = LogoutSerializer
    api_roles = ("student", "trainer", "admin", "institution")

    def post(self, request):
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            RefreshToken(serializer.validated_data["refresh"]).blacklist()
        except TokenError:
            return Response(
                {"detail": "Invalid or expired refresh token."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(status=status.HTTP_205_RESET_CONTENT)


class MeView(generics.RetrieveUpdateAPIView):
    """GET/PATCH /auth/me/ — the authenticated user's profile."""

    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated]
    api_roles = ("student", "trainer", "admin", "institution")

    def get_object(self):
        return self.request.user


# --- Scaffolded endpoints (Phase 2+). Contracts exist; logic is pending. ---


class _NotImplementedView(APIView):
    """Base for scaffolded auth flows: returns 501 with a clear message.

    Provider hooks live in accounts/providers.py. Replace these with real
    implementations in a later phase.
    """

    permission_classes = [AllowAny]
    api_roles = ("public",)
    feature = "This endpoint"

    def post(self, request, *args, **kwargs):
        return Response(
            {"detail": f"{self.feature} is not implemented yet (planned for a later phase)."},
            status=status.HTTP_501_NOT_IMPLEMENTED,
        )


class SocialLoginView(_NotImplementedView):
    # Social / SSO needs a configured OAuth provider (Google/etc.); left
    # scaffolded until credentials + a SocialProvider implementation are wired.
    feature = "Social / SSO login"


# --- Mobile OTP login (PRD §3.1) -------------------------------------------


def _otp_cache_key(phone):
    return f"otp:{phone}"


class OTPRequestView(APIView):
    """POST /auth/otp/request/ — send a login OTP to a mobile number."""

    permission_classes = [AllowAny]
    serializer_class = OTPRequestSerializer
    api_roles = ("public",)

    def post(self, request):
        serializer = OTPRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        phone = serializer.validated_data["phone"]
        code = f"{random.randint(0, 999999):06d}"
        cache.set(
            _otp_cache_key(phone),
            {"code": code, "attempts": 0},
            timeout=OTP_TTL_SECONDS,
        )
        # Only dispatch to a real account's number if one exists, but never
        # reveal whether it does (anti-enumeration).
        if User.objects.filter(phone=phone).exists():
            get_sms_provider().send_otp(phone, code)
        return Response(
            {"detail": "If that number is registered, an OTP has been sent."}
        )


class OTPVerifyView(APIView):
    """POST /auth/otp/verify/ — verify the OTP and issue JWT tokens."""

    permission_classes = [AllowAny]
    serializer_class = OTPVerifySerializer
    api_roles = ("public",)

    def post(self, request):
        serializer = OTPVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        phone = serializer.validated_data["phone"]
        code = serializer.validated_data["code"]

        key = _otp_cache_key(phone)
        entry = cache.get(key)
        if not entry:
            return Response(
                {"detail": "OTP expired or not requested. Request a new one."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if entry["attempts"] >= OTP_MAX_ATTEMPTS:
            cache.delete(key)
            return Response(
                {"detail": "Too many attempts. Request a new OTP."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        if code != entry["code"]:
            entry["attempts"] += 1
            cache.set(key, entry, timeout=OTP_TTL_SECONDS)
            return Response(
                {"detail": "Incorrect OTP."}, status=status.HTTP_400_BAD_REQUEST
            )

        user = User.objects.filter(phone=phone).first()
        if user is None:
            return Response(
                {"detail": "No account is registered with this number."},
                status=status.HTTP_404_NOT_FOUND,
            )
        cache.delete(key)
        if not user.phone_verified:
            user.phone_verified = True
            user.save(update_fields=["phone_verified"])
        refresh = RefreshToken.for_user(user)
        refresh["role"] = user.role
        refresh["email"] = user.email
        return Response(
            {
                "refresh": str(refresh),
                "access": str(refresh.access_token),
                "user": UserSerializer(user).data,
            }
        )


# --- Password reset (PRD §3.1) ---------------------------------------------


class PasswordResetRequestView(APIView):
    """POST /auth/password-reset/ — issue a uid+token for a known email.

    Always returns 200 (never reveals whether the email exists). The token is
    delivered out-of-band (email); in dev it's printed to the console.
    """

    permission_classes = [AllowAny]
    serializer_class = PasswordResetRequestSerializer
    api_roles = ("public",)

    def post(self, request):
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]
        user = User.objects.filter(email=email, is_active=True).first()
        if user is not None:
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            token = default_token_generator.make_token(user)
            print(f"[PasswordReset] {email} → uid={uid} token={token}")
        return Response(
            {"detail": "If that email is registered, a reset link has been sent."}
        )


class PasswordResetConfirmView(APIView):
    """POST /auth/password-reset/confirm/ — set a new password with uid+token."""

    permission_classes = [AllowAny]
    serializer_class = PasswordResetConfirmSerializer
    api_roles = ("public",)

    def post(self, request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            uid = force_str(urlsafe_base64_decode(data["uid"]))
            user = User.objects.get(pk=uid)
        except (User.DoesNotExist, ValueError, TypeError, OverflowError):
            return Response(
                {"detail": "Invalid reset link."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not default_token_generator.check_token(user, data["token"]):
            return Response(
                {"detail": "This reset link is invalid or has expired."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        user.set_password(data["new_password"])
        user.save(update_fields=["password"])
        return Response({"detail": "Password has been reset. You can now log in."})


class TrainerProfileViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Trainer teaching profiles and the **admin approval gate** (PRD §2.1
    "Trainer onboarding").

    A trainer only becomes bookable as a mentor once an admin approves them —
    ``GET /mentors/`` filters on exactly this flag. Before this existed the flag
    could only be flipped in a database shell, which meant the mentor list was
    permanently empty in production.

    * trainers see and edit **only their own** profile (rate, expertise)
    * admins see every profile and are the only ones who can approve
    """

    serializer_class = TrainerProfileSerializer
    permission_classes = [IsAuthenticated]
    api_roles = ("trainer", "admin")
    api_roles_by_action = {
        "list": ("admin",),
        "approve": ("admin",),
        "unapprove": ("admin",),
    }

    def get_queryset(self):
        qs = TrainerProfile.objects.select_related("user").order_by(
            "is_approved", "-created_at"
        )
        if _is_admin(self.request.user):
            pending = self.request.query_params.get("is_approved")
            if pending is not None:
                qs = qs.filter(is_approved=pending.lower() in ("1", "true", "yes"))
            return qs
        return qs.filter(user=self.request.user)

    def perform_update(self, serializer):
        if (
            serializer.instance.user_id != self.request.user.id
            and not _is_admin(self.request.user)
        ):
            raise PermissionDenied("You can only edit your own trainer profile.")
        serializer.save()

    @action(detail=False, methods=["get", "patch"])
    def me(self, request):
        """The caller's own profile, created on first read if missing."""
        if getattr(request.user, "role", None) not in ("trainer", "admin"):
            raise PermissionDenied("Only trainers have a teaching profile.")
        profile, _ = TrainerProfile.objects.get_or_create(user=request.user)
        if request.method == "GET":
            return Response(self.get_serializer(profile).data)
        serializer = self.get_serializer(profile, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def _set_approved(self, request, approved):
        if not _is_admin(request.user):
            raise PermissionDenied("Only an admin can change trainer approval.")
        profile = self.get_object()
        profile.is_approved = approved
        profile.save(update_fields=["is_approved", "updated_at"])
        notify(
            profile.user,
            NotificationCategory.SYSTEM,
            title=(
                "You're approved as a mentor 🎉" if approved
                else "Mentor approval withdrawn"
            ),
            body=(
                "Students can now find you and book 1:1 sessions."
                if approved
                else "Your profile is no longer listed for 1:1 bookings."
            ),
            link="/trainer/settings",
        )
        return Response(self.get_serializer(profile).data)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        """Admin: make this trainer discoverable and bookable as a mentor."""
        return self._set_approved(request, True)

    @action(detail=True, methods=["post"])
    def unapprove(self, request, pk=None):
        """Admin: remove them from the mentor list. Existing bookings stand."""
        return self._set_approved(request, False)
