"""Refund execution and its failure modes (PRD §3.13 refunds).

No network: ``gateway.refund_payment`` / ``fetch_refunds`` are monkeypatched to
raise or return what Razorpay would. The point of these tests is the *decision
table* — which failures write the money off, which keep it owed, and which are
successes wearing an error's clothes.
"""

import json
from decimal import Decimal

import pytest

from accounts.models import Role, User
from payments import gateway, services
from payments.models import Invoice, Order, Payment, Refund

pytestmark = pytest.mark.django_db

KEY_ID = "rzp_test_TJ0Xg4q0cMu4vL"
KEY_SECRET = "test_secret_value"
WEBHOOK_SECRET = "test_webhook_secret"


@pytest.fixture
def live_keys(settings):
    settings.RAZORPAY_KEY_ID = KEY_ID
    settings.RAZORPAY_KEY_SECRET = KEY_SECRET
    settings.RAZORPAY_WEBHOOK_SECRET = WEBHOOK_SECRET
    return settings


@pytest.fixture
def paid_order():
    user = User.objects.create_user(
        email="ref@example.com", password="StrongPass123!", role=Role.STUDENT
    )
    order = Order.objects.create(
        user=user, status=Order.Status.PAID, subtotal=1500,
        tax_gst=270, total=1770,
    )
    Payment.objects.create(
        order=order, gateway=Payment.Gateway.RAZORPAY,
        gateway_payment_id="pay_TEST123", amount=Decimal("1770.00"),
        status=Payment.Status.SUCCESS,
    )
    Invoice.objects.create(
        order=order, number="JIG-2026-9001", user=user, amount=1500, gst_amount=270
    )
    return order


# -- the decision table ----------------------------------------------------- #


def test_successful_refund_is_sent_and_awaits_the_webhook(
    live_keys, paid_order, monkeypatch
):
    monkeypatch.setattr(
        gateway, "refund_payment",
        lambda *a, **k: {"id": "rfnd_1", "status": "pending", "amount": 177000},
    )
    refund = services.request_refund(paid_order, reason="Trainer cancelled.")[0]

    assert refund.gateway_refund_id == "rfnd_1"
    assert refund.status == Refund.Status.REQUESTED  # pending until the webhook
    # Nothing is written off before the money actually moves.
    paid_order.refresh_from_db()
    assert paid_order.status == Order.Status.PAID


def test_immediately_processed_refund_settles_the_order(
    live_keys, paid_order, monkeypatch
):
    monkeypatch.setattr(
        gateway, "refund_payment",
        lambda *a, **k: {"id": "rfnd_2", "status": "processed", "amount": 177000},
    )
    refund = services.request_refund(paid_order, reason="Trainer cancelled.")[0]

    assert refund.status == Refund.Status.PROCESSED
    assert refund.processed_at is not None
    paid_order.refresh_from_db()
    assert paid_order.status == Order.Status.REFUNDED
    assert Invoice.objects.get(order=paid_order).status == Invoice.Status.REFUNDED


def test_insufficient_balance_keeps_the_debt_open(live_keys, paid_order, monkeypatch):
    """Money already settled to the bank: retryable, never written off."""
    def _raise(*a, **k):
        raise gateway.InsufficientBalance(
            "Your account does not have sufficient balance to carry out the refund"
        )

    monkeypatch.setattr(gateway, "refund_payment", _raise)
    refund = services.request_refund(paid_order, reason="Trainer cancelled.")[0]

    assert refund.status == Refund.Status.REQUESTED  # still owed
    assert refund.is_sent is False
    assert refund.raw["outcome"] == "insufficient_balance"
    paid_order.refresh_from_db()
    assert paid_order.status == Order.Status.PAID


def test_permanent_rejection_is_marked_failed(live_keys, paid_order, monkeypatch):
    def _raise(*a, **k):
        raise gateway.RefundNotPossible("The payment is not captured")

    monkeypatch.setattr(gateway, "refund_payment", _raise)
    refund = services.request_refund(paid_order, reason="Trainer cancelled.")[0]

    assert refund.status == Refund.Status.FAILED
    assert refund.raw["outcome"] == "permanent"


def test_already_fully_refunded_counts_as_success(live_keys, paid_order, monkeypatch):
    """Our row simply hadn't caught up — the student already has their money."""
    def _raise(*a, **k):
        raise gateway.RefundNotPossible("The payment has been fully refunded already")

    monkeypatch.setattr(gateway, "refund_payment", _raise)
    refund = services.request_refund(paid_order, reason="Trainer cancelled.")[0]

    assert refund.status == Refund.Status.PROCESSED
    paid_order.refresh_from_db()
    assert paid_order.status == Order.Status.REFUNDED


def test_network_failure_keeps_the_debt_open(live_keys, paid_order, monkeypatch):
    def _raise(*a, **k):
        raise gateway.GatewayError("Connection timed out")

    monkeypatch.setattr(gateway, "refund_payment", _raise)
    refund = services.request_refund(paid_order, reason="Trainer cancelled.")[0]

    assert refund.status == Refund.Status.REQUESTED
    assert refund.raw["outcome"] == "unknown"


