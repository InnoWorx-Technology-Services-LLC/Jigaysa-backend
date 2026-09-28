"""Trainer payout bank details: encryption at rest, masking, admin reveal."""

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Role, TrainerProfile
from payments.models import TrainerPayout

User = get_user_model()

pytestmark = pytest.mark.django_db

URL = "/api/v1/trainer/earnings/bank-account/"
ACCOUNT = "123456783333"

VALID = {
    "bank_name": "HDFC Bank",
    "account_number": ACCOUNT,
    "ifsc": "HDFC0001234",
    "account_type": "Savings",
    "account_holder": "Dr. Kapoor",
}


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def trainer(db):
    return User.objects.create_user(
        email="trainer@example.com", password="StrongPass123!", role=Role.TRAINER
    )


@pytest.fixture
def admin(db):
    return User.objects.create_user(
        email="admin@example.com", password="StrongPass123!", role=Role.ADMIN
    )


def auth(api, user):
    api.force_authenticate(user=user)
    return api


# --- saving -----------------------------------------------------------------


def test_trainer_saves_full_account_and_ifsc(api, trainer):
    auth(api, trainer)
    resp = api.put(URL, VALID, format="json")
    assert resp.status_code == status.HTTP_200_OK

    profile = TrainerProfile.objects.get(user=trainer)
    assert profile.payout_account_number == ACCOUNT
    assert profile.payout_ifsc == "HDFC0001234"
    assert profile.is_set if hasattr(profile, "is_set") else True


def test_last4_is_derived_from_the_number(api, trainer):
    auth(api, trainer)
    api.put(URL, VALID, format="json")
    profile = TrainerProfile.objects.get(user=trainer)
    assert profile.payout_account_last4 == "3333"


def test_last4_follows_a_changed_number(api, trainer):
    auth(api, trainer)
    api.put(URL, VALID, format="json")
    api.put(URL, {**VALID, "account_number": "999988887777"}, format="json")
    profile = TrainerProfile.objects.get(user=trainer)
    assert profile.payout_account_last4 == "7777"


# --- encryption at rest -----------------------------------------------------


def test_account_number_is_ciphertext_in_the_database(api, trainer):
    auth(api, trainer)
    api.put(URL, VALID, format="json")

    with connection.cursor() as cur:
        cur.execute(
            "SELECT payout_account_number FROM accounts_trainerprofile "
            "WHERE user_id = %s",
            [trainer.id],
        )
        stored = cur.fetchone()[0]

    assert stored.startswith("enc:v1:")
    assert ACCOUNT not in stored
    # …and still reads back as plaintext through the ORM.
    assert TrainerProfile.objects.get(user=trainer).payout_account_number == ACCOUNT


def test_ifsc_is_not_encrypted(api, trainer):
    """A branch code is public. Encrypting it would only make it unsearchable."""
    auth(api, trainer)
    api.put(URL, VALID, format="json")
    with connection.cursor() as cur:
        cur.execute(
            "SELECT payout_ifsc FROM accounts_trainerprofile WHERE user_id = %s",
            [trainer.id],
        )
        assert cur.fetchone()[0] == "HDFC0001234"


# --- the trainer never reads the number back --------------------------------


def test_response_never_returns_the_account_number(api, trainer):
    auth(api, trainer)
    put = api.put(URL, VALID, format="json")
    get = api.get(URL)

    for resp in (put, get):
        assert "account_number" not in resp.data
        assert ACCOUNT not in str(resp.data)
        assert resp.data["account_last4"] == "3333"
        assert resp.data["ifsc"] == "HDFC0001234"


def test_card_reports_is_set(api, trainer):
    auth(api, trainer)
    assert api.get(URL).data["is_set"] is False
    api.put(URL, VALID, format="json")
    assert api.get(URL).data["is_set"] is True


# --- validation -------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["HDFC001234", "hdfc0001234x", "HDFC1001234", "12340001234", "HDFC000123"],
)
def test_malformed_ifsc_is_rejected(api, trainer, bad):
    auth(api, trainer)
    resp = api.put(URL, {**VALID, "ifsc": bad}, format="json")
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_ifsc_is_upper_cased(api, trainer):
    auth(api, trainer)
    api.put(URL, {**VALID, "ifsc": "hdfc0001234"}, format="json")
    assert TrainerProfile.objects.get(user=trainer).payout_ifsc == "HDFC0001234"


def test_spaces_in_the_account_number_are_stripped(api, trainer):
    auth(api, trainer)
    api.put(URL, {**VALID, "account_number": "1234 5678 3333"}, format="json")
    assert TrainerProfile.objects.get(user=trainer).payout_account_number == ACCOUNT


@pytest.mark.parametrize("bad", ["12345678ABCD", "1234", "12345678", ""])
def test_malformed_account_number_is_rejected(api, trainer, bad):
    auth(api, trainer)
    resp = api.put(URL, {**VALID, "account_number": bad}, format="json")
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


# --- deleting ---------------------------------------------------------------


def test_delete_clears_the_stored_number(api, trainer):
    """A trainer removing their details must not leave the number behind."""
    auth(api, trainer)
    api.put(URL, VALID, format="json")
    resp = api.delete(URL)
    assert resp.status_code == status.HTTP_200_OK

    profile = TrainerProfile.objects.get(user=trainer)
    assert profile.payout_account_number == ""
    assert profile.payout_ifsc == ""
    assert profile.payout_account_last4 == ""
    assert resp.data["is_set"] is False


# --- admin reveal -----------------------------------------------------------


def test_admin_payout_row_carries_the_full_details(api, trainer, admin):
    auth(api, trainer)
    api.put(URL, VALID, format="json")
    payout = TrainerPayout.objects.create(trainer=trainer, gross=100, net=70)

    auth(api, admin)
    resp = api.get(f"/api/v1/admin/payouts/{payout.id}/")
    assert resp.status_code == status.HTTP_200_OK
    bank = resp.data["bank_account"]
    assert bank["account_number"] == ACCOUNT
    assert bank["ifsc"] == "HDFC0001234"
    assert bank["account_holder"] == "Dr. Kapoor"


def test_payout_for_a_trainer_with_no_details_is_not_an_error(api, trainer, admin):
    payout = TrainerPayout.objects.create(trainer=trainer, gross=100, net=70)
    auth(api, admin)
    resp = api.get(f"/api/v1/admin/payouts/{payout.id}/")
    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["bank_account"]["is_set"] is False


def test_trainers_cannot_reach_the_admin_payout_queue(api, trainer):
    TrainerPayout.objects.create(trainer=trainer, gross=100, net=70)
    auth(api, trainer)
    assert api.get("/api/v1/admin/payouts/").status_code in (
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
    )
