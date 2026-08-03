"""Course Management Module API (PRD §3.2, §3.12).

Trainers author courses → modules → lessons → resources and run batches.
Students browse the published catalog, enroll in free courses, track per-lesson
progress and leave reviews. Visibility is enforced in ``get_queryset``; write
access is enforced by ``IsTrainerOwnerOrReadOnly``.
"""

from django.db.models import Avg, Count, Q
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.permissions import IsAdmin
from payments import entitlements
from courses.models import (
    Batch,
    Category,
    Course,
    CourseReview,
    Enrollment,
    Lesson,
    LessonNote,
    LessonProgress,
    LessonResource,
    Module,
    Tag,
)
from courses import review
from courses.permissions import IsTrainerOwnerOrReadOnly, course_of
from courses.serializers import (
    BatchSerializer,
    CategorySerializer,
    CourseDetailSerializer,
    CourseListSerializer,
    CourseReviewSerializer,
    CourseWriteSerializer,
    EnrollmentCreateSerializer,
    EnrollmentSerializer,
    LessonNoteSerializer,
    LessonProgressSerializer,
    LessonResourceSerializer,
    LessonSerializer,
    ModulePlayerSerializer,
    ModuleSerializer,
    TagSerializer,
)


# Role sets for OpenAPI badges (see core.schema.RoleAwareAutoSchema). These are
# documentation declarations that mirror the enforced permissions/querysets.
ALL_ROLES = ("student", "trainer", "admin", "institution")
TRAINER_WRITE = ("trainer", "admin")
_AUTHORING_BY_ACTION = {
    "create": TRAINER_WRITE,
    "update": TRAINER_WRITE,
    "partial_update": TRAINER_WRITE,
    "destroy": TRAINER_WRITE,
}


def _is_admin(user):
    return getattr(user, "role", None) == "admin"


def _filter_by(qs, request, param, field=None):
    value = request.query_params.get(param)
    if value:
        qs = qs.filter(**{field or param: value})
    return qs


# --------------------------------------------------------------------------- #
# Taxonomy
# --------------------------------------------------------------------------- #


class CategoryViewSet(viewsets.ModelViewSet):
    """Course taxonomy. Anyone authenticated can read; only admins write."""

    queryset = Category.objects.all()
    serializer_class = CategorySerializer
    api_roles = ALL_ROLES
    api_roles_by_action = {
        "create": ("admin",),
        "update": ("admin",),
        "partial_update": ("admin",),
        "destroy": ("admin",),
    }

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated()]
        return [IsAdmin()]


class TagViewSet(viewsets.ModelViewSet):
    queryset = Tag.objects.all()
    serializer_class = TagSerializer
    api_roles = ALL_ROLES
    api_roles_by_action = {
        "create": ("admin",),
        "update": ("admin",),
        "partial_update": ("admin",),
        "destroy": ("admin",),
    }

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated()]
        return [IsAdmin()]


# --------------------------------------------------------------------------- #
# Courses
# --------------------------------------------------------------------------- #


