import pytest
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Role
from courses.models import Course, Enrollment, Lesson, Module

User = get_user_model()

pytestmark = pytest.mark.django_db


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


def auth(api, user):
    api.force_authenticate(user=user)
    return api


# --- authoring --------------------------------------------------------------


def test_trainer_creates_course(api, trainer):
    auth(api, trainer)
    resp = api.post(
        "/api/v1/courses/",
        {"title": "Python 101", "is_free": True, "course_type": "self_paced"},
        format="json",
    )
    assert resp.status_code == status.HTTP_201_CREATED
    course = Course.objects.get()
    assert course.trainer == trainer
    assert course.slug == "python-101"
    assert course.status == Course.Status.DRAFT


def test_student_cannot_create_course(api, student):
    auth(api, student)
    resp = api.post("/api/v1/courses/", {"title": "X"}, format="json")
    assert resp.status_code == status.HTTP_403_FORBIDDEN


def test_trainer_cannot_edit_others_course(api, trainer, other_trainer):
    course = Course.objects.create(title="A", trainer=trainer)
    auth(api, other_trainer)
    resp = api.patch(
        f"/api/v1/courses/{course.slug}/", {"title": "Hacked"}, format="json"
    )
    assert resp.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)


# --- visibility -------------------------------------------------------------


def test_student_sees_only_published_courses(api, trainer, student):
    Course.objects.create(title="Draft", trainer=trainer)
    Course.objects.create(
        title="Live", trainer=trainer, status=Course.Status.PUBLISHED
    )
    auth(api, student)
    resp = api.get("/api/v1/courses/")
    assert resp.status_code == status.HTTP_200_OK
    titles = [c["title"] for c in resp.data["results"]]
    assert titles == ["Live"]


def _with_curriculum(course):
    """Give a course the minimum an admin can meaningfully review."""
    module = Module.objects.create(course=course, title="M1")
    Lesson.objects.create(module=module, title="L1")
    return course


def test_publish_by_trainer_sets_pending(api, trainer):
    course = _with_curriculum(Course.objects.create(title="A", trainer=trainer))
    auth(api, trainer)
    resp = api.post(f"/api/v1/courses/{course.slug}/publish/")
    assert resp.status_code == status.HTTP_200_OK
    course.refresh_from_db()
    assert course.status == Course.Status.PENDING_REVIEW


# --- approval workflow (PRD §2.1 course approval) ---------------------------


@pytest.fixture
def admin(db):
    return User.objects.create_user(
        email="admin@example.com", password="StrongPass123!", role=Role.ADMIN
    )


def test_empty_course_cannot_be_submitted(api, trainer):
    """An empty shell must not reach the admin queue."""
    course = Course.objects.create(title="Hollow", trainer=trainer)
    auth(api, trainer)
    resp = api.post(f"/api/v1/courses/{course.slug}/publish/")
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    course.refresh_from_db()
    assert course.status == Course.Status.DRAFT


def test_module_without_lessons_cannot_be_submitted(api, trainer):
    course = Course.objects.create(title="Outline only", trainer=trainer)
    Module.objects.create(course=course, title="M1")
    auth(api, trainer)
    resp = api.post(f"/api/v1/courses/{course.slug}/publish/")
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_admin_approves_and_notifies_the_trainer(api, trainer, admin):
    course = _with_curriculum(
        Course.objects.create(
            title="Ready", trainer=trainer, status=Course.Status.PENDING_REVIEW
        )
    )
    auth(api, admin)
    resp = api.post(
        f"/api/v1/courses/{course.slug}/publish/", {"note": "Looks good"},
        format="json",
    )
    assert resp.status_code == status.HTTP_200_OK
    course.refresh_from_db()
    assert course.status == Course.Status.PUBLISHED
    assert course.published_at is not None
    assert trainer.notifications.filter(title__startswith="Course approved").exists()


def test_admin_rejects_back_to_draft_with_a_reason(api, trainer, admin):
    course = _with_curriculum(
        Course.objects.create(
            title="Thin", trainer=trainer, status=Course.Status.PENDING_REVIEW
        )
    )
    auth(api, admin)
    resp = api.post(
        f"/api/v1/courses/{course.slug}/reject/", {"note": "Add more depth"},
        format="json",
    )
    assert resp.status_code == status.HTTP_200_OK
    course.refresh_from_db()
    # Draft, not pending — otherwise the trainer could never resubmit.
    assert course.status == Course.Status.DRAFT
    assert course.review_note == "Add more depth"
    assert trainer.notifications.filter(title__startswith="Changes needed").exists()


def test_rejection_requires_a_reason(api, trainer, admin):
    course = Course.objects.create(
        title="X", trainer=trainer, status=Course.Status.PENDING_REVIEW
    )
    auth(api, admin)
    resp = api.post(f"/api/v1/courses/{course.slug}/reject/", {}, format="json")
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_trainer_cannot_reject(api, trainer, other_trainer):
    course = Course.objects.create(
        title="X", trainer=trainer, status=Course.Status.PENDING_REVIEW
    )
    auth(api, trainer)
    resp = api.post(
        f"/api/v1/courses/{course.slug}/reject/", {"note": "nope"}, format="json"
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN


def test_trainer_cannot_unpublish_a_live_course_by_republishing(api, trainer):
    """The old foot-gun: publish/ on a live course yanked it from the catalog."""
    course = _with_curriculum(
        Course.objects.create(
            title="Live", trainer=trainer, status=Course.Status.PUBLISHED
        )
    )
    auth(api, trainer)
    resp = api.post(f"/api/v1/courses/{course.slug}/publish/")
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    course.refresh_from_db()
    assert course.status == Course.Status.PUBLISHED


def test_editing_a_published_curriculum_flags_it_without_unpublishing(
    api, trainer, admin
):
    course = _with_curriculum(
        Course.objects.create(
            title="Live", trainer=trainer, status=Course.Status.PUBLISHED
        )
    )
    auth(api, trainer)
    resp = api.post(
        "/api/v1/modules/", {"course": course.id, "title": "Bonus"}, format="json"
    )
    assert resp.status_code == status.HTTP_201_CREATED

    course.refresh_from_db()
    assert course.has_unapproved_changes is True
    # Students must not lose a course they are part-way through.
    assert course.status == Course.Status.PUBLISHED
    assert admin.notifications.filter(title="Published course changed").exists()


def test_review_queue_shows_both_kinds_of_work(api, trainer, admin):
    submitted = _with_curriculum(
        Course.objects.create(
            title="New", trainer=trainer, status=Course.Status.PENDING_REVIEW
        )
    )
    changed = _with_curriculum(
        Course.objects.create(
            title="Changed", trainer=trainer, status=Course.Status.PUBLISHED,
            has_unapproved_changes=True,
        )
    )
    auth(api, admin)
    resp = api.get("/api/v1/courses/review-queue/")

    assert resp.status_code == status.HTTP_200_OK
    assert [c["title"] for c in resp.data["pending_review"]] == [submitted.title]
    assert [c["title"] for c in resp.data["changed_after_approval"]] == [changed.title]


def test_review_queue_is_admin_only(api, trainer):
    auth(api, trainer)
    assert api.get("/api/v1/courses/review-queue/").status_code == (
        status.HTTP_403_FORBIDDEN
    )


def test_approving_clears_the_change_flag(api, trainer, admin):
    course = _with_curriculum(
        Course.objects.create(
            title="Changed", trainer=trainer, status=Course.Status.PUBLISHED,
            has_unapproved_changes=True,
        )
    )
    auth(api, admin)
    api.post(f"/api/v1/courses/{course.slug}/publish/")
    course.refresh_from_db()
    assert course.has_unapproved_changes is False


def test_archiving_removes_it_from_the_catalog_but_keeps_enrollments(
    api, trainer, student
):
    course = _with_curriculum(
        Course.objects.create(
            title="Retired", trainer=trainer, status=Course.Status.PUBLISHED,
            is_free=True,
        )
    )
    Enrollment.objects.create(student=student, course=course)

    auth(api, trainer)
    assert api.post(f"/api/v1/courses/{course.slug}/archive/").status_code == (
        status.HTTP_200_OK
    )

    auth(api, student)
    titles = [c["title"] for c in api.get("/api/v1/courses/").data["results"]]
    assert "Retired" not in titles
    assert Enrollment.objects.filter(student=student, course=course).exists()


# --- enrollment + progress --------------------------------------------------


def test_free_course_enrollment_and_progress(api, trainer, student):
    course = Course.objects.create(
        title="Free Course",
        trainer=trainer,
        is_free=True,
        status=Course.Status.PUBLISHED,
    )
    module = Module.objects.create(course=course, title="M1")
    l1 = Lesson.objects.create(module=module, title="L1")
    l2 = Lesson.objects.create(module=module, title="L2")

    auth(api, student)
    enroll = api.post(f"/api/v1/courses/{course.slug}/enroll/")
    assert enroll.status_code == status.HTTP_201_CREATED
    enrollment = Enrollment.objects.get(student=student, course=course)
    course.refresh_from_db()
    assert course.enrolled_count == 1

    # complete one of two lessons -> 50%
    api.post(
        "/api/v1/lesson-progress/",
        {"enrollment": enrollment.id, "lesson": l1.id, "status": "completed"},
        format="json",
    )
    enrollment.refresh_from_db()
    assert enrollment.progress_pct == 50

    # complete the second -> 100% and enrollment marked completed
    api.post(
        "/api/v1/lesson-progress/",
        {"enrollment": enrollment.id, "lesson": l2.id, "status": "completed"},
        format="json",
    )
    enrollment.refresh_from_db()
    assert enrollment.progress_pct == 100
    assert enrollment.status == Enrollment.Status.COMPLETED


def test_paid_course_enrollment_blocked(api, trainer, student):
    course = Course.objects.create(
        title="Paid", trainer=trainer, is_free=False, status=Course.Status.PUBLISHED
    )
    auth(api, student)
    resp = api.post(f"/api/v1/courses/{course.slug}/enroll/")
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_duplicate_enrollment_rejected(api, trainer, student):
    course = Course.objects.create(
        title="Free", trainer=trainer, is_free=True, status=Course.Status.PUBLISHED
    )
    auth(api, student)
    assert api.post(f"/api/v1/courses/{course.slug}/enroll/").status_code == 201
    assert api.post(f"/api/v1/courses/{course.slug}/enroll/").status_code == 400


# --- reviews ----------------------------------------------------------------


def test_review_requires_enrollment(api, trainer, student):
    course = Course.objects.create(
        title="C", trainer=trainer, is_free=True, status=Course.Status.PUBLISHED
    )
    auth(api, student)
    resp = api.post(
        "/api/v1/reviews/",
        {"course": course.id, "rating": 5, "comment": "great"},
        format="json",
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_review_updates_course_rating(api, trainer, student):
    course = Course.objects.create(
        title="C", trainer=trainer, is_free=True, status=Course.Status.PUBLISHED
    )
    Enrollment.objects.create(student=student, course=course)
    auth(api, student)
    resp = api.post(
        "/api/v1/reviews/",
        {"course": course.id, "rating": 4, "comment": "good"},
        format="json",
    )
    assert resp.status_code == status.HTTP_201_CREATED
    course.refresh_from_db()
    assert course.rating_count == 1
    assert float(course.rating_avg) == 4.0


# --- student player: notes + new course fields ------------------------------


def test_lesson_notes_upsert_and_stay_private(api, trainer, student):
    course = _with_curriculum(
        Course.objects.create(title="Notes", trainer=trainer, is_free=True)
    )
    lesson = Lesson.objects.filter(module__course=course).first()

    auth(api, student)
    first = api.post(
        "/api/v1/lesson-notes/", {"lesson": lesson.id, "body": "draft"}, format="json"
    )
    assert first.status_code == status.HTTP_201_CREATED

    # Posting again edits the same pad rather than making a second note.
    second = api.post(
        "/api/v1/lesson-notes/",
        {"lesson": lesson.id, "body": "heuristics matter"},
        format="json",
    )
    assert second.status_code == status.HTTP_200_OK
    assert second.data["body"] == "heuristics matter"

    listed = api.get(f"/api/v1/lesson-notes/?lesson={lesson.id}").data["results"]
    assert len(listed) == 1

    # The trainer cannot read a student's private notes.
    auth(api, trainer)
    assert api.get(f"/api/v1/lesson-notes/?lesson={lesson.id}").data["results"] == []


def test_trainer_can_save_outcomes_and_settings(api, trainer):
    course = Course.objects.create(title="Settings", trainer=trainer)
    auth(api, trainer)
    resp = api.patch(
        f"/api/v1/courses/{course.slug}/",
        {
            "outcomes": ["Design a research plan", "Run a usability test"],
            "welcome_message": "Welcome aboard!",
            "completion_message": "You did it!",
            "certificate_enabled": False,
            "thumbnail_color": "#8FD14F",
        },
        format="json",
    )
    assert resp.status_code == status.HTTP_200_OK
    course.refresh_from_db()
    assert course.outcomes == ["Design a research plan", "Run a usability test"]
    assert course.certificate_enabled is False
    assert course.thumbnail_color == "#8FD14F"


def test_certificate_toggle_is_honoured(trainer, student):
    """Switching the toggle off must actually stop the certificate."""
    from certificates.services import issue_for_enrollment

    course = Course.objects.create(
        title="No cert", trainer=trainer, is_free=True, certificate_enabled=False
    )
    enrollment = Enrollment.objects.create(
        student=student, course=course, status=Enrollment.Status.COMPLETED
    )
    certificate, created = issue_for_enrollment(enrollment)
    assert certificate is None and created is False

    course.certificate_enabled = True
    course.save(update_fields=["certificate_enabled"])
    enrollment.refresh_from_db()
    certificate, created = issue_for_enrollment(enrollment)
    assert certificate is not None
