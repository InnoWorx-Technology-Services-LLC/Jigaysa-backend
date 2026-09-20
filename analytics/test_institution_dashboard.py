"""The institution console's Overview page (PRD §2.4 multi-tenancy, §3.14).

Weighted towards the two things that would bite in production:

* **Tenant isolation.** Every endpoint here exists to answer "for *my*
  institution", so the test that matters most is that a second institution's
  batches, rooms, bookings and learners never appear. Each test below seeds a
  rival org with tempting data for exactly that reason.
* **The unlinked account.** ``User.organization`` is nullable and nothing makes
  an ``institution``-role account have one, so the 409 path is reachable in
  normal operation, not a theoretical branch.
"""

import csv
import io
import json
from datetime import datetime, timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Role, User
from certificates.models import Certificate
from classrooms.models import ClassroomSession, Room
from core.models import Organization
from courses.models import (
    Batch, Category, Course, Enrollment, Lesson, LessonProgress, Module,
)
from live.models import Attendance, LiveSession

pytestmark = pytest.mark.django_db

OVERVIEW = "/api/v1/institution/overview/"
BATCHES = "/api/v1/institution/batches/"
BOOKINGS = "/api/v1/institution/bookings/"
ACTIVITY = "/api/v1/institution/activity/"
BATCH_SUMMARY = "/api/v1/institution/batches/summary/"


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
def org():
    return Organization.objects.create(name="St. Xavier College")


@pytest.fixture
def rival():
    """A second institution, seeded in most tests with data this one must not
    see. Tenant leaks do not show up against a single-tenant fixture."""
    return Organization.objects.create(name="Rival Polytechnic")


@pytest.fixture
def head(org):
    return _user("head@xavier.example", Role.INSTITUTION, organization=org)


@pytest.fixture
def trainer():
    return _user("kapoor@example.com", Role.TRAINER, full_name="Dr. Kapoor")


@pytest.fixture
def course(trainer):
    return Course.objects.create(title="CS 2025", trainer=trainer)


def _batch(org, course, name="Batch A", **kwargs):
    return Batch.objects.create(
        course=course, organization=org, name=name, **kwargs
    )


# --------------------------------------------------------------------------- #
# Access
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("url", [OVERVIEW, BATCHES, BOOKINGS, ACTIVITY])
@pytest.mark.parametrize("role", [Role.STUDENT, Role.TRAINER, Role.ADMIN])
def test_only_institutions_can_open_the_institution_console(url, role):
    """Admins are excluded too, deliberately.

    The trainer endpoints let an admin in because "your own courses" still
    means something for an admin. "Your own institution" does not — an admin
    has no ``organization``, so admitting them would require inventing a
    "whose institution?" parameter. The platform-wide view they want is
    ``/admin/reports/`` and ``/admin/organizations/``.
    """
    caller = _user(f"{role}-inst@example.com", role)
    assert _api(caller).get(url).status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.parametrize("url", [OVERVIEW, BATCHES, BOOKINGS, ACTIVITY])
def test_anonymous_callers_are_rejected(url):
    assert _api().get(url).status_code in (
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
    )


@pytest.mark.parametrize("url", [OVERVIEW, BATCHES, BOOKINGS, ACTIVITY])
def test_an_institution_account_with_no_institution_gets_a_clear_409(url):
    """Not zeros. "You have no learners" and "you are not attached to an
    institution" are different stories and only the second is true here."""
    stray = _user("stray@example.com", Role.INSTITUTION)
    response = _api(stray).get(url)
    assert response.status_code == status.HTTP_409_CONFLICT
    assert "not linked to an institution" in response.data["detail"].lower()


# --------------------------------------------------------------------------- #
# Overview — the four tiles
# --------------------------------------------------------------------------- #


def test_a_brand_new_institution_sees_honest_zeros(head, org):
    """Zeros and a null rate, never an error and never a gap."""
    body = _api(head).get(OVERVIEW).data

    assert body["organization"]["name"] == "St. Xavier College"
    assert body["active_batches"] == 0
    assert body["learners"] == 0
    assert body["classrooms"] == 0
    # Null, not 0: a college with no enrolments has no completion rate.
    assert body["avg_completion"] is None


def test_the_tiles_count_only_this_institution(head, org, rival, course, trainer):
    _batch(org, course, "Ours", start_date=timezone.localdate())
    _batch(rival, course, "Theirs", start_date=timezone.localdate())
    _user("mine@example.com", Role.STUDENT, organization=org)
    _user("theirs@example.com", Role.STUDENT, organization=rival)
    _user("nobodys@example.com", Role.STUDENT)
    Room.objects.create(organization=org, name="Room 101")
    Room.objects.create(organization=rival, name="Their Room")

    body = _api(head).get(OVERVIEW).data

    assert body["active_batches"] == 1
    assert body["learners"] == 1
    assert body["classrooms"] == 1


def test_active_batches_counts_the_window_not_the_table(head, org, course):
    today = timezone.localdate()
    _batch(org, course, "Running",
           start_date=today - timedelta(days=7),
           end_date=today + timedelta(days=7))
    _batch(org, course, "Finished",
           start_date=today - timedelta(days=90),
           end_date=today - timedelta(days=30))
    _batch(org, course, "Not started yet",
           start_date=today + timedelta(days=30))
    # No dates at all: counted as active on purpose. An unscheduled cohort is
    # an open one, and skipping it would under-count against a list the
    # institution can plainly see has rows in it.
    _batch(org, course, "Undated")

    body = _api(head).get(OVERVIEW).data
    assert body["active_batches"] == 2
    assert body["batches"] == 4


def test_the_learner_badge_counts_only_recent_joiners(head, org):
    recent = _user("new@example.com", Role.STUDENT, organization=org)
    old = _user("old@example.com", Role.STUDENT, organization=org)
    User.objects.filter(pk=old.pk).update(
        created_at=timezone.now() - timedelta(days=200)
    )

    body = _api(head).get(OVERVIEW).data
    assert body["learners"] == 2
    assert body["learners_joined_recently"] == 1
    # The window travels with the number so the frontend cannot mislabel it.
    assert body["new_learner_window_days"] == 30
    assert recent.organization_id == org.pk


def test_suspended_learners_are_not_counted(head, org):
    _user("active@example.com", Role.STUDENT, organization=org)
    _user("suspended@example.com", Role.STUDENT, organization=org,
          is_active=False)

    assert _api(head).get(OVERVIEW).data["learners"] == 1


def test_completion_averages_the_institutions_own_training(
    head, org, rival, course, trainer
):
    """A learner's unrelated personal course must not move the college's rate."""
    ours = _batch(org, course, "Ours")
    student = _user("s1@example.com", Role.STUDENT, organization=org)
    Enrollment.objects.create(
        student=student, course=course, batch=ours, progress_pct=80
    )

    # Same student, a public course they took on their own time. Excluded.
    personal = Course.objects.create(title="Guitar", trainer=trainer)
    Enrollment.objects.create(
        student=student, course=personal, progress_pct=10
    )

    # And a rival institution's batch. Also excluded.
    other_student = _user("s2@example.com", Role.STUDENT, organization=rival)
    Enrollment.objects.create(
        student=other_student, course=course,
        batch=_batch(rival, course, "Theirs"), progress_pct=20,
    )

    assert _api(head).get(OVERVIEW).data["avg_completion"] == 80.0


def test_completion_includes_enrolments_in_an_institution_owned_course(
    head, org, trainer
):
    """Batches alone would miss self-paced institutional content."""
    owned = Course.objects.create(
        title="Induction", trainer=trainer, organization=org
    )
    student = _user("s3@example.com", Role.STUDENT, organization=org)
    Enrollment.objects.create(student=student, course=owned, progress_pct=60)

    assert _api(head).get(OVERVIEW).data["avg_completion"] == 60.0


