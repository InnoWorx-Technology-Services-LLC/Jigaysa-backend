import pytest
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Role, User
from assessments.models import Assessment, Choice, Question, Submission
from courses.models import Category, Course

pytestmark = pytest.mark.django_db


@pytest.fixture
def trainer():
    return User.objects.create_user(
        email="trainer@example.com", password="StrongPass123!", role=Role.TRAINER
    )


@pytest.fixture
def student():
    return User.objects.create_user(
        email="stu@example.com", password="StrongPass123!", role=Role.STUDENT
    )


@pytest.fixture
def quiz(trainer):
    course = Course.objects.create(
        title="DS", trainer=trainer, category=Category.objects.create(name="Data")
    )
    assessment = Assessment.objects.create(
        course=course,
        trainer=trainer,
        title="Checkpoint",
        assessment_type=Assessment.AssessmentType.QUIZ,
        pass_percent=50,
        max_attempts=1,
        is_published=True,
    )
    q1 = Question.objects.create(
        assessment=assessment, text="2+2?", question_type=Question.QuestionType.MCQ, points=1
    )
    Choice.objects.create(question=q1, text="4", is_correct=True)
    Choice.objects.create(question=q1, text="5", is_correct=False)
    q2 = Question.objects.create(
        assessment=assessment, text="Sky?", question_type=Question.QuestionType.MCQ, points=1
    )
    Choice.objects.create(question=q2, text="Blue", is_correct=True)
    Choice.objects.create(question=q2, text="Green", is_correct=False)
    return assessment


def _correct_choice(question):
    return question.choices.get(is_correct=True).id


def test_choices_hide_answer_key_from_students(quiz, student):
    api = APIClient()
    api.force_authenticate(student)
    resp = api.get(f"/api/v1/assessments/{quiz.id}/")
    assert resp.status_code == status.HTTP_200_OK
    first_choice = resp.data["questions"][0]["choices"][0]
    assert "is_correct" not in first_choice


def test_submit_autogrades_and_passes(quiz, student):
    api = APIClient()
    api.force_authenticate(student)
    questions = list(quiz.questions.all())
    payload = {
        "answers": [
            {"question": q.id, "selected_choices": [_correct_choice(q)]}
            for q in questions
        ]
    }
    resp = api.post(
        f"/api/v1/assessments/{quiz.id}/submit/", payload, format="json"
    )
    assert resp.status_code == status.HTTP_201_CREATED
    assert resp.data["percent"] == 100
    assert resp.data["passed"] is True
    assert resp.data["status"] == Submission.Status.PASSED


def test_submit_partial_fails(quiz, student):
    api = APIClient()
    api.force_authenticate(student)
    questions = list(quiz.questions.all())
    wrong = questions[0].choices.get(is_correct=False).id
    payload = {
        "answers": [
            {"question": questions[0].id, "selected_choices": [wrong]},
            {"question": questions[1].id, "selected_choices": [_correct_choice(questions[1])]},
        ]
    }
    resp = api.post(f"/api/v1/assessments/{quiz.id}/submit/", payload, format="json")
    assert resp.data["percent"] == 50
    # pass_percent is 50, so exactly 50 passes
    assert resp.data["passed"] is True


def test_max_attempts_enforced(quiz, student):
    api = APIClient()
    api.force_authenticate(student)
    q = quiz.questions.first()
    payload = {"answers": [{"question": q.id, "selected_choices": [_correct_choice(q)]}]}
    first = api.post(f"/api/v1/assessments/{quiz.id}/submit/", payload, format="json")
    assert first.status_code == status.HTTP_201_CREATED
    second = api.post(f"/api/v1/assessments/{quiz.id}/submit/", payload, format="json")
    assert second.status_code == status.HTTP_400_BAD_REQUEST


