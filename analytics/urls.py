"""Reports routes for the admin console (PRD §3.14). Mounted at ``/api/v1/``.

Plain ``APIView``s rather than a router: these are four computed reports, not a
resource — there is nothing to retrieve by id and nothing to write.
"""

from django.urls import path

from analytics import views

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
]