# --------------------------------------------------------------------------- #
# Batches
# --------------------------------------------------------------------------- #


def test_the_batch_list_defaults_to_active_and_stays_in_the_tenant(
    head, org, rival, course
):
    today = timezone.localdate()
    _batch(org, course, "Running", start_date=today, capacity=80)
    _batch(org, course, "Finished",
           start_date=today - timedelta(days=90),
           end_date=today - timedelta(days=30))
    _batch(rival, course, "Rival running", start_date=today)

    rows = _api(head).get(BATCHES).data["results"]
    assert [row["name"] for row in rows] == ["Running"]
    assert rows[0]["capacity"] == 80
    assert rows[0]["course"] == "CS 2025"


@pytest.mark.parametrize(
    "state,expected",
    [("active", {"Running"}), ("upcoming", {"Later"}),
     ("ended", {"Finished"}), ("all", {"Running", "Later", "Finished"})],
)
def test_the_batch_list_filters_by_status(head, org, course, state, expected):
    today = timezone.localdate()
    _batch(org, course, "Running", start_date=today)
    _batch(org, course, "Later", start_date=today + timedelta(days=30))
    _batch(org, course, "Finished",
           start_date=today - timedelta(days=90),
           end_date=today - timedelta(days=30))

    rows = _api(head).get(BATCHES, {"status": state}).data["results"]
    assert {row["name"] for row in rows} == expected


def test_an_unrecognised_status_falls_back_to_active(head, org, course):
    """A typo'd filter shows the running cohorts, not an error page."""
    _batch(org, course, "Running", start_date=timezone.localdate())
    response = _api(head).get(BATCHES, {"status": "nonsense"})
    assert response.status_code == status.HTTP_200_OK
    assert [r["name"] for r in response.data["results"]] == ["Running"]


def test_a_batch_row_separates_seats_sold_from_people_still_on_it(
    head, org, course
):
    batch = _batch(org, course, "Batch A", capacity=10)
    batch.enrolled_count = 3
    batch.save(update_fields=["enrolled_count"])

    for i, state in enumerate(
        [Enrollment.Status.ACTIVE, Enrollment.Status.ACTIVE,
         Enrollment.Status.CANCELLED]
    ):
        Enrollment.objects.create(
            student=_user(f"row{i}@example.com", Role.STUDENT, organization=org),
            course=course, batch=batch, status=state, progress_pct=50,
        )

    row = _api(head).get(BATCHES).data["results"][0]
    assert row["seats_taken"] == 3   # the counter the enrolment flow maintains
    assert row["learners"] == 2      # people with an active enrolment
    assert row["completion"] == 50.0


def test_a_batch_with_no_enrolments_has_a_null_completion(head, org, course):
    _batch(org, course, "Empty")
    assert _api(head).get(BATCHES).data["results"][0]["completion"] is None


# --------------------------------------------------------------------------- #
# Bookings
# --------------------------------------------------------------------------- #


@pytest.fixture
def booking(org, course, trainer):
    room = Room.objects.create(
        organization=org, name="Room 101", location="Pune Campus"
    )
    batch = _batch(org, course, "Batch A")
    live = LiveSession.objects.create(
        course=course, batch=batch, trainer=trainer,
        title="Lecture 4", duration_minutes=180,
        scheduled_start=timezone.now() + timedelta(days=3),
    )
    return ClassroomSession.objects.create(
        room=room, live_session=live, remote_trainer=trainer,
        date=live.scheduled_start,
    )


def test_bookings_default_to_upcoming_and_carry_the_batch(head, booking):
    row = _api(head).get(BOOKINGS).data["results"][0]

    assert row["room"] == "Room 101"
    assert row["location"] == "Pune Campus"
    assert row["batch"] == "Batch A"
    assert row["course"] == "CS 2025"
    assert row["trainer"] == "Dr. Kapoor"
    # The real lifecycle value, not the mock's confirmed/pending badge — the
    # platform models no booking approval and this must not invent one.
    assert row["status"] == ClassroomSession.Status.SCHEDULED


def test_a_bookings_end_time_comes_from_the_session_length(head, booking):
    row = _api(head).get(BOOKINGS).data["results"][0]
    assert row["ends_at"] is not None
    # Compared as instants, not strings: DRF renders in the active timezone
    # while the model holds UTC, so a string compare here only passes on a
    # machine set to UTC.
    assert datetime.fromisoformat(row["ends_at"]) == booking.date + timedelta(
        minutes=180
    )
    assert datetime.fromisoformat(row["starts_at"]) == booking.date


def test_a_room_only_booking_has_no_end_time(head, org):
    """Null, not a guessed hour — a wrong time on a timetable is worse."""
    room = Room.objects.create(organization=org, name="Hall")
    ClassroomSession.objects.create(
        room=room, date=timezone.now() + timedelta(days=1)
    )
    row = _api(head).get(BOOKINGS).data["results"][0]
    assert row["ends_at"] is None
    assert row["batch"] == ""


def test_past_and_undated_bookings_are_not_upcoming(head, org, booking):
    room = Room.objects.get(name="Room 101")
    ClassroomSession.objects.create(
        room=room, date=timezone.now() - timedelta(days=5)
    )
    ClassroomSession.objects.create(room=room)  # never scheduled

    assert _api(head).get(BOOKINGS).data["count"] == 1
    assert _api(head).get(BOOKINGS, {"when": "past"}).data["count"] == 1
    assert _api(head).get(BOOKINGS, {"when": "all"}).data["count"] == 3


def test_bookings_never_show_another_institutions_rooms(head, rival, booking):
    ClassroomSession.objects.create(
        room=Room.objects.create(organization=rival, name="Their Hall"),
        date=timezone.now() + timedelta(days=1),
    )
    rooms = {row["room"] for row in _api(head).get(BOOKINGS).data["results"]}
    assert rooms == {"Room 101"}


# --------------------------------------------------------------------------- #
# Activity feed
# --------------------------------------------------------------------------- #


def test_the_feed_merges_real_events_newest_first(head, org, course, booking):
    student = _user("riya@example.com", Role.STUDENT,
                    organization=org, full_name="Riya Desai")
    Enrollment.objects.create(
        student=student, course=course,
        batch=Batch.objects.get(name="Batch A"),
    )

    body = _api(head).get(ACTIVITY).data
    kinds = [event["type"] for event in body["events"]]

    assert {"enrolment", "booking", "batch"} <= set(kinds)
    stamps = [event["at"] for event in body["events"]]
    assert stamps == sorted(stamps, reverse=True)

    enrolment = next(e for e in body["events"] if e["type"] == "enrolment")
    assert enrolment["actor"] == "Riya Desai"
    assert enrolment["target"] == "Batch A"


def test_the_feed_survives_actual_json_rendering(head, org, course, booking):
    """``meta`` is an untyped ``DictField`` and a booking puts a **datetime**
    inside it. Asserting on ``response.data`` would never touch the renderer,
    so this renders for real — the failure mode is a 500 in production against
    a green test suite."""
    response = _api(head).get(ACTIVITY)
    payload = json.loads(response.rendered_content)
    booking_event = next(
        e for e in payload["events"] if e["type"] == "booking"
    )
    assert isinstance(booking_event["meta"]["starts_at"], str)


def test_a_completion_is_its_own_event(head, org, course):
    batch = _batch(org, course, "Batch A")
    Enrollment.objects.create(
        student=_user("done@example.com", Role.STUDENT, organization=org),
        course=course, batch=batch, progress_pct=100,
        status=Enrollment.Status.COMPLETED, completed_at=timezone.now(),
    )

    kinds = [e["type"] for e in _api(head).get(ACTIVITY).data["events"]]
    assert "completion" in kinds


