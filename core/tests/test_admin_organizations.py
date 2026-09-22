"""The admin console's Institutions page (PRD §2.4 multi-tenancy).

Weighted towards the two things that would bite in production: a second
institution with the same name (which the model's own slug generation cannot
survive), and the membership-versus-role boundary — adding four hundred
students to a college must never be able to mint four hundred institution
accounts.
"""

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Role, User
from core.models import Organization
from courses.models import Course

pytestmark = pytest.mark.django_db

URL = "/api/v1/admin/organizations/"


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
    return _user("org-admin@example.com", Role.ADMIN)


@pytest.fixture
def student():
    return _user("org-student@example.com", Role.STUDENT, full_name="Riya Sharma")


@pytest.fixture
def org():
    return Organization.objects.create(name="St. Xavier College")


# --------------------------------------------------------------------------- #
# Access
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("role", [Role.STUDENT, Role.TRAINER, Role.INSTITUTION])
def test_only_admins_can_manage_institutions(role):
    caller = _user(f"{role}-org@example.com", role)
    assert _api(caller).get(URL).status_code == status.HTTP_403_FORBIDDEN
    assert _api(caller).post(
        URL, {"name": "Sneaky College"}, format="json"
    ).status_code == status.HTTP_403_FORBIDDEN


# --------------------------------------------------------------------------- #
# Creating
# --------------------------------------------------------------------------- #


def test_an_admin_can_create_an_institution(admin):
    resp = _api(admin).post(
        URL, {"name": "St. Xavier College", "type": "institution"}, format="json"
    )
    assert resp.status_code == status.HTTP_201_CREATED
    assert resp.data["name"] == "St. Xavier College"
    assert resp.data["slug"] == "st-xavier-college"
    assert resp.data["status"] == "active"
    # The create answers in the same shape the table reads, so no refetch.
    assert resp.data["member_count"] == 0
    assert resp.data["course_count"] == 0


def test_a_corporate_client_is_the_same_endpoint(admin):
    resp = _api(admin).post(
        URL, {"name": "Acme Corp", "type": "corporate"}, format="json"
    )
    assert resp.status_code == status.HTTP_201_CREATED
    assert resp.data["type"] == "corporate"
    assert resp.data["type_label"] == "Corporate"


