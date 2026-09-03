"""Account security: password change, active sessions, two-factor login.

Split from ``accounts.views`` because that module is the *unauthenticated* front
door — register, log in, reset a forgotten password. Everything here is a
signed-in user acting on their own account, which is a different posture: the
caller is already known, and what protects each action is re-proving they are
still the person holding the password, not that they hold a token.

### Two-factor is built on the OTP flow that already exists

Rather than add a TOTP dependency, ``/auth/otp/request/`` and
``/auth/otp/verify/`` already send and check a code against a phone number. Two
factor is that same code made *mandatory* after a correct password, for users
who opt in.

The consequence for the frontend is one branch on login: an account with 2FA on
gets ``200`` with ``otp_required: true`` **and no tokens**. The password was
right; it is simply not sufficient on its own any more.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.cache import cache
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.token_blacklist.models import (
    BlacklistedToken,
    OutstandingToken,
)

from accounts.serializers import (
    ActiveSessionSerializer,
    ChangePasswordSerializer,
    TwoFactorConfirmSerializer,
    TwoFactorStatusSerializer,
    TwoFactorToggleSerializer,
)
from core.pagination import DefaultPagination

User = get_user_model()

ALL_ROLES = ("student", "trainer", "admin", "institution")


def otp_cache_key(phone):
    """Shared with ``accounts.views`` so both halves read the same code."""
    from accounts.views import _otp_cache_key

    return _otp_cache_key(phone)


def send_login_otp(user):
    """Issue and dispatch a fresh login OTP. Returns a masked phone hint."""
    import random

    from accounts.providers import get_sms_provider
    from accounts.views import OTP_TTL_SECONDS

    code = f"{random.randint(0, 999999):06d}"
    cache.set(
        otp_cache_key(user.phone),
        {"code": code, "attempts": 0},
        timeout=OTP_TTL_SECONDS,
    )
    get_sms_provider().send_otp(user.phone, code)
    return mask_phone(user.phone)


def mask_phone(phone: str) -> str:
    """``+919876543210`` → ``•••••••3210``.

    Enough for the user to recognise which number the code went to, not enough
    for someone holding a stolen password to learn the number itself.
    """
    tail = (phone or "")[-4:]
    return f"{'•' * 7}{tail}" if tail else ""


class ChangePasswordView(APIView):
    """POST ``/auth/change-password/`` — for a user who knows their password.

    Distinct from the reset flow, which proves identity by email because the
    password is *forgotten*. Here it is known, so the current password is the
    proof — and requiring it is what stops a stolen access token being enough to
    take over an account permanently.

    Every other session is signed out on success. Someone changing a password
    usually suspects a device they no longer control.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = ChangePasswordSerializer
    api_roles = ALL_ROLES

    @extend_schema(request=ChangePasswordSerializer, responses={200: None})
    def post(self, request):
        body = ChangePasswordSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        user = request.user

        if not user.check_password(body.validated_data["current_password"]):
            return Response(
                {"current_password": ["That is not your current password."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        new_password = body.validated_data["new_password"]
        if user.check_password(new_password):
            return Response(
                {"new_password": ["That is already your password."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user.set_password(new_password)
        user.save(update_fields=["password", "updated_at"])
        revoked = _revoke_all_refresh_tokens(user)

        return Response(
            {
                "detail": "Password changed. Other devices have been signed out.",
                "sessions_ended": revoked,
            }
        )


def _revoke_all_refresh_tokens(user) -> int:
    """Blacklist every outstanding refresh token. Returns how many."""
    tokens = OutstandingToken.objects.filter(user=user)
    revoked = 0
    for token in tokens:
        _, created = BlacklistedToken.objects.get_or_create(token=token)
        revoked += 1 if created else 0
    return revoked


class ActiveSessionListView(APIView):
    """GET/DELETE ``/auth/sessions/`` — devices holding a live refresh token.

    **Paginated.** Each row is one refresh token issued at login and not yet
    blacklisted or expired — which is as close to "a session" as a stateless JWT
    API has.

    Two honest limits worth putting in the UI: an **access** token already
    issued keeps working until it expires (an hour by default) even after its
    session is revoked here, and the rows carry no device or location because
    nothing records them at issue time.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = ActiveSessionSerializer
    api_roles = ALL_ROLES

    @extend_schema(responses=ActiveSessionSerializer(many=True))
    def get(self, request):
        blacklisted = BlacklistedToken.objects.values_list("token_id", flat=True)
        sessions = (
            OutstandingToken.objects.filter(
                user=request.user, expires_at__gt=timezone.now()
            )
            .exclude(id__in=blacklisted)
            .order_by("-created_at")
        )
        paginator = DefaultPagination()
        page = paginator.paginate_queryset(sessions, request, view=self)
        return paginator.get_paginated_response(
            ActiveSessionSerializer(page, many=True).data
        )

    @extend_schema(responses={200: None})
    def delete(self, request):
        """Sign out everywhere. The caller's own session ends too."""
        return Response(
            {
                "detail": "Signed out on every device.",
                "sessions_ended": _revoke_all_refresh_tokens(request.user),
            }
        )


class ActiveSessionDetailView(APIView):
    """DELETE ``/auth/sessions/{id}/`` — end one session."""

    permission_classes = [IsAuthenticated]
    serializer_class = ActiveSessionSerializer
    api_roles = ALL_ROLES

    @extend_schema(responses={204: None})
    def delete(self, request, pk):
        token = OutstandingToken.objects.filter(
            pk=pk, user=request.user
        ).first()
        if token is None:
            return Response(
                {"detail": "No such session on this account."},
                status=status.HTTP_404_NOT_FOUND,
            )
        BlacklistedToken.objects.get_or_create(token=token)
        return Response(status=status.HTTP_204_NO_CONTENT)


class TwoFactorView(APIView):
    """GET/POST/DELETE ``/auth/2fa/`` — turn two-factor login on or off.

    Enabling is two steps on purpose: ``POST`` sends a code to the phone on the
    account, and ``/auth/2fa/confirm/`` switches it on only once that code comes
    back. Trusting the number without checking it is how someone locks
    themselves out of their own account with a typo.

    Disabling asks for the password, not a code — a phone that has been lost is
    exactly when someone needs to turn this off.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = TwoFactorStatusSerializer
    api_roles = ALL_ROLES

    @extend_schema(responses=TwoFactorStatusSerializer)
    def get(self, request):
        user = request.user
        return Response(
            TwoFactorStatusSerializer(
                {
                    "enabled": user.two_factor_enabled,
                    "method": "sms" if user.two_factor_enabled else "",
                    "phone_hint": mask_phone(user.phone),
                    "can_enable": bool(user.phone),
                }
            ).data
        )

    @extend_schema(request=None, responses=TwoFactorStatusSerializer)
    def post(self, request):
        """Start enabling: send a confirmation code to the phone on file."""
        user = request.user
        if not user.phone:
            return Response(
                {
                    "detail": "Add a mobile number to your profile first — the "
                    "code is sent there."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if user.two_factor_enabled:
            return Response(
                {"detail": "Two-factor login is already on."},
                status=status.HTTP_409_CONFLICT,
            )

        hint = send_login_otp(user)
        return Response(
            {
                "detail": f"Code sent to {hint}. Confirm it to finish turning "
                "two-factor on.",
                "phone_hint": hint,
            }
        )

    @extend_schema(
        request=TwoFactorToggleSerializer, responses=TwoFactorStatusSerializer
    )
    def delete(self, request):
        """Turn it off. Requires the account password."""
        body = TwoFactorToggleSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        user = request.user

        if not user.check_password(body.validated_data["password"]):
            return Response(
                {"password": ["That is not your password."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user.two_factor_enabled = False
        user.save(update_fields=["two_factor_enabled", "updated_at"])
        return Response(
            TwoFactorStatusSerializer(
                {
                    "enabled": False,
                    "method": "",
                    "phone_hint": mask_phone(user.phone),
                    "can_enable": bool(user.phone),
                }
            ).data
        )


class TwoFactorConfirmView(APIView):
    """POST ``/auth/2fa/confirm/`` — finish enabling with the code."""

    permission_classes = [IsAuthenticated]
    serializer_class = TwoFactorConfirmSerializer
    api_roles = ALL_ROLES

    @extend_schema(
        request=TwoFactorConfirmSerializer, responses=TwoFactorStatusSerializer
    )
    def post(self, request):
        from accounts.views import OTP_MAX_ATTEMPTS, OTP_TTL_SECONDS

        body = TwoFactorConfirmSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        user = request.user

        key = otp_cache_key(user.phone)
        entry = cache.get(key)
        if not entry:
            return Response(
                {"detail": "That code expired. Start again."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if entry["attempts"] >= OTP_MAX_ATTEMPTS:
            cache.delete(key)
            return Response(
                {"detail": "Too many attempts. Start again."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        if body.validated_data["code"] != entry["code"]:
            entry["attempts"] += 1
            cache.set(key, entry, timeout=OTP_TTL_SECONDS)
            return Response(
                {"detail": "Incorrect code."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        cache.delete(key)
        user.two_factor_enabled = True
        user.save(update_fields=["two_factor_enabled", "updated_at"])
        return Response(
            TwoFactorStatusSerializer(
                {
                    "enabled": True,
                    "method": "sms",
                    "phone_hint": mask_phone(user.phone),
                    "can_enable": True,
                }
            ).data
        )
