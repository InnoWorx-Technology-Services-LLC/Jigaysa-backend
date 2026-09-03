"""Password change, active sessions and two-factor login.

The load-bearing properties: changing a password must require the old one and
must end other sessions, and turning two-factor on must not be possible without
proving the phone actually receives the code.
"""

import pytest
from django.core.cache import cache
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.token_blacklist.models import (
    BlacklistedToken,
    OutstandingToken,
)

from accounts.models import Role, User

pytestmark = pytest.mark.django_db

LOGIN = "/api/v1/auth/login/"
CHANGE = "/api/v1/auth/change-password/"
SESSIONS = "/api/v1/auth/sessions/"
TWOFA = "/api/v1/auth/2fa/"
TWOFA_CONFIRM = "/api/v1/auth/2fa/confirm/"
OTP_VERIFY = "/api/v1/auth/otp/verify/"

PASSWORD = "StrongPass123!"


def _api(user=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def user():
    return User.objects.create_user(
        email="sec@example.com", password=PASSWORD, role=Role.STUDENT,
        phone="+919876543210",
    )


def _login(email=None, password=PASSWORD):
    return APIClient().post(
        LOGIN, {"email": email or "sec@example.com", "password": password},
        format="json",
    )


# --------------------------------------------------------------------------- #
# Change password
# --------------------------------------------------------------------------- #


def test_changing_a_password_requires_the_old_one(user):
    resp = _api(user).post(
        CHANGE,
        {"current_password": "not-it", "new_password": "AnotherPass456!"},
        format="json",
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    user.refresh_from_db()
    assert user.check_password(PASSWORD)


def test_a_correct_change_takes_effect(user):
    resp = _api(user).post(
        CHANGE,
        {"current_password": PASSWORD, "new_password": "AnotherPass456!"},
        format="json",
    )
    assert resp.status_code == status.HTTP_200_OK

    user.refresh_from_db()
    assert user.check_password("AnotherPass456!")
    assert _login(password=PASSWORD).status_code == status.HTTP_401_UNAUTHORIZED
    assert _login(password="AnotherPass456!").status_code == status.HTTP_200_OK


def test_changing_a_password_ends_other_sessions(user):
    """Someone changing a password usually suspects a device they don't hold."""
    _login()
    _login()
    assert OutstandingToken.objects.filter(user=user).count() == 2

    resp = _api(user).post(
        CHANGE,
        {"current_password": PASSWORD, "new_password": "AnotherPass456!"},
        format="json",
    )
    assert resp.data["sessions_ended"] == 2
    assert BlacklistedToken.objects.count() == 2


def test_a_weak_new_password_is_refused(user):
    resp = _api(user).post(
        CHANGE,
        {"current_password": PASSWORD, "new_password": "123"},
        format="json",
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_reusing_the_same_password_is_refused(user):
    resp = _api(user).post(
        CHANGE,
        {"current_password": PASSWORD, "new_password": PASSWORD},
        format="json",
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_anonymous_cannot_change_a_password():
    resp = _api().post(
        CHANGE,
        {"current_password": PASSWORD, "new_password": "AnotherPass456!"},
        format="json",
    )
    assert resp.status_code in (
        status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN
    )


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #


def test_each_login_shows_up_as_a_session(user):
    _login()
    _login()

    resp = _api(user).get(SESSIONS)
    assert resp.status_code == status.HTTP_200_OK
    assert set(resp.data) >= {"count", "next", "previous", "results"}
    assert resp.data["count"] == 2
    assert resp.data["results"][0]["expires_at"]


def test_one_session_can_be_ended(user):
    _login()
    _login()
    session_id = _api(user).get(SESSIONS).data["results"][0]["id"]

    resp = _api(user).delete(f"{SESSIONS}{session_id}/")
    assert resp.status_code == status.HTTP_204_NO_CONTENT
    assert _api(user).get(SESSIONS).data["count"] == 1


def test_you_cannot_end_someone_elses_session(user):
    _login()
    mine = _api(user).get(SESSIONS).data["results"][0]["id"]

    other = User.objects.create_user(email="other@example.com", password=PASSWORD)
    resp = _api(other).delete(f"{SESSIONS}{mine}/")
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert _api(user).get(SESSIONS).data["count"] == 1


def test_sign_out_everywhere(user):
    _login()
    _login()
    resp = _api(user).delete(SESSIONS)
    assert resp.data["sessions_ended"] == 2
    assert _api(user).get(SESSIONS).data["count"] == 0


# --------------------------------------------------------------------------- #
# Two-factor
# --------------------------------------------------------------------------- #


def _sent_code(phone="+919876543210"):
    from accounts.security import otp_cache_key

    return cache.get(otp_cache_key(phone))["code"]


def test_two_factor_starts_off_and_does_not_change_login(user):
    status_resp = _api(user).get(TWOFA)
    assert status_resp.data["enabled"] is False
    assert status_resp.data["can_enable"] is True
    assert status_resp.data["phone_hint"].endswith("3210")

    # Everyone who has not opted in sees the login contract unchanged.
    login = _login()
    assert "access" in login.data
    assert "otp_required" not in login.data


def test_enabling_needs_the_code_to_come_back(user):
    started = _api(user).post(TWOFA)
    assert started.status_code == status.HTTP_200_OK
    # Not on yet — sending a code is not proof the phone received it.
    user.refresh_from_db()
    assert user.two_factor_enabled is False

    confirmed = _api(user).post(
        TWOFA_CONFIRM, {"code": _sent_code()}, format="json"
    )
    assert confirmed.status_code == status.HTTP_200_OK
    assert confirmed.data["enabled"] is True
    user.refresh_from_db()
    assert user.two_factor_enabled is True


def test_a_wrong_confirmation_code_does_not_enable_it(user):
    _api(user).post(TWOFA)
    resp = _api(user).post(TWOFA_CONFIRM, {"code": "000000"}, format="json")
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    user.refresh_from_db()
    assert user.two_factor_enabled is False


def test_an_account_with_no_phone_cannot_enable_it():
    no_phone = User.objects.create_user(
        email="nophone@example.com", password=PASSWORD
    )
    assert _api(no_phone).get(TWOFA).data["can_enable"] is False
    assert _api(no_phone).post(TWOFA).status_code == status.HTTP_400_BAD_REQUEST


def test_login_withholds_tokens_once_two_factor_is_on(user):
    user.two_factor_enabled = True
    user.save(update_fields=["two_factor_enabled"])

    resp = _login()
    assert resp.status_code == status.HTTP_200_OK
    # The password was right; it is simply no longer sufficient.
    assert resp.data["otp_required"] is True
    assert "access" not in resp.data
    assert resp.data["phone_hint"].endswith("3210")

    # The existing OTP endpoint completes the sign-in.
    finish = APIClient().post(
        OTP_VERIFY,
        {"phone": user.phone, "code": _sent_code()},
        format="json",
    )
    assert finish.status_code == status.HTTP_200_OK
    assert "access" in finish.data


def test_a_wrong_password_still_fails_before_any_code_is_sent(user):
    user.two_factor_enabled = True
    user.save(update_fields=["two_factor_enabled"])

    from accounts.security import otp_cache_key

    assert _login(password="wrong").status_code == status.HTTP_401_UNAUTHORIZED
    assert cache.get(otp_cache_key(user.phone)) is None


def test_disabling_needs_the_password_not_a_code(user):
    """A lost phone is exactly when someone needs to turn this off."""
    user.two_factor_enabled = True
    user.save(update_fields=["two_factor_enabled"])

    refused = _api(user).delete(TWOFA, {"password": "wrong"}, format="json")
    assert refused.status_code == status.HTTP_400_BAD_REQUEST

    ok = _api(user).delete(TWOFA, {"password": PASSWORD}, format="json")
    assert ok.status_code == status.HTTP_200_OK
    assert ok.data["enabled"] is False

    user.refresh_from_db()
    assert user.two_factor_enabled is False
    assert "access" in _login().data


def test_enabling_twice_is_a_conflict(user):
    user.two_factor_enabled = True
    user.save(update_fields=["two_factor_enabled"])
    assert _api(user).post(TWOFA).status_code == status.HTTP_409_CONFLICT
