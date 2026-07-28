"""Razorpay checkout, verification and webhook tests (PRD §3.13).

No network: ``payments.gateway``'s two outbound calls (``create_order`` and
``fetch_payment``) are monkeypatched. Signatures are computed with the real HMAC
so the code under test is the production verification path, not a stub of it.
"""

import hashlib
import hmac
import json
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Role, User
from courses.models import Category, Course, Enrollment
from payments.models import CoursePrice, Invoice, Order, Payment

pytestmark = pytest.mark.django_db

KEY_ID = "rzp_test_TJ0Xg4q0cMu4vL"
KEY_SECRET = "test_secret_value"
WEBHOOK_SECRET = "test_webhook_secret"
RZP_ORDER = "order_TEST123"
RZP_PAYMENT = "pay_TEST123"


@pytest.fixture
def live_keys(settings):
    """Opt in to the Razorpay path (the autouse conftest fixture blanks keys)."""
    settings.RAZORPAY_KEY_ID = KEY_ID
    settings.RAZORPAY_KEY_SECRET = KEY_SECRET
    settings.RAZORPAY_WEBHOOK_SECRET = WEBHOOK_SECRET
    settings.RAZORPAY_CHECKOUT_NAME = "Jigyaasaa"
    settings.RAZORPAY_CHECKOUT_LOGO = ""
    settings.FRONTEND_URL = "https://lms.jigyaasaa.com/student"
    return settings


@pytest.fixture
def student():
    return User.objects.create_user(
        email="stu@example.com", password="StrongPass123!",
        role=Role.STUDENT, full_name="Asha R",
    )


@pytest.fixture
def paid_course():
    trainer = User.objects.create_user(
        email="t@example.com", password="StrongPass123!", role=Role.TRAINER
    )
    course = Course.objects.create(
        title="React Pro", trainer=trainer, is_free=False,
        category=Category.objects.create(name="Web"),
        status=Course.Status.PUBLISHED,
    )
    CoursePrice.objects.create(
        course=course, pricing_type=CoursePrice.PricingType.ONE_TIME, amount=1000
    )
    return course


