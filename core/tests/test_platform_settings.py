"""Platform settings: the admin config surface and what it actually changes.

Two things are worth testing here and they pull in opposite directions. A
setting is only real if some code path reads it — so most of this file changes a
value and then checks a *behaviour* somewhere else entirely. And a settings row
holds gateway credentials, so the other half is about what must never come back
out of the API.
"""

from decimal import Decimal

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Role, User
from core.models import PlatformSetting
from courses.models import Category, Course, Module, Lesson
from payments import gateway, services
from payments.models import Coupon, OrderItem

pytestmark = pytest.mark.django_db

SETTINGS_URL = "/api/v1/platform-settings/"
PUBLIC_URL = "/api/v1/platform-settings/public/"


@pytest.fixture
def admin():
    return User.objects.create_user(
        email="a-cfg@example.com", password="StrongPass123!", role=Role.ADMIN
    )


@pytest.fixture
def student():
    return User.objects.create_user(
        email="s-cfg@example.com", password="StrongPass123!", role=Role.STUDENT
    )


@pytest.fixture
def trainer():
    return User.objects.create_user(
        email="t-cfg@example.com", password="StrongPass123!", role=Role.TRAINER
    )


def _api(user=None):
    client = APIClient()
    if user is not None:
        client.force_authenticate(user)
    return client


def _configure(**kwargs):
    row = PlatformSetting.get_solo()
    for field, value in kwargs.items():
        setattr(row, field, value)
    row.save()
    return row


# -- the singleton ----------------------------------------------------------- #


def test_the_row_is_a_singleton():
    PlatformSetting.objects.all().delete()
    PlatformSetting(platform_name="One").save()
    PlatformSetting(platform_name="Two").save()

    assert PlatformSetting.objects.count() == 1
    assert PlatformSetting.get_solo().platform_name == "Two"


def test_get_solo_never_writes():
    """It runs inside the payment settlement transaction — it must only read."""
    PlatformSetting.objects.all().delete()

    row = PlatformSetting.get_solo()

    assert row.pk is None
    assert row.gst_percent == Decimal("18.00")  # field defaults, not an error
    assert PlatformSetting.objects.count() == 0


# -- access ------------------------------------------------------------------ #


def test_only_an_admin_reads_the_settings(student):
    assert _api().get(SETTINGS_URL).status_code == status.HTTP_401_UNAUTHORIZED
    assert _api(student).get(SETTINGS_URL).status_code == status.HTTP_403_FORBIDDEN


def test_only_an_admin_writes_the_settings(student):
    resp = _api(student).patch(SETTINGS_URL, {"gst_percent": "0"}, format="json")

    assert resp.status_code == status.HTTP_403_FORBIDDEN
    assert PlatformSetting.get_solo().gst_percent == Decimal("18.00")


def test_the_public_slice_is_readable_logged_out():
    _configure(platform_name="Jigyasa", allow_coupon_codes=False)

    data = _api().get(PUBLIC_URL).data

    assert data["platform_name"] == "Jigyasa"
    assert data["flags"]["allow_coupon_codes"] is False
    # Commercial terms and credentials are not the public's business.
    assert "gst_percent" not in data
    assert "platform_commission_percent" not in data
    assert not any("razorpay" in key for key in data)


# -- secrets ----------------------------------------------------------------- #


def test_secrets_never_come_back_out(admin):
    _configure(
        razorpay_key_id="rzp_test_public",
        razorpay_key_secret="super-secret-value",
        razorpay_webhook_secret="webhook-secret-value",
    )

    data = _api(admin).get(SETTINGS_URL).data

    assert "razorpay_key_secret" not in data
    assert "razorpay_webhook_secret" not in data
    assert "super-secret-value" not in str(data)
    assert "webhook-secret-value" not in str(data)
    # …but the UI can still say "configured ••••alue".
    assert data["razorpay_key_secret_set"] is True
    assert data["razorpay_webhook_secret_set"] is True
    assert data["razorpay_key_secret_hint"] == "••••alue"
    # The key id is public by design — it ships to the browser for Checkout.
    assert data["razorpay_key_id"] == "rzp_test_public"


def test_patching_another_field_leaves_the_secrets_alone(admin):
    _configure(razorpay_key_secret="keep-me")

    resp = _api(admin).patch(SETTINGS_URL, {"gst_percent": "5"}, format="json")

    assert resp.status_code == status.HTTP_200_OK
    row = PlatformSetting.get_solo()
    assert row.razorpay_key_secret == "keep-me"
    assert row.gst_percent == Decimal("5.00")


