"""Fixes raised by the frontend team: submission names, stats scope, rubrics."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Role, User
from assessments.models import Assessment, Rubric, Submission
from courses.models import Course

pytestmark = pytest.mark.django_db

SUBMISSIONS = "/api/v1/submissions/"
STATS = "/api/v1/assessments/stats/"


def _api(user=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


@pytest.fixture
def trainer():
    return User.objects.create_user(
        email="fx-t@example.com", password="StrongPass123!",
        role=Role.TRAINER, full_name="Dr. Kapoor",
    )


@pytest.fixture
def student():
    return User.objects.create_user(
        email="riya@example.com", password="StrongPass123!",
        role=Role.STUDENT, full_name="Riya Sharma",
    )


@pytest.fixture
def course(trainer):
    return Course.objects.create(title="Intro to Data Science", trainer=trainer)


def _assessment(trainer, course, **kwargs):
    defaults = {
        "title": "Build a pipeline",
        "assessment_type": Assessment.AssessmentType.ASSIGNMENT,
        "is_published": True,
    }
    defaults.update(kwargs)
    return Assessment.objects.create(course=course, trainer=trainer, **defaults)


# --------------------------------------------------------------------------- #
# #1 — the grading queue can name the student
# --------------------------------------------------------------------------- #


def test_a_submission_carries_the_students_name(trainer, course, student):
    assessment = _assessment(trainer, course)
    Submission.objects.create(
        assessment=assessment, student=student,
        status=Submission.Status.SUBMITTED,
    )

    row = _api(trainer).get(SUBMISSIONS).data["results"][0]
    assert row["student"] == student.pk
    assert row["student_name"] == "Riya Sharma"
    assert row["student_email"] == "riya@example.com"
    assert row["assessment_title"] == "Build a pipeline"


def test_a_nameless_student_gives_a_blank_not_an_error(trainer, course):
    anon = User.objects.create_user(
        email="noname@example.com", password="StrongPass123!"
    )
    assessment = _assessment(trainer, course)
    Submission.objects.create(assessment=assessment, student=anon)

    row = _api(trainer).get(SUBMISSIONS).data["results"][0]
    assert row["student_name"] == ""
    assert row["student_email"] == "noname@example.com"


# --------------------------------------------------------------------------- #
# #7 — stats and board count the same things
# --------------------------------------------------------------------------- #


def test_stats_can_be_narrowed_to_one_assessment_type(trainer, course):
    _assessment(trainer, course, title="An assignment")
    _assessment(
        trainer, course, title="A quiz",
        assessment_type=Assessment.AssessmentType.QUIZ,
    )

    everything = _api(trainer).get(STATS).data
    assert everything["open_assignments"] == 2

    assignments = _api(trainer).get(
        STATS, {"assessment_type": Assessment.AssessmentType.ASSIGNMENT}
    ).data
    assert assignments["open_assignments"] == 1


def test_the_filter_also_narrows_pending_and_average(trainer, course, student):
    quiz = _assessment(
        trainer, course, title="A quiz",
        assessment_type=Assessment.AssessmentType.QUIZ,
    )
    Submission.objects.create(
        assessment=quiz, student=student, status=Submission.Status.SUBMITTED
    )

    everything = _api(trainer).get(STATS).data
    assert everything["pending_reviews"] == 1

    # Filtered to assignments, the quiz's pending review must not appear.
    assignments = _api(trainer).get(
        STATS, {"assessment_type": Assessment.AssessmentType.ASSIGNMENT}
    ).data
    assert assignments["pending_reviews"] == 0
    assert assignments["average_score"] is None


# --------------------------------------------------------------------------- #
# #11 — rubric grading is now reachable
# --------------------------------------------------------------------------- #


def _rubric_url(assessment):
    return f"/api/v1/assessments/{assessment.pk}/rubric/"


def test_a_rubric_can_be_written_and_read(trainer, course):
    assessment = _assessment(
        trainer, course, grading_type=Assessment.GradingType.RUBRIC
    )
    criteria = [
        {"name": "Correctness", "max_points": 60},
        {"name": "Style", "max_points": 40},
    ]

    resp = _api(trainer).put(
        _rubric_url(assessment), {"criteria": criteria}, format="json"
    )
    assert resp.status_code == status.HTTP_200_OK
    assert len(resp.data["criteria"]) == 2

    fetched = _api(trainer).get(_rubric_url(assessment))
    assert fetched.data["criteria"][0]["name"] == "Correctness"


def test_a_malformed_criterion_is_refused(trainer, course):
    assessment = _assessment(
        trainer, course, grading_type=Assessment.GradingType.RUBRIC
    )
    for bad in (
        [{"max_points": 10}],                       # no name
        [{"name": "X", "max_points": 0}],           # worth nothing
        [{"name": "X", "max_points": "lots"}],      # not a number
        ["Correctness"],                            # not an object
    ):
        resp = _api(trainer).put(
            _rubric_url(assessment), {"criteria": bad}, format="json"
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST, bad


def test_grading_above_the_rubric_total_is_refused(trainer, course, student):
    assessment = _assessment(
        trainer, course, grading_type=Assessment.GradingType.RUBRIC
    )
    _api(trainer).put(
        _rubric_url(assessment),
        {"criteria": [{"name": "Correctness", "max_points": 50}]},
        format="json",
    )
    submission = Submission.objects.create(
        assessment=assessment, student=student,
        status=Submission.Status.SUBMITTED,
    )

    over = _api(trainer).post(
        f"{SUBMISSIONS}{submission.pk}/grade/",
        {"score": "80", "percent": 100},
        format="json",
    )
    assert over.status_code == status.HTTP_400_BAD_REQUEST

    ok = _api(trainer).post(
        f"{SUBMISSIONS}{submission.pk}/grade/",
        {"score": "45", "percent": 90},
        format="json",
    )
    assert ok.status_code == status.HTTP_200_OK


def test_rubric_grading_without_criteria_is_refused(trainer, course, student):
    """"Graded by rubric" with an empty rubric is a configuration someone has
    to fix, not a grade to wave through."""
    assessment = _assessment(
        trainer, course, grading_type=Assessment.GradingType.RUBRIC
    )
    Rubric.objects.create(assessment=assessment, criteria=[])
    submission = Submission.objects.create(
        assessment=assessment, student=student,
        status=Submission.Status.SUBMITTED,
    )

    resp = _api(trainer).post(
        f"{SUBMISSIONS}{submission.pk}/grade/",
        {"score": "10", "percent": 50},
        format="json",
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_manual_grading_is_unaffected_by_any_of_this(trainer, course, student):
    assessment = _assessment(
        trainer, course, grading_type=Assessment.GradingType.MANUAL
    )
    submission = Submission.objects.create(
        assessment=assessment, student=student,
        status=Submission.Status.SUBMITTED,
    )

    resp = _api(trainer).post(
        f"{SUBMISSIONS}{submission.pk}/grade/",
        {"score": "9999", "percent": 100},
        format="json",
    )
    assert resp.status_code == status.HTTP_200_OK


def test_only_the_owning_trainer_can_write_a_rubric(trainer, course):
    assessment = _assessment(
        trainer, course, grading_type=Assessment.GradingType.RUBRIC
    )
    rival = User.objects.create_user(
        email="rival-fx@example.com", password="StrongPass123!",
        role=Role.TRAINER,
    )
    resp = _api(rival).put(
        _rubric_url(assessment),
        {"criteria": [{"name": "Mine", "max_points": 10}]},
        format="json",
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN
