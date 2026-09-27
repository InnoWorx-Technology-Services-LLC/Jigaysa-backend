"""Course feedback form: authoring, submission rules and the trainer summary."""

import pytest
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Role
from courses.models import Course, Enrollment, FeedbackForm, FeedbackResponse

User = get_user_model()

pytestmark = pytest.mark.django_db

FORMS = "/api/v1/feedback-forms/"
RESPONSES = "/api/v1/feedback-responses/"


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def trainer(db):
    return User.objects.create_user(
        email="trainer@example.com", password="StrongPass123!", role=Role.TRAINER
    )


@pytest.fixture
def other_trainer(db):
    return User.objects.create_user(
        email="t2@example.com", password="StrongPass123!", role=Role.TRAINER
    )


@pytest.fixture
def student(db):
    return User.objects.create_user(
        email="stu@example.com", password="StrongPass123!", role=Role.STUDENT
    )


@pytest.fixture
def course(trainer):
    return Course.objects.create(
        title="Feedback 101",
        trainer=trainer,
        is_free=True,
        status=Course.Status.PUBLISHED,
    )


def auth(api, user):
    api.force_authenticate(user=user)
    return api


def build_form(api, course, **overrides):
    payload = {
        "course": course.id,
        "title": "How did we do?",
        "questions": [
            {"question_type": "rating", "text": "Rate the course", "order": 0},
            {
                "question_type": "text",
                "text": "What could be better?",
                "is_required": False,
                "order": 1,
            },
        ],
    }
    payload.update(overrides)
    return api.post(FORMS, payload, format="json")


# --- authoring --------------------------------------------------------------


def test_trainer_creates_form_with_questions(api, trainer, course):
    auth(api, trainer)
    resp = build_form(api, course)
    assert resp.status_code == status.HTTP_201_CREATED
    form = FeedbackForm.objects.get()
    assert form.course == course
    assert form.questions.count() == 2
    assert resp.data["response_count"] == 0


def test_trainer_cannot_add_form_to_another_trainers_course(
    api, other_trainer, course
):
    auth(api, other_trainer)
    resp = build_form(api, course)
    assert resp.status_code == status.HTTP_403_FORBIDDEN


def test_updating_form_replaces_the_question_set(api, trainer, course):
    auth(api, trainer)
    form_id = build_form(api, course).data["id"]
    resp = api.patch(
        f"{FORMS}{form_id}/",
        {
            "questions": [
                {"question_type": "yes_no", "text": "Would you recommend it?"}
            ]
        },
        format="json",
    )
    assert resp.status_code == status.HTTP_200_OK
    form = FeedbackForm.objects.get(pk=form_id)
    assert [q.question_type for q in form.questions.all()] == ["yes_no"]


