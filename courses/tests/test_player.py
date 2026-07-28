"""Course player: curriculum with per-lesson progress + gated video (§3.12)."""
import pytest
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Role, User
from courses.models import (
    Category, Course, Enrollment, Lesson, LessonProgress, Module,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def trainer():
    return User.objects.create_user(
        email="t@example.com", password="StrongPass123!", role=Role.TRAINER
    )


@pytest.fixture
def student():
    return User.objects.create_user(
        email="s@example.com", password="StrongPass123!", role=Role.STUDENT
    )


@pytest.fixture
def course(trainer):
    c = Course.objects.create(
        title="Intro to Data Science", trainer=trainer, is_free=True,
        category=Category.objects.create(name="Data"),
        status=Course.Status.PUBLISHED,
    )
    m = Module.objects.create(course=c, title="01 · Foundations", order=1)
    Lesson.objects.create(module=m, title="Welcome", order=1, content_type="video",
                          duration_minutes=6, is_preview=True,
                          video_url="https://cdn.example.com/welcome.mp4")
    Lesson.objects.create(module=m, title="Your first notebook", order=2,
                          content_type="video", duration_minutes=14,
                          video_url="https://cdn.example.com/nb.mp4")
    Lesson.objects.create(module=m, title="Checkpoint quiz", order=3,
                          content_type="quiz", duration_minutes=10)
    return c


def _lessons(resp):
    return {l["title"]: l for m in resp.data["modules"] for l in m["lessons"]}


def test_curriculum_locked_for_non_enrolled(course, student):
    api = APIClient(); api.force_authenticate(student)
    resp = api.get(f"/api/v1/courses/{course.slug}/curriculum/")
    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["has_access"] is False
    assert resp.data["progress_pct"] == 0
    lessons = _lessons(resp)
    # Preview lesson is playable even when not enrolled; others are locked.
    assert lessons["Welcome"]["locked"] is False
    assert lessons["Welcome"]["video_url"] == "https://cdn.example.com/welcome.mp4"
    assert lessons["Your first notebook"]["locked"] is True
    assert lessons["Your first notebook"]["video_url"] == ""
    # No progress for anyone yet.
    assert lessons["Welcome"]["completed"] is False


def test_curriculum_shows_completion_ticks_for_enrolled(course, student):
    enrollment = Enrollment.objects.create(
        student=student, course=course,
        status=Enrollment.Status.ACTIVE, source=Enrollment.Source.FREE,
    )
    welcome = course.modules.first().lessons.get(title="Welcome")
    LessonProgress.objects.create(
        enrollment=enrollment, lesson=welcome,
        status=LessonProgress.Status.COMPLETED, watch_pct=100,
        last_position_seconds=360,
    )
    nb = course.modules.first().lessons.get(title="Your first notebook")
    LessonProgress.objects.create(
        enrollment=enrollment, lesson=nb,
        status=LessonProgress.Status.IN_PROGRESS, watch_pct=40,
        last_position_seconds=120,
    )

    api = APIClient(); api.force_authenticate(student)
    resp = api.get(f"/api/v1/courses/{course.slug}/curriculum/")
    assert resp.data["has_access"] is True
    lessons = _lessons(resp)

    assert lessons["Welcome"]["completed"] is True
    assert lessons["Welcome"]["watch_pct"] == 100
    # In-progress lesson: not complete, but carries resume position.
    assert lessons["Your first notebook"]["completed"] is False
    assert lessons["Your first notebook"]["watch_pct"] == 40
    assert lessons["Your first notebook"]["last_position_seconds"] == 120
    # Untouched lesson defaults to zero.
    assert lessons["Checkpoint quiz"]["completed"] is False
    assert lessons["Checkpoint quiz"]["watch_pct"] == 0
    # Enrolled → all lessons unlocked and playable.
    assert lessons["Your first notebook"]["video_url"] == "https://cdn.example.com/nb.mp4"


def test_private_video_key_falls_back_when_storage_off(course, student, settings):
    # No bucket configured → video_key can't be presigned; fall back to video_url.
    settings.AWS_STORAGE_BUCKET_NAME = ""
    Enrollment.objects.create(
        student=student, course=course,
        status=Enrollment.Status.ACTIVE, source=Enrollment.Source.FREE,
    )
    nb = course.modules.first().lessons.get(title="Your first notebook")
    nb.video_key = "lesson-videos/2/2026/07/abc-nb.mp4"
    nb.save(update_fields=["video_key"])

    api = APIClient(); api.force_authenticate(student)
    resp = api.get(f"/api/v1/courses/{course.slug}/curriculum/")
    lessons = _lessons(resp)
    # Storage off → not a signed URL; falls back to the stored URL.
    assert lessons["Your first notebook"]["video_url"] == "https://cdn.example.com/nb.mp4"