def test_the_feed_is_scoped_and_bounded(head, org, rival, course):
    for i in range(5):
        _batch(org, course, f"Ours {i}")
    _batch(rival, course, "Theirs")

    body = _api(head).get(ACTIVITY, {"limit": 3}).data
    assert body["limit"] == 3
    assert len(body["events"]) == 3
    assert all("Theirs" != e["target"] for e in body["events"])


@pytest.mark.parametrize(
    "value,expected", [("0", 1), ("500", 100), ("nonsense", 20), ("", 20)]
)
def test_the_feed_limit_is_clamped_rather_than_rejected(
    head, org, value, expected
):
    body = _api(head).get(ACTIVITY, {"limit": value}).data
    assert body["limit"] == expected


def test_a_quiet_institution_gets_an_empty_feed_not_an_error(head, org):
    response = _api(head).get(ACTIVITY)
    assert response.status_code == status.HTTP_200_OK
    assert response.data["events"] == []


# --------------------------------------------------------------------------- #
# Batch writes — "New batch" and "Manage"
# --------------------------------------------------------------------------- #


def _detail(batch_id):
    return f"{BATCHES}{batch_id}/"


@pytest.fixture
def published(trainer):
    return Course.objects.create(
        title="Published Course", trainer=trainer,
        status=Course.Status.PUBLISHED,
    )


def test_an_institution_can_create_a_batch(head, org, published, trainer):
    today = timezone.localdate()
    response = _api(head).post(BATCHES, {
        "course": published.pk, "name": "Batch D · UX 2026",
        "trainer": trainer.pk, "capacity": 60,
        "start_date": str(today), "end_date": str(today + timedelta(days=90)),
    }, format="json")

    assert response.status_code == status.HTTP_201_CREATED
    # The create answers in the same shape the cards render, so the client does
    # not have to refetch to draw the row it just made.
    assert response.data["name"] == "Batch D · UX 2026"
    assert response.data["trainer"] == "Dr. Kapoor"
    assert response.data["learners"] == 0
    assert response.data["completion"] is None
    assert response.data["state"] == "active"
    assert Batch.objects.get(pk=response.data["id"]).organization_id == org.pk


def test_a_created_batch_belongs_to_the_caller_not_the_body(
    head, org, rival, published
):
    """The whole reason this endpoint exists instead of reusing
    ``/api/v1/batches/``, whose ``organization`` is writable."""
    response = _api(head).post(BATCHES, {
        "course": published.pk, "name": "Sneaky",
        "organization": rival.pk, "capacity": 10,
    }, format="json")

    assert response.status_code == status.HTTP_201_CREATED
    assert Batch.objects.get(name="Sneaky").organization_id == org.pk


