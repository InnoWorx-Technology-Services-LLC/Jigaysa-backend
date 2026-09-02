"""Trainer earnings: recognition, reversal, payouts and the Earnings page.

This is money, so the tests lean on the properties that make the ledger
trustworthy rather than on the happy path: settling twice must not pay twice,
a refund must reverse rather than delete, the rate is snapshotted at the sale,
and GST is nobody's to split.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Role, TrainerProfile, User
from core.models import PlatformSetting
from courses.models import Course
from payments import earnings as earnings_service
from payments import services
from payments.models import (
    Order,
    OrderItem,
    Payment,
    TrainerEarning,
    TrainerPayout,
)

pytestmark = pytest.mark.django_db

SUMMARY_URL = "/api/v1/trainer/earnings/summary/"
TREND_URL = "/api/v1/trainer/earnings/trend/"
LEDGER_URL = "/api/v1/trainer/earnings/"
PAYOUTS_URL = "/api/v1/trainer/earnings/payouts/"
BANK_URL = "/api/v1/trainer/earnings/bank-account/"


def _api(user=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


@pytest.fixture
def trainer():
    user = User.objects.create_user(
        email="earn-t@example.com", password="StrongPass123!",
        role=Role.TRAINER, full_name="Dr. Kapoor",
    )
    TrainerProfile.objects.get_or_create(user=user)
    return user


@pytest.fixture
def rival():
    user = User.objects.create_user(
        email="earn-r@example.com", password="StrongPass123!", role=Role.TRAINER
    )
    TrainerProfile.objects.get_or_create(user=user)
    return user


@pytest.fixture
def student():
    return User.objects.create_user(
        email="earn-s@example.com", password="StrongPass123!", role=Role.STUDENT
    )


@pytest.fixture
def course(trainer):
    return Course.objects.create(title="React 19 Pro", trainer=trainer)


def _paid_order(student, course, amount="1000.00", discount="0.00", gst="0.00"):
    """An order that has settled, with the earnings hook having run."""
    amount, discount, gst = (Decimal(amount), Decimal(discount), Decimal(gst))
    order = Order.objects.create(
        user=student,
        status=Order.Status.PENDING,
        subtotal=amount,
        discount=discount,
        tax_gst=gst,
        total=amount - discount + gst,
    )
    OrderItem.objects.create(
        order=order,
        item_type=OrderItem.ItemType.COURSE,
        object_id=course.pk,
        title=course.title,
        amount=amount,
    )
    payment = Payment.objects.create(
        order=order, gateway="razorpay", amount=order.total,
        status=Payment.Status.SUCCESS,
    )
    services._settle(order, payment)
    return order, payment


# --------------------------------------------------------------------------- #
# Recognition
# --------------------------------------------------------------------------- #


def test_settling_an_order_records_the_trainers_share(trainer, student, course):
    _paid_order(student, course, amount="1000.00")

    earning = TrainerEarning.objects.get(trainer=trainer)
    assert earning.gross == Decimal("1000.00")
    assert earning.share_pct == Decimal("80.00")  # 100 − 20% platform default
    assert earning.net == Decimal("800.00")
    assert earning.platform_fee == Decimal("200.00")
    assert earning.course_id == course.pk


def test_gst_is_not_split_between_platform_and_trainer(trainer, student, course):
    """Tax belongs to the government; dividing it would be dividing money that
    is neither party's."""
    _paid_order(student, course, amount="1000.00", gst="180.00")

    earning = TrainerEarning.objects.get(trainer=trainer)
    assert earning.gross == Decimal("1000.00")  # not 1180
    assert earning.net == Decimal("800.00")


def test_a_coupon_discount_is_not_revenue_anybody_earned(
    trainer, student, course
):
    _paid_order(student, course, amount="1000.00", discount="200.00")

    earning = TrainerEarning.objects.get(trainer=trainer)
    assert earning.gross == Decimal("800.00")
    assert earning.net == Decimal("640.00")


def test_a_discount_is_split_across_lines_in_proportion(
    trainer, rival, student, course
):
    """A coupon applies to the order, not to one course. Charging it all to the
    first line would underpay one trainer and overpay the other."""
    theirs = Course.objects.create(title="Theirs", trainer=rival)
    order = Order.objects.create(
        user=student, status=Order.Status.PENDING,
        subtotal=Decimal("1000.00"), discount=Decimal("100.00"),
        total=Decimal("900.00"),
    )
    OrderItem.objects.create(
        order=order, item_type=OrderItem.ItemType.COURSE,
        object_id=course.pk, title="A", amount=Decimal("750.00"),
    )
    OrderItem.objects.create(
        order=order, item_type=OrderItem.ItemType.COURSE,
        object_id=theirs.pk, title="B", amount=Decimal("250.00"),
    )
    payment = Payment.objects.create(
        order=order, gateway="razorpay", amount=order.total,
        status=Payment.Status.SUCCESS,
    )
    services._settle(order, payment)

    assert TrainerEarning.objects.get(trainer=trainer).gross == Decimal("675.00")
    assert TrainerEarning.objects.get(trainer=rival).gross == Decimal("225.00")


