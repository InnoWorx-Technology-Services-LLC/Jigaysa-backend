"""Event → notification + gamification wiring (PRD §3.12).

Connected in ``NotificationsConfig.ready()``. Kept as post_save receivers so
producers (enrollment, grading, certificates…) don't need to know notifications
exist. Handlers are defensive: a notification failure must never break the
underlying action, so callers wrap nothing and we swallow lookups gracefully.
"""

from django.db.models.signals import post_save
from django.dispatch import receiver

from engagement.services import award_badge, award_points
from notifications.models import NotificationCategory
from notifications.services import notify


@receiver(post_save, sender="courses.Enrollment", dispatch_uid="notify_enrollment")
def on_enrollment(sender, instance, created, **kwargs):
    if not created:
        return
    course = instance.course
    notify(
        instance.student,
        NotificationCategory.COURSE,
        title=f"Enrolled in {course.title}",
        body="You're all set — start learning any time.",
        link=f"/courses/{course.slug}",
    )
    award_points(instance.student, "enroll")


@receiver(post_save, sender="certificates.Certificate", dispatch_uid="notify_certificate")
def on_certificate(sender, instance, created, **kwargs):
    if not created:
        return
    notify(
        instance.student,
        NotificationCategory.CERTIFICATE,
        title="Certificate issued 🎓",
        body=f"Your certificate for {instance.course.title} is ready to download.",
        link="/certificates",
    )
    award_points(instance.student, "certificate")
    award_badge(instance.student, "first-steps")


@receiver(post_save, sender="assessments.Submission", dispatch_uid="notify_submission")
def on_submission(sender, instance, created, **kwargs):
    # Fire only when a submission reaches a passed/graded terminal state.
    if instance.status == instance.Status.PASSED:
        notify(
            instance.student,
            NotificationCategory.ASSESSMENT,
            title=f"You passed: {instance.assessment.title}",
            body=f"Score {instance.percent}%. Well done!",
            link=f"/assessments/{instance.assessment_id}",
        )
        award_points(instance.student, "assessment_pass")
        award_badge(instance.student, "quiz-master")
    elif instance.status == instance.Status.GRADED:
        notify(
            instance.student,
            NotificationCategory.ASSESSMENT,
            title=f"Graded: {instance.assessment.title}",
            body="Your submission has been reviewed.",
            link=f"/assessments/{instance.assessment_id}",
        )


@receiver(
    post_save,
    sender="live.IndividualBooking",
    dispatch_uid="notify_individual_booking",
)
def on_individual_booking(sender, instance, created, **kwargs):
    # Only the request itself; confirm/decline/cancel notify from the view,
    # where the transition that fired them is known.
    if not created:
        return
    student = instance.student
    notify(
        instance.trainer,
        NotificationCategory.LIVE_CLASS,
        title="New 1:1 booking request",
        body=f"{student.full_name or student.email} requested "
        f"“{instance.topic or 'a session'}”.",
        link="/trainer/sessions",
    )


@receiver(
    post_save,
    sender="live.SessionRegistration",
    dispatch_uid="notify_session_registration",
)
def on_session_registration(sender, instance, created, **kwargs):
    if not created:
        return
    notify(
        instance.student,
        NotificationCategory.LIVE_CLASS,
        title=f"Registered: {instance.session.title}",
        body="We'll remind you before it starts.",
        link=f"/live/{instance.session_id}",
    )
