"""The admin console's Reports page (PRD §3.14).

Two things carry real risk here and get most of the attention: the **dense month
series** (a sparse aggregate charted directly hides every quiet month) and the
**null-vs-zero attendance rate** (they mean opposite things and one of them
starts a meeting).
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Role, User
from courses.models import Batch, Course, Enrollment
from live.models import Attendance, LiveSession
from payments.models import Order, Payment

pytestmark = pytest.mark.django_db

SUMMARY_URL = "/api/v1/admin/reports/summary/"
TRENDS_URL = "/api/v1/admin/reports/trends/"
ROLES_URL = "/api/v1/admin/reports/users-by-role/"
ATTENDANCE_URL = "/api/v1/admin/reports/attendance/"


def _api(user=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


@pytest.fixture
def admin():
    return User.objects.create_user(
        email="rep-admin@example.com", password="StrongPass123!", role=Role.ADMIN
    )


@pytest.fixture
def trainer():
    return User.objects.create_user(
        email="rep-trainer@example.com", password="StrongPass123!",
        role=Role.TRAINER,
    )


@pytest.fixture
def course(trainer):
    return Course.objects.create(title="React 19 Pro", trainer=trainer)


# --------------------------------------------------------------------------- #
# Access
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url", [SUMMARY_URL, TRENDS_URL, ROLES_URL, ATTENDANCE_URL]
)
def test_reports_are_admin_only(url, trainer):
    assert _api(trainer).get(url).status_code == status.HTTP_403_FORBIDDEN


# --------------------------------------------------------------------------- #
# An empty platform
# --------------------------------------------------------------------------- #


def test_an_empty_platform_returns_zeros_not_errors(admin):
    """A fresh deployment opening Reports should see honest noughts."""
    summary = _api(admin).get(SUMMARY_URL)
    assert summary.status_code == status.HTTP_200_OK
    assert summary.data["enrollments"] == 0
    assert Decimal(summary.data["revenue"]) == 0

    trends = _api(admin).get(TRENDS_URL)
    assert trends.status_code == status.HTTP_200_OK
    # Still twelve points — an empty chart is a flat line, not a missing one.
    assert len(trends.data["enrollments"]) == 12
    assert all(Decimal(p["value"]) == 0 for p in trends.data["enrollments"])

    attendance = _api(admin).get(ATTENDANCE_URL)
    assert attendance.data["count"] == 0


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #


def test_summary_counts_enrollments_and_captured_revenue(admin, course):
    student = User.objects.create_user(
        email="s1@example.com", password="StrongPass123!", role=Role.STUDENT
    )
    Enrollment.objects.create(student=student, course=course)

    order = Order.objects.create(user=student, status=Order.Status.PAID, total=900)
    Payment.objects.create(
        order=order, gateway="razorpay", amount=Decimal("900.00"),
        status=Payment.Status.SUCCESS,
    )
    # A failed attempt is not revenue.
    Payment.objects.create(
        order=order, gateway="razorpay", amount=Decimal("100.00"),
        status=Payment.Status.FAILED,
    )

    data = _api(admin).get(SUMMARY_URL).data
    assert data["enrollments"] == 1
    assert Decimal(data["revenue"]) == Decimal("900.00")
    # The definition travels with the number so the frontend can label it.
    assert data["active_window_days"] == 90


def test_active_users_counts_recent_sign_ins_only(admin):
    recent = User.objects.create_user(
        email="recent@example.com", password="StrongPass123!"
    )
    recent.last_login = timezone.now() - timedelta(days=3)
    recent.save(update_fields=["last_login"])

    stale = User.objects.create_user(
        email="stale@example.com", password="StrongPass123!"
    )
    stale.last_login = timezone.now() - timedelta(days=200)
    stale.save(update_fields=["last_login"])

    assert _api(admin).get(SUMMARY_URL).data["active_users"] == 1


# --------------------------------------------------------------------------- #
# Trends
# --------------------------------------------------------------------------- #


def test_the_month_series_is_dense_and_ordered(admin, course):
    student = User.objects.create_user(
        email="s2@example.com", password="StrongPass123!", role=Role.STUDENT
    )
    Enrollment.objects.create(student=student, course=course)

    data = _api(admin).get(TRENDS_URL).data
    months = [p["month"] for p in data["enrollments"]]

    assert len(months) == 12
    assert months == sorted(months)  # oldest first, no gaps
    assert len(set(months)) == 12
    # Both charts share an axis, so index n must be the same month in each.
    assert months == [p["month"] for p in data["revenue"]]
    # This month has the one enrollment; the eleven before it are explicit zeros.
    assert Decimal(data["enrollments"][-1]["value"]) == 1
    assert all(Decimal(p["value"]) == 0 for p in data["enrollments"][:-1])


def test_months_is_clamped_rather_than_rejected(admin):
    assert len(_api(admin).get(TRENDS_URL, {"months": 3}).data["enrollments"]) == 3
    assert len(_api(admin).get(TRENDS_URL, {"months": 900}).data["enrollments"]) == 36
    assert len(_api(admin).get(TRENDS_URL, {"months": 0}).data["enrollments"]) == 1
    # A junk value falls back to the default instead of 400ing the dashboard.
    assert len(_api(admin).get(TRENDS_URL, {"months": "abc"}).data["enrollments"]) == 12


# --------------------------------------------------------------------------- #
# Users by role
# --------------------------------------------------------------------------- #


def test_users_by_role_returns_every_role_with_the_denominator(admin, trainer):
    data = _api(admin).get(ROLES_URL).data
    by_role = {r["role"]: r["count"] for r in data["roles"]}

    assert by_role[Role.ADMIN] == 1
    assert by_role[Role.TRAINER] == 1
    # Roles with nobody in them still appear, so the bar list is stable.
    assert by_role[Role.INSTITUTION] == 0
    assert data["total"] == 2
    assert sum(r["count"] for r in data["roles"]) == data["total"]


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #


def _batch_with_register(course, trainer, name, present, absent):
    batch = Batch.objects.create(course=course, name=name, capacity=30)
    session = LiveSession.objects.create(
        course=course, batch=batch, trainer=trainer, title=f"{name} session"
    )
    for i in range(present + absent):
        student = User.objects.create_user(
            email=f"{name}-{i}@example.com".lower().replace(" ", ""),
            password="StrongPass123!",
        )
        Attendance.objects.create(
            session=session, student=student, present=i < present
        )
    return batch


def test_attendance_rate_is_a_percentage_per_batch(admin, course, trainer):
    _batch_with_register(course, trainer, "Batch A", present=8, absent=2)

    resp = _api(admin).get(ATTENDANCE_URL)
    assert resp.status_code == status.HTTP_200_OK
    row = resp.data["results"][0]
    assert row["batch"] == "Batch A"
    assert row["course"] == "React 19 Pro"
    assert row["attendance_rate"] == 80.0


def test_a_batch_with_no_register_is_null_not_zero(admin, course):
    """Null means "no data"; zero means "nobody came". Colouring the first as
    the second is the kind of chart that starts a meeting."""
    Batch.objects.create(course=course, name="Batch B", capacity=30)

    row = _api(admin).get(ATTENDANCE_URL).data["results"][0]
    assert row["attendance_rate"] is None


def test_the_attendance_table_is_paginated(admin, course):
    for i in range(25):
        Batch.objects.create(course=course, name=f"Batch {i}", capacity=10)

    resp = _api(admin).get(ATTENDANCE_URL)
    assert set(resp.data) >= {"count", "next", "previous", "results"}
    assert resp.data["count"] == 25
    assert len(resp.data["results"]) == 20


def test_attendance_can_be_scoped_to_one_course(admin, course, trainer):
    other = Course.objects.create(title="Data Science", trainer=trainer)
    Batch.objects.create(course=course, name="A", capacity=10)
    Batch.objects.create(course=other, name="B", capacity=10)

    resp = _api(admin).get(ATTENDANCE_URL, {"course": other.slug})
    assert resp.data["count"] == 1
    assert resp.data["results"][0]["course"] == "Data Science"