def test_the_rate_is_snapshotted_at_the_sale(trainer, student, course):
    """A rate change must not restate what an earlier month paid."""
    profile = trainer.trainer_profile
    profile.revenue_share_pct = Decimal("70.00")
    profile.save(update_fields=["revenue_share_pct"])

    _paid_order(student, course, amount="1000.00")
    assert TrainerEarning.objects.get().net == Decimal("700.00")

    profile.revenue_share_pct = Decimal("50.00")
    profile.save(update_fields=["revenue_share_pct"])

    earning = TrainerEarning.objects.get()
    assert earning.share_pct == Decimal("70.00")
    assert earning.net == Decimal("700.00")


def test_a_trainer_rate_overrides_the_platform_default(trainer, student, course):
    profile = trainer.trainer_profile
    profile.revenue_share_pct = Decimal("90.00")
    profile.save(update_fields=["revenue_share_pct"])

    _paid_order(student, course)
    assert TrainerEarning.objects.get().share_pct == Decimal("90.00")


def test_settling_twice_does_not_pay_twice(trainer, student, course):
    """Settlement runs from both the verify call and the webhook, so recording
    again has to be impossible rather than unlikely."""
    order, payment = _paid_order(student, course)

    earnings_service.record_order_earnings(order)
    earnings_service.record_order_earnings(order)

    assert TrainerEarning.objects.filter(order=order).count() == 1


def test_a_plan_line_earns_nobody(student):
    """A platform subscription is not a sale of any one trainer's work."""
    order = Order.objects.create(
        user=student, status=Order.Status.PENDING,
        subtotal=Decimal("500.00"), total=Decimal("500.00"),
    )
    OrderItem.objects.create(
        order=order, item_type=OrderItem.ItemType.PLAN,
        object_id=1, title="Pro plan", amount=Decimal("500.00"),
    )
    payment = Payment.objects.create(
        order=order, gateway="razorpay", amount=order.total,
        status=Payment.Status.SUCCESS,
    )
    services._settle(order, payment)

    assert TrainerEarning.objects.count() == 0


# --------------------------------------------------------------------------- #
# Reversal
# --------------------------------------------------------------------------- #


def test_a_refund_reverses_rather_than_deletes(trainer, student, course):
    order, _ = _paid_order(student, course)

    earnings_service.reverse_order_earnings(order, note="Refunded.")

    earning = TrainerEarning.objects.get()
    # The row survives — a March statement still has to say what March was.
    assert earning.status == TrainerEarning.Status.REVERSED
    assert earning.net == Decimal("800.00")
    assert earning.reversed_at is not None


def test_reversed_money_leaves_the_totals(trainer, student, course):
    order, _ = _paid_order(student, course)
    assert Decimal(_api(trainer).get(SUMMARY_URL).data["lifetime"]) == Decimal("800.00")

    earnings_service.reverse_order_earnings(order)

    data = _api(trainer).get(SUMMARY_URL).data
    assert Decimal(data["lifetime"]) == 0
    assert Decimal(data["pending_payout"]) == 0


def test_reversed_lines_stay_visible_in_the_ledger(trainer, student, course):
    """Excluded from totals, visible in the ledger — the only combination that
    lets a trainer see why a number went down."""
    order, _ = _paid_order(student, course)
    earnings_service.reverse_order_earnings(order)

    rows = _api(trainer).get(LEDGER_URL).data["results"]
    assert len(rows) == 1
    assert rows[0]["status"] == "reversed"


# --------------------------------------------------------------------------- #
# Payouts
# --------------------------------------------------------------------------- #


def _age(order, days):
    TrainerEarning.objects.filter(order=order).update(
        earned_at=timezone.now() - timedelta(days=days)
    )


def test_a_fresh_earning_is_held_back_from_payout(trainer, student, course):
    """The hold is what keeps a refund from chasing money that already left."""
    _paid_order(student, course)
    assert earnings_service.generate_payouts() == []


def test_an_aged_earning_is_swept_into_a_payout(trainer, student, course):
    order, _ = _paid_order(student, course)
    _age(order, 10)

    payouts = earnings_service.generate_payouts()
    assert len(payouts) == 1
    payout = payouts[0]
    assert payout.trainer_id == trainer.pk
    assert payout.net == Decimal("800.00")
    assert payout.gross == Decimal("1000.00")
    assert payout.platform_fee == Decimal("200.00")
    assert payout.status == TrainerPayout.Status.PENDING

    # The line is linked, so it can never be swept twice.
    assert TrainerEarning.objects.get().payout_id == payout.pk


def test_running_the_sweep_twice_creates_nothing_new(trainer, student, course):
    order, _ = _paid_order(student, course)
    _age(order, 10)

    earnings_service.generate_payouts()
    assert earnings_service.generate_payouts() == []
    assert TrainerPayout.objects.count() == 1


def test_each_trainer_gets_their_own_payout(trainer, rival, student, course):
    theirs = Course.objects.create(title="Theirs", trainer=rival)
    order_a, _ = _paid_order(student, course, amount="1000.00")
    order_b, _ = _paid_order(student, theirs, amount="500.00")
    _age(order_a, 10)
    _age(order_b, 10)

    payouts = earnings_service.generate_payouts()
    assert len(payouts) == 2
    assert {p.trainer_id for p in payouts} == {trainer.pk, rival.pk}


