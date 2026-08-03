"""Course approval workflow (PRD §2.1 "Course approval").

The lifecycle:

    draft ──submit──▶ pending_review ──approve──▶ published ──archive──▶ archived
      ▲                     │                        │
      └──────reject─────────┘                        │
                                                     │
             curriculum edited ──▶ has_unapproved_changes=True (still live)

Kept out of the views so the rules — what may be submitted, who is told about
it — are stated once and cannot drift between the trainer's endpoint and the
admin's.
"""

from django.utils import timezone
from rest_framework.exceptions import ValidationError

from notifications.models import NotificationCategory
from notifications.services import notify


def assert_submittable(course):
    """A course must actually teach something before it can be reviewed.

    Without this an empty shell reaches the admin queue, gets approved, and
    students open a course with no lessons in it.
    """
    if course.status == course.Status.PUBLISHED:
        raise ValidationError(
            "This course is already published. Curriculum edits are queued for "
            "review automatically."
        )
    modules = course.modules.all()
    if not modules:
        raise ValidationError("Add at least one module before submitting for review.")
    if not any(module.lessons.exists() for module in modules):
        raise ValidationError("Add at least one lesson before submitting for review.")


def _admins():
    from accounts.models import User  # local: avoid an app-load cycle

    return User.objects.filter(role="admin", is_active=True)


def notify_admins(title, body, link):
    for admin in _admins():
        notify(admin, NotificationCategory.COURSE, title=title, body=body, link=link)


def submit_for_review(course):
    """Trainer submits. Validates first, then queues it for an admin."""
    assert_submittable(course)
    course.status = course.Status.PENDING_REVIEW
    course.review_note = ""
    course.save(update_fields=["status", "review_note", "updated_at"])
    notify_admins(
        "Course submitted for review",
        f"“{course.title}” by {course.trainer.full_name or course.trainer.email} "
        "is waiting for approval.",
        f"/admin/courses/{course.slug}",
    )
    return course


def approve(course, note=""):
    """Admin approves: publishes it and clears any queued curriculum changes."""
    course.status = course.Status.PUBLISHED
    course.published_at = course.published_at or timezone.now()
    course.has_unapproved_changes = False
    course.review_note = note
    course.save(
        update_fields=[
            "status", "published_at", "has_unapproved_changes", "review_note",
            "updated_at",
        ]
    )
    notify(
        course.trainer,
        NotificationCategory.COURSE,
        title=f"Course approved: {course.title} 🎉",
        body=note or "Your course is live and open for enrollment.",
        link=f"/trainer/courses/{course.slug}",
    )
    return course


def reject(course, note=""):
    """Admin sends it back to the trainer with a reason.

    Returns the course to ``draft`` so the trainer can edit and resubmit — a
    rejected course sitting in ``pending_review`` would be stuck, since only a
    draft can be submitted.
    """
    course.status = course.Status.DRAFT
    course.review_note = note
    course.save(update_fields=["status", "review_note", "updated_at"])
    notify(
        course.trainer,
        NotificationCategory.COURSE,
        title=f"Changes needed: {course.title}",
        body=note or "An admin sent your course back. Review it and resubmit.",
        link=f"/trainer/courses/{course.slug}",
    )
    return course


def flag_curriculum_change(course):
    """A published course's curriculum changed — queue it for re-review.

    Deliberately does **not** unpublish. Students keep the course they are part
    way through, and the admin gets a flag rather than an outage.
    """
    if course.status != course.Status.PUBLISHED or course.has_unapproved_changes:
        return course
    course.has_unapproved_changes = True
    course.save(update_fields=["has_unapproved_changes", "updated_at"])
    notify_admins(
        "Published course changed",
        f"“{course.title}” had its curriculum edited after approval.",
        f"/admin/courses/{course.slug}",
    )
    return course