def test_a_secret_can_be_rotated_and_cleared(admin):
    _configure(razorpay_key_secret="old")

    _api(admin).patch(SETTINGS_URL, {"razorpay_key_secret": "new"}, format="json")
    assert PlatformSetting.get_solo().razorpay_key_secret == "new"

    _api(admin).patch(SETTINGS_URL, {"razorpay_key_secret": ""}, format="json")
    assert PlatformSetting.get_solo().razorpay_key_secret == ""


# -- validation -------------------------------------------------------------- #


@pytest.mark.parametrize("bad", ["-1", "101"])
def test_gst_outside_0_100_is_rejected(admin, bad):
    resp = _api(admin).patch(SETTINGS_URL, {"gst_percent": bad}, format="json")

    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert PlatformSetting.get_solo().gst_percent == Decimal("18.00")


def test_currency_must_be_an_iso_code(admin):
    resp = _api(admin).patch(
        SETTINGS_URL, {"default_currency": "rupees"}, format="json"
    )

    assert resp.status_code == status.HTTP_400_BAD_REQUEST


# -- what the settings actually change --------------------------------------- #


def test_the_gst_rate_reprices_the_next_cart(trainer):
    from payments.models import CoursePrice

    course = Course.objects.create(
        title="Priced", trainer=trainer, is_free=False,
        category=Category.objects.create(name="Cfg"),
        status=Course.Status.PUBLISHED,
    )
    CoursePrice.objects.create(course=course, amount=Decimal("100"))
    items = [{"item_type": OrderItem.ItemType.COURSE, "object_id": course.pk}]

    _configure(gst_percent=Decimal("18"))
    assert services.quote(items)["total"] == Decimal("118.00")

    _configure(gst_percent=Decimal("5"))
    assert services.quote(items)["total"] == Decimal("105.00")


def test_a_placed_order_keeps_the_rate_it_was_quoted_at(student, trainer):
    from payments.models import CoursePrice

    course = Course.objects.create(
        title="Snapshot", trainer=trainer, is_free=False,
        category=Category.objects.create(name="Cfg2"),
        status=Course.Status.PUBLISHED,
    )
    CoursePrice.objects.create(course=course, amount=Decimal("100"))

    _configure(gst_percent=Decimal("18"))
    order = services.create_order(
        student, [{"item_type": OrderItem.ItemType.COURSE, "object_id": course.pk}]
    )

    _configure(gst_percent=Decimal("28"))

    order.refresh_from_db()
    assert order.tax_gst == Decimal("18.00")
    assert order.total == Decimal("118.00")


