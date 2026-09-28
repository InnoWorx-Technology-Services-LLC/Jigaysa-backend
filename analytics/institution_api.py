"""The institution portal's **Overview** page (PRD §2.4 multi-tenancy, §3.14).

Four tiles, the active-batch list, upcoming classroom bookings, and the recent
activity feed — the whole of ``/institution``, which until now had no backend of
any kind and rendered hard-coded sample numbers.

Kept apart from ``analytics.views`` (admin Reports) and ``analytics.trainer_api``
(a trainer's own courses) for the same reason those two are kept apart from each
other: same tables, different question. Reports is platform-wide, trainer
analytics follows ``course.trainer``, and **everything here follows
``user.organization``** — the tenant seam the ``Organization`` model exists to
draw. Scoping is the feature, so there is no query parameter to widen it and no
``if admin`` branch; one view that can answer for either the platform or a
tenant is how the wrong institution's numbers eventually reach a customer.

Two rules the module follows throughout:

* **An institution with no data returns zeros and empty arrays, never an error
  and never a gap** — the rule ``analytics.views`` already sets. A college
  onboarded this morning should see honest noughts.
* **Rates are ``null``, never ``0``, when there is nothing to average.** A
  brand-new institution has no average completion; rendering 0% says its
  learners are failing.

Institution-only, unlike the trainer endpoints which admins may also call. An
admin has no ``organization`` — letting them in would mean inventing a
"whose institution?" parameter, and the platform-wide view they actually want
already exists at ``/admin/reports/`` and ``/admin/organizations/``.
"""

import csv
import io

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import Avg, Count, Prefetch, Q
from django.db import transaction
from django.http import HttpResponse
from django.db.models.functions import Coalesce
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, serializers, status as http_status
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Role
from analytics.serializers import (
    ActivityFeedSerializer,
    InstitutionLearnerCreateSerializer,
    InstitutionLearnerDetailSerializer,
    InstitutionLearnerImportSerializer,
    InstitutionLearnerSerializer,
    InstitutionLearnerSummarySerializer,
    InstitutionReportCohortSerializer,
    InstitutionReportSummarySerializer,
    InstitutionReportTrendSerializer,
    InstitutionBatchSerializer,
    InstitutionCourseSerializer,
    InstitutionCourseSummarySerializer,
    InstitutionBatchSummarySerializer,
    InstitutionBatchWriteSerializer,
    InstitutionBookingSerializer,
    InstitutionOverviewSerializer,
)
from analytics.views import (
    ACTIVE_WINDOW_DAYS,
    DEFAULT_MONTHS,
    MAX_MONTHS,
    _month_bounds,
    _month_series,
    _months_param,
)
from certificates.models import Certificate
from classrooms.models import ClassroomSession, Room
from live.models import Attendance
from core.pagination import DefaultPagination
from core.permissions import IsInstitution
from courses.models import Batch, Course, Enrollment, Lesson, LessonProgress

User = get_user_model()

#: How recently a learner must have joined to count towards the "+N" badge on
#: the Learners tile. A month is what "new this month" means to the person
#: reading the dashboard; the window travels in the response so the frontend
#: cannot label it as something else.
NEW_LEARNER_WINDOW_DAYS = 30

#: How close to its end date an otherwise-active batch has to be before the
#: console calls it "completing" rather than "active".
#:
#: **A presentation threshold, not a fact about the batch.** Nothing in the
#: platform defines "completing"; the mock simply shows the badge. Thirty days
#: is a chosen default, named here so changing it is a one-line edit rather than
#: a hunt through a serializer. ``state`` is derived from dates only — never
#: from how far learners have got, which would make the badge a judgement about
#: people rather than a fact about the calendar.
COMPLETING_WINDOW_DAYS = 30

#: What makes a learner "at risk" on the Learners screen.
#:
#: **Both signals are required, deliberately.** Low progress alone flags
#: everyone who enrolled this morning; silence alone flags everyone on holiday.
#: A learner who is behind *and* has stopped showing up is the one worth a
#: phone call, and that is the only claim this label is making.
#:
#: Like ``COMPLETING_WINDOW_DAYS`` these are chosen defaults, not platform
#: facts — nothing in the PRD defines "at risk". They are named here so the
#: threshold is one edit, and they travel in the API response so the frontend
#: cannot describe them as something else.
AT_RISK_IDLE_DAYS = 14
AT_RISK_PROGRESS_PCT = 40

#: Ceiling on one CSV import. A roster upload is a person pasting a
#: spreadsheet, not a data pipeline; without a cap a mis-saved file becomes a
#: request that builds tens of thousands of accounts inside one transaction.
MAX_IMPORT_ROWS = 500

#: The activity feed is a bounded feed, not a paginated table — the mock has no
#: "View all" on it, and merging four tables cannot be paged coherently by
#: offset anyway. See ``InstitutionActivityView``.
DEFAULT_ACTIVITY_LIMIT = 20
MAX_ACTIVITY_LIMIT = 100


class OrganizationNotLinked(APIException):
    """The caller is an institution account attached to no institution.

    ``User.organization`` is nullable and nothing enforces that an
    ``institution``-role account has one, so this is reachable: an admin
    promotes someone to ``institution`` and forgets the separate call that adds
    them to the organisation (the two are deliberately separate — see
    ``core.admin_api``).

    A **409** rather than empty tiles. Zeros would read as "your institution has
    no learners yet", which is a different and wrong story — the real state is
    "this account is not attached to an institution", and only an admin can fix
    it. A dashboard that quietly reads empty is worse than one that says why.
    """

    status_code = http_status.HTTP_409_CONFLICT
    default_code = "organization_not_linked"
    default_detail = (
        "This account is not linked to an institution yet. A platform admin "
        "needs to add it to one before the institution console has anything "
        "to show."
    )


class OrganizationScopedMixin:
    """Institution-only, with ``self.organization`` resolved up front.

    Resolving in ``initial()`` means the 409 above happens once, before any view
    body runs, instead of each view remembering to check — the check that gets
    forgotten is the one that returns somebody else's numbers.

    A mixin rather than a base class because the reports here are ``APIView``s
    and batches are a ``ViewSet``, and the scoping rule must be identical for
    both. Two copies of it is one copy that drifts.
    """

    permission_classes = [IsInstitution]
    api_roles = ("institution",)

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        self.organization = getattr(request.user, "organization", None)
        if self.organization is None:
            raise OrganizationNotLinked()


class InstitutionAPIView(OrganizationScopedMixin, APIView):
    """A computed, read-only institution report."""


def _pct(value):
    return None if value is None else round(float(value), 1)


def _active_batches(queryset, today):
    """Batches whose window contains ``today``.

    Both dates are nullable, and a batch with neither is treated as **active**
    rather than skipped: an undated cohort is an open one that somebody forgot
    to schedule, and dropping it would under-count the tile against a list the
    institution can plainly see has rows in it.
    """
    return queryset.filter(
        (Q(start_date__isnull=True) | Q(start_date__lte=today))
        & (Q(end_date__isnull=True) | Q(end_date__gte=today))
    )


def institution_courses(org):
    """The catalogue an institution may browse **and** build batches on.

    Published-and-public courses, plus everything the institution owns whatever
    its state. One function rather than two rules because browsing and
    assigning have to agree: a course the Courses page offers but the batch
    endpoint then rejects is a dead "Assign to batch" button, and the drift
    would only show up when somebody clicked it.

    The institution's *own* drafts are included deliberately — its syllabus
    cannot change underneath it the way a stranger's can.
    """
    return Course.objects.filter(
        Q(
            status=Course.Status.PUBLISHED,
            visibility=Course.Visibility.PUBLIC,
        )
        | Q(organization=org)
    )


def _org_enrollments(org):
    """Every enrolment that is this institution's training.

    Two ways an enrolment belongs to an institution and both count: it sits in
    one of the institution's **batches**, or it is in a course the institution
    **owns**. Batches alone would miss self-paced institutional content (whose
    enrolments carry no batch); courses alone would miss an institution running
    a cohort through somebody else's course, which is the common case.

    What this deliberately excludes: a learner on the institution's roll taking
    an unrelated course from the public catalogue in their own time. That is
    theirs, not the college's, and averaging it into the college's completion
    rate would make the number unactionable.
    """
    return Enrollment.objects.filter(
        Q(batch__organization=org) | Q(course__organization=org)
    )


class InstitutionOverviewView(InstitutionAPIView):
    """GET ``/institution/overview/`` — the four tiles.

    Returns the organisation itself alongside the numbers. The console header
    needs the name regardless, and shipping ``is_active`` with it lets the UI
    warn a deactivated institution rather than silently serving a dashboard
    that nobody is paying for. A deactivated institution can still *read* its
    own console — deactivation keeps history (see ``core.admin_api``), and
    locking members out of their own numbers would be a surprising thing for an
    admin toggle to do.
    """

    serializer_class = InstitutionOverviewSerializer

    @extend_schema(responses=InstitutionOverviewSerializer)
    def get(self, request):
        org = self.organization
        today = timezone.localdate()
        since = timezone.now() - timedelta(days=NEW_LEARNER_WINDOW_DAYS)

        learners = User.objects.filter(
            organization=org, role=Role.STUDENT, is_active=True
        )
        completion = _org_enrollments(org).aggregate(
            avg=Avg("progress_pct")
        )["avg"]

        return Response(
            InstitutionOverviewSerializer(
                {
                    "organization": {
                        "id": org.pk,
                        "name": org.name,
                        "slug": org.slug,
                        "type": org.type,
                        "is_active": org.is_active,
                    },
                    "active_batches": _active_batches(
                        Batch.objects.filter(organization=org), today
                    ).count(),
                    "batches": Batch.objects.filter(organization=org).count(),
                    "learners": learners.count(),
                    "learners_joined_recently": learners.filter(
                        created_at__gte=since
                    ).count(),
                    "new_learner_window_days": NEW_LEARNER_WINDOW_DAYS,
                    "avg_completion": _pct(completion),
                    "classrooms": Room.objects.filter(
                        organization=org
                    ).count(),
                }
            ).data
        )


def _batch_state(batch, today):
    """``upcoming`` | ``active`` | ``completing`` | ``ended``, from dates alone.

    Mirrors the ``?status=`` filter below, and the two must stay in step: a row
    the filter returned for ``completing`` that labels itself ``active`` is the
    kind of disagreement nobody notices until a customer does.
    """
    if batch.start_date and batch.start_date > today:
        return "upcoming"
    if batch.end_date and batch.end_date < today:
        return "ended"
    if (
        batch.end_date
        and (batch.end_date - today).days <= COMPLETING_WINDOW_DAYS
    ):
        return "completing"
    return "active"


class InstitutionBatchViewSet(
    OrganizationScopedMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """The institution's **Batches** page: the cards, "New batch", "Manage".

    ``GET`` / ``POST`` ``/institution/batches/`` and ``GET`` / ``PATCH`` /
    ``PUT`` ``/institution/batches/{id}/``. Listing is **paginated** — batches
    accumulate for the life of the institution.

    ``?status=active`` (default) | ``upcoming`` | ``completing`` | ``ended`` |
    ``all``, and ``?course=<id>``. Ordered by start date, newest first.

    > ### Why this exists when ``/api/v1/batches/`` already has CRUD
    >
    > That endpoint is gated by ``IsTrainerOwnerOrReadOnly`` and
    > ``_assert_can_author``: writes are allowed only to an admin or the
    > course's **owning trainer**. An institution head is neither, so every
    > write there is a 403 — the Batches page could not create a batch through
    > it at all.
    >
    > It also exposes ``organization`` as a writable field. Simply widening its
    > permissions would have let one institution file a batch under another
    > institution's name. Here the tenant is taken from the caller and is not
    > an input.

    **There is no delete.** ``Enrollment.batch`` is ``SET_NULL``, so deleting a
    batch would silently detach every learner's enrolment from the cohort they
    took while leaving the enrolment behind — history quietly rewritten, no
    warning. ``Batch`` has no deactivation flag to use instead (the pattern
    ``core.admin_api`` uses for institutions), so adding one is a model change
    and a deliberate decision, not something to infer from a "Manage" button.
    """

    pagination_class = DefaultPagination

    #: Never used at runtime — ``get_queryset`` below replaces it entirely. It
    #: exists so schema generation, which has no request and therefore no
    #: ``self.organization``, can still derive the model and type the ``{id}``
    #: path parameter as an integer instead of falling back to a string.
    queryset = Batch.objects.none()

    def get_serializer_class(self):
        if self.action in ("create", "update", "partial_update"):
            return InstitutionBatchWriteSerializer
        return InstitutionBatchSerializer

    def get_serializer_context(self):
        # The write serializer validates ``course`` and ``trainer`` against the
        # caller's tenant, so it needs the tenant.
        return {**super().get_serializer_context(),
                "organization": self.organization}

    def get_queryset(self):
        """Always filtered to the caller's own institution.

        This is the single place tenant isolation is enforced for reads *and*
        writes: ``get_object()`` runs through it, so a ``PATCH`` against another
        institution's batch is a **404**, not a 403 — the caller has no business
        learning that the row exists.
        """
        return (
            Batch.objects.filter(organization=self.organization)
            .select_related("course", "trainer")
            .annotate(
                learners=Count(
                    "enrollments",
                    filter=Q(enrollments__status=Enrollment.Status.ACTIVE),
                    distinct=True,
                ),
                completion=Avg("enrollments__progress_pct"),
            )
            .order_by("-start_date", "-created_at")
        )

    def _filtered(self, request):
        today = timezone.localdate()
        batches = self.get_queryset()

        state = request.query_params.get("status", "active").strip().lower()
        if state == "upcoming":
            batches = batches.filter(start_date__gt=today)
        elif state == "ended":
            batches = batches.filter(end_date__lt=today)
        elif state == "completing":
            # Active *and* inside the closing window. Kept in step with
            # ``_batch_state`` by construction.
            batches = _active_batches(batches, today).filter(
                end_date__isnull=False,
                end_date__lte=today + timedelta(days=COMPLETING_WINDOW_DAYS),
            )
        elif state != "all":
            # Anything unrecognised falls through to the default rather than
            # 400-ing: this backs a panel, and a typo'd filter should show the
            # running cohorts, not an error page.
            batches = _active_batches(batches, today)

        course = request.query_params.get("course", "").strip()
        if course.isdigit():
            batches = batches.filter(course_id=int(course))
        return batches

    @staticmethod
    def _row(batch, today):
        return {
            "id": batch.pk,
            "name": batch.name,
            "course": batch.course.title,
            "course_id": batch.course_id,
            "course_slug": batch.course.slug,
            "trainer": getattr(batch.trainer, "full_name", "") or "",
            "trainer_id": batch.trainer_id,
            # ``enrolled_count`` is the denormalised counter the enrolment flow
            # maintains; ``learners`` is counted live off active enrolments.
            # Both ship because they answer different questions — seats sold
            # versus people still on the course — and a panel that showed only
            # the first would never notice a cohort emptying out.
            "seats_taken": batch.enrolled_count,
            "learners": getattr(batch, "learners", 0),
            "capacity": batch.capacity,
            "completion": _pct(getattr(batch, "completion", None)),
            "start_date": batch.start_date,
            "end_date": batch.end_date,
            "state": _batch_state(batch, today),
            "schedule": batch.schedule,
        }

    def _read(self, batch):
        """Re-read through the annotated queryset so a write answers in the
        same shape as a read — a create that comes back without the counts the
        card renders forces the client to refetch immediately."""
        return InstitutionBatchSerializer(
            self._row(self.get_queryset().get(pk=batch.pk), timezone.localdate())
        ).data

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "status", str, OpenApiParameter.QUERY,
                description="active (default) | upcoming | completing | "
                            "ended | all",
            ),
            OpenApiParameter(
                "course", int, OpenApiParameter.QUERY,
                description="Limit to one course, by id.",
            ),
        ],
        responses=InstitutionBatchSerializer(many=True),
    )
    def list(self, request, *args, **kwargs):
        today = timezone.localdate()
        page = self.paginate_queryset(self._filtered(request))
        rows = [self._row(batch, today) for batch in page]
        return self.get_paginated_response(
            InstitutionBatchSerializer(rows, many=True).data
        )

    @extend_schema(responses=InstitutionBatchSerializer)
    def retrieve(self, request, *args, **kwargs):
        return Response(
            self._row(self.get_object(), timezone.localdate())
        )

    @extend_schema(responses=InstitutionBatchSummarySerializer)
    @action(detail=False, methods=["get"])
    def summary(self, request):
        """GET ``/institution/batches/summary/`` — the Batches page's tiles.

        Its own endpoint rather than three more fields on
        ``/institution/overview/``: that one backs the **Dashboard** screen, and
        a "learners" tile meaning one thing there and another thing here is how
        two screens start disagreeing in a review. Same split as
        ``/admin/reports/summary/`` and ``/trainer/analytics/summary/``.

        One count per ``state`` the cards can show, so the tiles and the badges
        are the same arithmetic.
        """
        today = timezone.localdate()
        mine = Batch.objects.filter(organization=self.organization)
        active = _active_batches(mine, today)

        return Response(
            InstitutionBatchSummarySerializer(
                {
                    "active": active.count(),
                    "completing": active.filter(
                        end_date__isnull=False,
                        end_date__lte=today
                        + timedelta(days=COMPLETING_WINDOW_DAYS),
                    ).count(),
                    "upcoming": mine.filter(start_date__gt=today).count(),
                    "ended": mine.filter(end_date__lt=today).count(),
                    "total": mine.count(),
                    # Distinct people, not the sum of the per-card counts:
                    # somebody in two cohorts is one learner. This tile can
                    # therefore read lower than the cards add up to, which is
                    # correct — see the serializer.
                    "learners": Enrollment.objects.filter(
                        batch__organization=self.organization,
                        status=Enrollment.Status.ACTIVE,
                    ).values("student").distinct().count(),
                }
            ).data
        )

    @extend_schema(
        request=InstitutionBatchWriteSerializer,
        responses=InstitutionBatchSerializer,
    )
    def create(self, request, *args, **kwargs):
        body = self.get_serializer(data=request.data)
        body.is_valid(raise_exception=True)
        # The tenant is taken from the caller, never from the body. This is the
        # line that stops one institution filing a batch under another's name.
        batch = body.save(organization=self.organization)
        return Response(
            self._read(batch), status=http_status.HTTP_201_CREATED
        )

    @extend_schema(
        request=InstitutionBatchWriteSerializer,
        responses=InstitutionBatchSerializer,
    )
    def update(self, request, *args, **kwargs):
        batch = self.get_object()
        body = self.get_serializer(
            batch, data=request.data, partial=kwargs.pop("partial", False)
        )
        body.is_valid(raise_exception=True)
        # Re-asserted on update too: ``organization`` is not a writable field,
        # but pinning it here means a future change to the serializer cannot
        # quietly make moving a batch between tenants possible.
        body.save(organization=self.organization)
        return Response(self._read(batch))


class InstitutionCourseViewSet(
    OrganizationScopedMixin,
    mixins.ListModelMixin,
    viewsets.GenericViewSet,
):
    """The institution's **Courses** page: browse, and see what is assigned.

    ``GET /institution/courses/`` — **paginated**. ``?assigned=true|false``,
    ``?category=<id>``, ``?type=<course_type>``, ``?level=<skill_level>``,
    ``?q=<search>``.

    Read-only, and that is not an omission. **"Assign to batch" is
    ``POST /institution/batches/``** — a ``Batch`` carries exactly one required
    ``course``, so assigning a course to a cohort *is* creating the cohort.
    A second endpoint that did the same write through different words would be
    two code paths for one action and two places for the tenant check to be
    got wrong.

    Courses are not owned by the institution, so there is nothing else to
    write here: editing one is the trainer's job, at ``/api/v1/courses/``.
    """

    pagination_class = DefaultPagination
    serializer_class = InstitutionCourseSerializer
    queryset = Course.objects.none()  # schema only; see the batch viewset

    def get_queryset(self):
        org = self.organization
        # The institution's own batches for each course, in one extra query
        # rather than one per card. ``to_attr`` keeps them off the normal
        # ``batches`` accessor so nothing can accidentally read the unfiltered
        # set and leak another tenant's cohorts onto the card.
        return (
            institution_courses(org)
            .select_related("trainer", "category")
            .prefetch_related(
                Prefetch(
                    "batches",
                    queryset=Batch.objects.filter(organization=org)
                    .annotate(
                        learners=Count(
                            "enrollments",
                            filter=Q(
                                enrollments__status=Enrollment.Status.ACTIVE
                            ),
                            distinct=True,
                        )
                    )
                    .order_by("-start_date", "-created_at"),
                    to_attr="org_batches",
                )
            )
            .order_by("-created_at")
            .distinct()
        )

    def _filtered(self, request):
        courses = self.get_queryset()
        params = request.query_params

        assigned = params.get("assigned", "").strip().lower()
        if assigned in ("true", "1", "yes"):
            courses = courses.filter(batches__organization=self.organization)
        elif assigned in ("false", "0", "no"):
            courses = courses.exclude(batches__organization=self.organization)

        for param, field in (
            ("category", "category_id"),
            ("type", "course_type"),
            ("level", "skill_level"),
        ):
            value = params.get(param, "").strip()
            if value:
                courses = courses.filter(**{field: value})

        term = params.get("q", "").strip()
        if term:
            courses = courses.filter(
                Q(title__icontains=term) | Q(subtitle__icontains=term)
            )
        return courses.distinct()

    @staticmethod
    def _row(course, org):
        batches = getattr(course, "org_batches", [])
        return {
            "id": course.pk,
            "title": course.title,
            "slug": course.slug,
            "subtitle": course.subtitle,
            "category": getattr(course.category, "name", "") or "",
            "category_id": course.category_id,
            "trainer": getattr(course.trainer, "full_name", "") or "",
            "trainer_id": course.trainer_id,
            "course_type": course.course_type,
            "skill_level": course.skill_level,
            "duration_minutes": course.duration_minutes,
            # The card prints "40 h". Converted here so every card rounds the
            # same way — six clients dividing by 60 is six chances to disagree.
            "duration_hours": round(course.duration_minutes / 60.0, 1),
            "thumbnail": course.thumbnail,
            "is_free": course.is_free,
            "rating_avg": course.rating_avg,
            "rating_count": course.rating_count,
            # Your own programme versus one from the shared catalogue. The
            # card can badge it; the batch endpoint also lets you build on your
            # own unpublished ones, which is the visible difference.
            "is_own": course.organization_id == org.pk,
            "assigned": bool(batches),
            # A course can be assigned to more than one of your cohorts. The
            # mock shows a single "Assigned to"; this returns them all so the
            # card can say "+2 more" instead of silently hiding them.
            "batches": [
                {
                    "id": batch.pk,
                    "name": batch.name,
                    "learners": batch.learners,
                    "capacity": batch.capacity,
                }
                for batch in batches
            ],
        }

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "assigned", bool, OpenApiParameter.QUERY,
                description="true = only courses already on one of your "
                            "batches; false = only unassigned.",
            ),
            OpenApiParameter("category", int, OpenApiParameter.QUERY),
            OpenApiParameter(
                "type", str, OpenApiParameter.QUERY,
                description="Course.CourseType, e.g. live_batch | self_paced.",
            ),
            OpenApiParameter(
                "level", str, OpenApiParameter.QUERY,
                description="beginner | intermediate | advanced",
            ),
            OpenApiParameter(
                "q", str, OpenApiParameter.QUERY,
                description="Matches title or subtitle.",
            ),
        ],
        responses=InstitutionCourseSerializer(many=True),
    )
    def list(self, request, *args, **kwargs):
        page = self.paginate_queryset(self._filtered(request))
        rows = [self._row(course, self.organization) for course in page]
        return self.get_paginated_response(
            InstitutionCourseSerializer(rows, many=True).data
        )

    @extend_schema(responses=InstitutionCourseSummarySerializer)
    @action(detail=False, methods=["get"])
    def summary(self, request):
        """GET ``/institution/courses/summary/`` — the three tiles."""
        org = self.organization
        today = timezone.localdate()
        mine = Batch.objects.filter(organization=org)

        return Response(
            InstitutionCourseSummarySerializer(
                {
                    "available": institution_courses(org).distinct().count(),
                    "assigned": mine.values("course").distinct().count(),
                    "active": _active_batches(mine, today)
                    .values("course")
                    .distinct()
                    .count(),
                    "learners": Enrollment.objects.filter(
                        batch__organization=org,
                        status=Enrollment.Status.ACTIVE,
                    ).values("student").distinct().count(),
                }
            ).data
        )


def _org_enrollment_q(org):
    """``_org_enrollments`` as a filter on ``User.enrollments``.

    Same two-way definition — the institution's batches, or courses it owns —
    expressed for the reverse relation so a learner's progress averages exactly
    the enrolments the Dashboard's completion tile averages.
    """
    return Q(enrollments__batch__organization=org) | Q(
        enrollments__course__organization=org
    )


def _learner_status(learner, idle_before):
    """``completed`` | ``at-risk`` | ``active``, from annotations only.

    Reads the *same* annotated columns the ``?status=`` filter queries, so a
    row returned under one label can never render as another.
    """
    if learner.active_count == 0 and learner.completed_count > 0:
        return "completed"
    if learner.active_count > 0:
        behind = (learner.progress or 0) < AT_RISK_PROGRESS_PCT
        idle = learner.last_login is None or learner.last_login < idle_before
        if behind and idle:
            return "at-risk"
    return "active"


class InstitutionLearnerViewSet(
    OrganizationScopedMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """The institution's **Learners** page: the roster, and one learner's detail.

    ``GET /institution/learners/`` — **paginated**; a roll grows without bound
    and this is the list that must never come back whole.
    ``?status=active|at-risk|completed``, ``?batch=<id>``, ``?q=<name or email>``.

    ``GET /institution/learners/{id}/`` — the "View" action: the same row plus
    a per-enrolment breakdown.

    Membership is ``User.organization``: everyone on your roll, whether or not
    they are in a cohort. Progress, status and batches are computed from **your
    institution's training only** — a learner's unrelated personal course does
    not move their progress bar here.
    """

    pagination_class = DefaultPagination
    serializer_class = InstitutionLearnerSerializer
    queryset = User.objects.none()  # schema only; see the batch viewset

    def get_queryset(self):
        org = self.organization
        in_org = _org_enrollment_q(org)
        return (
            User.objects.filter(
                organization=org, role=Role.STUDENT, is_active=True
            )
            .annotate(
                # Coalesced so the column is never NULL: the Python label and
                # the SQL filter below must agree about a learner with no
                # enrolments, and NULL compares false to everything.
                progress=Coalesce(
                    Avg("enrollments__progress_pct", filter=in_org), 0.0
                ),
                active_count=Count(
                    "enrollments",
                    filter=in_org & Q(
                        enrollments__status=Enrollment.Status.ACTIVE
                    ),
                    distinct=True,
                ),
                completed_count=Count(
                    "enrollments",
                    filter=in_org & Q(
                        enrollments__status=Enrollment.Status.COMPLETED
                    ),
                    distinct=True,
                ),
            )
            .prefetch_related(
                Prefetch(
                    "enrollments",
                    queryset=_org_enrollments(org)
                    .select_related("batch", "batch__course", "course")
                    .order_by("-enrolled_at"),
                    to_attr="org_enrollments",
                )
            )
            .order_by("full_name", "email")
        )

    def _filtered(self, request):
        learners = self.get_queryset()
        idle_before = timezone.now() - timedelta(days=AT_RISK_IDLE_DAYS)
        params = request.query_params

        at_risk = (
            Q(active_count__gt=0)
            & Q(progress__lt=AT_RISK_PROGRESS_PCT)
            & (Q(last_login__isnull=True) | Q(last_login__lt=idle_before))
        )
        completed = Q(active_count=0) & Q(completed_count__gt=0)

        state = params.get("status", "").strip().lower()
        if state in ("at-risk", "at_risk"):
            learners = learners.filter(at_risk)
        elif state == "completed":
            learners = learners.filter(completed)
        elif state == "active":
            # Everyone the other two labels did not claim — built from the same
            # two Q objects, so the three sets partition the roll exactly.
            learners = learners.exclude(at_risk).exclude(completed)

        batch = params.get("batch", "").strip()
        if batch.isdigit():
            learners = learners.filter(enrollments__batch_id=int(batch))

        term = params.get("q", "").strip()
        if term:
            learners = learners.filter(
                Q(full_name__icontains=term) | Q(email__icontains=term)
            )
        return learners.distinct()

    @staticmethod
    def _row(learner, idle_before):
        enrollments = getattr(learner, "org_enrollments", [])
        batches = [
            {"id": e.batch_id, "name": e.batch.name}
            for e in enrollments
            if e.batch_id
        ]
        # De-duplicate while keeping newest-first order: a learner can have two
        # enrolments pointing at the same cohort.
        seen, unique = set(), []
        for batch in batches:
            if batch["id"] not in seen:
                seen.add(batch["id"])
                unique.append(batch)
        return {
            "id": learner.pk,
            "full_name": learner.full_name,
            "email": learner.email,
            "progress": round(float(learner.progress or 0), 1),
            "last_active": learner.last_login,
            "status": _learner_status(learner, idle_before),
            "enrollments": len(enrollments),
            "active_enrollments": learner.active_count,
            "completed_enrollments": learner.completed_count,
            # The table prints one batch; a learner can be in several. The
            # array carries them all and ``batch`` is the newest, so the cell
            # has something to show without the rest being silently dropped.
            "batch": unique[0]["name"] if unique else "",
            "batches": unique,
            "joined": learner.created_at,
        }

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "status", str, OpenApiParameter.QUERY,
                description="active | at-risk | completed",
            ),
            OpenApiParameter("batch", int, OpenApiParameter.QUERY),
            OpenApiParameter(
                "q", str, OpenApiParameter.QUERY,
                description="Matches name or email.",
            ),
        ],
        responses=InstitutionLearnerSerializer(many=True),
    )
    def list(self, request, *args, **kwargs):
        idle_before = timezone.now() - timedelta(days=AT_RISK_IDLE_DAYS)
        page = self.paginate_queryset(self._filtered(request))
        rows = [self._row(learner, idle_before) for learner in page]
        return self.get_paginated_response(
            InstitutionLearnerSerializer(rows, many=True).data
        )

    @extend_schema(responses=InstitutionLearnerDetailSerializer)
    def retrieve(self, request, *args, **kwargs):
        """The "View" action: the roster row plus what they are actually on."""
        learner = self.get_object()
        idle_before = timezone.now() - timedelta(days=AT_RISK_IDLE_DAYS)
        row = self._row(learner, idle_before)
        row["courses"] = [
            {
                "enrollment_id": e.pk,
                "course": e.course.title,
                "course_id": e.course_id,
                "batch": getattr(e.batch, "name", "") or "",
                "batch_id": e.batch_id,
                "status": e.status,
                "progress": e.progress_pct,
                "enrolled_at": e.enrolled_at,
                "completed_at": e.completed_at,
            }
            for e in getattr(learner, "org_enrollments", [])
        ]
        row["certificates"] = Certificate.objects.filter(
            student=learner, status=Certificate.Status.ISSUED
        ).count()
        return Response(InstitutionLearnerDetailSerializer(row).data)

    def get_serializer_class(self):
        if self.action == "create":
            return InstitutionLearnerCreateSerializer
        if self.action == "retrieve":
            return InstitutionLearnerDetailSerializer
        return InstitutionLearnerSerializer

    def get_serializer_context(self):
        return {**super().get_serializer_context(),
                "organization": self.organization}

    # --- "Add learner" and "Import CSV" ----------------------------------- #

    def _create_learner(self, email, full_name, batch):
        """One roster addition: the account, the membership, the enrolment.

        The account is created with **no usable password** — nobody, including
        the institution, is handed a credential for somebody else. The learner
        sets their own through the existing ``/auth/password-reset/`` flow,
        which is a real implemented endpoint rather than a stub.
        """
        learner = User.objects.create_user(
            email=email,
            password=None,
            full_name=full_name,
            role=Role.STUDENT,
            organization=self.organization,
        )
        if batch is not None:
            Enrollment.objects.create(
                student=learner,
                course=batch.course,
                batch=batch,
                source=Enrollment.Source.INSTITUTION,
            )
            Batch.objects.filter(pk=batch.pk).update(
                enrolled_count=batch.enrolled_count + 1
            )
        return learner

    @extend_schema(
        request=InstitutionLearnerCreateSerializer,
        responses=InstitutionLearnerSerializer,
    )
    def create(self, request, *args, **kwargs):
        """POST ``/institution/learners/`` — "Add learner".

        Creates the account, puts it on your roll, and optionally enrols it in
        one of your batches in the same call — the three things the button
        means, so a half-added learner is not a state the page can reach.
        """
        body = self.get_serializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data

        with transaction.atomic():
            learner = self._create_learner(
                data["email"], data.get("full_name", ""), data.get("batch")
            )

        idle_before = timezone.now() - timedelta(days=AT_RISK_IDLE_DAYS)
        return Response(
            InstitutionLearnerSerializer(
                self._row(self.get_queryset().get(pk=learner.pk), idle_before)
            ).data,
            status=http_status.HTTP_201_CREATED,
        )

    @extend_schema(
        request=InstitutionLearnerImportSerializer,
        responses=InstitutionLearnerImportSerializer,
    )
    @action(detail=False, methods=["post"])
    def import_csv(self, request):
        """POST ``/institution/learners/import_csv/`` — "Import CSV".

        Send the file as multipart ``file``, or the text as ``csv``. Header row
        required; recognised columns are ``email`` (required), ``full_name``
        and ``batch`` (one of your batch ids). Unknown columns are ignored.

        > ### All rows or none
        >
        > Every row is validated first and **nothing is created unless all of
        > them pass**. A partial import is the worst outcome available here:
        > the institution cannot tell which half landed, and re-running to
        > catch the rest would collide with the half that already exists.
        > Fix the file, send it again.
        >
        > The response lists **every** bad row, not just the first, so one
        > round trip is enough to fix the file.
        """
        rows, error = self._read_csv(request)
        if error:
            return Response({"detail": error},
                            status=http_status.HTTP_400_BAD_REQUEST)

        results, seen = [], set()
        for number, raw in enumerate(rows, start=2):  # row 1 is the header
            email = (raw.get("email") or "").strip().lower()
            entry = {"row": number, "email": email,
                     "full_name": (raw.get("full_name") or "").strip()}

            if not email:
                entry["error"] = "No email in this row."
            elif email in seen:
                entry["error"] = "This email appears earlier in the file."
            else:
                seen.add(email)
                item = InstitutionLearnerCreateSerializer(
                    data={"email": email,
                          "full_name": entry["full_name"],
                          "batch": (raw.get("batch") or "").strip() or None},
                    context=self.get_serializer_context(),
                )
                if item.is_valid():
                    entry["batch"] = item.validated_data.get("batch")
                else:
                    entry["error"] = "; ".join(
                        f"{field}: {' '.join(str(m) for m in messages)}"
                        for field, messages in item.errors.items()
                    )
            results.append(entry)

        bad = [r for r in results if r.get("error")]
        if bad:
            return Response(
                InstitutionLearnerImportSerializer(
                    {"created": 0, "rejected": len(bad),
                     "rows": results, "applied": False}
                ).data,
                status=http_status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            for entry in results:
                self._create_learner(
                    entry["email"], entry["full_name"], entry.get("batch")
                )

        return Response(
            InstitutionLearnerImportSerializer(
                {"created": len(results), "rejected": 0,
                 "rows": results, "applied": True}
            ).data,
            status=http_status.HTTP_201_CREATED,
        )

    @staticmethod
    def _read_csv(request):
        """``(rows, error)``. Accepts a multipart ``file`` or a ``csv`` string."""
        upload = request.FILES.get("file")
        if upload is not None:
            if upload.size > 2 * 1024 * 1024:
                return None, "That file is larger than 2 MB."
            try:
                text = upload.read().decode("utf-8-sig")
            except UnicodeDecodeError:
                return None, ("That file is not UTF-8 text. Re-export it as "
                              "CSV UTF-8 from your spreadsheet.")
        else:
            text = request.data.get("csv") or ""
        if not text.strip():
            return None, "Send a CSV file as \"file\", or its text as \"csv\"."

        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames or "email" not in [
            (name or "").strip().lower() for name in reader.fieldnames
        ]:
            return None, ("The first row must be a header containing an "
                          "\"email\" column.")
        reader.fieldnames = [
            (name or "").strip().lower() for name in reader.fieldnames
        ]

        rows = list(reader)
        if not rows:
            return None, "That file has a header but no rows."
        if len(rows) > MAX_IMPORT_ROWS:
            return None, (f"That file has {len(rows)} rows; the limit is "
                          f"{MAX_IMPORT_ROWS} per import.")
        return rows, None

    @extend_schema(responses=InstitutionLearnerSummarySerializer)
    @action(detail=False, methods=["get"])
    def summary(self, request):
        """GET ``/institution/learners/summary/`` — the four tiles."""
        org = self.organization
        seen_since = timezone.now() - timedelta(days=ACTIVE_WINDOW_DAYS)
        issued_since = timezone.now() - timedelta(
            days=NEW_LEARNER_WINDOW_DAYS
        )

        roll = User.objects.filter(
            organization=org, role=Role.STUDENT, is_active=True
        )
        certificates = Certificate.objects.filter(
            student__organization=org, status=Certificate.Status.ISSUED
        )

        return Response(
            InstitutionLearnerSummarySerializer(
                {
                    "total": roll.count(),
                    # The platform's existing definition of an active user, not
                    # a second one invented for this screen — see
                    # ``analytics.views.ACTIVE_WINDOW_DAYS``.
                    "active": roll.filter(
                        last_login__gte=seen_since
                    ).count(),
                    "active_window_days": ACTIVE_WINDOW_DAYS,
                    "avg_completion": _pct(
                        _org_enrollments(org).aggregate(
                            avg=Avg("progress_pct")
                        )["avg"]
                    ),
                    "certificates": certificates.count(),
                    "certificates_recent": certificates.filter(
                        created_at__gte=issued_since
                    ).count(),
                    "certificate_window_days": NEW_LEARNER_WINDOW_DAYS,
                    "at_risk_idle_days": AT_RISK_IDLE_DAYS,
                    "at_risk_progress_pct": AT_RISK_PROGRESS_PCT,
                }
            ).data
        )


class InstitutionBookingsView(InstitutionAPIView):
    """GET ``/institution/bookings/`` — the "Upcoming bookings" panel.

    **Paginated**, soonest first. ``?when=upcoming`` (default) | ``past`` |
    ``all``.

    A booking is a ``classrooms.ClassroomSession``: a room, a time, and
    optionally the live session (and therefore the batch and trainer) being
    held in it. Sessions with no date are excluded from ``upcoming`` — an
    unscheduled room booking is not upcoming, it is unscheduled.

    > ### The mock's `confirmed` / `pending` badge does not exist
    >
    > ``ClassroomSession.status`` is ``scheduled | live | completed`` — a
    > lifecycle, not an approval state. There is no booking-approval workflow
    > in the platform, so this returns the real status rather than inventing
    > two values to match the sample screen. Label the badge from ``status``;
    > if approvals are genuinely wanted, that is a model change, not a
    > serializer one.
    """

    serializer_class = InstitutionBookingSerializer

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "when", str, OpenApiParameter.QUERY,
                description="upcoming (default) | past | all",
            ),
            OpenApiParameter(
                "room", int, OpenApiParameter.QUERY,
                description="Limit to one room, by id.",
            ),
        ],
        responses=InstitutionBookingSerializer(many=True),
    )
    def get(self, request):
        now = timezone.now()
        bookings = ClassroomSession.objects.filter(
            room__organization=self.organization
        ).select_related(
            "room",
            "live_session",
            "live_session__batch",
            "live_session__batch__course",
            "live_session__course",
            "remote_trainer",
        )

        when = request.query_params.get("when", "upcoming").strip().lower()
        if when == "past":
            bookings = bookings.filter(date__lt=now).order_by("-date")
        elif when == "all":
            bookings = bookings.order_by("-date")
        else:
            bookings = bookings.filter(date__gte=now).order_by("date")

        room = request.query_params.get("room", "").strip()
        if room.isdigit():
            bookings = bookings.filter(room_id=int(room))

        paginator = DefaultPagination()
        page = paginator.paginate_queryset(bookings, request, view=self)
        return paginator.get_paginated_response(
            InstitutionBookingSerializer(
                [self._row(booking) for booking in page], many=True
            ).data
        )

    @staticmethod
    def _row(booking):
        live = booking.live_session
        batch = getattr(live, "batch", None)
        course = getattr(batch, "course", None) or getattr(live, "course", None)
        # End time is derived, not stored: a room is occupied for the length of
        # the session held in it. Null when there is no session attached, which
        # is honest — an empty room booking has no duration to report.
        ends_at = None
        if booking.date and live and live.duration_minutes:
            ends_at = booking.date + timedelta(minutes=live.duration_minutes)
        return {
            "id": booking.pk,
            "room": booking.room.name,
            "room_id": booking.room_id,
            "location": booking.room.location,
            "starts_at": booking.date,
            "ends_at": ends_at,
            "status": booking.status,
            "batch": getattr(batch, "name", "") or "",
            "batch_id": getattr(batch, "pk", None),
            "course": getattr(course, "title", "") or "",
            "session_title": getattr(live, "title", "") or "",
            "trainer": getattr(booking.remote_trainer, "full_name", "") or "",
        }


class InstitutionActivityView(InstitutionAPIView):
    """GET ``/institution/activity/`` — the recent activity feed.

    ``?limit=`` (default 20, max 100), newest first.

    > ### This is assembled from real events, not an audit log
    >
    > The platform has no activity/audit table, so rather than leave the panel
    > empty this merges four things that genuinely happened to this
    > institution: enrolments, course completions, room bookings, and batch
    > creations. Every row is a real database record with a real timestamp.
    >
    > Two kinds the mock shows are **deliberately absent**. *"Flagged at-risk"*
    > has no definition anywhere in the platform — inventing a threshold here
    > would put a label on a named student on the strength of a number I made
    > up. *"Monthly analytics report exported"* has no export log to read. Both
    > want a real feature behind them, not a serializer that guesses.

    Deliberately a bounded feed rather than a paginated table: page 2 of an
    offset over four separately-ordered tables is not a coherent thing to ask
    for, and the panel has no "View all" for it. Each source is capped at
    ``limit`` before the merge, which is safe — the newest ``limit`` rows
    overall must be a subset of the newest ``limit`` from each source.
    """

    serializer_class = ActivityFeedSerializer

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "limit", int, OpenApiParameter.QUERY,
                description=f"1–{MAX_ACTIVITY_LIMIT}, default "
                            f"{DEFAULT_ACTIVITY_LIMIT}.",
            )
        ],
        responses=ActivityFeedSerializer,
    )
    def get(self, request):
        limit = self._limit(request)
        org = self.organization

        events = [
            *self._enrolments(org, limit),
            *self._completions(org, limit),
            *self._bookings(org, limit),
            *self._batches(org, limit),
        ]
        events.sort(key=lambda event: event["at"], reverse=True)

        return Response(
            ActivityFeedSerializer(
                {"limit": limit, "events": events[:limit]}
            ).data
        )

    @staticmethod
    def _limit(request):
        try:
            limit = int(request.query_params.get("limit", DEFAULT_ACTIVITY_LIMIT))
        except (TypeError, ValueError):
            return DEFAULT_ACTIVITY_LIMIT
        return max(1, min(limit, MAX_ACTIVITY_LIMIT))

    @staticmethod
    def _who(user):
        """A person's display name, falling back to the email.

        Members of an institution are already visible to it by email through
        ``/admin/organizations/{id}/members/``, so this leaks nothing new — but
        a feed row reading "None enrolled in Batch A" is worse than either.
        """
        if user is None:
            return ""
        return user.full_name or user.email

    @classmethod
    def _enrolments(cls, org, limit):
        rows = (
            _org_enrollments(org)
            .select_related("student", "course", "batch")
            .order_by("-enrolled_at")[:limit]
        )
        return [
            {
                "type": "enrolment",
                "at": row.enrolled_at,
                "actor": cls._who(row.student),
                "target": row.batch.name if row.batch else row.course.title,
                "meta": {
                    "batch_id": row.batch_id,
                    "course_id": row.course_id,
                    "course": row.course.title,
                },
            }
            for row in rows
        ]

    @classmethod
    def _completions(cls, org, limit):
        rows = (
            _org_enrollments(org)
            .filter(
                status=Enrollment.Status.COMPLETED, completed_at__isnull=False
            )
            .select_related("student", "course", "batch")
            .order_by("-completed_at")[:limit]
        )
        return [
            {
                "type": "completion",
                "at": row.completed_at,
                "actor": cls._who(row.student),
                "target": row.course.title,
                "meta": {
                    "batch_id": row.batch_id,
                    "course_id": row.course_id,
                    "progress_pct": row.progress_pct,
                },
            }
            for row in rows
        ]

    @staticmethod
    def _bookings(org, limit):
        rows = (
            ClassroomSession.objects.filter(room__organization=org)
            .select_related("room", "live_session", "live_session__batch")
            .order_by("-created_at")[:limit]
        )
        return [
            {
                "type": "booking",
                "at": row.created_at,
                "actor": "",
                "target": row.room.name,
                "meta": {
                    "room_id": row.room_id,
                    "starts_at": row.date,
                    "status": row.status,
                    "batch": getattr(
                        getattr(row.live_session, "batch", None), "name", ""
                    ) or "",
                },
            }
            for row in rows
        ]

    @staticmethod
    def _batches(org, limit):
        rows = (
            Batch.objects.filter(organization=org)
            .select_related("course")
            .order_by("-created_at")[:limit]
        )
        return [
            {
                "type": "batch",
                "at": row.created_at,
                "actor": "",
                "target": row.name,
                "meta": {
                    "batch_id": row.pk,
                    "course_id": row.course_id,
                    "course": row.course.title,
                    "capacity": row.capacity,
                },
            }
            for row in rows
        ]


# --------------------------------------------------------------------------- #
# The Reports screen
# --------------------------------------------------------------------------- #


def _rate(part, whole):
    """``part/whole`` as a percentage, or **None when there is no denominator**.

    Null and zero mean opposite things on every chart this feeds: a cohort
    nobody has taken a register for has no attendance rate, and painting that
    as 0% says everybody stayed away.
    """
    if not whole:
        return None
    return round(part * 100.0 / whole, 1)


def _attendance_rates(batch_ids):
    """Present ÷ recorded, per batch, in one query.

    Grouped rather than per-row: this is a report, and the row count is exactly
    the thing that grows. Batches with no register taken are simply absent, so
    the caller's ``.get()`` returns ``None`` rather than a misleading zero —
    the same shape ``analytics.views.ReportAttendanceView`` uses.
    """
    if not batch_ids:
        return {}
    rows = (
        Attendance.objects.filter(session__batch_id__in=batch_ids)
        .values("session__batch_id")
        .annotate(
            records=Count("id"), present=Count("id", filter=Q(present=True))
        )
    )
    return {
        row["session__batch_id"]: _rate(row["present"], row["records"])
        for row in rows
    }


def _certified_counts(batch_ids):
    """Issued certificates per batch, via ``Certificate.enrollment.batch``."""
    if not batch_ids:
        return {}
    rows = (
        Certificate.objects.filter(
            enrollment__batch_id__in=batch_ids,
            status=Certificate.Status.ISSUED,
        )
        .values("enrollment__batch_id")
        .annotate(n=Count("id"))
    )
    return {row["enrollment__batch_id"]: row["n"] for row in rows}


def _engagement_rates(batches):
    """Lessons actually completed ÷ lessons the cohort could have completed.

    > ### "Engagement" is a chosen definition
    >
    > Nothing in the PRD defines it. This counts **work done**: completed
    > ``LessonProgress`` rows for the cohort's enrolments, over
    > *learners × lessons in the course*.
    >
    > Logins were the obvious alternative and are worse — they measure showing
    > up, not getting anywhere, and a cohort that opens the app daily without
    > finishing anything would score 100%. Attendance is already its own
    > column, so reusing it here would print the same number twice.
    >
    > ``null`` when the course has no lessons yet or the cohort has nobody in
    > it — there is nothing to be a fraction of.

    The denominator is **every enrolment the cohort has ever had**, not the
    ``learners`` column beside it, which counts only *active* ones. Two reasons:
    the numerator counts those learners' completed lessons too, so the halves
    must match; and Reports is precisely where finished cohorts are read, and a
    cohort whose enrolments have all moved to ``completed`` would otherwise
    show a dash for the work it actually did.

    Two queries for the whole page rather than two per row.
    """
    batch_ids = [b.pk for b in batches]
    if not batch_ids:
        return {}

    done = {
        row["enrollment__batch_id"]: row["n"]
        for row in LessonProgress.objects.filter(
            enrollment__batch_id__in=batch_ids,
            status=LessonProgress.Status.COMPLETED,
        )
        .values("enrollment__batch_id")
        .annotate(n=Count("id"))
    }
    lessons = {
        row["module__course_id"]: row["n"]
        for row in Lesson.objects.filter(
            module__course_id__in={b.course_id for b in batches}
        )
        .values("module__course_id")
        .annotate(n=Count("id"))
    }

    rates = {}
    for batch in batches:
        available = lessons.get(batch.course_id, 0) * getattr(
            batch, "enrolled_total", 0
        )
        rate = _rate(done.get(batch.pk, 0), available)
        # Guard, not arithmetic: ``Enrollment.course`` and ``Enrollment.batch``
        # are independent FKs, so an enrolment filed under this batch but
        # pointing at a different course contributes completions the
        # denominator's lesson count knows nothing about. Rare and arguably
        # bad data — but a report that prints 140% is a report nobody trusts
        # again.
        rates[batch.pk] = None if rate is None else min(rate, 100.0)
    return rates


def _report_cohorts(org):
    """The cohort table's queryset: every batch, with its learner count."""
    return (
        Batch.objects.filter(organization=org)
        .select_related("course")
        .annotate(
            learners=Count(
                "enrollments",
                filter=Q(enrollments__status=Enrollment.Status.ACTIVE),
                distinct=True,
            ),
            # Everyone who was ever in the cohort — the denominator engagement
            # needs, and not the same number as ``learners``.
            enrolled_total=Count("enrollments", distinct=True),
            completion=Avg("enrollments__progress_pct"),
        )
        .order_by("-start_date", "-created_at")
    )


def _cohort_rows(batches):
    """Assemble the table's rows: three grouped queries for the whole page."""
    batch_ids = [b.pk for b in batches]
    attendance = _attendance_rates(batch_ids)
    certified = _certified_counts(batch_ids)
    engagement = _engagement_rates(batches)

    return [
        {
            "batch_id": batch.pk,
            "batch": batch.name,
            "course": batch.course.title,
            "course_id": batch.course_id,
            "learners": batch.learners,
            "capacity": batch.capacity,
            "completion": _pct(batch.completion),
            "certified": certified.get(batch.pk, 0),
            "certified_pct": _rate(
                certified.get(batch.pk, 0), batch.learners
            ),
            "engagement": engagement.get(batch.pk),
            "attendance": attendance.get(batch.pk),
            "start_date": batch.start_date,
            "end_date": batch.end_date,
        }
        for batch in batches
    ]


class InstitutionReportSummaryView(InstitutionAPIView):
    """GET ``/institution/reports/summary/`` — the four tiles."""

    serializer_class = InstitutionReportSummarySerializer

    @extend_schema(responses=InstitutionReportSummarySerializer)
    def get(self, request):
        org = self.organization
        batches = Batch.objects.filter(organization=org)
        register = Attendance.objects.filter(
            session__batch__organization=org
        ).aggregate(
            records=Count("id"), present=Count("id", filter=Q(present=True))
        )

        return Response(
            InstitutionReportSummarySerializer(
                {
                    "cohorts": batches.count(),
                    "active_cohorts": _active_batches(
                        batches, timezone.localdate()
                    ).count(),
                    "completion": _pct(
                        _org_enrollments(org).aggregate(
                            avg=Avg("progress_pct")
                        )["avg"]
                    ),
                    # A **count**, matching the tile. The table's ``certified``
                    # column is a count too and ``certified_pct`` is the rate —
                    # the mock shows a count here and a percentage there, so
                    # both are returned explicitly rather than left to be
                    # inferred from each other.
                    "certified": Certificate.objects.filter(
                        student__organization=org,
                        status=Certificate.Status.ISSUED,
                    ).count(),
                    "attendance": _rate(
                        register["present"], register["records"]
                    ),
                }
            ).data
        )


class InstitutionReportCohortsView(InstitutionAPIView):
    """GET ``/institution/reports/cohorts/`` — the "Cohort completion" table.

    **Paginated.** Cohorts accumulate for the life of the institution and this
    is the one list on the page whose row count grows without bound.

    Every rate is ``null`` rather than ``0`` when it has no denominator — see
    ``_rate``.
    """

    serializer_class = InstitutionReportCohortSerializer

    @extend_schema(responses=InstitutionReportCohortSerializer(many=True))
    def get(self, request):
        paginator = DefaultPagination()
        page = paginator.paginate_queryset(
            _report_cohorts(self.organization), request, view=self
        )
        return paginator.get_paginated_response(
            InstitutionReportCohortSerializer(
                _cohort_rows(page), many=True
            ).data
        )


class InstitutionReportAttendanceTrendView(InstitutionAPIView):
    """GET ``/institution/reports/attendance/`` — the attendance trend.

    ``?months=`` (1–36, default 12). Monthly present-rate across every session
    of your cohorts, oldest first, **dense**: every month in the window is
    present, including quiet ones.

    Bucketed by the **session's scheduled date**, not when the register row was
    written — a register filled in late belongs to the class it was taken for.

    > ### A month with no sessions is ``null``, not ``0``
    >
    > Zero would draw the line to the floor and say nobody turned up. Null says
    > no class was held. Break the line, or skip the point.

    Month boundaries come from ``analytics.views._month_bounds``, which
    computes them in Python for a specific reason — see its docstring; doing it
    in SQL silently renders an all-zero chart on a MySQL server whose timezone
    tables were never loaded.
    """

    serializer_class = InstitutionReportTrendSerializer

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "months", int, OpenApiParameter.QUERY,
                description=f"1–{MAX_MONTHS}, default {DEFAULT_MONTHS}.",
            )
        ],
        responses=InstitutionReportTrendSerializer,
    )
    def get(self, request):
        months = _months_param(request)
        bounds = _month_bounds(months)
        register = Attendance.objects.filter(
            session__batch__organization=self.organization,
            session__scheduled_start__gte=bounds[0][1],
            session__scheduled_start__lt=bounds[-1][2],
        )

        def per_month(extra=None):
            return _month_series(
                register,
                bounds,
                lambda start, end: Count(
                    "id",
                    filter=Q(
                        session__scheduled_start__gte=start,
                        session__scheduled_start__lt=end,
                        **(extra or {}),
                    ),
                ),
            )

        records, present = per_month(), per_month({"present": True})
        return Response(
            InstitutionReportTrendSerializer(
                {
                    "months": months,
                    "attendance": [
                        {
                            "month": row["month"],
                            "value": _rate(hit["value"], row["value"]),
                            "sessions_recorded": row["value"],
                        }
                        for row, hit in zip(records, present)
                    ],
                }
            ).data
        )


class InstitutionReportExportView(InstitutionAPIView):
    """GET ``/institution/reports/export/`` — "Export CSV".

    The cohort table, **whole** — not the current page. An export that stopped
    at twenty rows would be a quietly wrong spreadsheet, which is worse than no
    export at all.

    Empty cells for rates with no denominator, so a spreadsheet does not
    average a missing attendance figure in as a zero.
    """

    serializer_class = None

    @extend_schema(responses={200: OpenApiTypes.BINARY})
    def get(self, request):
        response = HttpResponse(content_type="text/csv")
        stamp = timezone.localdate().isoformat()
        response["Content-Disposition"] = (
            f'attachment; filename="{self.organization.slug}-cohorts-'
            f'{stamp}.csv"'
        )

        writer = csv.writer(response)
        writer.writerow([
            "Batch", "Course", "Learners", "Capacity", "Completion %",
            "Certified", "Certified %", "Engagement %", "Attendance %",
            "Start", "End",
        ])
        for row in _cohort_rows(list(_report_cohorts(self.organization))):
            writer.writerow([
                row["batch"], row["course"], row["learners"], row["capacity"],
                # "" rather than 0 for a rate that does not exist: a
                # spreadsheet averaging a missing figure as zero is exactly the
                # silent wrongness this whole module avoids.
                "" if row["completion"] is None else row["completion"],
                row["certified"],
                "" if row["certified_pct"] is None else row["certified_pct"],
                "" if row["engagement"] is None else row["engagement"],
                "" if row["attendance"] is None else row["attendance"],
                row["start_date"] or "", row["end_date"] or "",
            ])
        return response
