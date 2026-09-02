"""The admin console's Payments page (PRD §3.13).

The load-bearing thing here is **scope**: ``OrderViewSet`` deliberately shows an
admin only their own orders, so the admin endpoints have to show everyone's or
the page reports zero revenue on a platform that has plenty. Most of these
tests are that assertion in different clothes, plus the refund arithmetic.
"""

from decimal import Decimal

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Role, User
from payments.models import Order, Payment, Refund, TrainerPayout

pytestmark = pytest.mark.django_db

PAYMENTS_URL = "/api/v1/admin/payments/"
SUMMARY_URL = "/api/v1/admin/payments/summary/"
REFUNDS_URL = "/api/v1/admin/refunds/"
PAYOUTS_URL = "/api/v1/admin/payouts/"


def _api(user=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


@pytest.fixture
def admin():
    return User.objects.create_user(
        email="pay-admin@example.com", password="StrongPass123!", role=Role.ADMIN
    )


@pytest.fixture
def buyer():
    return User.objects.create_user(
        email="buyer@example.com", password="StrongPass123!",
        role=Role.STUDENT, full_name="Riya Sharma",
    )


def _payment(user, amount, state=Payment.Status.SUCCESS, gateway="razorpay"):
    order = Order.objects.create(user=user, status=Order.Status.PAID, total=amount)
    return Payment.objects.create(
        order=order,
        gateway=gateway,
        gateway_payment_id=f"pay_{order.pk}",
        amount=Decimal(amount),
        status=state,
    )


# --------------------------------------------------------------------------- #
# Access and scope
# --------------------------------------------------------------------------- #


def test_only_admins_can_read_platform_payments(buyer):
    assert _api(buyer).get(PAYMENTS_URL).status_code == status.HTTP_403_FORBIDDEN
    assert _api(buyer).get(SUMMARY_URL).status_code == status.HTTP_403_FORBIDDEN
    assert _api(buyer).get(REFUNDS_URL).status_code == status.HTTP_403_FORBIDDEN


def test_an_admin_sees_everyone_elses_payments(admin, buyer):
    """The whole reason this module exists — ``OrderViewSet`` would show the
    admin their own (zero) orders and report an empty platform."""
    _payment(buyer, "1500.00")

    resp = _api(admin).get(PAYMENTS_URL)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["count"] == 1
    row = resp.data["results"][0]
    assert row["payer_email"] == buyer.email
    assert row["payer_name"] == "Riya Sharma"


# --------------------------------------------------------------------------- #
# Pagination and filters
# --------------------------------------------------------------------------- #


def test_the_transaction_table_is_paginated(admin, buyer):
    for i in range(25):
        _payment(buyer, "100.00")

    resp = _api(admin).get(PAYMENTS_URL)
    assert set(resp.data) >= {"count", "next", "previous", "results"}
    assert resp.data["count"] == 25
    assert len(resp.data["results"]) == 20
    assert resp.data["next"]


def test_filters_narrow_the_table(admin, buyer):
    _payment(buyer, "100.00", Payment.Status.SUCCESS)
    _payment(buyer, "200.00", Payment.Status.FAILED)
    _payment(buyer, "300.00", Payment.Status.CREATED)

    ok = _api(admin).get(PAYMENTS_URL, {"status": Payment.Status.SUCCESS})
    assert ok.data["count"] == 1

    by_search = _api(admin).get(PAYMENTS_URL, {"search": "buyer@"})
    assert by_search.data["count"] == 3

    none = _api(admin).get(PAYMENTS_URL, {"search": "nobody@nowhere"})
    assert none.data["count"] == 0


def test_a_nonsense_date_filter_shows_the_page_rather_than_erroring(admin, buyer):
    """A typo in a URL should not replace the report with a 400."""
    _payment(buyer, "100.00")
    resp = _api(admin).get(PAYMENTS_URL, {"from": "not-a-date"})
    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["count"] == 1


# --------------------------------------------------------------------------- #
# The four tiles
# --------------------------------------------------------------------------- #


def test_summary_separates_gross_pending_and_refunds(admin, buyer):
    captured = _payment(buyer, "1000.00", Payment.Status.SUCCESS)
    _payment(buyer, "250.00", Payment.Status.CREATED)   # pending
    _payment(buyer, "999.00", Payment.Status.FAILED)    # counted nowhere

    Refund.objects.create(
        payment=captured, amount=Decimal("300.00"),
        status=Refund.Status.PROCESSED,
    )
    # A requested refund is an intention, not a movement — it must not show up.
    Refund.objects.create(
        payment=captured, amount=Decimal("100.00"),
        status=Refund.Status.REQUESTED,
    )

    data = _api(admin).get(SUMMARY_URL).data
    assert Decimal(data["gross_volume"]) == Decimal("1000.00")
    assert Decimal(data["pending"]) == Decimal("250.00")
    assert Decimal(data["refunds"]) == Decimal("300.00")
    assert Decimal(data["net"]) == Decimal("700.00")
    assert data["currency"]


def test_summary_honours_the_same_filter_as_the_table(admin, buyer):
    """Tiles that ignore the filter beside a table that honours it is a lie."""
    _payment(buyer, "1000.00", gateway="razorpay")
    _payment(buyer, "500.00", gateway="stripe")

    everything = _api(admin).get(SUMMARY_URL).data
    assert Decimal(everything["gross_volume"]) == Decimal("1500.00")

    filtered = _api(admin).get(SUMMARY_URL, {"gateway": "stripe"}).data
    assert Decimal(filtered["gross_volume"]) == Decimal("500.00")


def test_an_empty_platform_reports_zeros_not_nulls(admin):
    data = _api(admin).get(SUMMARY_URL).data
    assert Decimal(data["gross_volume"]) == 0
    assert Decimal(data["net"]) == 0


# --------------------------------------------------------------------------- #
# Refunds
# --------------------------------------------------------------------------- #


def test_an_admin_can_refund_a_captured_payment(admin, buyer):
    payment = _payment(buyer, "1000.00")

    resp = _api(admin).post(
        REFUNDS_URL,
        {"payment": payment.pk, "amount": "400.00", "reason": "Partial"},
        format="json",
    )
    assert resp.status_code == status.HTTP_201_CREATED
    assert Decimal(resp.data["amount"]) == Decimal("400.00")
    assert resp.data["payer_email"] == buyer.email

    # The row exists whether or not the gateway could be reached — an unsent
    # refund is a debt someone can chase.
    assert Refund.objects.filter(payment=payment).count() == 1


def test_a_refund_cannot_exceed_what_is_left(admin, buyer):
    payment = _payment(buyer, "1000.00")
    _api(admin).post(
        REFUNDS_URL, {"payment": payment.pk, "amount": "800.00"}, format="json"
    )

    resp = _api(admin).post(
        REFUNDS_URL, {"payment": payment.pk, "amount": "500.00"}, format="json"
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "refundable" in str(resp.data).lower()


def test_omitting_the_amount_refunds_the_remainder(admin, buyer):
    payment = _payment(buyer, "1000.00")
    _api(admin).post(
        REFUNDS_URL, {"payment": payment.pk, "amount": "250.00"}, format="json"
    )

    resp = _api(admin).post(REFUNDS_URL, {"payment": payment.pk}, format="json")
    assert resp.status_code == status.HTTP_201_CREATED
    assert Decimal(resp.data["amount"]) == Decimal("750.00")


def test_a_payment_that_never_captured_cannot_be_refunded(admin, buyer):
    payment = _payment(buyer, "1000.00", Payment.Status.FAILED)
    resp = _api(admin).post(REFUNDS_URL, {"payment": payment.pk}, format="json")
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_the_refund_queue_is_paginated(admin, buyer):
    for _ in range(25):
        Refund.objects.create(payment=_payment(buyer, "10.00"), amount=Decimal("10"))

    resp = _api(admin).get(REFUNDS_URL)
    assert resp.data["count"] == 25
    assert len(resp.data["results"]) == 20


# --------------------------------------------------------------------------- #
# Payouts
# --------------------------------------------------------------------------- #


def test_the_payout_queue_is_readable_and_paginated(admin):
    trainer = User.objects.create_user(
        email="payee@example.com", password="StrongPass123!", role=Role.TRAINER
    )
    for _ in range(25):
        TrainerPayout.objects.create(trainer=trainer, net=Decimal("100"))

    resp = _api(admin).get(PAYOUTS_URL)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["count"] == 25
    assert len(resp.data["results"]) == 20
    assert resp.data["results"][0]["trainer_email"] == trainer.email


def test_the_payout_queue_is_empty_on_a_real_platform(admin, buyer):
    """Nothing computes payouts yet. Settling an order must not conjure a row —
    if this ever fails, payout generation was built and the docs are stale."""
    _payment(buyer, "5000.00")
    assert _api(admin).get(PAYOUTS_URL).data["count"] == 0
