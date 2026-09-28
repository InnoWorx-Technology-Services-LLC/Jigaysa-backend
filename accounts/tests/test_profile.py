"""The public profile behind ``GET/PATCH /auth/me/`` (all roles)."""

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Role, UserProfile

User = get_user_model()

pytestmark = pytest.mark.django_db


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def student(db):
    return User.objects.create_user(
        email="stu@example.com", password="StrongPass123!", role=Role.STUDENT
    )


@pytest.fixture
def trainer(db):
    return User.objects.create_user(
        email="trainer@example.com", password="StrongPass123!", role=Role.TRAINER
    )


def me():
    return reverse("accounts:me")


# --- the row exists at all --------------------------------------------------


def test_every_new_account_gets_a_profile(student, trainer):
    assert UserProfile.objects.filter(user=student).exists()
    assert UserProfile.objects.filter(user=trainer).exists()


def test_legacy_account_without_a_profile_is_backfilled_on_read(api, student):
    UserProfile.objects.filter(user=student).delete()
    api.force_authenticate(student)
    resp = api.get(me())
    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["profile"]["headline"] == ""
    assert UserProfile.objects.filter(user=student).exists()


# --- saving -----------------------------------------------------------------


@pytest.mark.parametrize("role_fixture", ["student", "trainer"])
def test_profile_saves_and_survives_a_reload(api, request, role_fixture):
    user = request.getfixturevalue(role_fixture)
    api.force_authenticate(user)
    resp = api.patch(
        me(),
        {
            "full_name": "Riya Sharma",
            "profile": {
                "headline": "Aspiring Data Scientist",
                "location": "Pune, India",
                "bio": "Final-year BCA student.",
                "skills": ["Python", "SQL"],
                "cover_color": "#8FD14F",
            },
        },
        format="json",
    )
    assert resp.status_code == status.HTTP_200_OK

    # The reload the banner in the UI complained about.
    reread = api.get(me()).data
    assert reread["full_name"] == "Riya Sharma"
    assert reread["profile"]["headline"] == "Aspiring Data Scientist"
    assert reread["profile"]["location"] == "Pune, India"
    assert reread["profile"]["bio"] == "Final-year BCA student."
    assert reread["profile"]["skills"] == ["Python", "SQL"]
    assert reread["profile"]["cover_color"] == "#8FD14F"


def test_patching_the_account_alone_leaves_the_profile_alone(api, student):
    api.force_authenticate(student)
    api.patch(
        me(), {"profile": {"headline": "Keep me"}}, format="json"
    )
    api.patch(me(), {"full_name": "Riya"}, format="json")
    assert api.get(me()).data["profile"]["headline"] == "Keep me"


def test_profile_patch_is_partial(api, student):
    api.force_authenticate(student)
    api.patch(
        me(),
        {"profile": {"headline": "Data Scientist", "location": "Pune"}},
        format="json",
    )
    api.patch(me(), {"profile": {"location": "Mumbai"}}, format="json")
    profile = api.get(me()).data["profile"]
    assert profile["location"] == "Mumbai"
    assert profile["headline"] == "Data Scientist"


def test_role_and_email_stay_read_only(api, student):
    api.force_authenticate(student)
    resp = api.patch(
        me(), {"role": "admin", "email": "hacker@example.com"}, format="json"
    )
    assert resp.status_code == status.HTTP_200_OK
    student.refresh_from_db()
    assert student.role == Role.STUDENT
    assert student.email == "stu@example.com"


def test_anonymous_caller_is_rejected(api):
    assert api.get(me()).status_code in (
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
    )


# --- skills -----------------------------------------------------------------


def test_skills_are_trimmed_deduped_and_blanks_dropped(api, student):
    api.force_authenticate(student)
    resp = api.patch(
        me(),
        {"profile": {"skills": ["  Python ", "python", "", "SQL"]}},
        format="json",
    )
    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["profile"]["skills"] == ["Python", "SQL"]


def test_too_many_skills_is_rejected(api, student):
    api.force_authenticate(student)
    resp = api.patch(
        me(),
        {"profile": {"skills": [f"skill-{n}" for n in range(31)]}},
        format="json",
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_overlong_skill_is_rejected(api, student):
    api.force_authenticate(student)
    resp = api.patch(
        me(), {"profile": {"skills": ["x" * 51]}}, format="json"
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_non_text_skill_is_rejected(api, student):
    api.force_authenticate(student)
    resp = api.patch(
        me(), {"profile": {"skills": [{"name": "Python"}]}}, format="json"
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


# --- cover colour -----------------------------------------------------------


@pytest.mark.parametrize("value", ["#8FD14F", "#fff", ""])
def test_valid_cover_colours_are_accepted(api, student, value):
    api.force_authenticate(student)
    resp = api.patch(
        me(), {"profile": {"cover_color": value}}, format="json"
    )
    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["profile"]["cover_color"] == value


@pytest.mark.parametrize("value", ["green", "8FD14F", "#12345", "#xyzxyz"])
def test_invalid_cover_colours_are_rejected(api, student, value):
    api.force_authenticate(student)
    resp = api.patch(
        me(), {"profile": {"cover_color": value}}, format="json"
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
