"""The trainer's Analytics page (PRD §3.14).

Two things carry the risk: **scoping** — every number must come from the
caller's own courses, and a leak here shows one trainer another's performance —
and **null-versus-zero**, because "no submissions yet" and "everyone scored 0"
must not render the same.
"""

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Role, User
from assessments.models import Assessment, Submission
from courses.models import Course, Enrollment, Lesson, LessonProgress, Module
from live.models import LiveSession, SessionDoubt

pytestmark = pytest.mark.django_db

SUMMARY_URL = "/api/v1/trainer/analytics/summary/"
ENGAGEMENT_URL = "/api/v1/trainer/analytics/engagement/"
COURSES_URL = "/api/v1/trainer/analytics/courses/"
DOUBTS_URL = "/api/v1/trainer/analytics/doubts/"


def _api(user=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


@pytest.fixture
def trainer():
    return User.objects.create_user(
        email="an-t@example.com", password="StrongPass123!",
        role=Role.TRAINER, full_name="Rohan Deshpande",
    )


@pytest.fixture
def rival():
    return User.objects.create_user(
        email="an-r@example.com", password="StrongPass123!", role=Role.TRAINER
    )


@pytest.fixture
def student():
    return User.objects.create_user(
        email="an-s@example.com", password="StrongPass123!",
        role=Role.STUDENT, full_name="Riya Sharma",
    )


@pytest.fixture
def course(trainer):
    return Course.objects.create(title="React 19 Pro", trainer=trainer)


def _enroll(student, course, progress=0, state=Enrollment.Status.ACTIVE):
    return Enrollment.objects.create(
        student=student, course=course, progress_pct=progress, status=state
    )


def _submission(course, trainer, student, state, percent=0):
    assessment = Assessment.objects.create(
        course=course, trainer=trainer, title="Quiz"
    )
    return Submission.objects.create(
        assessment=assessment, student=student, status=state, percent=percent
    )


# --------------------------------------------------------------------------- #
# Access
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url", [SUMMARY_URL, ENGAGEMENT_URL, COURSES_URL, DOUBTS_URL]
)
def test_students_are_refused(url, student):
    assert _api(student).get(url).status_code == status.HTTP_403_FORBIDDEN


# --------------------------------------------------------------------------- #
# Nothing yet
# --------------------------------------------------------------------------- #


def test_a_new_trainer_gets_nulls_not_zeros(trainer):
    """No submissions is not the same as everyone scoring zero."""
    data = _api(trainer).get(SUMMARY_URL).data
    assert data["completion"] is None
    assert data["quiz_average"] is None
    assert data["submission_rate"] is None
    assert data["active_learners"] == 0
    assert data["courses"] == 0

    trend = _api(trainer).get(ENGAGEMENT_URL)
    assert trend.status_code == status.HTTP_200_OK
    assert len(trend.data["lessons_completed"]) == 12

    assert _api(trainer).get(COURSES_URL).data["count"] == 0
    assert _api(trainer).get(DOUBTS_URL).data["count"] == 0


# --------------------------------------------------------------------------- #
# The four tiles
# --------------------------------------------------------------------------- #


def test_completion_averages_over_enrollments(trainer, course, student):
    other = User.objects.create_user(
        email="an-s2@example.com", password="StrongPass123!"
    )
    _enroll(student, course, progress=100)
    _enroll(other, course, progress=50)

    assert _api(trainer).get(SUMMARY_URL).data["completion"] == 75.0


def test_quiz_average_uses_graded_work_only(trainer, course, student):
    other = User.objects.create_user(
        email="an-s3@example.com", password="StrongPass123!"
    )
    _submission(course, trainer, student, Submission.Status.GRADED, percent=90)
    _submission(course, trainer, other, Submission.Status.PASSED, percent=70)
    # Handed in but not marked — must not drag the average to 53.
    third = User.objects.create_user(
        email="an-s4@example.com", password="StrongPass123!"
    )
    _submission(course, trainer, third, Submission.Status.SUBMITTED, percent=0)

    assert _api(trainer).get(SUMMARY_URL).data["quiz_average"] == 80.0


def test_submission_rate_counts_abandoned_attempts_against_you(
    trainer, course, student
):
    other = User.objects.create_user(
        email="an-s5@example.com", password="StrongPass123!"
    )
    _submission(course, trainer, student, Submission.Status.SUBMITTED)
    _submission(course, trainer, other, Submission.Status.IN_PROGRESS)

    # One of two attempts was actually handed in.
    assert _api(trainer).get(SUMMARY_URL).data["submission_rate"] == 50.0


