"""The trainer's Assignments page (PRD §3.12).

The list and grading already existed; what this covers is the dashboard layer
on top — the per-row counts, the open/closed/draft badge, the three tiles, and
the scoping that stops a trainer's own page listing a colleague's work.
"""

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Role, User
from assessments.models import Assessment, Submission
from courses.models import Course, Enrollment

pytestmark = pytest.mark.django_db

BOARD_URL = "/api/v1/assessments/board/"
STATS_URL = "/api/v1/assessments/stats/"


def _api(user=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


@pytest.fixture
def trainer():
    return User.objects.create_user(
        email="board-t@example.com", password="StrongPass123!",
        role=Role.TRAINER, full_name="Dr. Kapoor",
    )


@pytest.fixture
def rival():
    return User.objects.create_user(
        email="board-r@example.com", password="StrongPass123!", role=Role.TRAINER
    )


@pytest.fixture
def student():
    return User.objects.create_user(
        email="board-s@example.com", password="StrongPass123!", role=Role.STUDENT
    )


@pytest.fixture
def course(trainer):
    return Course.objects.create(title="Intro to Data Science", trainer=trainer)


def _assignment(trainer, course, title="Build a pandas pipeline", **kwargs):
    defaults = {
        "assessment_type": Assessment.AssessmentType.ASSIGNMENT,
        "is_published": True,
    }
    defaults.update(kwargs)
    return Assessment.objects.create(
        course=course, trainer=trainer, title=title, **defaults
    )


def _submission(assessment, student, state):
    return Submission.objects.create(
        assessment=assessment, student=student, status=state
    )


# --------------------------------------------------------------------------- #
# Scoping
# --------------------------------------------------------------------------- #


def test_the_board_shows_only_your_own_assignments(trainer, rival, course):
    _assignment(trainer, course, "Mine")
    rival_course = Course.objects.create(title="Their course", trainer=rival)
    _assignment(rival, rival_course, "Theirs")

    titles = [
        row["title"] for row in _api(trainer).get(BOARD_URL).data["results"]
    ]
    assert titles == ["Mine"]


def test_the_plain_list_still_shows_other_published_assessments(
    trainer, rival, course
):
    """The board is scoped; the browse list deliberately is not. ``?mine=true``
    is how a caller asks the plain list for the narrow view."""
    _assignment(trainer, course, "Mine")
    rival_course = Course.objects.create(title="Their course", trainer=rival)
    _assignment(rival, rival_course, "Theirs")

    everything = _api(trainer).get("/api/v1/assessments/")
    assert everything.data["count"] == 2

    mine = _api(trainer).get("/api/v1/assessments/", {"mine": "true"})
    assert mine.data["count"] == 1


def test_students_cannot_open_the_board(student, trainer, course):
    _assignment(trainer, course)
    assert _api(student).get(BOARD_URL).status_code == status.HTTP_403_FORBIDDEN
    assert _api(student).get(STATS_URL).status_code == status.HTTP_403_FORBIDDEN


# --------------------------------------------------------------------------- #
# The per-row counts
# --------------------------------------------------------------------------- #


def test_a_row_carries_submitted_pending_and_enrolled_counts(
    trainer, course, student
):
    assignment = _assignment(trainer, course)

    other = User.objects.create_user(
        email="board-s2@example.com", password="StrongPass123!"
    )
    Enrollment.objects.create(student=student, course=course)
    Enrollment.objects.create(student=other, course=course)

    _submission(assignment, student, Submission.Status.SUBMITTED)
    _submission(assignment, other, Submission.Status.GRADED)
    third = User.objects.create_user(
        email="board-s3@example.com", password="StrongPass123!"
    )
    # Started but never handed in — counts as neither submitted nor pending.
    _submission(assignment, third, Submission.Status.IN_PROGRESS)

    row = _api(trainer).get(BOARD_URL).data["results"][0]
    assert row["submitted_count"] == 2
    assert row["pending_review_count"] == 1  # only the ungraded one
    assert row["enrolled_count"] == 2
    assert row["course_title"] == "Intro to Data Science"


def test_counts_are_not_multiplied_by_the_enrollment_join(trainer, course):
    """Joining submissions and enrolments in one query inflates both counts
    unless each is counted distinctly. Thirty students, one submission."""
    assignment = _assignment(trainer, course)
    for i in range(5):
        learner = User.objects.create_user(
            email=f"multi{i}@example.com", password="StrongPass123!"
        )
        Enrollment.objects.create(student=learner, course=course)

    solo = User.objects.create_user(
        email="solo@example.com", password="StrongPass123!"
    )
    _submission(assignment, solo, Submission.Status.SUBMITTED)

    row = _api(trainer).get(BOARD_URL).data["results"][0]
    assert row["enrolled_count"] == 5
    assert row["submitted_count"] == 1


# --------------------------------------------------------------------------- #
# The open / closed / draft badge
# --------------------------------------------------------------------------- #


def _state_of(trainer, title):
    rows = _api(trainer).get(BOARD_URL).data["results"]
    return next(r["state"] for r in rows if r["title"] == title)


def test_an_unpublished_assignment_is_a_draft(trainer, course):
    _assignment(trainer, course, "Draft one", is_published=False)
    assert _state_of(trainer, "Draft one") == "draft"


def test_a_past_deadline_closes_it(trainer, course):
    _assignment(
        trainer, course, "Done",
        available_to=timezone.now() - timedelta(days=1),
    )
    assert _state_of(trainer, "Done") == "closed"


def test_a_future_deadline_leaves_it_open(trainer, course):
    _assignment(
        trainer, course, "Live",
        available_to=timezone.now() + timedelta(days=7),
    )
    assert _state_of(trainer, "Live") == "open"


def test_no_deadline_never_closes_on_its_own(trainer, course):
    _assignment(trainer, course, "Evergreen", available_to=None)
    assert _state_of(trainer, "Evergreen") == "open"


# --------------------------------------------------------------------------- #
# The three tiles
# --------------------------------------------------------------------------- #


def test_stats_count_open_assignments_and_pending_reviews(
    trainer, course, student
):
    _assignment(trainer, course, "Open one")
    _assignment(trainer, course, "Draft one", is_published=False)
    _assignment(
        trainer, course, "Closed one",
        available_to=timezone.now() - timedelta(days=1),
    )

    waiting = _assignment(trainer, course, "Has work")
    _submission(waiting, student, Submission.Status.SUBMITTED)

    data = _api(trainer).get(STATS_URL).data
    # "Has work" and "Open one" are open; the draft and the closed one are not.
    assert data["open_assignments"] == 2
    assert data["pending_reviews"] == 1


def test_pending_reviews_survive_the_assignment_closing(trainer, course, student):
    """Closing an assignment does not grade what students already handed in."""
    closed = _assignment(
        trainer, course, "Closed",
        available_to=timezone.now() - timedelta(days=1),
    )
    _submission(closed, student, Submission.Status.SUBMITTED)

    data = _api(trainer).get(STATS_URL).data
    assert data["open_assignments"] == 0
    assert data["pending_reviews"] == 1


def test_average_score_is_null_before_anything_is_graded(trainer, course, student):
    assignment = _assignment(trainer, course)
    _submission(assignment, student, Submission.Status.SUBMITTED)

    # Null, not zero — no average is not the same as everyone failing.
    assert _api(trainer).get(STATS_URL).data["average_score"] is None


def test_average_score_uses_graded_submissions_only(trainer, course, student):
    assignment = _assignment(trainer, course)
    other = User.objects.create_user(
        email="avg2@example.com", password="StrongPass123!"
    )

    graded = _submission(assignment, student, Submission.Status.GRADED)
    graded.percent = 80
    graded.save(update_fields=["percent"])

    passed = _submission(assignment, other, Submission.Status.PASSED)
    passed.percent = 60
    passed.save(update_fields=["percent"])

    third = User.objects.create_user(
        email="avg3@example.com", password="StrongPass123!"
    )
    ungraded = _submission(assignment, third, Submission.Status.SUBMITTED)
    ungraded.percent = 0  # would drag the average down if it counted
    ungraded.save(update_fields=["percent"])

    assert _api(trainer).get(STATS_URL).data["average_score"] == 70.0


def test_stats_ignore_another_trainers_work(trainer, rival, course, student):
    rival_course = Course.objects.create(title="Theirs", trainer=rival)
    theirs = _assignment(rival, rival_course, "Theirs")
    _submission(theirs, student, Submission.Status.SUBMITTED)

    data = _api(trainer).get(STATS_URL).data
    assert data["open_assignments"] == 0
    assert data["pending_reviews"] == 0


def test_the_board_is_paginated(trainer, course):
    for i in range(25):
        _assignment(trainer, course, f"Assignment {i}")

    resp = _api(trainer).get(BOARD_URL)
    assert set(resp.data) >= {"count", "next", "previous", "results"}
    assert resp.data["count"] == 25
    assert len(resp.data["results"]) == 20
