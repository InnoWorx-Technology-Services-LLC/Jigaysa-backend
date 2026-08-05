"""Trainer onboarding → mentor approval (PRD §2.1).

Approval is what makes a trainer discoverable at ``GET /mentors/``. Before this
existed the flag could only be set in a database shell, so the mentor list was
permanently empty in production — these tests pin the whole path shut.
"""

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Role, TrainerProfile, User

pytestmark = pytest.mark.django_db


@pytest.fixture
def trainer():
    return User.objects.create_user(
        email="approve-t@example.com", password="StrongPass123!",
        role=Role.TRAINER, full_name="Dr. Kapoor",
    )


@pytest.fixture
def admin():
    return User.objects.create_user(
        email="approve-a@example.com", password="StrongPass123!", role=Role.ADMIN
    )


@pytest.fixture
def student():
    return User.objects.create_user(
        email="approve-s@example.com", password="StrongPass123!", role=Role.STUDENT
    )


def _api(user=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


def test_registering_as_a_trainer_creates_the_profile(trainer):
    """Nothing used to create this row, so nobody could ever be approved."""
    assert TrainerProfile.objects.filter(user=trainer).exists()
    assert trainer.trainer_profile.is_approved is False  # approval is earned


def test_students_get_no_trainer_profile(student):
    assert not TrainerProfile.objects.filter(user=student).exists()


def test_trainer_edits_their_own_rate_but_cannot_self_approve(trainer):
    resp = _api(trainer).patch(
        "/api/v1/trainer-profiles/me/",
        {"expertise": "React", "hourly_rate": "1500.00", "is_approved": True},
        format="json",
    )
    assert resp.status_code == status.HTTP_200_OK
    trainer.trainer_profile.refresh_from_db()
    assert trainer.trainer_profile.expertise == "React"
    assert str(trainer.trainer_profile.hourly_rate) == "1500.00"
    # is_approved is read-only — the payload must not have granted it.
    assert trainer.trainer_profile.is_approved is False


def test_admin_approves_and_the_trainer_is_notified(trainer, admin):
    profile = trainer.trainer_profile
    resp = _api(admin).post(f"/api/v1/trainer-profiles/{profile.id}/approve/")

    assert resp.status_code == status.HTTP_200_OK
    profile.refresh_from_db()
    assert profile.is_approved is True
    assert trainer.notifications.filter(title__startswith="You're approved").exists()


def test_approval_makes_the_trainer_appear_in_the_mentor_list(
    trainer, admin, student
):
    """The end-to-end point of the whole feature."""
    profile = trainer.trainer_profile
    profile.hourly_rate = 1500
    profile.save(update_fields=["hourly_rate"])

    assert _api(student).get("/api/v1/mentors/").data["results"] == []

    _api(admin).post(f"/api/v1/trainer-profiles/{profile.id}/approve/")

    mentors = _api(student).get("/api/v1/mentors/").data["results"]
    assert [m["full_name"] for m in mentors] == ["Dr. Kapoor"]


def test_unapprove_removes_them_from_the_mentor_list(trainer, admin, student):
    profile = trainer.trainer_profile
    _api(admin).post(f"/api/v1/trainer-profiles/{profile.id}/approve/")
    _api(admin).post(f"/api/v1/trainer-profiles/{profile.id}/unapprove/")

    assert _api(student).get("/api/v1/mentors/").data["results"] == []


def test_only_admins_can_approve(trainer, student):
    profile = trainer.trainer_profile
    assert _api(trainer).post(
        f"/api/v1/trainer-profiles/{profile.id}/approve/"
    ).status_code == status.HTTP_403_FORBIDDEN
    assert _api(student).post(
        f"/api/v1/trainer-profiles/{profile.id}/approve/"
    ).status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)


def test_admin_sees_the_pending_queue(trainer, admin):
    resp = _api(admin).get("/api/v1/trainer-profiles/?is_approved=false")
    assert resp.status_code == status.HTTP_200_OK
    assert [p["email"] for p in resp.data["results"]] == [trainer.email]


def test_a_trainer_only_sees_their_own_profile(trainer, admin):
    other = User.objects.create_user(
        email="other-t@example.com", password="StrongPass123!", role=Role.TRAINER
    )
    resp = _api(trainer).get("/api/v1/trainer-profiles/")
    assert [p["email"] for p in resp.data["results"]] == [trainer.email]
    assert other.email not in str(resp.data)
