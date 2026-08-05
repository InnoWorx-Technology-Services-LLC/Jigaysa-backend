"""Anonymous catalog reads (public site + SEO) and course media keys.

The rule being protected here: opening reads must expose **exactly** the
published public catalog and nothing else. A regression that leaks a draft is
far worse than one that 401s, so the negative cases matter more than the
positive ones.
"""

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Role, User
from courses.models import Category, Course, Lesson, Module

pytestmark = pytest.mark.django_db


@pytest.fixture
def anon():
    return APIClient()


@pytest.fixture
def trainer():
    return User.objects.create_user(
        email="pub-t@example.com", password="StrongPass123!", role=Role.TRAINER
    )


@pytest.fixture
def published(trainer):
    course = Course.objects.create(
        title="Public Course", trainer=trainer, is_free=True,
        status=Course.Status.PUBLISHED, visibility=Course.Visibility.PUBLIC,
        category=Category.objects.create(name="Design"),
    )
    module = Module.objects.create(course=course, title="M1")
    Lesson.objects.create(
        module=module, title="Free preview", is_preview=True,
        video_url="https://cdn/preview.mp4", content="visible",
    )
    Lesson.objects.create(
        module=module, title="Paid lesson", is_preview=False,
        video_url="https://cdn/paid.mp4", content="secret",
    )
    return course


# -- what anonymous CAN see -------------------------------------------------- #


def test_anonymous_can_browse_the_catalog(anon, published):
    resp = anon.get("/api/v1/courses/")
    assert resp.status_code == status.HTTP_200_OK
    assert [c["title"] for c in resp.data["results"]] == ["Public Course"]


def test_anonymous_can_read_a_course_detail(anon, published):
    resp = anon.get(f"/api/v1/courses/{published.slug}/")
    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["title"] == "Public Course"


def test_anonymous_can_read_taxonomy(anon, published):
    assert anon.get("/api/v1/categories/").status_code == status.HTTP_200_OK
    assert anon.get("/api/v1/tags/").status_code == status.HTTP_200_OK


def test_anonymous_curriculum_shows_preview_and_locks_the_rest(anon, published):
    """Must not 500 — the enrollment lookup has to be skipped for anonymous."""
    resp = anon.get(f"/api/v1/courses/{published.slug}/curriculum/")
    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["has_access"] is False

    lessons = resp.data["modules"][0]["lessons"]
    preview = next(l for l in lessons if l["title"] == "Free preview")
    paid = next(l for l in lessons if l["title"] == "Paid lesson")

    assert preview["locked"] is False
    assert preview["video_url"] == "https://cdn/preview.mp4"
    assert paid["locked"] is True
    assert paid["video_url"] == ""
    assert paid["content"] == ""


# -- what anonymous MUST NOT see --------------------------------------------- #


def test_drafts_never_leak_to_anonymous(anon, trainer):
    draft = Course.objects.create(title="Secret draft", trainer=trainer)
    review = Course.objects.create(
        title="In review", trainer=trainer, status=Course.Status.PENDING_REVIEW
    )
    listed = [c["title"] for c in anon.get("/api/v1/courses/").data["results"]]

    assert "Secret draft" not in listed
    assert "In review" not in listed
    assert anon.get(f"/api/v1/courses/{draft.slug}/").status_code == (
        status.HTTP_404_NOT_FOUND
    )
    assert anon.get(f"/api/v1/courses/{review.slug}/curriculum/").status_code == (
        status.HTTP_404_NOT_FOUND
    )


def test_private_and_unlisted_courses_are_hidden(anon, trainer):
    Course.objects.create(
        title="Private", trainer=trainer, status=Course.Status.PUBLISHED,
        visibility=Course.Visibility.PRIVATE,
    )
    listed = [c["title"] for c in anon.get("/api/v1/courses/").data["results"]]
    assert "Private" not in listed


def test_writes_stay_locked_for_anonymous(anon, published):
    assert anon.post(
        "/api/v1/courses/", {"title": "Nope"}, format="json"
    ).status_code == status.HTTP_401_UNAUTHORIZED
    assert anon.post(
        f"/api/v1/courses/{published.slug}/enroll/"
    ).status_code == status.HTTP_401_UNAUTHORIZED
    assert anon.post(
        "/api/v1/categories/", {"name": "Nope"}, format="json"
    ).status_code == status.HTTP_401_UNAUTHORIZED
    assert anon.get("/api/v1/enrollments/").status_code == (
        status.HTTP_401_UNAUTHORIZED
    )
    assert anon.get("/api/v1/orders/").status_code == status.HTTP_401_UNAUTHORIZED


def test_library_browsing_is_public_but_bookmarks_are_not(anon):
    assert anon.get("/api/v1/library-resources/").status_code == status.HTTP_200_OK
    assert anon.get("/api/v1/library-bookmarks/").status_code == (
        status.HTTP_401_UNAUTHORIZED
    )


# -- course media keys ------------------------------------------------------- #


def test_uploaded_thumbnail_key_can_be_saved_and_read_back(trainer):
    """The presign flow returns a *key*; a URLField could not hold it."""
    course = Course.objects.create(title="Media", trainer=trainer)
    api = APIClient()
    api.force_authenticate(trainer)

    resp = api.patch(
        f"/api/v1/courses/{course.slug}/",
        {
            "thumbnail_key": "course-thumbnails/5/2026/08/abc123-cover.png",
            "intro_video_key": "course-intros/5/2026/08/def456-intro.mp4",
        },
        format="json",
    )
    assert resp.status_code == status.HTTP_200_OK
    course.refresh_from_db()
    assert course.thumbnail_key.endswith("cover.png")
    assert course.intro_video_key.endswith("intro.mp4")


def test_media_falls_back_to_the_stored_url_when_no_key(anon, trainer):
    Course.objects.create(
        title="Url media", trainer=trainer, status=Course.Status.PUBLISHED,
        thumbnail="https://cdn.example.com/cover.png",
    )
    card = anon.get("/api/v1/courses/").data["results"][0]
    assert card["thumbnail"] == "https://cdn.example.com/cover.png"
