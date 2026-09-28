"""Fixes raised by the frontend team: plan delete, payouts, bank details.

One test per reported defect, each asserting the behaviour they asked for rather
than the implementation that produces it.
"""

from decimal import Decimal

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Role, TrainerProfile, User
from payments.models import (
    Order,
    OrderItem,
    Payment,
    PricingPlan,
    Subscription,
    TrainerEarning,
    TrainerPayout,
)

pytestmark = pytest.mark.django_db

PLANS = "/api/v1/pricing-plans/"
PAYOUTS = "/api/v1/admin/payouts/"
BANK = "/api/v1/trainer/earnings/bank-account/"
EARNINGS = "/api/v1/trainer/earnings/summary/"


def _api(user=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


@pytest.fixture
def admin():
    return User.objects.create_user(
        email="fx-admin@example.com", password="StrongPass123!", role=Role.ADMIN
    )


@pytest.fixture
def trainer():
    user = User.objects.create_user(
        email="fx-trainer@example.com", password="StrongPass123!",
        role=Role.TRAINER,
    )
    TrainerProfile.objects.get_or_create(user=user)
    return user


@pytest.fixture
def plan():
    return PricingPlan.objects.create(name="Pro", price=Decimal("999.00"))


# --------------------------------------------------------------------------- #
# #3 — plan delete returns 409, not a 500 page
# --------------------------------------------------------------------------- #


def test_deleting_a_plan_with_subscribers_is_a_409(admin, plan):
    student = User.objects.create_user(
        email="sub@example.com", password="StrongPass123!"
    )
    Subscription.objects.create(
        user=student, plan=plan, status=Subscription.Status.ACTIVE
    )

    resp = _api(admin).delete(f"{PLANS}{plan.pk}/")
    assert resp.status_code == status.HTTP_409_CONFLICT
    # JSON the frontend can render, not a Django HTML error page.
    assert resp.data["subscriptions"] == 1
    assert resp.data["active_subscriptions"] == 1
    assert "subscription" in resp.data["detail"].lower()
    assert PricingPlan.objects.filter(pk=plan.pk).exists()


def test_orders_do_not_block_a_plan_delete(admin, plan):
    """Orders reference a plan by a loose integer, not a foreign key — they
    cannot protect it, whatever the delete failure looked like."""
    buyer = User.objects.create_user(
        email="buyer2@example.com", password="StrongPass123!"
    )
    order = Order.objects.create(user=buyer, status=Order.Status.PAID, total=999)
    OrderItem.objects.create(
        order=order, item_type=OrderItem.ItemType.PLAN,
        object_id=plan.pk, title="Pro", amount=Decimal("999.00"),
    )

    resp = _api(admin).delete(f"{PLANS}{plan.pk}/")
    assert resp.status_code == status.HTTP_204_NO_CONTENT
    assert not PricingPlan.objects.filter(pk=plan.pk).exists()


def test_an_unused_plan_still_deletes_cleanly(admin, plan):
    assert _api(admin).delete(
        f"{PLANS}{plan.pk}/"
    ).status_code == status.HTTP_204_NO_CONTENT


# --------------------------------------------------------------------------- #
# #5 / #9 — payouts: mark paid, and delete an orphan
# --------------------------------------------------------------------------- #


def test_an_admin_can_mark_a_payout_paid(admin, trainer):
    payout = TrainerPayout.objects.create(trainer=trainer, net=Decimal("100"))

    resp = _api(admin).post(f"{PAYOUTS}{payout.pk}/mark-paid/")
    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["status"] == TrainerPayout.Status.PAID
    assert resp.data["paid_at"]


def test_marking_paid_twice_does_not_move_the_date(admin, trainer):
    payout = TrainerPayout.objects.create(trainer=trainer, net=Decimal("100"))
    first = _api(admin).post(f"{PAYOUTS}{payout.pk}/mark-paid/").data["paid_at"]
    second = _api(admin).post(f"{PAYOUTS}{payout.pk}/mark-paid/").data["paid_at"]
    assert first == second


def test_a_payout_marked_paid_by_mistake_can_be_undone(admin, trainer):
    payout = TrainerPayout.objects.create(trainer=trainer, net=Decimal("100"))
    _api(admin).post(f"{PAYOUTS}{payout.pk}/mark-paid/")

    resp = _api(admin).post(f"{PAYOUTS}{payout.pk}/mark-unpaid/")
    assert resp.data["status"] == TrainerPayout.Status.PENDING
    assert resp.data["paid_at"] is None


def test_an_orphan_payout_can_be_deleted(admin, trainer):
    """The seeded row the frontend asked about: no earning lines behind it."""
    payout = TrainerPayout.objects.create(
        trainer=trainer, gross=Decimal("50000"),
        platform_fee=Decimal("15000"), net=Decimal("35000"),
        status=TrainerPayout.Status.PAID,
    )

    resp = _api(admin).delete(f"{PAYOUTS}{payout.pk}/")
    assert resp.status_code == status.HTTP_204_NO_CONTENT
    assert not TrainerPayout.objects.filter(pk=payout.pk).exists()


def test_a_payout_with_earnings_behind_it_is_not_deletable(admin, trainer):
    """Deleting it would return already-paid money to the pending balance."""
    from django.utils import timezone

    payout = TrainerPayout.objects.create(trainer=trainer, net=Decimal("800"))
    buyer = User.objects.create_user(
        email="buyer3@example.com", password="StrongPass123!"
    )
    order = Order.objects.create(user=buyer, status=Order.Status.PAID, total=1000)
    item = OrderItem.objects.create(
        order=order, item_type=OrderItem.ItemType.COURSE,
        object_id=1, title="A course", amount=Decimal("1000.00"),
    )
    TrainerEarning.objects.create(
        trainer=trainer, order=order, order_item=item,
        gross=Decimal("1000"), share_pct=Decimal("80"),
        platform_fee=Decimal("200"), net=Decimal("800"),
        earned_at=timezone.now(), payout=payout,
    )

    resp = _api(admin).delete(f"{PAYOUTS}{payout.pk}/")
    assert resp.status_code == status.HTTP_409_CONFLICT
    assert resp.data["earning_lines"] == 1
    assert TrainerPayout.objects.filter(pk=payout.pk).exists()


def test_only_admins_can_mark_a_payout_paid(trainer):
    payout = TrainerPayout.objects.create(trainer=trainer, net=Decimal("100"))
    resp = _api(trainer).post(f"{PAYOUTS}{payout.pk}/mark-paid/")
    assert resp.status_code == status.HTTP_403_FORBIDDEN


# --------------------------------------------------------------------------- #
# #6 — average_per_course is null when nothing has earned
# --------------------------------------------------------------------------- #


def test_average_per_course_is_null_not_zero(trainer):
    data = _api(trainer).get(EARNINGS).data
    assert data["earning_courses"] == 0
    # Null, like average_score already is — not "0.00".
    assert data["average_per_course"] is None


# --------------------------------------------------------------------------- #
# #8 — bank details can be cleared
# --------------------------------------------------------------------------- #


def test_bank_details_can_be_cleared(trainer):
    _api(trainer).put(
        BANK,
        {
            "bank_name": "HDFC", "account_number": "50100123458821",
            "ifsc": "HDFC0001234",
            "account_type": "Savings", "account_holder": "Dr. Kapoor",
        },
        format="json",
    )
    assert _api(trainer).get(BANK).data["is_set"] is True

    resp = _api(trainer).delete(BANK)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["is_set"] is False
    assert resp.data["bank_name"] == ""
    assert resp.data["account_last4"] == ""

    # And it stays cleared.
    assert _api(trainer).get(BANK).data["is_set"] is False


def test_clearing_bank_details_when_none_are_set_is_harmless(trainer):
    resp = _api(trainer).delete(BANK)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["is_set"] is False