def test_unconfigured_gateway_records_without_sending(paid_order):
    """The dev/test path: the obligation exists, nothing is called."""
    refund = services.request_refund(paid_order, reason="Trainer cancelled.")[0]
    assert refund.status == Refund.Status.REQUESTED
    assert refund.is_sent is False


def test_refunding_twice_does_not_create_a_second_obligation(
    live_keys, paid_order, monkeypatch
):
    monkeypatch.setattr(
        gateway, "refund_payment",
        lambda *a, **k: {"id": "rfnd_3", "status": "pending"},
    )
    services.request_refund(paid_order, reason="first")
    services.request_refund(paid_order, reason="second")
    assert Refund.objects.filter(payment__order=paid_order).count() == 1


# -- retrying --------------------------------------------------------------- #


def test_retry_adopts_a_refund_that_already_exists_at_the_gateway(
    live_keys, paid_order, monkeypatch
):
    """The timed-out call actually worked — adopt it, never send a second."""
    monkeypatch.setattr(gateway, "refund_payment", lambda *a, **k: pytest.fail(
        "must not re-send when the gateway already has a refund"
    ))
    monkeypatch.setattr(
        gateway, "fetch_refunds",
        lambda *a, **k: [{"id": "rfnd_existing", "status": "processed"}],
    )
    refund = Refund.objects.create(
        payment=Payment.objects.get(order=paid_order),
        amount=Decimal("1770.00"),
        reason="timed out",
    )
    services.retry_refund(refund)
    refund.refresh_from_db()

    assert refund.gateway_refund_id == "rfnd_existing"
    assert refund.status == Refund.Status.PROCESSED


def test_retry_sends_when_the_gateway_has_nothing(live_keys, paid_order, monkeypatch):
    monkeypatch.setattr(gateway, "fetch_refunds", lambda *a, **k: [])
    monkeypatch.setattr(
        gateway, "refund_payment",
        lambda *a, **k: {"id": "rfnd_retry", "status": "pending"},
    )
    refund = Refund.objects.create(
        payment=Payment.objects.get(order=paid_order),
        amount=Decimal("1770.00"), reason="balance was short",
    )
    services.retry_refund(refund)
    refund.refresh_from_db()
    assert refund.gateway_refund_id == "rfnd_retry"


# -- webhooks --------------------------------------------------------------- #


def _post_webhook(client, live_keys, event, entity_key="refund"):
    body = json.dumps(
        {"event": event, "payload": {entity_key: {"entity": entity_for(event)}}}
    ).encode()
    import hashlib
    import hmac as _hmac

    signature = _hmac.new(
        WEBHOOK_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    return client.post(
        "/api/v1/payments/webhook/razorpay/",
        data=body,
        content_type="application/json",
        HTTP_X_RAZORPAY_SIGNATURE=signature,
    )


def entity_for(event):
    return {
        "id": "rfnd_hook",
        "payment_id": "pay_TEST123",
        "amount": 177000,
        "status": "processed" if event == "refund.processed" else "failed",
    }


def test_refund_processed_webhook_settles_the_order(client, live_keys, paid_order):
    refund = Refund.objects.create(
        payment=Payment.objects.get(order=paid_order),
        gateway_refund_id="rfnd_hook", amount=Decimal("1770.00"),
    )
    resp = _post_webhook(client, live_keys, "refund.processed")
    assert resp.status_code == 200

    refund.refresh_from_db()
    assert refund.status == Refund.Status.PROCESSED
    paid_order.refresh_from_db()
    assert paid_order.status == Order.Status.REFUNDED


def test_refund_failed_webhook_marks_it_failed(client, live_keys, paid_order):
    refund = Refund.objects.create(
        payment=Payment.objects.get(order=paid_order),
        gateway_refund_id="rfnd_hook", amount=Decimal("1770.00"),
    )
    resp = _post_webhook(client, live_keys, "refund.failed")
    assert resp.status_code == 200

    refund.refresh_from_db()
    assert refund.status == Refund.Status.FAILED
    # The money never came back, so the order must not read as refunded.
    paid_order.refresh_from_db()
    assert paid_order.status == Order.Status.PAID


def test_dashboard_refund_is_adopted(client, live_keys, paid_order):
    """A refund issued by hand in the Razorpay dashboard still lands on our books."""
    assert not Refund.objects.filter(payment__order=paid_order).exists()
    resp = _post_webhook(client, live_keys, "refund.processed")
    assert resp.status_code == 200

    refund = Refund.objects.get(payment__order=paid_order)
    assert refund.status == Refund.Status.PROCESSED
    assert refund.amount == Decimal("1770.00")


# -- error classification --------------------------------------------------- #


@pytest.mark.parametrize(
    "message,expected",
    [
        ("Your account does not have sufficient balance", gateway.InsufficientBalance),
        ("The payment has been fully refunded already", gateway.RefundNotPossible),
        ("The payment is not captured", gateway.RefundNotPossible),
        ("Read timed out", gateway.GatewayError),
        ("some brand new error nobody predicted", gateway.GatewayError),
    ],
)
def test_error_classification(message, expected):
    """Unknown errors must fall through to retryable — never written off."""
    assert isinstance(gateway.classify_refund_error(message), expected)
