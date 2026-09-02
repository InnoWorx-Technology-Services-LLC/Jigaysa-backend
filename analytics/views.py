"""The admin console's **Reports** page (PRD §3.14).

Four counters, two twelve-month trend lines, a role breakdown, and a per-batch
attendance table.

**Everything here is computed on read from the operational tables.** There is an
``AnalyticsSnapshot`` model for caching periodic metrics and nothing writes to
it; reading live means the numbers cannot silently go stale, which matters far
more at this size than the query cost. If these ever get slow the fix is to fill
that table on a schedule and read it here — the shape of these responses is
designed to survive that swap.

One rule the whole module follows: **an empty platform returns zeros and empty
arrays, never an error and never a gap.** A brand-new deployment opening Reports
should see an honest set of noughts, not a broken chart.
"""

from datetime import datetime, timedelta

from django.db.models import Count, Q, Sum
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Role, User
from analytics.serializers import (
    AttendanceRowSerializer,
    ReportSummarySerializer,
    RoleBreakdownSerializer,
    TrendSerializer,
)
from core.pagination import DefaultPagination
from core.permissions import IsAdmin
from courses.models import Batch, Enrollment
from live.models import Attendance
from payments.models import Payment, TrainerPayout
from payments.services import platform_currency

#: How far back the trend charts look, and the ceiling a caller can ask for.
#: Twelve months is what the mock draws; the cap stops a `?months=600` turning a
#: dashboard into a table scan.
DEFAULT_MONTHS = 12
MAX_MONTHS = 36

#: "Active" for the headline counter. Signed in within this window — a
#: registered account that has not appeared in three months is a row in the
#: users table, not a user of the platform, and counting it flatters the number.
ACTIVE_WINDOW_DAYS = 90


def _months_param(request) -> int:
    try:
        months = int(request.query_params.get("months", DEFAULT_MONTHS))
    except (TypeError, ValueError):
        return DEFAULT_MONTHS
    return max(1, min(months, MAX_MONTHS))


def _month_bounds(months: int):
    """``[(label, start, end)]`` for the window, oldest first, in local time.

    Boundaries are computed **in Python**, not with ``TruncMonth``, and the
    queries below compare plain datetimes rather than using ``__date``. Both of
    those emit ``CONVERT_TZ`` on MySQL, which returns ``NULL`` unless the
    server's timezone tables have been loaded (``mysql_tzinfo_to_sql``) — and a
    ``NULL`` there does not error, it silently filters every row out and paints
    an honest-looking chart of all zeros. A dashboard that quietly reads empty
    is worse than one that fails, so this does not depend on that data load.
    """
    tz = timezone.get_current_timezone()
    today = timezone.localdate()
    year, month = today.year, today.month - (months - 1)
    while month <= 0:
        month += 12
        year -= 1

    bounds = []
    for _ in range(months):
        next_year, next_month = (
            (year + 1, 1) if month == 12 else (year, month + 1)
        )
        bounds.append(
            (
                f"{year:04d}-{month:02d}",
                timezone.make_aware(datetime(year, month, 1), tz),
                timezone.make_aware(datetime(next_year, next_month, 1), tz),
            )
        )
        year, month = next_year, next_month
    return bounds


def _month_series(queryset, bounds, aggregate):
    """One query, one conditional aggregate per month, dense by construction.

    Grouping in SQL returns only the months that had rows, so a quiet December
    is simply absent — chart that directly and the line runs straight from
    November to January, hiding the dip. Asking for every bucket explicitly
    means a month with nothing in it comes back as a zero rather than a gap.
    """
    row = queryset.aggregate(
        **{
            f"m{i}": aggregate(start, end)
            for i, (_, start, end) in enumerate(bounds)
        }
    )
    return [
        {"month": label, "value": row[f"m{i}"] or 0}
        for i, (label, _, _) in enumerate(bounds)
    ]


class ReportSummaryView(APIView):
    """GET ``/admin/reports/summary/`` — the four tiles."""

    permission_classes = [IsAdmin]
    serializer_class = ReportSummarySerializer
    api_roles = ("admin",)

    @extend_schema(responses=ReportSummarySerializer)
    def get(self, request):
        since = timezone.now() - timedelta(days=ACTIVE_WINDOW_DAYS)
        revenue = Payment.objects.filter(
            status=Payment.Status.SUCCESS
        ).aggregate(total=Sum("amount"))["total"] or 0
        payouts = TrainerPayout.objects.filter(
            status=TrainerPayout.Status.PAID
        ).aggregate(total=Sum("net"))["total"] or 0

        return Response(
            ReportSummarySerializer(
                {
                    "active_users": User.objects.filter(
                        is_active=True, last_login__gte=since
                    ).count(),
                    "enrollments": Enrollment.objects.count(),
                    "revenue": revenue,
                    "payouts": payouts,
                    "currency": platform_currency(),
                    "active_window_days": ACTIVE_WINDOW_DAYS,
                }
            ).data
        )


