"""The admin console's Users page (PRD §3.1).

Weighted towards the refusals, because they are the ones that turn a misclick
into a database shell: you cannot suspend or demote yourself, and the last
active admin is protected. Everything else on the page is a filter.
"""

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Role, TrainerProfile, User

pytestmark = pytest.mark.django_db

LIST_URL = "/api/v1/admin/users/"
STATS_URL = "/api/v1/admin/users/stats/"


def _api(user=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


def _user(email, role=Role.STUDENT, **kwargs):
    return User.objects.create_user(
        email=email, password="StrongPass123!", role=role, **kwargs
    )


@pytest.fixture
def admin():
    return _user("admin@jigyasa.local", Role.ADMIN, full_name="Admin User")


@pytest.fixture
def second_admin():
    return _user("admin2@jigyasa.local", Role.ADMIN)


@pytest.fixture
def student():
    return _user("riya@jigyasa.local", Role.STUDENT, full_name="Riya Sharma")


@pytest.fixture
def trainer():
    return _user("kapoor@jigyasa.local", Role.TRAINER, full_name="Dr. Kapoor")


# --------------------------------------------------------------------------- #
# Access
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("role", [Role.STUDENT, Role.TRAINER, Role.INSTITUTION])
def test_only_admins_can_open_the_roster(role):
    resp = _api(_user(f"{role}@example.com", role)).get(LIST_URL)
    assert resp.status_code == status.HTTP_403_FORBIDDEN


def test_anonymous_gets_401(admin):
    assert _api().get(LIST_URL).status_code in (
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
    )


# --------------------------------------------------------------------------- #
# The roster
# --------------------------------------------------------------------------- #


def test_the_roster_is_paginated(admin):
    for i in range(25):
        _user(f"bulk{i}@example.com")

    resp = _api(admin).get(LIST_URL)
    assert resp.status_code == status.HTTP_200_OK
    # The envelope matters as much as the rows: a client that indexes
    # resp.data[0] against a paginated endpoint breaks on the first full page.
    assert set(resp.data) >= {"count", "next", "previous", "results"}
    assert resp.data["count"] == 26
    assert len(resp.data["results"]) == 20
    assert resp.data["next"]


def test_page_size_is_client_overridable_up_to_the_cap(admin):
    for i in range(10):
        _user(f"size{i}@example.com")

    assert len(_api(admin).get(LIST_URL, {"page_size": 5}).data["results"]) == 5
    # 100 is the platform max; asking for more is clamped, not rejected.
    resp = _api(admin).get(LIST_URL, {"page_size": 500})
    assert len(resp.data["results"]) == 11


def test_search_matches_name_or_email(admin, student, trainer):
    by_name = _api(admin).get(LIST_URL, {"search": "riya"})
    assert [r["email"] for r in by_name.data["results"]] == [student.email]

    by_email = _api(admin).get(LIST_URL, {"search": "kapoor@"})
    assert [r["email"] for r in by_email.data["results"]] == [trainer.email]


def test_filter_by_role_and_status(admin, student, trainer):
    trainer.is_active = False
    trainer.save(update_fields=["is_active"])

    trainers = _api(admin).get(LIST_URL, {"role": Role.TRAINER})
    assert [r["email"] for r in trainers.data["results"]] == [trainer.email]

    suspended = _api(admin).get(LIST_URL, {"status": "suspended"})
    assert [r["email"] for r in suspended.data["results"]] == [trainer.email]

    active = _api(admin).get(LIST_URL, {"status": "active"})
    assert trainer.email not in [r["email"] for r in active.data["results"]]


def test_a_row_carries_the_status_word_the_table_prints(admin, student):
    row = next(
        r for r in _api(admin).get(LIST_URL).data["results"]
        if r["email"] == student.email
    )
    assert row["status"] == "active"
    assert row["role_label"] == "Student / Learner"
    assert row["date_joined"]


def test_stats_count_every_role(admin, student, trainer):
    suspended = _user("gone@example.com")
    suspended.is_active = False
    suspended.save(update_fields=["is_active"])

    data = _api(admin).get(STATS_URL).data
    assert data["total_users"] == 4
    assert data["students"] == 2
    assert data["trainers"] == 1
    assert data["admins"] == 1
    assert data["suspended"] == 1


# --------------------------------------------------------------------------- #
# Changing a role
# --------------------------------------------------------------------------- #


def _role_url(user):
    return f"/api/v1/admin/users/{user.pk}/role/"


def test_make_trainer_creates_an_unapproved_profile(admin, student):
    resp = _api(admin).patch(
        _role_url(student), {"role": Role.TRAINER}, format="json"
    )
    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["role"] == Role.TRAINER

    profile = TrainerProfile.objects.get(user=student)
    # Appointing a trainer and listing them as a bookable mentor are two
    # different decisions; the second still needs an admin.
    assert profile.is_approved is False


def test_role_change_is_idempotent(admin, trainer):
    resp = _api(admin).patch(
        _role_url(trainer), {"role": Role.TRAINER}, format="json"
    )
    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["role"] == Role.TRAINER


def test_you_cannot_demote_yourself(admin, second_admin):
    resp = _api(admin).patch(
        _role_url(admin), {"role": Role.STUDENT}, format="json"
    )
    assert resp.status_code == status.HTTP_409_CONFLICT
    admin.refresh_from_db()
    assert admin.role == Role.ADMIN


def test_demoting_another_admin_is_allowed_while_one_remains(admin, second_admin):
    resp = _api(admin).patch(
        _role_url(second_admin), {"role": Role.STUDENT}, format="json"
    )
    assert resp.status_code == status.HTTP_200_OK
    second_admin.refresh_from_db()
    assert second_admin.role == Role.STUDENT


def test_last_admin_guard(admin, second_admin):
    """The backstop, tested where it actually lives.

    It is unreachable over HTTP by design — an active admin acting on another
    admin always leaves themselves behind, and acting on themselves is caught by
    the self-rules first. Asserting it through a contrived request would be
    testing the contrivance, so this tests the predicate.
    """
    from accounts.admin_api import _last_admin

    assert _last_admin(admin) is False  # second_admin is still active
    assert _last_admin(second_admin) is False

    second_admin.is_active = False
    second_admin.save(update_fields=["is_active"])
    assert _last_admin(admin) is True

    student = _user("not-an-admin@example.com", Role.STUDENT)
    assert _last_admin(student) is False  # the rule is about admins only


def test_an_unknown_role_is_rejected(admin, student):
    resp = _api(admin).patch(
        _role_url(student), {"role": "superuser"}, format="json"
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


# --------------------------------------------------------------------------- #
# Suspension
# --------------------------------------------------------------------------- #


def _suspend_url(user):
    return f"/api/v1/admin/users/{user.pk}/suspend/"


def _reactivate_url(user):
    return f"/api/v1/admin/users/{user.pk}/reactivate/"


def test_suspend_and_reactivate_round_trip(admin, student):
    resp = _api(admin).post(
        _suspend_url(student), {"reason": "Chargeback fraud."}, format="json"
    )
    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["status"] == "suspended"
    student.refresh_from_db()
    assert student.is_active is False

    back = _api(admin).post(_reactivate_url(student))
    assert back.data["status"] == "active"
    student.refresh_from_db()
    assert student.is_active is True


def test_a_suspended_user_cannot_log_in(admin, student):
    _api(admin).post(_suspend_url(student), {}, format="json")
    resp = _api().post(
        "/api/v1/auth/login/",
        {"email": student.email, "password": "StrongPass123!"},
        format="json",
    )
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED


def test_you_cannot_suspend_yourself(admin, second_admin):
    resp = _api(admin).post(_suspend_url(admin), {}, format="json")
    assert resp.status_code == status.HTTP_409_CONFLICT
    admin.refresh_from_db()
    assert admin.is_active is True


def test_suspending_another_admin_is_allowed_while_one_remains(admin, second_admin):
    resp = _api(admin).post(_suspend_url(second_admin), {}, format="json")
    assert resp.status_code == status.HTTP_200_OK
    second_admin.refresh_from_db()
    assert second_admin.is_active is False


def test_suspension_is_blocked_once_the_target_is_the_last_active_admin(
    admin, second_admin
):
    """Reachable only from a session that outlived its own suspension — which
    is precisely the case the backstop exists for."""
    _api(admin).post(_suspend_url(second_admin), {}, format="json")
    second_admin.refresh_from_db()
    assert second_admin.is_active is False

    # force_authenticate skips the is_active check a real JWT would apply, which
    # is how we get to stand in the shoes of a stale session.
    resp = _api(second_admin).post(_suspend_url(admin), {}, format="json")
    assert resp.status_code == status.HTTP_409_CONFLICT
    admin.refresh_from_db()
    assert admin.is_active is True
