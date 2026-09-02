"""Analytics routes (PRD §3.14). Mounted at ``/api/v1/``.

Two audiences, deliberately two prefixes: ``admin/reports/`` is platform-wide
and admin-only, ``trainer/analytics/`` is scoped to the caller's own courses.
Same tables, different questions — see ``analytics.trainer_api``.

Plain ``APIView``s rather than a router: these are computed reports, not
resources — there is nothing to retrieve by id and nothing to write.
"""

from django.urls import path

from analytics import trainer_api, views

app_name = "analytics"

urlpatterns = [
    path(
        "admin/reports/summary/",
        views.ReportSummaryView.as_view(),
        name="report-summary",
    ),
    path(
        "admin/reports/trends/",
        views.ReportTrendsView.as_view(),
        name="report-trends",
    ),
    path(
        "admin/reports/users-by-role/",
        views.ReportUsersByRoleView.as_view(),
        name="report-users-by-role",
    ),
    path(
        "admin/reports/attendance/",
        views.ReportAttendanceView.as_view(),
        name="report-attendance",
    ),
    # --- Trainer analytics: the same tables, scoped to your own courses --- #
    path(
        "trainer/analytics/summary/",
        trainer_api.TrainerAnalyticsSummaryView.as_view(),
        name="trainer-summary",
    ),
    path(
        "trainer/analytics/engagement/",
        trainer_api.TrainerEngagementTrendView.as_view(),
        name="trainer-engagement",
    ),
    path(
        "trainer/analytics/courses/",
        trainer_api.TrainerCourseInsightsView.as_view(),
        name="trainer-course-insights",
    ),
    path(
        "trainer/analytics/doubts/",
        trainer_api.TrainerDoubtsView.as_view(),
        name="trainer-doubts",
    ),
]