class CourseViewSet(viewsets.ModelViewSet):
    """Catalog + authoring for courses.

    Filters (query params): ``category``, ``tag``, ``skill_level``,
    ``course_type``, ``is_free``, ``q`` (title/subtitle search), ``trainer``,
    ``mine`` (trainer's own), ``status``. Ordering via ``ordering`` among
    ``created_at``, ``rating_avg``, ``enrolled_count`` (prefix ``-`` for desc).
    """

    lookup_field = "slug"
    permission_classes = [IsAuthenticated, IsTrainerOwnerOrReadOnly]
    api_roles = ALL_ROLES  # list / retrieve / curriculum
    api_roles_by_action = {
        **_AUTHORING_BY_ACTION,
        "publish": TRAINER_WRITE,
        "reject": ("admin",),
        "review_queue": ("admin",),
        "archive": TRAINER_WRITE,
        "enroll": ("student",),
    }

    def get_permissions(self):
        # Enrolling is open to any authenticated user; the ownership check only
        # gates authoring/publish. (Reads pass IsTrainerOwnerOrReadOnly anyway
        # since SAFE methods are allowed.) Without this, get_object() in the
        # enroll action would run the owner check and 403 every student.
        if self.action == "enroll":
            return [IsAuthenticated()]
        return [IsAuthenticated(), IsTrainerOwnerOrReadOnly()]

    def get_serializer_class(self):
        if self.action == "list":
            return CourseListSerializer
        if self.action in ("create", "update", "partial_update"):
            return CourseWriteSerializer
        return CourseDetailSerializer

    def get_queryset(self):
        user = self.request.user
        qs = Course.objects.select_related("trainer", "category").prefetch_related(
            "tags"
        )

        # Visibility: admins see all; trainers see their own + published public;
        # everyone else sees only published public courses.
        if not _is_admin(user):
            if getattr(user, "role", None) == "trainer":
                qs = qs.filter(
                    Q(
                        status=Course.Status.PUBLISHED,
                        visibility=Course.Visibility.PUBLIC,
                    )
                    | Q(trainer=user)
                )
            else:
                qs = qs.filter(
                    status=Course.Status.PUBLISHED,
                    visibility=Course.Visibility.PUBLIC,
                )

        params = self.request.query_params
        qs = _filter_by(qs, self.request, "category", "category_id")
        qs = _filter_by(qs, self.request, "skill_level")
        qs = _filter_by(qs, self.request, "course_type")
        qs = _filter_by(qs, self.request, "trainer", "trainer_id")
        qs = _filter_by(qs, self.request, "status")
        if "tag" in params:
            qs = qs.filter(tags__slug=params["tag"])
        if "is_free" in params:
            qs = qs.filter(is_free=params["is_free"].lower() in ("1", "true", "yes"))
        if params.get("mine") and user.is_authenticated:
            qs = qs.filter(trainer=user)
        if params.get("q"):
            term = params["q"]
            qs = qs.filter(Q(title__icontains=term) | Q(subtitle__icontains=term))

        ordering = params.get("ordering")
        allowed = {
            "created_at", "-created_at",
            "rating_avg", "-rating_avg",
            "enrolled_count", "-enrolled_count",
        }
        if ordering in allowed:
            qs = qs.order_by(ordering)
        return qs.distinct()

    def perform_create(self, serializer):
        if getattr(self.request.user, "role", None) not in ("trainer", "admin"):
            raise PermissionDenied("Only trainers can create courses.")
        serializer.save()

    # -- custom actions ----------------------------------------------------- #

    @action(detail=True, methods=["post"])
    def publish(self, request, slug=None):
        """Submit for review (trainer) or approve and publish (admin).

        A trainer may only submit a course that has a curriculum, and may not
        re-submit one that is already live — that used to silently pull the
        course out of the catalog and take enrolled students' access with it.
        """
        course = self.get_object()
        self.check_object_permissions(request, course)
        if _is_admin(request.user):
            review.approve(course, note=(request.data.get("note") or "").strip())
        else:
            review.submit_for_review(course)
        return Response(CourseDetailSerializer(course).data)

    @action(detail=True, methods=["post"])
    def reject(self, request, slug=None):
        """Admin sends a submitted course back to the trainer with a reason."""
        if not _is_admin(request.user):
            raise PermissionDenied("Only an admin can reject a course.")
        course = self.get_object()
        note = (request.data.get("note") or "").strip()
        if not note:
            raise ValidationError(
                {"note": "Tell the trainer what needs changing."}
            )
        review.reject(course, note=note)
        return Response(CourseDetailSerializer(course).data)

    @action(detail=True, methods=["post"])
    def archive(self, request, slug=None):
        """Retire a course: it leaves the catalog, enrolled students keep it.

        Archiving rather than deleting keeps enrollments, progress, certificates
        and the order history intact — deleting a sold course would tear out
        rows people paid for.
        """
        course = self.get_object()
        self.check_object_permissions(request, course)
        course.status = Course.Status.ARCHIVED
        course.save(update_fields=["status", "updated_at"])
        return Response(CourseDetailSerializer(course).data)

    @action(detail=False, methods=["get"], url_path="review-queue")
    def review_queue(self, request):
        """Admin queue: everything waiting on a decision.

        Both things an admin acts on in one call — fresh submissions, and live
        courses whose curriculum changed after approval.
        """
        if not _is_admin(request.user):
            raise PermissionDenied("Only an admin can see the review queue.")
        pending = Course.objects.filter(
            Q(status=Course.Status.PENDING_REVIEW)
            | Q(status=Course.Status.PUBLISHED, has_unapproved_changes=True)
        ).select_related("trainer", "category")
        return Response(
            {
                "pending_review": CourseListSerializer(
                    pending.filter(status=Course.Status.PENDING_REVIEW),
                    many=True,
                    context={"request": request},
                ).data,
                "changed_after_approval": CourseListSerializer(
                    pending.filter(has_unapproved_changes=True),
                    many=True,
                    context={"request": request},
                ).data,
            }
        )

    @action(detail=True, methods=["get"])
    def curriculum(self, request, slug=None):
        """Course player tree: modules + lessons, with gated content masked for
        users who are not enrolled / the owner / an admin."""
        course = self.get_object()
        user = request.user
        enrollment = Enrollment.objects.filter(student=user, course=course).first()
        has_access = _has_course_access(user, course, enrollment)

        # Fold the student's per-lesson progress in so the player renders the
        # completion ticks / resume point from this single call.
        progress_map = {}
        if enrollment is not None:
            for lp in LessonProgress.objects.filter(enrollment=enrollment).values(
                "lesson_id", "status", "watch_pct", "last_position_seconds"
            ):
                progress_map[lp["lesson_id"]] = {
                    "completed": lp["status"] == LessonProgress.Status.COMPLETED,
                    "watch_pct": lp["watch_pct"],
                    "last_position_seconds": lp["last_position_seconds"],
                }

        modules = course.modules.prefetch_related("lessons__resources")
        serializer = ModulePlayerSerializer(
            modules,
            many=True,
            context={
                "has_access": has_access,
                "progress_map": progress_map,
                "request": request,
            },
        )
        return Response(
            {
                "course": course.slug,
                "has_access": has_access,
                "progress_pct": enrollment.progress_pct if enrollment else 0,
                "modules": serializer.data,
            }
        )

    @action(detail=True, methods=["post"])
    def enroll(self, request, slug=None):
        """Self-enroll the current user into this (free, published) course."""
        course = self.get_object()
        serializer = EnrollmentCreateSerializer(
            data={"course": course.id, **request.data},
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        enrollment = _create_enrollment(
            student=request.user,
            course=course,
            batch=serializer.validated_data.get("batch"),
            source=serializer.enrollment_source(course),
        )
        return Response(
            EnrollmentSerializer(enrollment).data, status=status.HTTP_201_CREATED
        )


def _has_course_access(user, course, enrollment):
    """Whether ``user`` may open ``course``'s gated content.

    Access comes from being staff, owning the course, having bought or been
    granted an enrollment, or holding a plan that includes all paid courses.

    The subtle case is a **lapsed subscriber**: their subscription-sourced
    enrollment row still exists, so checking "is there an enrollment?" alone
    would keep the content unlocked forever after they stop paying. Those
    enrollments only count while the entitlement is still live; a purchased or
    free enrollment is theirs to keep either way.
    """
    if _is_admin(user) or course.trainer_id == user.id:
        return True
    if entitlements.can_access_paid_courses(user):
        return True
    if enrollment is None:
        return False
    return enrollment.source != Enrollment.Source.SUBSCRIPTION


def _create_enrollment(student, course, batch=None, source=None):
    enrollment = Enrollment.objects.create(
        student=student,
        course=course,
        batch=batch,
        source=source or Enrollment.Source.FREE,
        status=Enrollment.Status.ACTIVE,
    )
    Course.objects.filter(pk=course.pk).update(
        enrolled_count=course.enrolled_count + 1
    )
    if batch:
        Batch.objects.filter(pk=batch.pk).update(
            enrolled_count=batch.enrolled_count + 1
        )
    return enrollment


# --------------------------------------------------------------------------- #
# Course structure (trainer authoring)
# --------------------------------------------------------------------------- #


class CurriculumChangeMixin:
    """Flag the owning course for re-review whenever its structure changes.

    Applied to every authoring viewset below the course itself, so an admin
    cannot approve a curriculum and then have it quietly rewritten underneath
    them. The course stays published throughout — see ``courses.review``.
    """

    def _flag(self, obj):
        course = course_of(obj)
        if course is not None:
            review.flag_curriculum_change(course)

    def perform_update(self, serializer):
        serializer.save()
        self._flag(serializer.instance)

    def perform_destroy(self, instance):
        course = course_of(instance)
        instance.delete()
        if course is not None:
            review.flag_curriculum_change(course)


class ModuleViewSet(CurriculumChangeMixin, viewsets.ModelViewSet):
    """Modules within a course. Filter by ``?course=<id>``."""

    serializer_class = ModuleSerializer
    permission_classes = [IsAuthenticated, IsTrainerOwnerOrReadOnly]
    api_roles = ALL_ROLES
    api_roles_by_action = _AUTHORING_BY_ACTION

    def get_queryset(self):
        qs = Module.objects.select_related("course").prefetch_related(
            "lessons__resources"
        )
        return _filter_by(qs, self.request, "course", "course_id")

    def perform_create(self, serializer):
        _assert_can_author(self.request.user, serializer.validated_data["course"])
        serializer.save()
        self._flag(serializer.instance)


class LessonViewSet(CurriculumChangeMixin, viewsets.ModelViewSet):
    """Lessons within a module. Filter by ``?module=<id>``."""

    serializer_class = LessonSerializer
    permission_classes = [IsAuthenticated, IsTrainerOwnerOrReadOnly]
    api_roles = ALL_ROLES
    api_roles_by_action = _AUTHORING_BY_ACTION

    def get_queryset(self):
        qs = Lesson.objects.select_related("module__course").prefetch_related(
            "resources"
        )
        return _filter_by(qs, self.request, "module", "module_id")

    def perform_create(self, serializer):
        module = serializer.validated_data["module"]
        _assert_can_author(self.request.user, module.course)
        serializer.save()
        self._flag(serializer.instance)


class LessonResourceViewSet(CurriculumChangeMixin, viewsets.ModelViewSet):
    """Downloadable/linked resources on a lesson. Filter by ``?lesson=<id>``."""

    serializer_class = LessonResourceSerializer
    permission_classes = [IsAuthenticated, IsTrainerOwnerOrReadOnly]
    api_roles = ALL_ROLES
    api_roles_by_action = _AUTHORING_BY_ACTION

    def get_queryset(self):
        qs = LessonResource.objects.select_related("lesson__module__course")
        return _filter_by(qs, self.request, "lesson", "lesson_id")

    def perform_create(self, serializer):
        lesson = serializer.validated_data["lesson"]
        _assert_can_author(self.request.user, lesson.module.course)
        serializer.save()


class BatchViewSet(viewsets.ModelViewSet):
    """Cohorts/batches of a course. Filter by ``?course=<id>``."""

    serializer_class = BatchSerializer
    permission_classes = [IsAuthenticated, IsTrainerOwnerOrReadOnly]
    api_roles = ALL_ROLES
    api_roles_by_action = _AUTHORING_BY_ACTION

    def get_queryset(self):
        qs = Batch.objects.select_related("course", "trainer")
        return _filter_by(qs, self.request, "course", "course_id")

    def perform_create(self, serializer):
        _assert_can_author(self.request.user, serializer.validated_data["course"])
        serializer.save()


def _assert_can_author(user, course):
    if _is_admin(user) or course.trainer_id == user.id:
        return
    raise PermissionDenied("You do not own this course.")


# --------------------------------------------------------------------------- #
# Enrollment, progress, reviews (student)
# --------------------------------------------------------------------------- #


class EnrollmentViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """A student's own enrollments. Admins see all; trainers see enrollments in
    their own courses."""

    permission_classes = [IsAuthenticated]
    api_roles = ("student", "trainer", "admin")
    api_roles_by_action = {"create": ("student",)}

    def get_serializer_class(self):
        if self.action == "create":
            return EnrollmentCreateSerializer
        return EnrollmentSerializer

    def get_queryset(self):
        user = self.request.user
        qs = Enrollment.objects.select_related("course", "course__trainer", "batch")
        if _is_admin(user):
            return qs
        if getattr(user, "role", None) == "trainer":
            return qs.filter(Q(course__trainer=user) | Q(student=user))
        return qs.filter(student=user)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        course = serializer.validated_data["course"]
        enrollment = _create_enrollment(
            student=request.user,
            course=course,
            batch=serializer.validated_data.get("batch"),
            source=serializer.enrollment_source(course),
        )
        return Response(
            EnrollmentSerializer(enrollment).data, status=status.HTTP_201_CREATED
        )


class LessonNoteViewSet(viewsets.ModelViewSet):
    """The player's Notes tab. Filter by ``?lesson=<id>``.

    POST upserts, so the tab can just save whatever is in the textarea without
    first checking whether a note already exists. Notes are private to the
    student — the queryset never widens, not even for an admin.
    """

    serializer_class = LessonNoteSerializer
    permission_classes = [IsAuthenticated]
    api_roles = ("student", "trainer", "admin")

    def get_queryset(self):
        qs = LessonNote.objects.filter(student=self.request.user).select_related(
            "lesson"
        )
        return _filter_by(qs, self.request, "lesson", "lesson_id")

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        note, created = LessonNote.objects.update_or_create(
            student=request.user,
            lesson=serializer.validated_data["lesson"],
            defaults={"body": serializer.validated_data.get("body", "")},
        )
        return Response(
            self.get_serializer(note).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    def perform_update(self, serializer):
        serializer.save(student=self.request.user)


class LessonProgressViewSet(viewsets.ModelViewSet):
    """Per-lesson progress for the current student. Use POST to upsert: posting
    progress for an already-tracked lesson updates the existing row."""

    serializer_class = LessonProgressSerializer
    permission_classes = [IsAuthenticated]
    api_roles = ("student", "admin")

    def get_queryset(self):
        qs = LessonProgress.objects.select_related(
            "enrollment", "lesson__module__course"
        )
        if not _is_admin(self.request.user):
            qs = qs.filter(enrollment__student=self.request.user)
        return _filter_by(qs, self.request, "enrollment", "enrollment_id")

    def perform_create(self, serializer):
        enrollment = serializer.validated_data["enrollment"]
        if enrollment.student_id != self.request.user.id and not _is_admin(
            self.request.user
        ):
            raise PermissionDenied("Not your enrollment.")
        lesson = serializer.validated_data["lesson"]
        existing = LessonProgress.objects.filter(
            enrollment=enrollment, lesson=lesson
        ).first()
        if existing:
            for field, value in serializer.validated_data.items():
                setattr(existing, field, value)
            instance = existing
        else:
            instance = LessonProgress(**serializer.validated_data)
        self._finalize(instance)
        serializer.instance = instance

    def perform_update(self, serializer):
        instance = serializer.save()
        self._finalize(instance)

    def _finalize(self, instance):
        if (
            instance.status == LessonProgress.Status.COMPLETED
            and instance.completed_at is None
        ):
            instance.completed_at = timezone.now()
            instance.watch_pct = 100
        instance.save()
        _recompute_enrollment_progress(instance.enrollment)


def _recompute_enrollment_progress(enrollment):
    """Recompute ``Enrollment.progress_pct`` from completed lessons."""
    total = Lesson.objects.filter(module__course=enrollment.course).count()
    if total == 0:
        return
    completed = LessonProgress.objects.filter(
        enrollment=enrollment, status=LessonProgress.Status.COMPLETED
    ).count()
    pct = round(completed / total * 100)
    fields = {"progress_pct": pct}
    just_completed = pct >= 100 and enrollment.status != Enrollment.Status.COMPLETED
    if just_completed:
        fields["status"] = Enrollment.Status.COMPLETED
        fields["completed_at"] = timezone.now()
    Enrollment.objects.filter(pk=enrollment.pk).update(**fields)

    if just_completed:
        # Auto-issue the completion certificate (PRD §3.12). Never let a
        # certificate failure break progress tracking.
        try:
            from certificates.services import issue_for_enrollment

            enrollment.refresh_from_db()
            issue_for_enrollment(enrollment)
        except Exception:
            pass


class CourseReviewViewSet(viewsets.ModelViewSet):
    """Course ratings/reviews. Filter by ``?course=<id>``. A student may review
    a course once, and only if enrolled."""

    serializer_class = CourseReviewSerializer
    permission_classes = [IsAuthenticated]
    api_roles = ALL_ROLES
    api_roles_by_action = {
        "create": ("student",),
        "update": ("student", "admin"),
        "partial_update": ("student", "admin"),
        "destroy": ("student", "admin"),
    }

    def get_queryset(self):
        qs = CourseReview.objects.select_related("student", "course")
        return _filter_by(qs, self.request, "course", "course_id")

    def get_object(self):
        obj = super().get_object()
        if (
            self.request.method not in ("GET", "HEAD", "OPTIONS")
            and obj.student_id != self.request.user.id
            and not _is_admin(self.request.user)
        ):
            raise PermissionDenied("You can only modify your own review.")
        return obj

    def perform_create(self, serializer):
        user = self.request.user
        course = serializer.validated_data["course"]
        if not Enrollment.objects.filter(student=user, course=course).exists():
            raise ValidationError("You must be enrolled to review this course.")
        if CourseReview.objects.filter(course=course, student=user).exists():
            raise ValidationError("You have already reviewed this course.")
        serializer.save(student=user)
        _recompute_course_rating(course)

    def perform_update(self, serializer):
        review = serializer.save()
        _recompute_course_rating(review.course)

    def perform_destroy(self, instance):
        course = instance.course
        instance.delete()
        _recompute_course_rating(course)


def _recompute_course_rating(course):
    agg = course.reviews.aggregate(avg=Avg("rating"), count=Count("id"))
    Course.objects.filter(pk=course.pk).update(
        rating_avg=round(agg["avg"] or 0, 2),
        rating_count=agg["count"] or 0,
    )
