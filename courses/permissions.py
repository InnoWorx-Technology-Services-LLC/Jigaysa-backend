"""Object-level permissions for course authoring (PRD §3.2).

Write access to a course and everything hanging off it (modules, lessons,
resources, batches) is limited to the course's trainer or an admin. Reads are
governed by the view's queryset, so SAFE methods pass through here.
"""

from rest_framework.permissions import SAFE_METHODS, BasePermission

from courses.models import (
    Batch,
    Course,
    CourseReview,
    Enrollment,
    FeedbackForm,
    FeedbackQuestion,
    Lesson,
    LessonProgress,
    LessonResource,
    Module,
)


def course_of(obj):
    """Resolve the owning ``Course`` for any course-tree object."""
    if isinstance(obj, Course):
        return obj
    if isinstance(obj, Module):
        return obj.course
    if isinstance(obj, Lesson):
        return obj.module.course
    if isinstance(obj, LessonResource):
        return obj.lesson.module.course
    if isinstance(obj, (Batch, CourseReview, Enrollment, FeedbackForm)):
        return obj.course
    if isinstance(obj, FeedbackQuestion):
        return obj.form.course
    if isinstance(obj, LessonProgress):
        return obj.lesson.module.course
    return None


class IsTrainerOwnerOrReadOnly(BasePermission):
    """Allow reads to anyone the queryset permits; writes only to the owning
    trainer of the related course, or an admin."""

    def has_object_permission(self, request, view, obj):
        if request.method in SAFE_METHODS:
            return True
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if getattr(user, "role", None) == "admin":
            return True
        course = course_of(obj)
        return bool(course and course.trainer_id == user.id)