def test_a_reversed_line_is_never_paid_out(trainer, student, course):
    order, _ = _paid_order(student, course)
    _age(order, 10)
    earnings_service.reverse_order_earnings(order)

    assert earnings_service.generate_payouts() == []


# --------------------------------------------------------------------------- #
# The page
# --------------------------------------------------------------------------- #


def test_summary_reports_the_tiles_and_the_split(trainer, student, course):
    _paid_order(student, course, amount="1000.00")

    data = _api(trainer).get(SUMMARY_URL).data
    assert Decimal(data["this_month"]) == Decimal("800.00")
    assert Decimal(data["lifetime"]) == Decimal("800.00")
    assert Decimal(data["pending_payout"]) == Decimal("800.00")
    assert Decimal(data["average_per_course"]) == Decimal("800.00")
    assert Decimal(data["share_pct"]) == Decimal("80.00")
    assert Decimal(data["platform_fee_pct"]) == Decimal("20.00")
    assert data["currency"]


def test_the_split_always_sums_to_a_hundred(trainer):
    PlatformSetting.objects.update_or_create(
        pk=1, defaults={"platform_commission_percent": Decimal("35.00")}
    )
    data = _api(trainer).get(SUMMARY_URL).data
    assert Decimal(data["share_pct"]) + Decimal(data["platform_fee_pct"]) == 100


def test_an_empty_platform_reports_zeros(trainer):
    data = _api(trainer).get(SUMMARY_URL).data
    assert Decimal(data["lifetime"]) == 0
    assert Decimal(data["average_per_course"]) == 0
    assert data["earning_courses"] == 0


def test_average_per_course_ignores_courses_that_never_sold(
    trainer, student, course
):
    """Dividing by every published course measures how much you publish, not
    how much you earn."""
    Course.objects.create(title="Never sold", trainer=trainer)
    _paid_order(student, course, amount="1000.00")

    data = _api(trainer).get(SUMMARY_URL).data
    assert data["earning_courses"] == 1
    assert Decimal(data["average_per_course"]) == Decimal("800.00")


def test_the_trend_is_dense(trainer, student, course):
    _paid_order(student, course, amount="1000.00")

    data = _api(trainer).get(TREND_URL).data
    months = [p["month"] for p in data["earnings"]]
    assert len(months) == 12
    assert months == sorted(months)
    assert Decimal(data["earnings"][-1]["value"]) == Decimal("800.00")
    assert all(Decimal(p["value"]) == 0 for p in data["earnings"][:-1])


def test_you_never_see_another_trainers_earnings(trainer, rival, student):
    theirs = Course.objects.create(title="Theirs", trainer=rival)
    _paid_order(student, theirs, amount="1000.00")

    assert Decimal(_api(trainer).get(SUMMARY_URL).data["lifetime"]) == 0
    assert _api(trainer).get(LEDGER_URL).data["count"] == 0
    assert _api(trainer).get(PAYOUTS_URL).data["count"] == 0


def test_students_are_refused(student):
    for url in (SUMMARY_URL, TREND_URL, LEDGER_URL, PAYOUTS_URL, BANK_URL):
        assert _api(student).get(url).status_code == status.HTTP_403_FORBIDDEN


def test_the_ledger_and_payouts_are_paginated(trainer, student, course):
    for _ in range(25):
        _paid_order(student, course, amount="100.00")

    ledger = _api(trainer).get(LEDGER_URL)
    assert set(ledger.data) >= {"count", "next", "previous", "results"}
    assert ledger.data["count"] == 25
    assert len(ledger.data["results"]) == 20

    payouts = _api(trainer).get(PAYOUTS_URL)
    assert set(payouts.data) >= {"count", "next", "previous", "results"}


# --------------------------------------------------------------------------- #
# Bank account
# --------------------------------------------------------------------------- #


def test_bank_account_starts_unset(trainer):
    data = _api(trainer).get(BANK_URL).data
    assert data["is_set"] is False
    assert data["account_last4"] == ""


def test_bank_account_records_only_the_last_four_digits(trainer):
    resp = _api(trainer).put(
        BANK_URL,
        {
            "bank_name": "HDFC",
            "account_last4": "8821",
            "account_type": "Savings",
            "account_holder": "Dr. Kapoor",
        },
        format="json",
    )
    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["is_set"] is True
    assert resp.data["account_last4"] == "8821"
    assert resp.data["bank_name"] == "HDFC"


def test_a_full_account_number_is_refused_not_truncated(trainer):
    """Silently keeping four digits of a number someone believed they had
    registered is worse than saying we do not take it."""
    resp = _api(trainer).put(
        BANK_URL,
        {"bank_name": "HDFC", "account_last4": "50100123456789"},
        format="json",
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_non_numeric_last4_is_refused(trainer):
    resp = _api(trainer).put(
        BANK_URL, {"bank_name": "HDFC", "account_last4": "abcd"}, format="json"
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