def test_active_learners_counts_people_not_enrollments(trainer, course, student):
    second = Course.objects.create(title="Another", trainer=trainer)
    _enroll(student, course)
    _enroll(student, second)  # same person, two courses

    assert _api(trainer).get(SUMMARY_URL).data["active_learners"] == 1


def test_cancelled_enrollments_are_not_active_learners(trainer, course, student):
    _enroll(student, course, state=Enrollment.Status.CANCELLED)
    assert _api(trainer).get(SUMMARY_URL).data["active_learners"] == 0


# --------------------------------------------------------------------------- #
# Scoping — the leak that matters
# --------------------------------------------------------------------------- #


def test_another_trainers_numbers_never_appear(trainer, rival, student):
    theirs = Course.objects.create(title="Theirs", trainer=rival)
    _enroll(student, theirs, progress=100)
    _submission(theirs, rival, student, Submission.Status.GRADED, percent=95)

    data = _api(trainer).get(SUMMARY_URL).data
    assert data["completion"] is None
    assert data["quiz_average"] is None
    assert data["active_learners"] == 0
    assert data["courses"] == 0

    assert _api(trainer).get(COURSES_URL).data["count"] == 0


# --------------------------------------------------------------------------- #
# Engagement trend
# --------------------------------------------------------------------------- #


def test_engagement_counts_lessons_completed_per_month(trainer, course, student):
    module = Module.objects.create(course=course, title="M1")
    lesson = Lesson.objects.create(module=module, title="L1")
    enrollment = _enroll(student, course)
    LessonProgress.objects.create(
        enrollment=enrollment,
        lesson=lesson,
        status=LessonProgress.Status.COMPLETED,
        completed_at=timezone.now(),
    )

    data = _api(trainer).get(ENGAGEMENT_URL).data
    months = [p["month"] for p in data["lessons_completed"]]

    assert len(months) == 12
    assert months == sorted(months)
    # Both series share one axis, so index n is the same month in each.
    assert months == [p["month"] for p in data["submissions"]]
    assert float(data["lessons_completed"][-1]["value"]) == 1
    assert all(
        float(p["value"]) == 0 for p in data["lessons_completed"][:-1]
    )


def test_engagement_months_are_clamped(trainer):
    assert len(
        _api(trainer).get(ENGAGEMENT_URL, {"months": 3}).data["lessons_completed"]
    ) == 3
    assert len(
        _api(trainer).get(ENGAGEMENT_URL, {"months": 900}).data["lessons_completed"]
    ) == 36


# --------------------------------------------------------------------------- #
# Per-course insights
# --------------------------------------------------------------------------- #


def test_course_insights_carry_learners_completion_and_quiz_average(
    trainer, course, student
):
    other = User.objects.create_user(
        email="an-s6@example.com", password="StrongPass123!"
    )
    _enroll(student, course, progress=80)
    _enroll(other, course, progress=40)
    _submission(course, trainer, student, Submission.Status.GRADED, percent=81)

    row = _api(trainer).get(COURSES_URL).data["results"][0]
    assert row["course"] == "React 19 Pro"
    assert row["learners"] == 2
    assert row["completion"] == 60.0
    assert row["quiz_average"] == 81.0


def test_a_course_with_no_graded_work_has_a_null_quiz_average(
    trainer, course, student
):
    _enroll(student, course, progress=10)

    row = _api(trainer).get(COURSES_URL).data["results"][0]
    assert row["completion"] == 10.0
    assert row["quiz_average"] is None


def test_enrollment_and_submission_joins_do_not_skew_each_other(
    trainer, course, student
):
    """Averaging both in one statement multiplies the rows and quietly changes
    both numbers. Three learners, one graded submission at 60."""
    for i in range(3):
        learner = User.objects.create_user(
            email=f"skew{i}@example.com", password="StrongPass123!"
        )
        _enroll(learner, course, progress=30)
    _submission(course, trainer, student, Submission.Status.GRADED, percent=60)

    row = _api(trainer).get(COURSES_URL).data["results"][0]
    assert row["learners"] == 3
    assert row["completion"] == 30.0
    assert row["quiz_average"] == 60.0


def test_course_insights_are_paginated(trainer):
    for i in range(25):
        Course.objects.create(title=f"Course {i}", trainer=trainer)

    resp = _api(trainer).get(COURSES_URL)
    assert set(resp.data) >= {"count", "next", "previous", "results"}
    assert resp.data["count"] == 25
    assert len(resp.data["results"]) == 20


# --------------------------------------------------------------------------- #
# Doubts
# --------------------------------------------------------------------------- #


