"""What a platform plan actually unlocks (PRD §3.4 platform access pricing).

The behaviour that matters here is the *edges*: a plan with nothing ticked must
grant nothing, and access granted by a subscription must disappear when the
subscription does — while a course the student genuinely bought stays theirs.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Role, User
from courses.models import Category, Course, Enrollment
from payments import entitlements
from payments.models import Order, PricingPlan, Subscription

pytestmark = pytest.mark.django_db


@pytest.fixture
def trainer():
    return User.objects.create_user(
        email="t-ent@example.com", password="StrongPass123!", role=Role.TRAINER
    )


@pytest.fixture
def student():
    return User.objects.create_user(
        email="s-ent@example.com", password="StrongPass123!", role=Role.STUDENT
    )


@pytest.fixture
def paid_course(trainer):
    return Course.objects.create(
        title="React Pro", trainer=trainer, is_free=False,
        category=Category.objects.create(name="Web"),
        status=Course.Status.PUBLISHED,
    )


@pytest.fixture
def pro_plan():
    return PricingPlan.objects.create(
        name="Pro", slug="pro", price=499,
        includes_all_paid_courses=True, includes_live_sessions=True,
        includes_certificates=True, priority_support=True,
    )


@pytest.fixture
def basic_plan():
    """A plan with nothing ticked — must unlock nothing."""
    return PricingPlan.objects.create(name="Basic", slug="basic", price=99)


def subscribe(user, plan, *, days=30, status_=Subscription.Status.ACTIVE):
    now = timezone.now()
    return Subscription.objects.create(
        user=user, plan=plan, status=status_,
        current_period_start=now - timedelta(days=1),
        current_period_end=now + timedelta(days=days),
    )


def _api(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


# -- the entitlement resolver ----------------------------------------------- #


def test_no_subscription_grants_nothing(student):
    assert entitlements.entitlements_for(student) == {
        "all_paid_courses": False, "live_sessions": False,
        "certificates": False, "priority_support": False,
    }


def test_ticks_on_the_plan_become_the_entitlements(student, pro_plan):
    subscribe(student, pro_plan)
    assert entitlements.entitlements_for(student) == {
        "all_paid_courses": True, "live_sessions": True,
        "certificates": True, "priority_support": True,
    }


def test_untinked_plan_unlocks_nothing(student, basic_plan):
    subscribe(student, basic_plan)
    assert entitlements.can_access_paid_courses(student) is False


def test_expired_period_grants_nothing_even_while_marked_active(student, pro_plan):
    """Nothing auto-renews, so a stale 'active' row must not keep paying out."""
    subscribe(student, pro_plan, days=-1)
    assert entitlements.active_subscription(student) is None
    assert entitlements.can_access_paid_courses(student) is False


def test_cancelled_subscription_grants_nothing(student, pro_plan):
    subscribe(student, pro_plan, status_=Subscription.Status.CANCELLED)
    assert entitlements.can_access_paid_courses(student) is False


# -- course access ---------------------------------------------------------- #


def test_subscriber_can_open_a_paid_course_without_buying_it(
    student, paid_course, pro_plan
):
    api = _api(student)
    assert api.get(
        f"/api/v1/courses/{paid_course.slug}/curriculum/"
    ).data["has_access"] is False

    subscribe(student, pro_plan)
    assert api.get(
        f"/api/v1/courses/{paid_course.slug}/curriculum/"
    ).data["has_access"] is True


def test_subscriber_can_enrol_in_a_paid_course(student, paid_course, pro_plan):
    subscribe(student, pro_plan)
    api = _api(student)
    resp = api.post(f"/api/v1/courses/{paid_course.slug}/enroll/", {}, format="json")

    assert resp.status_code == status.HTTP_201_CREATED
    enrollment = Enrollment.objects.get(student=student, course=paid_course)
    assert enrollment.source == Enrollment.Source.SUBSCRIPTION


def test_without_the_tick_a_paid_course_still_needs_buying(
    student, paid_course, basic_plan
):
    subscribe(student, basic_plan)
    resp = _api(student).post(
        f"/api/v1/courses/{paid_course.slug}/enroll/", {}, format="json"
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_access_lapses_with_the_subscription(student, paid_course, pro_plan):
    """The whole point of tagging the enrollment ``subscription``."""
    subscription = subscribe(student, pro_plan)
    api = _api(student)
    api.post(f"/api/v1/courses/{paid_course.slug}/enroll/", {}, format="json")
    assert api.get(
        f"/api/v1/courses/{paid_course.slug}/curriculum/"
    ).data["has_access"] is True

    subscription.status = Subscription.Status.CANCELLED
    subscription.save(update_fields=["status"])

    # The enrollment row survives, but it no longer opens the content.
    assert Enrollment.objects.filter(student=student, course=paid_course).exists()
    assert api.get(
        f"/api/v1/courses/{paid_course.slug}/curriculum/"
    ).data["has_access"] is False


def test_a_bought_course_survives_the_subscription_ending(
    student, paid_course, pro_plan
):
    """Purchased access is owned outright — losing a plan must not revoke it."""
    subscription = subscribe(student, pro_plan)
    Enrollment.objects.create(
        student=student, course=paid_course,
        source=Enrollment.Source.PURCHASE, status=Enrollment.Status.ACTIVE,
    )
    subscription.status = Subscription.Status.EXPIRED
    subscription.save(update_fields=["status"])

    assert _api(student).get(
        f"/api/v1/courses/{paid_course.slug}/curriculum/"
    ).data["has_access"] is True


# -- billing page ----------------------------------------------------------- #


def test_billing_summary_for_a_free_user(student):
    resp = _api(student).get("/api/v1/billing/summary/")
    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["active_plan"] is None      # UI renders "Free"
    assert resp.data["total_spent"] == Decimal("0.00")
    assert resp.data["invoice_count"] == 0
    assert resp.data["entitlements"]["all_paid_courses"] is False


def test_billing_summary_reports_plan_and_spend(student, pro_plan):
    subscribe(student, pro_plan)
    Order.objects.create(user=student, status=Order.Status.PAID, total=Decimal("1770"))
    Order.objects.create(user=student, status=Order.Status.PAID, total=Decimal("499"))
    # A pending order is not "spent".
    Order.objects.create(user=student, status=Order.Status.PENDING, total=Decimal("999"))

    resp = _api(student).get("/api/v1/billing/summary/")
    assert resp.data["active_plan"]["name"] == "Pro"
    assert resp.data["total_spent"] == Decimal("2269.00")
    assert resp.data["entitlements"]["all_paid_courses"] is True


def test_plan_list_exposes_the_admin_ticks(student, pro_plan):
    resp = _api(student).get("/api/v1/pricing-plans/")
    plan = resp.data["results"][0]
    assert plan["entitlements"] == {
        "all_paid_courses": True, "live_sessions": True,
        "certificates": True, "priority_support": True,
    }


def test_cancelling_keeps_access_until_the_period_ends(student, pro_plan):
    subscription = subscribe(student, pro_plan)
    resp = _api(student).post(f"/api/v1/subscriptions/{subscription.id}/cancel/")

    assert resp.status_code == status.HTTP_200_OK
    subscription.refresh_from_db()
    assert subscription.cancel_at == subscription.current_period_end