class ReportTrendsView(APIView):
    """GET ``/admin/reports/trends/`` — the two line charts.

    Both series come back over the **same dense month range**, so the frontend
    can share one x-axis and trust that index *n* means the same month in each.
    """

    permission_classes = [IsAdmin]
    serializer_class = TrendSerializer
    api_roles = ("admin",)

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "months", int, OpenApiParameter.QUERY,
                description=f"1–{MAX_MONTHS}, default {DEFAULT_MONTHS}.",
            )
        ],
        responses=TrendSerializer,
    )
    def get(self, request):
        months = _months_param(request)
        bounds = _month_bounds(months)
        window = Q(created_at__gte=bounds[0][1], created_at__lt=bounds[-1][2])

        enrollments = _month_series(
            Enrollment.objects.filter(window),
            bounds,
            lambda start, end: Count(
                "id", filter=Q(created_at__gte=start, created_at__lt=end)
            ),
        )
        revenue = _month_series(
            Payment.objects.filter(window, status=Payment.Status.SUCCESS),
            bounds,
            lambda start, end: Sum(
                "amount", filter=Q(created_at__gte=start, created_at__lt=end)
            ),
        )

        return Response(
            TrendSerializer(
                {
                    "months": months,
                    "enrollments": enrollments,
                    "revenue": revenue,
                    "currency": platform_currency(),
                }
            ).data
        )


class ReportUsersByRoleView(APIView):
    """GET ``/admin/reports/users-by-role/`` — the four bars.

    Returns ``total`` alongside the counts so the bar widths are the server's
    arithmetic, not four client-side divisions that disagree at the rounding.
    """

    permission_classes = [IsAdmin]
    serializer_class = RoleBreakdownSerializer
    api_roles = ("admin",)

    @extend_schema(responses=RoleBreakdownSerializer)
    def get(self, request):
        counts = {
            row["role"]: row["n"]
            for row in User.objects.values("role").annotate(n=Count("id"))
        }
        roles = [
            {"role": role, "label": label, "count": counts.get(role, 0)}
            for role, label in Role.choices
        ]
        return Response(
            RoleBreakdownSerializer(
                {"total": sum(r["count"] for r in roles), "roles": roles}
            ).data
        )


class ReportAttendanceView(APIView):
    """GET ``/admin/reports/attendance/`` — attendance rate per batch.

    **Paginated**, unlike the tiles above it: batches accumulate for the life of
    the platform, and this is the one report on the page whose row count grows
    without bound.

    The rate is *present sessions ÷ attendance records* for sessions in the
    batch's course. A batch nobody has taken a register for yet returns
    ``null``, not ``0`` — those mean opposite things and colouring "no data" as
    a total failure is the kind of chart that starts a meeting.
    """

    permission_classes = [IsAdmin]
    serializer_class = AttendanceRowSerializer
    api_roles = ("admin",)

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "course", str, OpenApiParameter.QUERY,
                description="Limit to one course, by slug.",
            )
        ],
        responses=AttendanceRowSerializer(many=True),
    )
    def get(self, request):
        batches = Batch.objects.select_related("course").order_by(
            "-start_date", "-created_at"
        )
        course = request.query_params.get("course", "").strip()
        if course:
            batches = batches.filter(course__slug=course)

        paginator = DefaultPagination()
        page = paginator.paginate_queryset(batches, request, view=self)

        rates = self._rates([b.pk for b in page])
        rows = [
            {
                "batch_id": batch.pk,
                "batch": batch.name,
                "course": batch.course.title,
                "course_slug": batch.course.slug,
                "enrolled": batch.enrolled_count,
                "capacity": batch.capacity,
                "attendance_rate": rates.get(batch.pk),
            }
            for batch in page
        ]
        return paginator.get_paginated_response(
            AttendanceRowSerializer(rows, many=True).data
        )

    @staticmethod
    def _rates(batch_ids):
        """Attendance rate for a whole page of batches in one query.

        Grouped rather than per-row on purpose: a rate each would be twenty
        queries for a twenty-row page, and this is a report — the row count is
        exactly the thing that grows.

        Batches with no register taken are simply absent from the result, which
        is what makes ``rates.get(...)`` return ``None`` for them rather than a
        misleading zero.
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
            row["session__batch_id"]: round(
                row["present"] * 100.0 / row["records"], 1
            )
            for row in rows
            if row["records"]
        }