def _doubt(trainer, course, student, text, state=SessionDoubt.Status.OPEN):
    session = LiveSession.objects.create(
        course=course, trainer=trainer, title="Session 1"
    )
    return SessionDoubt.objects.create(
        session=session, student=student, text=text, status=state
    )


def test_doubts_return_the_questions_themselves(trainer, course, student):
    _doubt(trainer, course, student, "How does useEffect cleanup work?")

    row = _api(trainer).get(DOUBTS_URL).data["results"][0]
    assert row["text"] == "How does useEffect cleanup work?"
    assert row["status"] == "open"
    assert row["student_name"] == "Riya Sharma"
    assert row["course"] == "React 19 Pro"


def test_open_doubts_come_first(trainer, course, student):
    _doubt(trainer, course, student, "Answered one", SessionDoubt.Status.ANSWERED)
    _doubt(trainer, course, student, "Open one", SessionDoubt.Status.OPEN)

    rows = _api(trainer).get(DOUBTS_URL).data["results"]
    assert rows[0]["text"] == "Open one"


def test_doubts_can_be_filtered_to_the_actionable_ones(trainer, course, student):
    _doubt(trainer, course, student, "Answered", SessionDoubt.Status.ANSWERED)
    _doubt(trainer, course, student, "Open", SessionDoubt.Status.OPEN)

    resp = _api(trainer).get(DOUBTS_URL, {"status": "open"})
    assert resp.data["count"] == 1
    assert resp.data["results"][0]["text"] == "Open"


def test_you_never_see_another_trainers_doubts(trainer, rival, student):
    theirs = Course.objects.create(title="Theirs", trainer=rival)
    _doubt(rival, theirs, student, "Not yours")

    assert _api(trainer).get(DOUBTS_URL).data["count"] == 0


def test_doubts_are_paginated(trainer, course, student):
    session = LiveSession.objects.create(
        course=course, trainer=trainer, title="Session 1"
    )
    for i in range(25):
        SessionDoubt.objects.create(
            session=session, student=student, text=f"Question {i}"
        )

    resp = _api(trainer).get(DOUBTS_URL)
    assert resp.data["count"] == 25
    assert len(resp.data["results"]) == 20


# --------------------------------------------------------------------------- #
# Answering a doubt
# --------------------------------------------------------------------------- #


def _answer_url(doubt):
    return f"{DOUBTS_URL}{doubt.pk}/answer/"


def test_answering_records_the_reply_and_closes_the_doubt(trainer, course, student):
    doubt = _doubt(trainer, course, student, "How does useEffect cleanup work?")

    resp = _api(trainer).post(
        _answer_url(doubt), {"answer": "It runs before the next effect."},
        format="json",
    )

    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["status"] == SessionDoubt.Status.ANSWERED
    assert resp.data["answer"] == "It runs before the next effect."
    assert resp.data["answered_at"] is not None

    doubt.refresh_from_db()
    assert doubt.status == SessionDoubt.Status.ANSWERED
    assert doubt.answered_by_id == trainer.id


def test_an_answered_doubt_leaves_the_open_queue(trainer, course, student):
    doubt = _doubt(trainer, course, student, "Why is my build failing?")
    _api(trainer).post(_answer_url(doubt), {"answer": "Pin the node version."},
                       format="json")

    resp = _api(trainer).get(DOUBTS_URL, {"status": "open"})
    assert resp.data["count"] == 0


def test_an_empty_answer_is_refused(trainer, course, student):
    doubt = _doubt(trainer, course, student, "Anyone?")

    resp = _api(trainer).post(_answer_url(doubt), {"answer": "   "}, format="json")

    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    doubt.refresh_from_db()
    assert doubt.status == SessionDoubt.Status.OPEN


def test_you_cannot_answer_another_trainers_doubt(trainer, rival, course, student):
    theirs = Course.objects.create(title="Theirs", trainer=rival, is_free=True)
    doubt = _doubt(rival, theirs, student, "Not yours")

    resp = _api(trainer).post(_answer_url(doubt), {"answer": "Hijacked."},
                              format="json")

    assert resp.status_code == status.HTTP_404_NOT_FOUND
    doubt.refresh_from_db()
    assert doubt.status == SessionDoubt.Status.OPEN
    assert doubt.answer == ""


def test_students_cannot_answer_doubts(trainer, course, student):
    doubt = _doubt(trainer, course, student, "Can I answer my own question?")

    resp = _api(student).post(_answer_url(doubt), {"answer": "Yes."}, format="json")

    assert resp.status_code in (
        status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND
    )
    doubt.refresh_from_db()
    assert doubt.status == SessionDoubt.Status.OPEN