def test_a_batch_cannot_be_built_on_someone_elses_draft(head, org, trainer):
    draft = Course.objects.create(
        title="Half-written", trainer=trainer, status=Course.Status.DRAFT
    )
    response = _api(head).post(BATCHES, {
        "course": draft.pk, "name": "Too early", "capacity": 10,
    }, format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "catalogue" in str(response.data["course"]).lower()


def test_an_institution_may_build_on_its_own_unpublished_course(
    head, org, trainer
):
    """Its own syllabus cannot change under it the way a stranger's can."""
    mine = Course.objects.create(
        title="Induction", trainer=trainer, organization=org,
        status=Course.Status.DRAFT,
    )
    response = _api(head).post(BATCHES, {
        "course": mine.pk, "name": "Induction 2026", "capacity": 10,
    }, format="json")
    assert response.status_code == status.HTTP_201_CREATED


def test_a_batch_trainer_must_actually_be_a_trainer(head, org, published):
    student = _user("notatrainer@example.com", Role.STUDENT, organization=org)
    response = _api(head).post(BATCHES, {
        "course": published.pk, "name": "Wrong trainer",
        "trainer": student.pk, "capacity": 10,
    }, format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "not a trainer" in str(response.data["trainer"]).lower()


def test_a_batch_cannot_end_before_it_starts(head, org, published):
    today = timezone.localdate()
    response = _api(head).post(BATCHES, {
        "course": published.pk, "name": "Backwards", "capacity": 10,
        "start_date": str(today), "end_date": str(today - timedelta(days=1)),
    }, format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "end_date" in response.data


def test_a_patch_is_validated_against_the_merged_object(head, org, course):
    """A PATCH sends one field. Checking only what was sent would let an
    ``end_date`` slide behind an untouched ``start_date``."""
    today = timezone.localdate()
    batch = _batch(org, course, "Batch A", start_date=today)

    response = _api(head).patch(
        _detail(batch.pk),
        {"end_date": str(today - timedelta(days=10))}, format="json",
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "end_date" in response.data


def test_capacity_cannot_be_cut_below_the_people_already_enrolled(
    head, org, course
):
    batch = _batch(org, course, "Batch A", capacity=50)
    for i in range(3):
        Enrollment.objects.create(
            student=_user(f"cap{i}@example.com", Role.STUDENT,
                          organization=org),
            course=course, batch=batch,
        )

    response = _api(head).patch(
        _detail(batch.pk), {"capacity": 2}, format="json"
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "already enrolled" in str(response.data["capacity"])

    # Raising it is fine, and so is meeting the enrolled number exactly.
    assert _api(head).patch(
        _detail(batch.pk), {"capacity": 3}, format="json"
    ).status_code == status.HTTP_200_OK


def test_manage_can_rename_and_reschedule(head, org, course, trainer):
    batch = _batch(org, course, "Old name", capacity=10)
    today = timezone.localdate()

    response = _api(head).patch(_detail(batch.pk), {
        "name": "New name", "trainer": trainer.pk,
        "start_date": str(today), "end_date": str(today + timedelta(days=60)),
    }, format="json")

    assert response.status_code == status.HTTP_200_OK
    assert response.data["name"] == "New name"
    assert response.data["trainer"] == "Dr. Kapoor"
    assert Batch.objects.get(pk=batch.pk).name == "New name"


def test_a_blank_name_is_refused(head, org, published):
    response = _api(head).post(BATCHES, {
        "course": published.pk, "name": "   ", "capacity": 10,
    }, format="json")
    assert response.status_code == status.HTTP_400_BAD_REQUEST


# --- tenant isolation on the detail route ---------------------------------- #


def test_another_institutions_batch_is_a_404_not_a_403(
    head, rival, course
):
    """404, deliberately. A 403 would confirm the row exists, and the caller
    has no business learning that."""
    theirs = _batch(rival, course, "Theirs")

    assert _api(head).get(
        _detail(theirs.pk)
    ).status_code == status.HTTP_404_NOT_FOUND
    assert _api(head).patch(
        _detail(theirs.pk), {"name": "Mine now"}, format="json"
    ).status_code == status.HTTP_404_NOT_FOUND
    theirs.refresh_from_db()
    assert theirs.name == "Theirs"


def test_a_batch_cannot_be_moved_to_another_institution_by_patch(
    head, org, rival, course
):
    batch = _batch(org, course, "Batch A")
    response = _api(head).patch(
        _detail(batch.pk), {"organization": rival.pk}, format="json"
    )
    assert response.status_code == status.HTTP_200_OK
    batch.refresh_from_db()
    assert batch.organization_id == org.pk


def test_there_is_no_delete(head, org, course):
    """``Enrollment.batch`` is SET_NULL — a delete would silently detach every
    learner's enrolment from the cohort they took."""
    batch = _batch(org, course, "Batch A")
    assert _api(head).delete(
        _detail(batch.pk)
    ).status_code == status.HTTP_405_METHOD_NOT_ALLOWED
    assert Batch.objects.filter(pk=batch.pk).exists()


@pytest.mark.parametrize("role", [Role.STUDENT, Role.TRAINER, Role.ADMIN])
def test_nobody_else_can_create_an_institution_batch(role, published):
    caller = _user(f"{role}-w@example.com", role)
    assert _api(caller).post(BATCHES, {
        "course": published.pk, "name": "Nope", "capacity": 5,
    }, format="json").status_code == status.HTTP_403_FORBIDDEN


# --------------------------------------------------------------------------- #
# The card badge and the tiles
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "start_offset,end_offset,expected",
    [
        (30, 90, "upcoming"),
        (-10, 200, "active"),
        (-10, 15, "completing"),    # inside the 30-day closing window
        (-200, -10, "ended"),
        (None, None, "active"),     # undated cohorts are open
    ],
)
def test_the_card_badge_is_derived_from_dates(
    head, org, course, start_offset, end_offset, expected
):
    today = timezone.localdate()
    _batch(
        org, course, "Batch A",
        start_date=None if start_offset is None
        else today + timedelta(days=start_offset),
        end_date=None if end_offset is None
        else today + timedelta(days=end_offset),
    )
    row = _api(head).get(BATCHES, {"status": "all"}).data["results"][0]
    assert row["state"] == expected


def test_the_completing_filter_and_the_badge_agree(head, org, course):
    """A row the filter returned for ``completing`` must not label itself
    ``active`` — that disagreement is the kind nobody notices until a
    customer does."""
    today = timezone.localdate()
    _batch(org, course, "Closing soon",
           start_date=today - timedelta(days=10),
           end_date=today + timedelta(days=15))
    _batch(org, course, "Plenty of time",
           start_date=today - timedelta(days=10),
           end_date=today + timedelta(days=200))

    rows = _api(head).get(BATCHES, {"status": "completing"}).data["results"]
    assert [row["name"] for row in rows] == ["Closing soon"]
    assert all(row["state"] == "completing" for row in rows)


def test_the_batches_page_tiles(head, org, rival, course, trainer):
    today = timezone.localdate()
    running = _batch(org, course, "Running",
                     start_date=today, end_date=today + timedelta(days=200))
    _batch(org, course, "Closing", start_date=today,
           end_date=today + timedelta(days=10))
    _batch(org, course, "Later", start_date=today + timedelta(days=30))
    _batch(org, course, "Done", start_date=today - timedelta(days=90),
           end_date=today - timedelta(days=30))
    _batch(rival, course, "Theirs", start_date=today)

    second = Course.objects.create(title="Second", trainer=trainer)
    both = _user("both@example.com", Role.STUDENT, organization=org)
    Enrollment.objects.create(student=both, course=course, batch=running)
    # Same person, a second cohort. One learner, not two.
    Enrollment.objects.create(
        student=both, course=second,
        batch=_batch(org, second, "Running 2", start_date=today),
    )

    body = _api(head).get(BATCH_SUMMARY).data
    assert body["active"] == 3        # Running, Closing, Running 2
    assert body["completing"] == 1    # ...of which Closing is inside the window
    assert body["upcoming"] == 1
    assert body["ended"] == 1
    assert body["total"] == 5         # the rival's is not counted
    assert body["learners"] == 1      # one person, two cohorts


def test_the_two_screens_count_learners_differently_on_purpose(
    head, org, course
):
    """The Dashboard tile is the roll; the Batches tile is people in a cohort.
    Someone on the roll but in no batch belongs only to the first."""
    enrolled = _user("in@example.com", Role.STUDENT, organization=org)
    _user("onroll@example.com", Role.STUDENT, organization=org)
    Enrollment.objects.create(
        student=enrolled, course=course,
        batch=_batch(org, course, "Batch A"),
    )

    assert _api(head).get(OVERVIEW).data["learners"] == 2
    assert _api(head).get(BATCH_SUMMARY).data["learners"] == 1


def test_the_batch_tiles_are_institution_only(published):
    for role in (Role.STUDENT, Role.TRAINER, Role.ADMIN):
        caller = _user(f"{role}-sum@example.com", role)
        assert _api(caller).get(
            BATCH_SUMMARY
        ).status_code == status.HTTP_403_FORBIDDEN


def test_the_batch_tiles_409_for_an_unlinked_account():
    stray = _user("stray-sum@example.com", Role.INSTITUTION)
    assert _api(stray).get(
        BATCH_SUMMARY
    ).status_code == status.HTTP_409_CONFLICT


def test_a_quiet_institution_gets_zero_tiles(head, org):
    body = _api(head).get(BATCH_SUMMARY).data
    assert body == {"active": 0, "completing": 0, "upcoming": 0,
                    "ended": 0, "total": 0, "learners": 0}


# --------------------------------------------------------------------------- #
# The Courses screen
# --------------------------------------------------------------------------- #

COURSES = "/api/v1/institution/courses/"
COURSE_SUMMARY = "/api/v1/institution/courses/summary/"


def _course(title, trainer, **kwargs):
    kwargs.setdefault("status", Course.Status.PUBLISHED)
    kwargs.setdefault("visibility", Course.Visibility.PUBLIC)
    return Course.objects.create(title=title, trainer=trainer, **kwargs)


@pytest.mark.parametrize("url", [COURSES, COURSE_SUMMARY])
@pytest.mark.parametrize("role", [Role.STUDENT, Role.TRAINER, Role.ADMIN])
def test_the_courses_screen_is_institution_only(url, role):
    caller = _user(f"{role}-c@example.com", role)
    assert _api(caller).get(url).status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.parametrize("url", [COURSES, COURSE_SUMMARY])
def test_the_courses_screen_409s_for_an_unlinked_account(url):
    stray = _user("stray-c@example.com", Role.INSTITUTION)
    assert _api(stray).get(url).status_code == status.HTTP_409_CONFLICT


def test_the_catalogue_is_published_public_plus_your_own(head, org, trainer):
    _course("Public & published", trainer)
    _course("Someone's draft", trainer, status=Course.Status.DRAFT)
    _course("Unlisted", trainer, visibility=Course.Visibility.UNLISTED)
    # Your own, whatever state — your syllabus cannot change under you.
    _course("Our induction", trainer, organization=org,
            status=Course.Status.DRAFT)

    titles = {
        row["title"] for row in _api(head).get(COURSES).data["results"]
    }
    assert titles == {"Public & published", "Our induction"}


def test_another_institutions_private_course_is_not_in_your_catalogue(
    head, rival, trainer
):
    _course("Theirs", trainer, organization=rival,
            status=Course.Status.DRAFT)
    assert _api(head).get(COURSES).data["count"] == 0


def test_a_card_carries_the_category_trainer_and_hours(head, trainer):
    category = Category.objects.create(name="Web Development")
    _course("React 19 Professional", trainer, category=category,
            course_type=Course.CourseType.LIVE_BATCH,
            skill_level=Course.SkillLevel.INTERMEDIATE,
            duration_minutes=2400)

    row = _api(head).get(COURSES).data["results"][0]
    assert row["category"] == "Web Development"
    assert row["trainer"] == "Dr. Kapoor"
    assert row["course_type"] == "live_batch"
    assert row["skill_level"] == "intermediate"
    # The card prints "40 h" — converted server-side so every client rounds
    # the same way.
    assert row["duration_hours"] == 40.0
    assert row["assigned"] is False
    assert row["batches"] == []


def test_an_assigned_card_shows_your_cohort_and_only_yours(
    head, org, rival, trainer
):
    course = _course("React 19 Professional", trainer)
    mine = Batch.objects.create(
        course=course, organization=org, name="Batch A · CS 2025", capacity=150
    )
    Batch.objects.create(
        course=course, organization=rival, name="Their cohort"
    )
    for i in range(2):
        Enrollment.objects.create(
            student=_user(f"c{i}@example.com", Role.STUDENT, organization=org),
            course=course, batch=mine,
        )

    row = _api(head).get(COURSES).data["results"][0]
    assert row["assigned"] is True
    assert [b["name"] for b in row["batches"]] == ["Batch A · CS 2025"]
    assert row["batches"][0]["learners"] == 2
    assert row["batches"][0]["capacity"] == 150


def test_a_course_on_several_of_your_cohorts_lists_them_all(
    head, org, trainer
):
    """The mock shows a single "Assigned to"; hiding the rest would make a
    course look free to assign when it is already running twice."""
    course = _course("Popular", trainer)
    for name in ("Cohort 1", "Cohort 2"):
        Batch.objects.create(course=course, organization=org, name=name)

    row = _api(head).get(COURSES).data["results"][0]
    assert len(row["batches"]) == 2


@pytest.mark.parametrize(
    "value,expected", [("true", {"Assigned"}), ("false", {"Free"})]
)
def test_the_assigned_filter(head, org, trainer, value, expected):
    assigned = _course("Assigned", trainer)
    _course("Free", trainer)
    Batch.objects.create(course=assigned, organization=org, name="Batch A")

    rows = _api(head).get(COURSES, {"assigned": value}).data["results"]
    assert {row["title"] for row in rows} == expected


def test_the_catalogue_filters(head, trainer):
    web = Category.objects.create(name="Web Development")
    _course("React", trainer, category=web,
            course_type=Course.CourseType.LIVE_BATCH,
            skill_level=Course.SkillLevel.ADVANCED)
    _course("UX Basics", trainer,
            course_type=Course.CourseType.SELF_PACED,
            skill_level=Course.SkillLevel.BEGINNER)

    def titles(**params):
        return {r["title"] for r in _api(head).get(COURSES, params).data["results"]}

    assert titles(category=web.pk) == {"React"}
    assert titles(type="self_paced") == {"UX Basics"}
    assert titles(level="advanced") == {"React"}
    assert titles(q="ux") == {"UX Basics"}


def test_the_courses_tiles(head, org, trainer):
    today = timezone.localdate()
    running = _course("Running", trainer)
    finished = _course("Finished", trainer)
    _course("Never assigned", trainer)

    Batch.objects.create(
        course=running, organization=org, name="Live cohort",
        start_date=today, end_date=today + timedelta(days=60),
    )
    Batch.objects.create(
        course=finished, organization=org, name="Old cohort",
        start_date=today - timedelta(days=200),
        end_date=today - timedelta(days=100),
    )
    Enrollment.objects.create(
        student=_user("learner@example.com", Role.STUDENT, organization=org),
        course=running, batch=Batch.objects.get(name="Live cohort"),
    )

    body = _api(head).get(COURSE_SUMMARY).data
    assert body["available"] == 3
    assert body["assigned"] == 2
    assert body["active"] == 1
    assert body["learners"] == 1
    # A course cannot be running for you without being assigned to you.
    assert body["active"] <= body["assigned"]


def test_a_quiet_institution_gets_zero_course_tiles(head, org):
    assert _api(head).get(COURSE_SUMMARY).data == {
        "available": 0, "assigned": 0, "active": 0, "learners": 0
    }


# --- the rule that browsing and assigning must share ----------------------- #


def test_every_course_you_can_browse_you_can_also_assign(head, org, trainer):
    """A card with a live "Assign to batch" button that 400s when pressed is
    the exact failure this shares one queryset to avoid."""
    _course("Public & published", trainer)
    _course("Our draft", trainer, organization=org,
            status=Course.Status.DRAFT)

    for row in _api(head).get(COURSES).data["results"]:
        response = _api(head).post(BATCHES, {
            "course": row["id"], "name": f"Cohort for {row['title']}",
            "capacity": 10,
        }, format="json")
        assert response.status_code == status.HTTP_201_CREATED, row["title"]


def test_a_course_you_cannot_browse_you_cannot_assign(head, org, trainer):
    hidden = _course("Someone's draft", trainer, status=Course.Status.DRAFT)
    assert _api(head).get(COURSES).data["count"] == 0

    response = _api(head).post(BATCHES, {
        "course": hidden.pk, "name": "Sneaky", "capacity": 10,
    }, format="json")
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "catalogue" in str(response.data["course"]).lower()


# --------------------------------------------------------------------------- #
# The Learners screen
# --------------------------------------------------------------------------- #

LEARNERS = "/api/v1/institution/learners/"
LEARNER_SUMMARY = "/api/v1/institution/learners/summary/"
LEARNER_IMPORT = "/api/v1/institution/learners/import_csv/"


def _learner(email, org, name="", last_login=None, **kwargs):
    user = _user(email, Role.STUDENT, organization=org, full_name=name, **kwargs)
    if last_login is not None:
        User.objects.filter(pk=user.pk).update(last_login=last_login)
        user.refresh_from_db()
    return user


@pytest.mark.parametrize("url", [LEARNERS, LEARNER_SUMMARY])
@pytest.mark.parametrize("role", [Role.STUDENT, Role.TRAINER, Role.ADMIN])
def test_the_learners_screen_is_institution_only(url, role):
    caller = _user(f"{role}-l@example.com", role)
    assert _api(caller).get(url).status_code == status.HTTP_403_FORBIDDEN


def test_the_roster_is_your_own_students_only(head, org, rival, trainer):
    _learner("mine@example.com", org, "Riya Desai")
    _learner("theirs@example.com", rival, "Someone Else")
    _user("orphan@example.com", Role.STUDENT)
    # A trainer on your roll is not a learner.
    _user("staff@example.com", Role.TRAINER, organization=org)

    rows = _api(head).get(LEARNERS).data["results"]
    assert [row["email"] for row in rows] == ["mine@example.com"]


def test_a_suspended_learner_is_off_the_roster(head, org):
    _learner("here@example.com", org)
    _learner("gone@example.com", org, is_active=False)
    assert _api(head).get(LEARNERS).data["count"] == 1


def test_a_row_carries_progress_batch_and_last_active(head, org, course):
    batch = _batch(org, course, "Batch A · CS 2025")
    seen = timezone.now() - timedelta(days=1)
    learner = _learner("riya@example.com", org, "Riya Desai", last_login=seen)
    Enrollment.objects.create(
        student=learner, course=course, batch=batch, progress_pct=84
    )

    row = _api(head).get(LEARNERS).data["results"][0]
    assert row["full_name"] == "Riya Desai"
    assert row["progress"] == 84.0
    assert row["batch"] == "Batch A · CS 2025"
    assert row["status"] == "active"
    assert row["last_active"] is not None


def test_last_active_is_null_for_someone_who_never_signed_in(head, org):
    """Null, so the table renders "never" rather than "today"."""
    _learner("silent@example.com", org)
    assert _api(head).get(LEARNERS).data["results"][0]["last_active"] is None


def test_progress_ignores_a_learners_personal_courses(head, org, course, trainer):
    """The institution's bar measures the institution's training."""
    learner = _learner("riya@example.com", org)
    Enrollment.objects.create(
        student=learner, course=course,
        batch=_batch(org, course, "Batch A"), progress_pct=80,
    )
    Enrollment.objects.create(
        student=learner,
        course=Course.objects.create(title="Guitar", trainer=trainer),
        progress_pct=0,
    )

    assert _api(head).get(LEARNERS).data["results"][0]["progress"] == 80.0


def test_a_learner_in_several_cohorts_lists_them_all(head, org, course, trainer):
    learner = _learner("busy@example.com", org)
    second = Course.objects.create(title="Second", trainer=trainer)
    Enrollment.objects.create(
        student=learner, course=course, batch=_batch(org, course, "Batch A")
    )
    Enrollment.objects.create(
        student=learner, course=second, batch=_batch(org, second, "Batch B")
    )

    row = _api(head).get(LEARNERS).data["results"][0]
    assert len(row["batches"]) == 2
    assert row["batch"] in {"Batch A", "Batch B"}


# --- the at-risk rule ------------------------------------------------------ #


def test_at_risk_needs_both_low_progress_and_silence(head, org, course, trainer):
    """Either signal alone is noise: low progress flags everyone who enrolled
    this morning, silence alone flags everyone on holiday."""
    long_ago = timezone.now() - timedelta(days=60)
    recently = timezone.now() - timedelta(days=1)

    cases = [
        ("both@example.com", 20, long_ago, "at-risk"),
        ("behind-but-here@example.com", 20, recently, "active"),
        ("quiet-but-ahead@example.com", 90, long_ago, "active"),
        ("fine@example.com", 90, recently, "active"),
    ]
    for i, (email, pct, seen, _expected) in enumerate(cases):
        learner = _learner(email, org, last_login=seen)
        Enrollment.objects.create(
            student=learner,
            course=Course.objects.create(title=f"C{i}", trainer=trainer),
            batch=_batch(org, Course.objects.get(title=f"C{i}"), f"B{i}"),
            progress_pct=pct,
        )

    rows = {r["email"]: r["status"] for r in _api(head).get(LEARNERS).data["results"]}
    for email, _pct, _seen, expected in cases:
        assert rows[email] == expected, email


def test_a_finished_learner_reads_completed_not_at_risk(head, org, course):
    """No active enrolment left, so the idle clock is irrelevant."""
    learner = _learner("done@example.com", org,
                       last_login=timezone.now() - timedelta(days=90))
    Enrollment.objects.create(
        student=learner, course=course, batch=_batch(org, course, "Batch A"),
        progress_pct=100, status=Enrollment.Status.COMPLETED,
        completed_at=timezone.now(),
    )
    assert _api(head).get(LEARNERS).data["results"][0]["status"] == "completed"


@pytest.mark.parametrize("state", ["active", "at-risk", "completed"])
def test_the_status_filter_and_the_label_agree(head, org, course, trainer, state):
    long_ago = timezone.now() - timedelta(days=60)
    for i, (email, pct, seen, st) in enumerate([
        ("risky@example.com", 10, long_ago, Enrollment.Status.ACTIVE),
        ("busy@example.com", 90, timezone.now(), Enrollment.Status.ACTIVE),
        ("finished@example.com", 100, long_ago, Enrollment.Status.COMPLETED),
    ]):
        learner = _learner(email, org, last_login=seen)
        c = Course.objects.create(title=f"S{i}", trainer=trainer)
        Enrollment.objects.create(
            student=learner, course=c, batch=_batch(org, c, f"SB{i}"),
            progress_pct=pct, status=st,
            completed_at=timezone.now()
            if st == Enrollment.Status.COMPLETED else None,
        )

    rows = _api(head).get(LEARNERS, {"status": state}).data["results"]
    assert rows, state
    assert all(row["status"] == state for row in rows)


def test_the_three_statuses_partition_the_roll(head, org, course, trainer):
    """No learner in two buckets, none in none."""
    for i in range(3):
        learner = _learner(f"p{i}@example.com", org)
        c = Course.objects.create(title=f"P{i}", trainer=trainer)
        Enrollment.objects.create(
            student=learner, course=c, batch=_batch(org, c, f"PB{i}"),
            progress_pct=i * 45,
        )
    _learner("nocourses@example.com", org)

    total = _api(head).get(LEARNERS).data["count"]
    counted = sum(
        _api(head).get(LEARNERS, {"status": s}).data["count"]
        for s in ("active", "at-risk", "completed")
    )
    assert counted == total


def test_the_roster_filters(head, org, course):
    batch = _batch(org, course, "Batch A")
    enrolled = _learner("riya@example.com", org, "Riya Desai")
    Enrollment.objects.create(student=enrolled, course=course, batch=batch)
    _learner("other@example.com", org, "Someone Else")

    def emails(**params):
        return {r["email"] for r in _api(head).get(LEARNERS, params).data["results"]}

    assert emails(batch=batch.pk) == {"riya@example.com"}
    assert emails(q="riya") == {"riya@example.com"}
    assert emails(q="Someone") == {"other@example.com"}


# --- tiles ----------------------------------------------------------------- #


def test_the_learner_tiles(head, org, course):
    batch = _batch(org, course, "Batch A")
    recent = _learner("seen@example.com", org,
                      last_login=timezone.now() - timedelta(days=2))
    _learner("stale@example.com", org,
             last_login=timezone.now() - timedelta(days=200))
    Enrollment.objects.create(
        student=recent, course=course, batch=batch, progress_pct=78
    )
    Certificate.objects.create(
        student=recent, course=course, serial_number="JGY-2026-000001",
        verification_code="abc123",
    )

    body = _api(head).get(LEARNER_SUMMARY).data
    assert body["total"] == 2
    assert body["active"] == 1
    assert body["avg_completion"] == 78.0
    assert body["certificates"] == 1
    assert body["certificates_recent"] == 1
    # The definitions travel with the numbers.
    assert body["active_window_days"] == 90
    assert body["at_risk_idle_days"] == 14
    assert body["at_risk_progress_pct"] == 40


def test_a_quiet_institution_gets_zero_learner_tiles(head, org):
    body = _api(head).get(LEARNER_SUMMARY).data
    assert body["total"] == 0 and body["certificates"] == 0
    assert body["avg_completion"] is None    # null, not 0


def test_certificates_count_only_your_own_learners(head, org, rival, course):
    theirs = _learner("theirs@example.com", rival)
    Certificate.objects.create(
        student=theirs, course=course, serial_number="JGY-2026-000009",
        verification_code="zzz999",
    )
    assert _api(head).get(LEARNER_SUMMARY).data["certificates"] == 0


# --- the View action ------------------------------------------------------- #


def test_view_shows_the_learners_enrolments(head, org, course):
    batch = _batch(org, course, "Batch A")
    learner = _learner("riya@example.com", org, "Riya Desai")
    Enrollment.objects.create(
        student=learner, course=course, batch=batch, progress_pct=84
    )

    body = _api(head).get(f"{LEARNERS}{learner.pk}/").data
    assert body["full_name"] == "Riya Desai"
    assert len(body["courses"]) == 1
    assert body["courses"][0]["course"] == "CS 2025"
    assert body["courses"][0]["progress"] == 84
    assert body["certificates"] == 0


def test_another_institutions_learner_is_a_404(head, rival):
    theirs = _learner("theirs@example.com", rival)
    assert _api(head).get(
        f"{LEARNERS}{theirs.pk}/"
    ).status_code == status.HTTP_404_NOT_FOUND


# --- Add learner ----------------------------------------------------------- #


def test_add_learner_creates_enrols_and_returns_the_row(head, org, course):
    batch = _batch(org, course, "Batch A", capacity=10)
    response = _api(head).post(LEARNERS, {
        "email": "New.Person@Example.com", "full_name": "New Person",
        "batch": batch.pk,
    }, format="json")

    assert response.status_code == status.HTTP_201_CREATED
    assert response.data["batch"] == "Batch A"
    learner = User.objects.get(email="new.person@example.com")
    assert learner.organization_id == org.pk
    assert learner.role == Role.STUDENT
    # No usable password: nobody is handed a credential for somebody else.
    assert not learner.has_usable_password()
    batch.refresh_from_db()
    assert batch.enrolled_count == 1


def test_add_learner_works_without_a_batch(head, org):
    response = _api(head).post(
        LEARNERS, {"email": "solo@example.com"}, format="json"
    )
    assert response.status_code == status.HTTP_201_CREATED
    assert response.data["batches"] == []


def test_an_existing_email_is_refused_not_absorbed(head, org):
    """Quietly attaching a stranger's account would hand the institution a
    view of their learning off the back of typing an email address."""
    _user("already@example.com", Role.STUDENT)
    response = _api(head).post(
        LEARNERS, {"email": "already@example.com"}, format="json"
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "already exists" in str(response.data["email"]).lower()


def test_a_learner_cannot_be_enrolled_into_another_institutions_batch(
    head, org, rival, course
):
    theirs = _batch(rival, course, "Theirs")
    response = _api(head).post(LEARNERS, {
        "email": "new@example.com", "batch": theirs.pk,
    }, format="json")
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert not User.objects.filter(email="new@example.com").exists()


@pytest.mark.parametrize("role", [Role.STUDENT, Role.TRAINER, Role.ADMIN])
def test_nobody_else_can_add_a_learner(role):
    caller = _user(f"{role}-add@example.com", role)
    assert _api(caller).post(
        LEARNERS, {"email": "x@example.com"}, format="json"
    ).status_code == status.HTTP_403_FORBIDDEN


# --- Import CSV ------------------------------------------------------------ #


def test_a_clean_csv_creates_every_row(head, org, course):
    batch = _batch(org, course, "Batch A")
    csv_text = (
        "email,full_name,batch\n"
        f"a@example.com,Ana Roy,{batch.pk}\n"
        f"b@example.com,Bo Singh,{batch.pk}\n"
        "c@example.com,Cy Das,\n"
    )
    response = _api(head).post(
        LEARNER_IMPORT, {"csv": csv_text}, format="json"
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.data["created"] == 3
    assert response.data["applied"] is True
    assert User.objects.filter(organization=org, role=Role.STUDENT).count() == 3
    assert Enrollment.objects.filter(batch=batch).count() == 2


def test_one_bad_row_rejects_the_whole_file(head, org):
    """A partial import leaves the institution unable to tell which half
    landed, and unable to safely re-run."""
    _user("taken@example.com", Role.STUDENT)
    csv_text = (
        "email,full_name\n"
        "good@example.com,Good Row\n"
        "taken@example.com,Clashes\n"
        "not-an-email,Broken\n"
    )
    response = _api(head).post(
        LEARNER_IMPORT, {"csv": csv_text}, format="json"
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.data["applied"] is False
    assert response.data["created"] == 0
    # Every bad row is reported, not just the first — one round trip to fix it.
    assert response.data["rejected"] == 2
    assert not User.objects.filter(email="good@example.com").exists()


def test_a_duplicate_inside_the_file_is_caught(head, org):
    csv_text = ("email\nsame@example.com\nsame@example.com\n")
    response = _api(head).post(
        LEARNER_IMPORT, {"csv": csv_text}, format="json"
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "appears earlier" in response.data["rows"][1]["error"]


def test_a_csv_upload_is_accepted_as_a_file(head, org):
    upload = SimpleUploadedFile(
        "learners.csv", b"email,full_name\nfile@example.com,From File\n",
        content_type="text/csv",
    )
    response = _api(head).post(
        LEARNER_IMPORT, {"file": upload}, format="multipart"
    )
    assert response.status_code == status.HTTP_201_CREATED
    assert User.objects.filter(email="file@example.com").exists()


@pytest.mark.parametrize("body,fragment", [
    ({"csv": ""}, "send a csv"),
    ({"csv": "name\nNo email column\n"}, "email"),
    ({"csv": "email\n"}, "no rows"),
])
def test_a_malformed_csv_is_explained(head, org, body, fragment):
    response = _api(head).post(LEARNER_IMPORT, body, format="json")
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert fragment in response.data["detail"].lower()


def test_an_oversized_import_is_refused(head, org):
    rows = "\n".join(f"r{i}@example.com" for i in range(501))
    response = _api(head).post(
        LEARNER_IMPORT, {"csv": f"email\n{rows}\n"}, format="json"
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "limit is 500" in response.data["detail"]


# --------------------------------------------------------------------------- #
# The Reports screen
# --------------------------------------------------------------------------- #

R_SUMMARY = "/api/v1/institution/reports/summary/"
R_COHORTS = "/api/v1/institution/reports/cohorts/"
R_TREND = "/api/v1/institution/reports/attendance/"
R_EXPORT = "/api/v1/institution/reports/export/"
REPORT_URLS = [R_SUMMARY, R_COHORTS, R_TREND, R_EXPORT]


def _lessons(course, how_many):
    module = Module.objects.create(course=course, title="M1")
    return [
        Lesson.objects.create(module=module, title=f"L{i}", order=i)
        for i in range(how_many)
    ]


def _session(batch, when, trainer):
    return LiveSession.objects.create(
        course=batch.course, batch=batch, trainer=trainer,
        title=f"Session {when:%Y-%m}", scheduled_start=when,
    )


@pytest.mark.parametrize("url", REPORT_URLS)
@pytest.mark.parametrize("role", [Role.STUDENT, Role.TRAINER, Role.ADMIN])
def test_reports_are_institution_only(url, role):
    caller = _user(f"{role}-r@example.com", role)
    assert _api(caller).get(url).status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.parametrize("url", REPORT_URLS)
def test_reports_409_for_an_unlinked_account(url):
    stray = _user("stray-r@example.com", Role.INSTITUTION)
    assert _api(stray).get(url).status_code == status.HTTP_409_CONFLICT


# --- tiles ----------------------------------------------------------------- #


def test_a_quiet_institution_gets_honest_report_tiles(head, org):
    body = _api(head).get(R_SUMMARY).data
    assert body["cohorts"] == 0
    assert body["certified"] == 0
    # Null, not 0 — nobody stayed away, there was simply nothing to measure.
    assert body["completion"] is None
    assert body["attendance"] is None


def test_the_report_tiles(head, org, rival, course, trainer):
    today = timezone.localdate()
    batch = _batch(org, course, "Batch A",
                   start_date=today, end_date=today + timedelta(days=30))
    _batch(org, course, "Old", start_date=today - timedelta(days=300),
           end_date=today - timedelta(days=200))
    _batch(rival, course, "Theirs", start_date=today)

    learner = _learner("riya@example.com", org)
    enrollment = Enrollment.objects.create(
        student=learner, course=course, batch=batch, progress_pct=78
    )
    Certificate.objects.create(
        student=learner, course=course, enrollment=enrollment,
        serial_number="JGY-2026-000100", verification_code="rep001",
    )
    session = _session(batch, timezone.now() - timedelta(days=3), trainer)
    Attendance.objects.create(session=session, student=learner, present=True)
    Attendance.objects.create(
        session=session,
        student=_learner("absent@example.com", org), present=False,
    )

    body = _api(head).get(R_SUMMARY).data
    assert body["cohorts"] == 2          # the rival's is not counted
    assert body["active_cohorts"] == 1
    assert body["completion"] == 78.0
    assert body["certified"] == 1
    assert body["attendance"] == 50.0    # one present of two recorded


# --- the cohort table ------------------------------------------------------ #


def test_a_cohort_row_carries_every_column(head, org, course, trainer):
    batch = _batch(org, course, "Batch A", capacity=20)
    lessons = _lessons(course, 4)
    learner = _learner("riya@example.com", org)
    enrollment = Enrollment.objects.create(
        student=learner, course=course, batch=batch, progress_pct=72
    )
    # 3 of 4 lessons done by the only learner → 75% engagement.
    for lesson in lessons[:3]:
        LessonProgress.objects.create(
            enrollment=enrollment, lesson=lesson,
            status=LessonProgress.Status.COMPLETED,
        )
    Certificate.objects.create(
        student=learner, course=course, enrollment=enrollment,
        serial_number="JGY-2026-000200", verification_code="rep002",
    )
    session = _session(batch, timezone.now() - timedelta(days=2), trainer)
    Attendance.objects.create(session=session, student=learner, present=True)

    row = _api(head).get(R_COHORTS).data["results"][0]
    assert row["batch"] == "Batch A"
    assert row["course"] == "CS 2025"
    assert row["learners"] == 1
    assert row["completion"] == 72.0
    assert row["certified"] == 1
    assert row["certified_pct"] == 100.0
    assert row["engagement"] == 75.0
    assert row["attendance"] == 100.0


def test_a_finished_cohort_still_reports_its_engagement(head, org, course):
    """The denominator is every enrolment the cohort ever had, not the active
    ``learners`` count — Reports is exactly where finished cohorts are read,
    and one showing a dash for work it genuinely did would be useless."""
    batch = _batch(org, course, "Batch A")
    lessons = _lessons(course, 2)
    enrollment = Enrollment.objects.create(
        student=_learner("done@example.com", org), course=course, batch=batch,
        status=Enrollment.Status.COMPLETED, progress_pct=100,
        completed_at=timezone.now(),
    )
    for lesson in lessons:
        LessonProgress.objects.create(
            enrollment=enrollment, lesson=lesson,
            status=LessonProgress.Status.COMPLETED,
        )

    row = _api(head).get(R_COHORTS).data["results"][0]
    assert row["learners"] == 0          # nobody is still on it
    assert row["engagement"] == 100.0    # but the work is still counted


def test_engagement_divides_by_enrolments_times_lessons(head, org, course):
    """Two learners, four lessons, four completions between them → 50%."""
    batch = _batch(org, course, "Batch A")
    lessons = _lessons(course, 4)
    for i in range(2):
        learner = _learner(f"e{i}@example.com", org)
        enrollment = Enrollment.objects.create(
            student=learner, course=course, batch=batch
        )
        for lesson in lessons[:2]:
            LessonProgress.objects.create(
                enrollment=enrollment, lesson=lesson,
                status=LessonProgress.Status.COMPLETED,
            )

    assert _api(head).get(R_COHORTS).data["results"][0]["engagement"] == 50.0


def test_unfinished_lessons_do_not_count_towards_engagement(head, org, course):
    """Opening a lesson is not doing it — that is the difference between this
    and a login count."""
    batch = _batch(org, course, "Batch A")
    lessons = _lessons(course, 2)
    enrollment = Enrollment.objects.create(
        student=_learner("riya@example.com", org), course=course, batch=batch
    )
    LessonProgress.objects.create(
        enrollment=enrollment, lesson=lessons[0],
        status=LessonProgress.Status.IN_PROGRESS, watch_pct=90,
    )
    assert _api(head).get(R_COHORTS).data["results"][0]["engagement"] == 0.0


def test_engagement_is_null_when_the_course_has_no_lessons(head, org, course):
    batch = _batch(org, course, "Batch A")
    Enrollment.objects.create(
        student=_learner("riya@example.com", org), course=course, batch=batch
    )
    assert _api(head).get(R_COHORTS).data["results"][0]["engagement"] is None


def test_engagement_goes_null_when_the_curriculum_is_removed(head, org, course):
    """Deleting a lesson cascades its progress rows away, so both halves of the
    fraction vanish together and the cell reads "no data" rather than 0%."""
    batch = _batch(org, course, "Batch A")
    lessons = _lessons(course, 2)
    enrollment = Enrollment.objects.create(
        student=_learner("riya@example.com", org), course=course, batch=batch
    )
    for lesson in lessons:
        LessonProgress.objects.create(
            enrollment=enrollment, lesson=lesson,
            status=LessonProgress.Status.COMPLETED,
        )
    lessons[0].delete()  # curriculum trimmed; the completion row goes with it
    Lesson.objects.filter(pk=lessons[1].pk).delete()

    assert _api(head).get(R_COHORTS).data["results"][0]["engagement"] is None


def test_an_untouched_cohort_has_nulls_not_zeros(head, org, course):
    _batch(org, course, "Nothing happened yet")
    row = _api(head).get(R_COHORTS).data["results"][0]
    assert row["completion"] is None
    assert row["certified_pct"] is None
    assert row["attendance"] is None


def test_the_cohort_table_is_your_institution_only(head, org, rival, course):
    _batch(org, course, "Mine")
    _batch(rival, course, "Theirs")
    rows = _api(head).get(R_COHORTS).data["results"]
    assert [r["batch"] for r in rows] == ["Mine"]


# --- attendance trend ------------------------------------------------------ #


def test_the_trend_is_dense_and_null_for_quiet_months(head, org, course, trainer):
    batch = _batch(org, course, "Batch A")
    learner = _learner("riya@example.com", org)
    session = _session(batch, timezone.now() - timedelta(days=2), trainer)
    Attendance.objects.create(session=session, student=learner, present=True)

    body = _api(head).get(R_TREND).data
    assert body["months"] == 12
    # Dense: every month present, so the chart cannot hide a gap by running
    # the line straight over it.
    assert len(body["attendance"]) == 12
    months = [point["month"] for point in body["attendance"]]
    assert months == sorted(months)

    this_month = body["attendance"][-1]
    assert this_month["value"] == 100.0
    assert this_month["sessions_recorded"] == 1
    # A month with no sessions: null, not 0 — nobody failed to turn up.
    assert body["attendance"][0]["value"] is None
    assert body["attendance"][0]["sessions_recorded"] == 0


def test_the_trend_months_parameter_is_clamped(head, org):
    assert _api(head).get(R_TREND, {"months": 3}).data["months"] == 3
    assert _api(head).get(R_TREND, {"months": 500}).data["months"] == 36
    assert _api(head).get(R_TREND, {"months": "nonsense"}).data["months"] == 12


def test_the_trend_ignores_another_institutions_sessions(
    head, org, rival, course, trainer
):
    theirs = _batch(rival, course, "Theirs")
    session = _session(theirs, timezone.now(), trainer)
    Attendance.objects.create(
        session=session, student=_learner("t@example.com", rival), present=True
    )
    body = _api(head).get(R_TREND).data
    assert all(point["sessions_recorded"] == 0 for point in body["attendance"])


# --- Export CSV ------------------------------------------------------------ #


def test_the_export_is_a_csv_attachment(head, org, course):
    _batch(org, course, "Batch A", capacity=20)
    response = _api(head).get(R_EXPORT)

    assert response.status_code == status.HTTP_200_OK
    assert response["Content-Type"] == "text/csv"
    assert "attachment;" in response["Content-Disposition"]
    assert org.slug in response["Content-Disposition"]

    rows = list(csv.reader(io.StringIO(response.content.decode())))
    assert rows[0][0] == "Batch"
    assert rows[1][0] == "Batch A"


def test_the_export_covers_every_cohort_not_just_a_page(head, org, course):
    """An export that stopped at the page size would be a quietly wrong
    spreadsheet, which is worse than no export."""
    for i in range(25):     # DefaultPagination is 20
        _batch(org, course, f"Batch {i:02d}")

    response = _api(head).get(R_EXPORT)
    rows = list(csv.reader(io.StringIO(response.content.decode())))
    assert len(rows) == 26  # header + 25

    assert _api(head).get(R_COHORTS).data["count"] == 25
    assert len(_api(head).get(R_COHORTS).data["results"]) == 20


def test_the_export_leaves_missing_rates_blank(head, org, course):
    """Empty, not 0 — a spreadsheet averaging a missing figure as zero is the
    silent wrongness this module exists to avoid."""
    _batch(org, course, "Untouched")
    response = _api(head).get(R_EXPORT)
    header, row = list(csv.reader(io.StringIO(response.content.decode())))[:2]

    for column in ("Completion %", "Certified %", "Engagement %", "Attendance %"):
        assert row[header.index(column)] == ""


def test_the_export_never_leaks_another_institution(head, org, rival, course):
    _batch(org, course, "Mine")
    _batch(rival, course, "Theirs")
    body = _api(head).get(R_EXPORT).content.decode()
    assert "Mine" in body and "Theirs" not in body