def _api(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


def _make_order(api, course):
    """A 1000.00 course → 1180.00 total after 18% GST → 118000 paise."""
    return api.post(
        "/api/v1/orders/",
        {"items": [{"item_type": "course", "object_id": course.id}]},
        format="json",
    ).data


def _patch_create_order(monkeypatch, amount_paise=118000):
    mock = MagicMock(
        return_value={"id": RZP_ORDER, "amount": amount_paise,
                      "currency": "INR", "status": "created"}
    )
    monkeypatch.setattr("payments.gateway.create_order", mock)
    return mock


def _patch_fetch_payment(monkeypatch, amount_paise=118000, state="captured"):
    mock = MagicMock(return_value=_entity(amount_paise, status=state))
    monkeypatch.setattr("payments.gateway.fetch_payment", mock)
    return mock


def _entity(amount_paise=118000, status="captured", order_id=RZP_ORDER):
    return {"id": RZP_PAYMENT, "order_id": order_id, "amount": amount_paise,
            "currency": "INR", "status": status, "method": "upi"}


def _signature(rzp_order_id, rzp_payment_id, secret=KEY_SECRET):
    return hmac.new(
        secret.encode(), f"{rzp_order_id}|{rzp_payment_id}".encode(), hashlib.sha256
    ).hexdigest()


def _start_checkout(api, order_id, monkeypatch, amount_paise=118000):
    _patch_create_order(monkeypatch, amount_paise)
    return api.post(f"/api/v1/orders/{order_id}/checkout/", {}, format="json")


def _verify_body(order_id=RZP_ORDER, payment_id=RZP_PAYMENT, signature=None):
    return {
        "razorpay_order_id": order_id,
        "razorpay_payment_id": payment_id,
        "razorpay_signature": signature or _signature(order_id, payment_id),
    }


# --------------------------------------------------------------------------- #
# checkout/
# --------------------------------------------------------------------------- #


def test_checkout_returns_razorpay_params(live_keys, student, paid_course, monkeypatch):
    api = _api(student)
    order = _make_order(api, paid_course)

    resp = _start_checkout(api, order["id"], monkeypatch)

    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["key"] == KEY_ID
    assert resp.data["razorpay_order_id"] == RZP_ORDER
    assert resp.data["amount"] == 118000
    assert Decimal(resp.data["amount_display"]) == Decimal("1180.00")
    assert resp.data["currency"] == "INR"
    assert resp.data["is_test_mode"] is True
    assert resp.data["prefill"]["email"] == "stu@example.com"
    assert resp.data["prefill"]["name"] == "Asha R"
    # The API secret must never reach the browser.
    assert KEY_SECRET not in json.dumps(resp.data, default=str)


def test_checkout_reuses_gateway_order(live_keys, student, paid_course, monkeypatch):
    api = _api(student)
    order = _make_order(api, paid_course)
    create = _patch_create_order(monkeypatch)

    api.post(f"/api/v1/orders/{order['id']}/checkout/", {}, format="json")
    api.post(f"/api/v1/orders/{order['id']}/checkout/", {}, format="json")

    assert create.call_count == 1
    assert Payment.objects.filter(order_id=order["id"]).count() == 1


def test_checkout_503_without_keys(student, paid_course):
    api = _api(student)
    order = _make_order(api, paid_course)
    resp = api.post(f"/api/v1/orders/{order['id']}/checkout/", {}, format="json")
    assert resp.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_checkout_scoped_to_owner(live_keys, student, paid_course, monkeypatch):
    api = _api(student)
    order = _make_order(api, paid_course)
    _patch_create_order(monkeypatch)
    other = User.objects.create_user(
        email="other@example.com", password="StrongPass123!", role=Role.STUDENT
    )

    resp = _api(other).post(
        f"/api/v1/orders/{order['id']}/checkout/", {}, format="json"
    )

    assert resp.status_code == status.HTTP_404_NOT_FOUND


# --------------------------------------------------------------------------- #
# verify/
# --------------------------------------------------------------------------- #


def test_verify_settles_order_and_enrolls(live_keys, student, paid_course, monkeypatch):
    api = _api(student)
    order = _make_order(api, paid_course)
    _start_checkout(api, order["id"], monkeypatch)
    _patch_fetch_payment(monkeypatch)

    resp = api.post(
        f"/api/v1/orders/{order['id']}/verify/", _verify_body(), format="json"
    )

    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["status"] == "paid"
    enrollment = Enrollment.objects.get(student=student, course=paid_course)
    assert enrollment.source == Enrollment.Source.PURCHASE
    assert enrollment.order_id == order["id"]
    assert Invoice.objects.filter(user=student, status="paid").exists()
    payment = Payment.objects.get(order_id=order["id"])
    assert payment.status == Payment.Status.SUCCESS
    assert payment.gateway_payment_id == RZP_PAYMENT
    assert payment.method == "upi"


def test_verify_rejects_forged_signature(live_keys, student, paid_course, monkeypatch):
    api = _api(student)
    order = _make_order(api, paid_course)
    _start_checkout(api, order["id"], monkeypatch)
    fetch = _patch_fetch_payment(monkeypatch)

    resp = api.post(
        f"/api/v1/orders/{order['id']}/verify/",
        _verify_body(signature="deadbeef"),
        format="json",
    )

    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert fetch.call_count == 0  # bails before asking Razorpay anything
    assert Order.objects.get(pk=order["id"]).status == Order.Status.PENDING
    assert not Enrollment.objects.filter(student=student, course=paid_course).exists()


def test_verify_rejects_amount_mismatch(live_keys, student, paid_course, monkeypatch):
    """A genuinely-signed payment for less than the total must not fulfil."""
    api = _api(student)
    order = _make_order(api, paid_course)
    _start_checkout(api, order["id"], monkeypatch)
    _patch_fetch_payment(monkeypatch, amount_paise=100)  # ₹1 instead of ₹1180

    resp = api.post(
        f"/api/v1/orders/{order['id']}/verify/", _verify_body(), format="json"
    )

    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert Order.objects.get(pk=order["id"]).status == Order.Status.PENDING
    assert not Enrollment.objects.filter(student=student, course=paid_course).exists()


def test_verify_rejects_uncaptured_payment(live_keys, student, paid_course, monkeypatch):
    api = _api(student)
    order = _make_order(api, paid_course)
    _start_checkout(api, order["id"], monkeypatch)
    _patch_fetch_payment(monkeypatch, state="failed")

    resp = api.post(
        f"/api/v1/orders/{order['id']}/verify/", _verify_body(), format="json"
    )

    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert not Enrollment.objects.filter(student=student, course=paid_course).exists()


def test_verify_rejects_payment_from_another_order(
    live_keys, student, paid_course, monkeypatch
):
    """A validly-signed receipt for a different (cheaper) order can't pay this one."""
    api = _api(student)
    order = _make_order(api, paid_course)
    _start_checkout(api, order["id"], monkeypatch)
    _patch_fetch_payment(monkeypatch)

    resp = api.post(
        f"/api/v1/orders/{order['id']}/verify/",
        _verify_body(order_id="order_SOMEONE_ELSE", payment_id="pay_X"),
        format="json",
    )

    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert not Enrollment.objects.filter(student=student, course=paid_course).exists()


def test_verify_is_idempotent(live_keys, student, paid_course, monkeypatch):
    api = _api(student)
    order = _make_order(api, paid_course)
    _start_checkout(api, order["id"], monkeypatch)
    _patch_fetch_payment(monkeypatch)

    api.post(f"/api/v1/orders/{order['id']}/verify/", _verify_body(), format="json")
    second = api.post(
        f"/api/v1/orders/{order['id']}/verify/", _verify_body(), format="json"
    )

    assert second.status_code == status.HTTP_200_OK
    assert Enrollment.objects.filter(student=student, course=paid_course).count() == 1
    assert Invoice.objects.filter(user=student).count() == 1


# --------------------------------------------------------------------------- #
# pay/ (mock) gating
# --------------------------------------------------------------------------- #


def test_mock_pay_disabled_when_gateway_configured(live_keys, student, paid_course):
    api = _api(student)
    order = _make_order(api, paid_course)
    resp = api.post(f"/api/v1/orders/{order['id']}/pay/", {}, format="json")
    assert resp.status_code == status.HTTP_409_CONFLICT
    assert not Enrollment.objects.filter(student=student, course=paid_course).exists()


def test_mock_pay_still_works_without_keys(student, paid_course):
    api = _api(student)
    order = _make_order(api, paid_course)
    resp = api.post(f"/api/v1/orders/{order['id']}/pay/", {}, format="json")
    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["status"] == "paid"


# --------------------------------------------------------------------------- #
# webhook
# --------------------------------------------------------------------------- #

WEBHOOK_URL = "/api/v1/payments/webhook/razorpay/"


def _webhook_post(payload, secret=WEBHOOK_SECRET):
    body = json.dumps(payload).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return APIClient().post(
        WEBHOOK_URL, data=body, content_type="application/json",
        HTTP_X_RAZORPAY_SIGNATURE=signature,
    )


def _captured_event(amount_paise=118000):
    return {
        "event": "payment.captured",
        "payload": {"payment": {"entity": _entity(amount_paise)}},
    }


def test_webhook_fulfils_order(live_keys, student, paid_course, monkeypatch):
    api = _api(student)
    order = _make_order(api, paid_course)
    _start_checkout(api, order["id"], monkeypatch)

    resp = _webhook_post(_captured_event())

    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["status"] == "ok"
    assert Order.objects.get(pk=order["id"]).status == Order.Status.PAID
    assert Enrollment.objects.filter(student=student, course=paid_course).exists()
    assert Invoice.objects.filter(user=student, status="paid").exists()


def test_webhook_rejects_bad_signature(live_keys, student, paid_course, monkeypatch):
    api = _api(student)
    order = _make_order(api, paid_course)
    _start_checkout(api, order["id"], monkeypatch)

    resp = _webhook_post(_captured_event(), secret="wrong_secret")

    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert Order.objects.get(pk=order["id"]).status == Order.Status.PENDING
    assert not Enrollment.objects.filter(student=student, course=paid_course).exists()


def test_webhook_rejects_amount_mismatch(live_keys, student, paid_course, monkeypatch):
    """Permanent mismatch: ack so Razorpay stops retrying, but grant nothing."""
    api = _api(student)
    order = _make_order(api, paid_course)
    _start_checkout(api, order["id"], monkeypatch)

    resp = _webhook_post(_captured_event(amount_paise=100))

    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["status"] == "rejected"
    assert Order.objects.get(pk=order["id"]).status == Order.Status.PENDING
    assert not Enrollment.objects.filter(student=student, course=paid_course).exists()


def test_webhook_and_verify_do_not_double_fulfil(
    live_keys, student, paid_course, monkeypatch
):
    """Browser handler and webhook race: one enrollment, one invoice."""
    api = _api(student)
    order = _make_order(api, paid_course)
    _start_checkout(api, order["id"], monkeypatch)
    _patch_fetch_payment(monkeypatch)

    api.post(f"/api/v1/orders/{order['id']}/verify/", _verify_body(), format="json")
    _webhook_post(_captured_event())

    assert Enrollment.objects.filter(student=student, course=paid_course).count() == 1
    assert Invoice.objects.filter(user=student).count() == 1
    assert Payment.objects.filter(order_id=order["id"]).count() == 1


def test_webhook_unknown_order_is_acked(live_keys):
    """Razorpay must not be told to retry an event we can never fulfil."""
    resp = _webhook_post(_captured_event())
    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["status"] == "unknown_order"


def test_webhook_ignores_unhandled_events(live_keys):
    resp = _webhook_post({"event": "refund.created", "payload": {}})
    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["status"] == "ignored"


def test_webhook_records_failure(live_keys, student, paid_course, monkeypatch):
    api = _api(student)
    order = _make_order(api, paid_course)
    _start_checkout(api, order["id"], monkeypatch)

    resp = _webhook_post({
        "event": "payment.failed",
        "payload": {"payment": {"entity": _entity(status="failed")}},
    })

    assert resp.status_code == status.HTTP_200_OK
    assert Payment.objects.get(order_id=order["id"]).status == Payment.Status.FAILED
    # A failed attempt leaves the order payable so the student can retry.
    assert Order.objects.get(pk=order["id"]).status == Order.Status.PENDING


def test_webhook_needs_no_auth_header(live_keys, student, paid_course, monkeypatch):
    """The webhook must work without JWT — Razorpay has no credentials of ours."""
    api = _api(student)
    order = _make_order(api, paid_course)
    _start_checkout(api, order["id"], monkeypatch)

    resp = _webhook_post(_captured_event())

    assert resp.status_code != status.HTTP_401_UNAUTHORIZED
    assert resp.status_code == status.HTTP_200_OK
