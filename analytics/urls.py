"""Analytics routes (PRD §3.14). Mounted at ``/api/v1/``.

Three audiences, deliberately three prefixes: ``admin/reports/`` is
platform-wide and admin-only, ``trainer/analytics/`` is scoped to the caller's
own courses, and ``institution/`` is scoped to the caller's own organisation.
Same tables, different questions — see ``analytics.trainer_api`` and
``analytics.institution_api``.

Plain ``APIView``s rather than a router: these are computed reports, not
resources — there is nothing to retrieve by id and nothing to write.
"""

from django.urls import path
from rest_framework.routers import DefaultRouter

from analytics import institution_api, trainer_api, views

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
    path(
        "trainer/analytics/doubts/<int:pk>/answer/",
        trainer_api.TrainerDoubtAnswerView.as_view(),
        name="trainer-doubt-answer",
    ),

    # --- The institution console: the same tables, scoped to your tenant --- #
    path(
        "institution/overview/",
        institution_api.InstitutionOverviewView.as_view(),
        name="institution-overview",
    ),
    path(
        "institution/bookings/",
        institution_api.InstitutionBookingsView.as_view(),
        name="institution-bookings",
    ),
    path(
        "institution/activity/",
        institution_api.InstitutionActivityView.as_view(),
        name="institution-activity",
    ),
    path(
        "institution/reports/summary/",
        institution_api.InstitutionReportSummaryView.as_view(),
        name="institution-report-summary",
    ),
    path(
        "institution/reports/cohorts/",
        institution_api.InstitutionReportCohortsView.as_view(),
        name="institution-report-cohorts",
    ),
    path(
        "institution/reports/attendance/",
        institution_api.InstitutionReportAttendanceTrendView.as_view(),
        name="institution-report-attendance",
    ),
    path(
        "institution/reports/export/",
        institution_api.InstitutionReportExportView.as_view(),
        name="institution-report-export",
    ),
]

#: Batches are the one institution resource with writes ("New batch",
#: "Manage"), so they get a router rather than a hand-written path pair. The
#: list URL is unchanged — ``/institution/batches/``.
_institution_router = DefaultRouter()
_institution_router.register(
    "institution/batches",
    institution_api.InstitutionBatchViewSet,
    basename="institution-batch",
)
_institution_router.register(
    "institution/learners",
    institution_api.InstitutionLearnerViewSet,
    basename="institution-learner",
)
_institution_router.register(
    "institution/courses",
    institution_api.InstitutionCourseViewSet,
    basename="institution-course",
)
urlpatterns += _institution_router.urls