def test_turning_coupons_off_rejects_them_everywhere(student):
    Coupon.objects.create(code="SAVE10", value=Decimal("10"))
    _configure(allow_coupon_codes=False)

    resp = _api(student).post(
        "/api/v1/coupons/validate/",
        {"code": "SAVE10", "items": []},
        format="json",
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST

    with pytest.raises(Exception, match="disabled"):
        services.validate_coupon("SAVE10", Decimal("100"), {"course"})


def test_the_currency_is_stamped_onto_new_orders(student, trainer):
    from payments.models import CoursePrice

    course = Course.objects.create(
        title="Currency", trainer=trainer, is_free=False,
        category=Category.objects.create(name="Cfg3"),
        status=Course.Status.PUBLISHED,
    )
    CoursePrice.objects.create(course=course, amount=Decimal("100"))
    _configure(default_currency="USD")

    order = services.create_order(
        student, [{"item_type": OrderItem.ItemType.COURSE, "object_id": course.pk}]
    )

    assert order.currency == "USD"


def test_gateway_credentials_prefer_the_settings_row(settings):
    settings.RAZORPAY_KEY_ID = "rzp_live_from_env"
    settings.RAZORPAY_KEY_SECRET = "env-secret"
    settings.RAZORPAY_WEBHOOK_SECRET = ""

    # Blank fields fall through to the environment…
    _configure(razorpay_key_id="", razorpay_key_secret="")
    assert gateway.credentials()[0] == "rzp_live_from_env"
    assert gateway.is_configured() is True

    # …and a typed-in key wins, with no restart.
    _configure(razorpay_key_id="rzp_test_from_db", razorpay_key_secret="db-secret")
    assert gateway.credentials() == ("rzp_test_from_db", "db-secret", "")
    assert gateway.is_test_mode() is True


def test_the_seeded_row_carries_no_credentials():
    """Secrets belong in one store. A fresh row defers to the environment."""
    row = PlatformSetting.get_solo()

    assert row.pk == 1  # the migration ran
    assert row.razorpay_key_id == ""
    assert row.razorpay_key_secret == ""
    assert row.razorpay_webhook_secret == ""


def test_blank_credentials_report_the_environment_as_the_source(admin, settings):
    settings.RAZORPAY_KEY_ID = "rzp_live_env"
    settings.RAZORPAY_KEY_SECRET = "env-secret"
    _configure(razorpay_key_id="", razorpay_key_secret="")

    data = _api(admin).get(SETTINGS_URL).data
    assert data["razorpay_key_source"] == "environment"
    assert data["gateway_configured"] is True

    _configure(razorpay_key_id="rzp_test_db", razorpay_key_secret="db-secret")
    assert _api(admin).get(SETTINGS_URL).data["razorpay_key_source"] == "settings"

    settings.RAZORPAY_KEY_ID = ""
    settings.RAZORPAY_KEY_SECRET = ""
    _configure(razorpay_key_id="", razorpay_key_secret="")
    data = _api(admin).get(SETTINGS_URL).data
    assert data["razorpay_key_source"] == "none"
    assert data["gateway_configured"] is False


def test_trainer_self_registration_can_be_switched_off():
    payload = {
        "email": "new-trainer@example.com",
        "password": "StrongPass123!",
        "role": Role.TRAINER,
    }

    _configure(trainer_self_onboarding=False)
    resp = _api().post("/api/v1/auth/register/", payload, format="json")
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert not User.objects.filter(email=payload["email"]).exists()

    # Students are never affected by the trainer switch.
    assert _api().post(
        "/api/v1/auth/register/",
        {**payload, "email": "new-student@example.com", "role": Role.STUDENT},
        format="json",
    ).status_code == status.HTTP_201_CREATED

    _configure(trainer_self_onboarding=True)
    assert _api().post(
        "/api/v1/auth/register/", payload, format="json"
    ).status_code == status.HTTP_201_CREATED


def _submittable_course(trainer, title="Reviewable"):
    course = Course.objects.create(
        title=title, trainer=trainer,
        category=Category.objects.create(name=f"Cat-{title}"),
        status=Course.Status.DRAFT,
    )
    module = Module.objects.create(course=course, title="M1")
    Lesson.objects.create(module=module, title="L1")
    return course


def test_approval_off_publishes_a_submission_immediately(trainer):
    _configure(course_approval_required=False)
    course = _submittable_course(trainer, "AutoPublish")

    _api(trainer).post(f"/api/v1/courses/{course.slug}/publish/")

    course.refresh_from_db()
    assert course.status == Course.Status.PUBLISHED
    assert course.published_at is not None


def test_approval_on_still_queues_for_review(trainer):
    _configure(course_approval_required=True)
    course = _submittable_course(trainer, "NeedsReview")

    _api(trainer).post(f"/api/v1/courses/{course.slug}/publish/")

    course.refresh_from_db()
    assert course.status == Course.Status.PENDING_REVIEW


def test_skipping_review_does_not_skip_the_content_checks(trainer):
    """"No approval needed" is about who signs off, not publishing an empty shell."""
    _configure(course_approval_required=False)
    empty = Course.objects.create(
        title="Empty", trainer=trainer,
        category=Category.objects.create(name="CatEmpty"),
        status=Course.Status.DRAFT,
    )

    resp = _api(trainer).post(f"/api/v1/courses/{empty.slug}/publish/")

    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    empty.refresh_from_db()
    assert empty.status == Course.Status.DRAFT


def test_unenforced_flags_are_stored_and_advertised_as_such(admin):
    resp = _api(admin).patch(
        SETTINGS_URL, {"ai_suggestions": True}, format="json"
    )

    assert resp.data["flags"]["ai_suggestions"] is True
    assert "ai_suggestions" not in resp.data["enforced_flags"]
    assert set(resp.data["enforced_flags"]) == {
        "course_approval_required", "trainer_self_onboarding", "allow_coupon_codes",
    }