def test_descriptive_holds_for_manual_grade(trainer, student):
    course = Course.objects.create(
        title="Writing", trainer=trainer,
        category=Category.objects.create(name="Lang"),
    )
    assessment = Assessment.objects.create(
        course=course, trainer=trainer, title="Essay",
        assessment_type=Assessment.AssessmentType.DESCRIPTIVE,
        grading_type=Assessment.GradingType.MANUAL,
        pass_percent=50, max_attempts=3, is_published=True,
    )
    q = Question.objects.create(
        assessment=assessment, text="Discuss.",
        question_type=Question.QuestionType.DESCRIPTIVE, points=10,
    )
    api = APIClient()
    api.force_authenticate(student)
    resp = api.post(
        f"/api/v1/assessments/{assessment.id}/submit/",
        {"answers": [{"question": q.id, "text_answer": "My essay."}]},
        format="json",
    )
    assert resp.status_code == status.HTTP_201_CREATED
    assert resp.data["status"] == Submission.Status.SUBMITTED
    submission_id = resp.data["id"]

    # Trainer grades it.
    api.force_authenticate(trainer)
    graded = api.post(
        f"/api/v1/submissions/{submission_id}/grade/",
        {"score": "8", "percent": 80, "feedback": "Good"},
        format="json",
    )
    assert graded.status_code == status.HTTP_200_OK
    assert graded.data["passed"] is True
    assert graded.data["status"] == Submission.Status.PASSED


# --- trainer question authoring (the editor's "Save questions") -------------


def _api(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def test_trainer_saves_a_question_set_in_one_call(trainer, quiz):
    resp = _api(trainer).post(
        f"/api/v1/assessments/{quiz.id}/questions/",
        {
            "questions": [
                {
                    "question_type": "mcq",
                    "text": "Which is a hook?",
                    "points": 2,
                    "choices": [
                        {"text": "useState", "is_correct": True},
                        {"text": "componentDidMount", "is_correct": False},
                    ],
                },
                {"question_type": "descriptive", "text": "Explain the diff.", "points": 5},
            ]
        },
        format="json",
    )

    assert resp.status_code == status.HTTP_200_OK
    assert len(resp.data) == 2
    # Replaces the whole set — the fixture's questions are gone.
    assert Question.objects.filter(assessment=quiz).count() == 2
    quiz.refresh_from_db()
    assert quiz.total_questions == 2
    assert resp.data[0]["choices"][0]["is_correct"] is True


def test_students_cannot_read_the_answer_key(student, quiz):
    resp = _api(student).get(f"/api/v1/assessments/{quiz.id}/questions/")
    assert resp.status_code == status.HTTP_403_FORBIDDEN


def test_another_trainer_cannot_edit_the_question_set(quiz):
    intruder = User.objects.create_user(
        email="other-t@example.com", password="StrongPass123!", role=Role.TRAINER
    )
    resp = _api(intruder).post(
        f"/api/v1/assessments/{quiz.id}/questions/", {"questions": []}, format="json"
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN


def test_single_answer_question_rejects_two_correct_options(trainer, quiz):
    resp = _api(trainer).post(
        f"/api/v1/assessments/{quiz.id}/questions/",
        {
            "questions": [
                {
                    "question_type": "mcq",
                    "text": "Pick one",
                    "choices": [
                        {"text": "a", "is_correct": True},
                        {"text": "b", "is_correct": True},
                    ],
                }
            ]
        },
        format="json",
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_objective_question_needs_a_correct_option(trainer, quiz):
    resp = _api(trainer).post(
        f"/api/v1/assessments/{quiz.id}/questions/",
        {
            "questions": [
                {
                    "question_type": "mcq",
                    "text": "Pick one",
                    "choices": [
                        {"text": "a", "is_correct": False},
                        {"text": "b", "is_correct": False},
                    ],
                }
            ]
        },
        format="json",
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_authored_questions_are_gradable_end_to_end(trainer, quiz, student):
    """The whole point: what the trainer saves must actually score a student."""
    from courses.models import Enrollment

    _api(trainer).post(
        f"/api/v1/assessments/{quiz.id}/questions/",
        {
            "questions": [
                {
                    "question_type": "mcq",
                    "text": "2+2?",
                    "points": 1,
                    "choices": [
                        {"text": "4", "is_correct": True},
                        {"text": "5", "is_correct": False},
                    ],
                }
            ]
        },
        format="json",
    )
    Enrollment.objects.create(student=student, course=quiz.course)
    question = Question.objects.get(assessment=quiz)
    correct = question.choices.get(is_correct=True)

    resp = _api(student).post(
        f"/api/v1/assessments/{quiz.id}/submit/",
        {"answers": [{"question": question.id, "selected_choices": [correct.id]}]},
        format="json",
    )
    assert resp.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED)
    assert resp.data["percent"] == 100