def test_choice_question_requires_options(api, trainer, course):
    auth(api, trainer)
    resp = build_form(
        api,
        course,
        questions=[
            {"question_type": "choice", "text": "Pace?", "options": ["Too fast"]}
        ],
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


# --- student submission -----------------------------------------------------


def test_enrolled_student_submits_feedback(api, trainer, student, course):
    auth(api, trainer)
    form = build_form(api, course).data
    rating_q, text_q = form["questions"]
    Enrollment.objects.create(student=student, course=course)

    auth(api, student)
    resp = api.post(
        RESPONSES,
        {
            "form": form["id"],
            "answers": [
                {"question": rating_q["id"], "rating": 5},
                {"question": text_q["id"], "text": "More exercises"},
            ],
        },
        format="json",
    )
    assert resp.status_code == status.HTTP_201_CREATED
    saved = FeedbackResponse.objects.get()
    assert saved.student == student
    assert saved.submitted_at is not None
    assert saved.answers.count() == 2


def test_unenrolled_student_cannot_submit(api, trainer, student, course):
    auth(api, trainer)
    form = build_form(api, course).data

    auth(api, student)
    resp = api.post(
        RESPONSES,
        {
            "form": form["id"],
            "answers": [{"question": form["questions"][0]["id"], "rating": 4}],
        },
        format="json",
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_student_cannot_submit_twice(api, trainer, student, course):
    auth(api, trainer)
    form = build_form(api, course).data
    Enrollment.objects.create(student=student, course=course)
    body = {
        "form": form["id"],
        "answers": [{"question": form["questions"][0]["id"], "rating": 4}],
    }

    auth(api, student)
    assert api.post(RESPONSES, body, format="json").status_code == 201
    assert api.post(RESPONSES, body, format="json").status_code == 400


def test_required_question_must_be_answered(api, trainer, student, course):
    auth(api, trainer)
    form = build_form(api, course).data
    Enrollment.objects.create(student=student, course=course)

    auth(api, student)
    resp = api.post(
        RESPONSES,
        {
            "form": form["id"],
            "answers": [
                {"question": form["questions"][1]["id"], "text": "only optional"}
            ],
        },
        format="json",
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_rating_out_of_range_is_rejected(api, trainer, student, course):
    auth(api, trainer)
    form = build_form(api, course).data
    Enrollment.objects.create(student=student, course=course)

    auth(api, student)
    resp = api.post(
        RESPONSES,
        {
            "form": form["id"],
            "answers": [{"question": form["questions"][0]["id"], "rating": 9}],
        },
        format="json",
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_require_completion_blocks_until_course_is_finished(
    api, trainer, student, course
):
    auth(api, trainer)
    form = build_form(api, course, require_completion=True).data
    enrollment = Enrollment.objects.create(student=student, course=course)
    body = {
        "form": form["id"],
        "answers": [{"question": form["questions"][0]["id"], "rating": 5}],
    }

    auth(api, student)
    assert api.post(RESPONSES, body, format="json").status_code == 400

    enrollment.completed_at = "2026-01-01T00:00:00Z"
    enrollment.save(update_fields=["completed_at"])
    assert api.post(RESPONSES, body, format="json").status_code == 201


def test_inactive_form_rejects_submissions(api, trainer, student, course):
    auth(api, trainer)
    form = build_form(api, course, is_active=False).data
    Enrollment.objects.create(student=student, course=course)

    auth(api, student)
    resp = api.post(
        RESPONSES,
        {
            "form": form["id"],
            "answers": [{"question": form["questions"][0]["id"], "rating": 5}],
        },
        format="json",
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


# --- visibility -------------------------------------------------------------


def test_student_sees_only_their_own_response(
    api, trainer, student, other_trainer, course
):
    auth(api, trainer)
    form = build_form(api, course).data
    peer = User.objects.create_user(
        email="peer@example.com", password="StrongPass123!", role=Role.STUDENT
    )
    for learner in (student, peer):
        Enrollment.objects.create(student=learner, course=course)
        auth(api, learner)
        api.post(
            RESPONSES,
            {
                "form": form["id"],
                "answers": [{"question": form["questions"][0]["id"], "rating": 5}],
            },
            format="json",
        )

    auth(api, student)
    listed = api.get(RESPONSES).data["results"]
    assert [r["student"]["id"] for r in listed] == [student.id]

    auth(api, trainer)
    assert len(api.get(RESPONSES).data["results"]) == 2


def test_anonymous_form_hides_the_respondent(api, trainer, student, course):
    auth(api, trainer)
    form = build_form(api, course, is_anonymous=True).data
    Enrollment.objects.create(student=student, course=course)

    auth(api, student)
    api.post(
        RESPONSES,
        {
            "form": form["id"],
            "answers": [{"question": form["questions"][0]["id"], "rating": 5}],
        },
        format="json",
    )

    auth(api, trainer)
    listed = api.get(RESPONSES).data["results"]
    assert listed[0]["student"] is None
    # The link is still stored — it is what stops a second submission.
    assert FeedbackResponse.objects.get().student == student


# --- summary ----------------------------------------------------------------


def test_summary_aggregates_answers(api, trainer, student, course):
    auth(api, trainer)
    form = build_form(api, course).data
    rating_q, text_q = form["questions"]
    Enrollment.objects.create(student=student, course=course)

    auth(api, student)
    api.post(
        RESPONSES,
        {
            "form": form["id"],
            "answers": [
                {"question": rating_q["id"], "rating": 4},
                {"question": text_q["id"], "text": "More exercises"},
            ],
        },
        format="json",
    )

    auth(api, trainer)
    resp = api.get(f"{FORMS}{form['id']}/summary/")
    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["response_count"] == 1
    rating_summary, text_summary = resp.data["questions"]
    assert rating_summary["average"] == 4
    assert rating_summary["distribution"] == {"4": 1}
    assert text_summary["responses"] == ["More exercises"]


def test_summary_is_closed_to_other_trainers(api, trainer, other_trainer, course):
    auth(api, trainer)
    form = build_form(api, course).data

    auth(api, other_trainer)
    resp = api.get(f"{FORMS}{form['id']}/summary/")
    assert resp.status_code in (
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
    )