def test_two_institutions_with_the_same_name_are_refused(admin, org):
    resp = _api(admin).post(
        URL, {"name": "st. xavier college"}, format="json"
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "already exists" in str(resp.data)


def test_similar_names_get_distinct_slugs(admin):
    """The model derives a slug with no de-duplication and the column is
    unique, so a collision would be a 500. The API generates it instead."""
    first = _api(admin).post(URL, {"name": "St Xavier College"}, format="json")
    second = _api(admin).post(URL, {"name": "St. Xavier: College"}, format="json")

    assert first.status_code == status.HTTP_201_CREATED
    assert second.status_code == status.HTTP_201_CREATED
    # Different names, identical slugify() output — the second is suffixed.
    assert first.data["slug"] == "st-xavier-college"
    assert second.data["slug"] == "st-xavier-college-2"


def test_a_blank_name_is_refused(admin):
    resp = _api(admin).post(URL, {"name": "   "}, format="json")
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #


def test_the_list_is_paginated(admin):
    for i in range(25):
        Organization.objects.create(name=f"College {i:02d}")

    resp = _api(admin).get(URL)
    assert set(resp.data) >= {"count", "next", "previous", "results"}
    assert resp.data["count"] == 25
    assert len(resp.data["results"]) == 20


def test_rows_carry_member_and_course_counts(admin, org, student):
    trainer = _user("org-trainer@example.com", Role.TRAINER)
    User.objects.filter(pk__in=[student.pk, trainer.pk]).update(organization=org)
    Course.objects.create(title="Physics 101", trainer=trainer, organization=org)

    row = _api(admin).get(URL).data["results"][0]
    assert row["member_count"] == 2
    assert row["course_count"] == 1


def test_search_and_filters_narrow_the_list(admin, org):
    Organization.objects.create(name="Acme Corp", type="corporate")

    assert _api(admin).get(URL, {"search": "xavier"}).data["count"] == 1
    assert _api(admin).get(URL, {"type": "corporate"}).data["count"] == 1
    assert _api(admin).get(URL, {"type": "institution"}).data["count"] == 1


# --------------------------------------------------------------------------- #
# Editing, deactivating
# --------------------------------------------------------------------------- #


def test_renaming_does_not_change_the_slug(admin, org):
    """The slug is in URLs; re-slugging on a rename breaks shared links."""
    resp = _api(admin).patch(
        f"{URL}{org.pk}/", {"name": "St. Xavier University"}, format="json"
    )
    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["name"] == "St. Xavier University"
    assert resp.data["slug"] == "st-xavier-college"


def test_delete_deactivates_rather_than_removes(admin, org, student):
    User.objects.filter(pk=student.pk).update(organization=org)

    resp = _api(admin).delete(f"{URL}{org.pk}/")
    assert resp.status_code == status.HTTP_204_NO_CONTENT

    org.refresh_from_db()
    assert org.is_active is False
    # The row survives, and so does its membership — nobody is orphaned.
    student.refresh_from_db()
    assert student.organization_id == org.pk


def test_a_deactivated_institution_can_be_brought_back(admin, org):
    _api(admin).delete(f"{URL}{org.pk}/")
    resp = _api(admin).post(f"{URL}{org.pk}/activate/")
    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["status"] == "active"


def test_status_filter_finds_deactivated_ones(admin, org):
    _api(admin).delete(f"{URL}{org.pk}/")
    assert _api(admin).get(URL, {"status": "active"}).data["count"] == 0
    assert _api(admin).get(URL, {"status": "inactive"}).data["count"] == 1


# --------------------------------------------------------------------------- #
# Members
# --------------------------------------------------------------------------- #


def test_adding_members_links_them(admin, org, student):
    resp = _api(admin).post(
        f"{URL}{org.pk}/members/add/", {"users": [student.pk]}, format="json"
    )
    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["added"] == 1

    student.refresh_from_db()
    assert student.organization_id == org.pk


def test_adding_members_never_changes_their_role(admin, org, student):
    """Bundling role into membership would make "add 400 students" able to
    mint 400 institution accounts."""
    _api(admin).post(
        f"{URL}{org.pk}/members/add/", {"users": [student.pk]}, format="json"
    )
    student.refresh_from_db()
    assert student.role == Role.STUDENT


def test_moving_someone_between_institutions_is_reported(admin, org, student):
    other = Organization.objects.create(name="Other College")
    User.objects.filter(pk=student.pk).update(organization=other)

    resp = _api(admin).post(
        f"{URL}{org.pk}/members/add/", {"users": [student.pk]}, format="json"
    )
    assert resp.data["reassigned_from_another_organization"] == [student.email]
    student.refresh_from_db()
    assert student.organization_id == org.pk


def test_an_unknown_user_id_is_refused_before_anything_moves(
    admin, org, student
):
    resp = _api(admin).post(
        f"{URL}{org.pk}/members/add/",
        {"users": [student.pk, 999999]},
        format="json",
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    # All-or-nothing: the valid id must not have been attached either.
    student.refresh_from_db()
    assert student.organization_id is None


def test_an_empty_user_list_is_refused(admin, org):
    resp = _api(admin).post(
        f"{URL}{org.pk}/members/add/", {"users": []}, format="json"
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_the_member_roll_is_paginated(admin, org):
    for i in range(25):
        learner = _user(f"member{i}@example.com")
        User.objects.filter(pk=learner.pk).update(organization=org)

    resp = _api(admin).get(f"{URL}{org.pk}/members/")
    assert set(resp.data) >= {"count", "next", "previous", "results"}
    assert resp.data["count"] == 25
    assert len(resp.data["results"]) == 20


def test_members_can_be_filtered_by_role(admin, org, student):
    head = _user("head@example.com", Role.INSTITUTION)
    User.objects.filter(pk__in=[student.pk, head.pk]).update(organization=org)

    resp = _api(admin).get(f"{URL}{org.pk}/members/", {"role": Role.INSTITUTION})
    assert resp.data["count"] == 1
    assert resp.data["results"][0]["email"] == head.email


def test_removing_a_member_keeps_their_account(admin, org, student):
    User.objects.filter(pk=student.pk).update(organization=org)

    resp = _api(admin).delete(f"{URL}{org.pk}/members/{student.pk}/")
    assert resp.status_code == status.HTTP_204_NO_CONTENT

    student.refresh_from_db()
    # Leaving an institution is not leaving the platform.
    assert student.organization_id is None
    assert student.is_active is True
    assert student.role == Role.STUDENT


def test_removing_someone_who_is_not_a_member_is_a_404(admin, org, student):
    resp = _api(admin).delete(f"{URL}{org.pk}/members/{student.pk}/")
    assert resp.status_code == status.HTTP_404_NOT_FOUND


# --------------------------------------------------------------------------- #
# The whole onboarding, end to end
# --------------------------------------------------------------------------- #


def test_onboarding_an_institution_takes_three_calls(admin):
    """What previously needed three visits to Django admin."""
    head = _user("principal@example.com", Role.STUDENT)

    created = _api(admin).post(
        URL, {"name": "Nalanda Institute", "type": "institution"}, format="json"
    )
    org_id = created.data["id"]

    promoted = _api(admin).patch(
        f"/api/v1/admin/users/{head.pk}/role/",
        {"role": Role.INSTITUTION},
        format="json",
    )
    assert promoted.status_code == status.HTTP_200_OK

    linked = _api(admin).post(
        f"{URL}{org_id}/members/add/", {"users": [head.pk]}, format="json"
    )
    assert linked.status_code == status.HTTP_200_OK

    head.refresh_from_db()
    assert head.role == Role.INSTITUTION
    assert head.organization_id == org_id
    assert _api(admin).get(URL).data["results"][0]["member_count"] == 1


def test_onboard_creates_org_and_admin_login_in_one_call(admin):
    resp = _api(admin).post(
        f"{URL}onboard/",
        {
            "name": "Nalanda Institute",
            "type": "institution",
            "admin_email": "principal@nalanda.edu",
            "admin_full_name": "Dr. Principal",
            "admin_password": "StrongPass123!",
            "admin_phone": "9876543210",
        },
        format="json",
    )
    assert resp.status_code == status.HTTP_201_CREATED

    org = Organization.objects.get(pk=resp.data["organization"]["id"])
    assert org.name == "Nalanda Institute"

    head = User.objects.get(email="principal@nalanda.edu")
    assert head.role == Role.INSTITUTION
    assert head.organization_id == org.pk
    assert head.phone == "9876543210"
    assert head.check_password("StrongPass123!")

    assert resp.data["admin"]["email"] == "principal@nalanda.edu"
    assert resp.data["organization"]["member_count"] == 1


def test_onboard_rejects_duplicate_org_name(admin, org):
    resp = _api(admin).post(
        f"{URL}onboard/",
        {
            "name": org.name,
            "admin_email": "someone@example.com",
            "admin_full_name": "Someone",
            "admin_password": "StrongPass123!",
        },
        format="json",
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert not User.objects.filter(email="someone@example.com").exists()


def test_onboard_rejects_an_email_already_in_use(admin, student):
    resp = _api(admin).post(
        f"{URL}onboard/",
        {
            "name": "Brand New Institute",
            "admin_email": student.email,
            "admin_full_name": "Someone Else",
            "admin_password": "StrongPass123!",
        },
        format="json",
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert not Organization.objects.filter(name="Brand New Institute").exists()


def test_onboard_is_admin_only():
    caller = _user("not-admin@example.com", Role.STUDENT)
    resp = _api(caller).post(
        f"{URL}onboard/",
        {
            "name": "Some Institute",
            "admin_email": "x@example.com",
            "admin_full_name": "X",
            "admin_password": "StrongPass123!",
        },
        format="json",
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN
