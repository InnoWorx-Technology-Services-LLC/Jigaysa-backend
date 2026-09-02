"""The trainer's **Analytics** page (PRD §3.14).

Four counters, an engagement trend, per-course insights, and the doubt queue.

Kept apart from ``analytics.views`` — the admin Reports page — because the two
answer different questions from the same tables. Reports is platform-wide and
`IsAdmin`; this is **always scoped to the caller's own courses**, and that
scoping is the feature. A trainer looking at completion rates wants theirs, not
the platform's, and an `if admin` branch inside one view is how the wrong number
eventually reaches the wrong person.

Every number here is derived from courses where ``course.trainer == you``.
Admins get the same endpoints scoped to *their* own courses, which for most
admins is none — that is correct, and the admin view of the platform lives at
``/admin/reports/``.
"""

from django.db.models import Avg, Case, Count, IntegerField, Q, Value, When
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView

from analytics.serializers import (
    CourseInsightSerializer,
    DoubtSerializer,
    TrainerSummarySerializer,
    TrainerTrendSerializer,
)
from analytics.views import (
    DEFAULT_MONTHS,
    MAX_MONTHS,
    _month_bounds,
    _month_series,
    _months_param,
)
from assessments.models import Submission
from core.pagination import DefaultPagination
from core.permissions import HasRole
from courses.models import Course, Enrollment, LessonProgress
from live.models import SessionDoubt

TRAINER_OR_ADMIN = HasRole("trainer", "admin")

#: Statuses that mean a student actually handed something in, as opposed to
#: opening an assessment and wandering off.
SUBMITTED_STATES = (
    Submission.Status.SUBMITTED,
    Submission.Status.GRADED,
    Submission.Status.PASSED,
    Submission.Status.FAILED,
)


def _my_courses(user):
    return Course.objects.filter(trainer=user)


class TrainerAnalyticsSummaryView(APIView):
    """GET ``/trainer/analytics/summary/`` — the four tiles.

    Each is a rate over the caller's own courses, and each is ``null`` rather
    than ``0`` when there is nothing to average. A trainer with no submissions
    has no quiz average; printing 0% would read as "everyone failed".
    """

    permission_classes = [TRAINER_OR_ADMIN]
    serializer_class = TrainerSummarySerializer
    api_roles = ("trainer", "admin")

    @extend_schema(responses=TrainerSummarySerializer)
    def get(self, request):
        courses = _my_courses(request.user)

        enrollments = Enrollment.objects.filter(course__in=courses)
        active = enrollments.filter(status=Enrollment.Status.ACTIVE)

        # Completion is averaged over enrolments, not lessons: a course where
        # everyone finished half of it and one where half the students finished
        # all of it are not the same story, and the enrolment view is the one a
        # trainer acts on.
        completion = enrollments.aggregate(avg=Avg("progress_pct"))["avg"]

        submissions = Submission.objects.filter(assessment__course__in=courses)
        graded = submissions.filter(
            status__in=(
                Submission.Status.GRADED,
                Submission.Status.PASSED,
                Submission.Status.FAILED,
            )
        )
        quiz_average = graded.aggregate(avg=Avg("percent"))["avg"]

        # Submission rate: of everything started, how much was actually handed
        # in. An abandoned attempt is the denominator's whole point.
        started = submissions.count()
        handed_in = submissions.filter(status__in=SUBMITTED_STATES).count()
        submission_rate = (handed_in * 100.0 / started) if started else None

        return Response(
            TrainerSummarySerializer(
                {
                    "completion": _pct(completion),
                    "quiz_average": _pct(quiz_average),
                    "submission_rate": _pct(submission_rate),
                    "active_learners": active.values("student").distinct().count(),
                    "courses": courses.count(),
                }
            ).data
        )


def _pct(value):
    return None if value is None else round(float(value), 1)


class TrainerEngagementTrendView(APIView):
    """GET ``/trainer/analytics/engagement/`` — the line chart.

    "Engagement" is **lessons completed per month** across the trainer's
    courses. Enrolments would measure marketing rather than teaching, and a
    login count would flatter a course nobody is actually working through.

    Dense by construction, like the admin trends — see
    ``analytics.views._month_series`` for why a sparse series must not be
    charted directly.
    """

    permission_classes = [TRAINER_OR_ADMIN]
    serializer_class = TrainerTrendSerializer
    api_roles = ("trainer", "admin")

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "months", int, OpenApiParameter.QUERY,
                description=f"1–{MAX_MONTHS}, default {DEFAULT_MONTHS}.",
            )
        ],
        responses=TrainerTrendSerializer,
    )
    def get(self, request):
        months = _months_param(request)
        bounds = _month_bounds(months)
        courses = _my_courses(request.user)

        completions = LessonProgress.objects.filter(
            lesson__module__course__in=courses,
            status=LessonProgress.Status.COMPLETED,
            completed_at__gte=bounds[0][1],
            completed_at__lt=bounds[-1][2],
        )
        submissions = Submission.objects.filter(
            assessment__course__in=courses,
            status__in=SUBMITTED_STATES,
            created_at__gte=bounds[0][1],
            created_at__lt=bounds[-1][2],
        )

        return Response(
            TrainerTrendSerializer(
                {
                    "months": months,
                    "lessons_completed": _month_series(
                        completions,
                        bounds,
                        lambda start, end: Count(
                            "id",
                            filter=Q(
                                completed_at__gte=start, completed_at__lt=end
                            ),
                        ),
                    ),
                    "submissions": _month_series(
                        submissions,
                        bounds,
                        lambda start, end: Count(
                            "id",
                            filter=Q(created_at__gte=start, created_at__lt=end),
                        ),
                    ),
                }
            ).data
        )


class TrainerCourseInsightsView(APIView):
    """GET ``/trainer/analytics/courses/`` — the per-course table.

    **Paginated.** A prolific trainer accumulates courses without bound, and
    this is the only list on the page that does.

    Learners, completion and quiz average, in two queries rather than three per
    row. Both averages are ``null`` when there is nothing to average.
    """

    permission_classes = [TRAINER_OR_ADMIN]
    serializer_class = CourseInsightSerializer
    api_roles = ("trainer", "admin")

    @extend_schema(responses=CourseInsightSerializer(many=True))
    def get(self, request):
        courses = (
            _my_courses(request.user)
            .annotate(
                learners=Count(
                    "enrollments",
                    filter=Q(enrollments__status=Enrollment.Status.ACTIVE),
                    distinct=True,
                ),
                completion=Avg("enrollments__progress_pct"),
            )
            .order_by("-enrolled_count", "-created_at")
        )

        paginator = DefaultPagination()
        page = paginator.paginate_queryset(courses, request, view=self)

        quiz = self._quiz_averages([c.pk for c in page])
        rows = [
            {
                "course_id": course.pk,
                "course": course.title,
                "course_slug": course.slug,
                "learners": course.learners,
                "completion": _pct(course.completion),
                "quiz_average": quiz.get(course.pk),
            }
            for course in page
        ]
        return paginator.get_paginated_response(
            CourseInsightSerializer(rows, many=True).data
        )

    @staticmethod
    def _quiz_averages(course_ids):
        """Graded-submission averages for a page of courses, in one query.

        Separate from the annotation above rather than a third `Avg` on the
        same statement: joining enrolments and submissions together multiplies
        the rows and quietly skews both averages. Two honest queries beat one
        clever wrong one.
        """
        if not course_ids:
            return {}
        rows = (
            Submission.objects.filter(
                assessment__course_id__in=course_ids,
                status__in=(
                    Submission.Status.GRADED,
                    Submission.Status.PASSED,
                    Submission.Status.FAILED,
                ),
            )
            .values("assessment__course_id")
            .annotate(avg=Avg("percent"))
        )
        return {
            row["assessment__course_id"]: _pct(row["avg"])
            for row in rows
            if row["avg"] is not None
        }


class TrainerDoubtsView(APIView):
    """GET ``/trainer/analytics/doubts/`` — questions waiting on this trainer.

    **Paginated**, open doubts first, newest within that.

    > ### This is not the mock's "doubt frequency"
    >
    > That panel is labelled *AI-detected from forum & Q&A* and ranks clustered
    > topics ("useEffect cleanup", 47). Clustering free text into topics needs a
    > language model, and this project has none — so rather than fabricate the
    > ranking, this returns the doubts themselves, which is real data and
    > arguably more useful: a trainer can answer these.
    >
    > ``?status=open`` is the actionable filter. Build the panel as a queue, not
    > a bar chart, until there is something behind the clustering.
    """

    permission_classes = [TRAINER_OR_ADMIN]
    serializer_class = DoubtSerializer
    api_roles = ("trainer", "admin")

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "status", str, OpenApiParameter.QUERY,
                description="open | answered",
            ),
            OpenApiParameter("course", int, OpenApiParameter.QUERY),
        ],
        responses=DoubtSerializer(many=True),
    )
    def get(self, request):
        doubts = (
            SessionDoubt.objects.filter(session__trainer=request.user)
            .select_related("session", "session__course", "student")
            # Explicitly open-first. Ordering by ``status`` alone sorts
            # alphabetically, which puts "answered" above "open" — the exact
            # inverse of a queue, and silently so.
            .annotate(
                is_open=Case(
                    When(status=SessionDoubt.Status.OPEN, then=Value(0)),
                    default=Value(1),
                    output_field=IntegerField(),
                )
            )
            .order_by("is_open", "-asked_at")
        )

        state = request.query_params.get("status", "").strip()
        if state in dict(SessionDoubt.Status.choices):
            doubts = doubts.filter(status=state)
        course = request.query_params.get("course", "").strip()
        if course.isdigit():
            doubts = doubts.filter(session__course_id=int(course))

        paginator = DefaultPagination()
        page = paginator.paginate_queryset(doubts, request, view=self)
        rows = [
            {
                "id": doubt.pk,
                "text": doubt.text,
                "status": doubt.status,
                "asked_at": doubt.asked_at,
                "student_name": getattr(doubt.student, "full_name", "") or "",
                "session_title": doubt.session.title,
                "course": getattr(doubt.session.course, "title", "") or "",
            }
            for doubt in page
        ]
        return paginator.get_paginated_response(
            DoubtSerializer(rows, many=True).data
        )
