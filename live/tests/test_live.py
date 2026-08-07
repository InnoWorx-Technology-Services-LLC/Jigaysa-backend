from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Role, TrainerProfile, User
from courses.models import Batch, Category, Course, Enrollment
from live.models import (
    IndividualBooking,
    LiveSession,
    SessionRegistration,
    TrainerAvailability,
)
from payments.models import Order, Refund

pytestmark = pytest.mark.django_db


@pytest.fixture
def trainer():
    return User.objects.create_user(
        email="trainer@example.com", password="StrongPass123!", role=Role.TRAINER
    )


@pytest.fixture
def student():
    return User.objects.create_user(
        email="stu@example.com", password="StrongPass123!", role=Role.STUDENT
    )


def set_profile(trainer, **fields):
    """Set teaching-profile fields on a trainer, returning the trainer.

    ``accounts.signals.ensure_trainer_profile`` already created the row when the
    user was saved, so a plain ``create()`` here would collide with the
    one-profile-per-trainer constraint. Tests describe the profile they want and
    let this reconcile it.
    """
    TrainerProfile.objects.update_or_create(user=trainer, defaults=fields)
    return trainer


@pytest.fixture
def session(trainer):
    return LiveSession.objects.create(
        trainer=trainer, title="Pandas workshop", registration_limit=1
    )


def test_student_cannot_schedule_session(student):
    api = APIClient()
    api.force_authenticate(student)
    resp = api.post("/api/v1/live-sessions/", {"title": "x"}, format="json")
    assert resp.status_code == status.HTTP_403_FORBIDDEN


def test_register_join_and_raise_doubt(session, student):
    api = APIClient()
    api.force_authenticate(student)

    reg = api.post(f"/api/v1/live-sessions/{session.id}/register/")
    assert reg.status_code == status.HTTP_201_CREATED
    assert reg.data["status"] == SessionRegistration.Status.REGISTERED

    join = api.post(f"/api/v1/live-sessions/{session.id}/join/")
    assert join.status_code == status.HTTP_200_OK

    doubt = api.post(
        f"/api/v1/live-sessions/{session.id}/raise-doubt/",
        {"text": "reshape with melt?"},
        format="json",
    )
    assert doubt.status_code == status.HTTP_201_CREATED


def test_registration_waitlists_past_limit(session, student):
    other = User.objects.create_user(
        email="o@example.com", password="StrongPass123!", role=Role.STUDENT
    )
    # First registrant takes the single slot.
    SessionRegistration.objects.create(session=session, student=other)
    api = APIClient()
    api.force_authenticate(student)
    reg = api.post(f"/api/v1/live-sessions/{session.id}/register/")
    assert reg.data["status"] == SessionRegistration.Status.WAITLISTED


def test_join_requires_registration(session, student):
    api = APIClient()
    api.force_authenticate(student)
    resp = api.post(f"/api/v1/live-sessions/{session.id}/join/")
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


# -- visibility: course-bound sessions are for enrolled students only -------- #


@pytest.fixture
def course(trainer):
    return Course.objects.create(
        title="Pandas", trainer=trainer, is_free=True,
        category=Category.objects.create(name="Data"),
        status=Course.Status.PUBLISHED,
    )


def _titles(api):
    return {s["title"] for s in api.get("/api/v1/live-sessions/").data["results"]}


def test_course_session_hidden_from_unenrolled_student(course, trainer, student):
    LiveSession.objects.create(trainer=trainer, course=course, title="Course only")
    LiveSession.objects.create(trainer=trainer, title="Open workshop")
    api = APIClient()
    api.force_authenticate(student)
    assert _titles(api) == {"Open workshop"}


def test_course_session_visible_once_enrolled(course, trainer, student):
    session = LiveSession.objects.create(
        trainer=trainer, course=course, title="Course only"
    )
    Enrollment.objects.create(
        student=student, course=course,
        status=Enrollment.Status.ACTIVE, source=Enrollment.Source.FREE,
    )
    api = APIClient()
    api.force_authenticate(student)
    assert _titles(api) == {"Course only"}
    # And the whole action surface opens up with it.
    assert (
        api.post(f"/api/v1/live-sessions/{session.id}/register/").status_code
        == status.HTTP_201_CREATED
    )


def test_unenrolled_student_cannot_register_or_join(course, trainer, student):
    session = LiveSession.objects.create(
        trainer=trainer, course=course, title="Course only"
    )
    api = APIClient()
    api.force_authenticate(student)
    # 404, not 403 — the session's existence isn't leaked.
    for path in ("register", "join", "raise-doubt"):
        resp = api.post(f"/api/v1/live-sessions/{session.id}/{path}/")
        assert resp.status_code == status.HTTP_404_NOT_FOUND


def test_refunded_enrollment_loses_access(course, trainer, student):
    LiveSession.objects.create(trainer=trainer, course=course, title="Course only")
    Enrollment.objects.create(
        student=student, course=course,
        status=Enrollment.Status.REFUNDED, source=Enrollment.Source.PURCHASE,
    )
    api = APIClient()
    api.force_authenticate(student)
    assert _titles(api) == set()


def test_batch_session_limited_to_that_batch(course, trainer, student):
    mine = Batch.objects.create(course=course, name="Morning")
    theirs = Batch.objects.create(course=course, name="Evening")
    LiveSession.objects.create(trainer=trainer, course=course, batch=mine, title="Mine")
    LiveSession.objects.create(
        trainer=trainer, course=course, batch=theirs, title="Theirs"
    )
    Enrollment.objects.create(
        student=student, course=course, batch=mine,
        status=Enrollment.Status.ACTIVE, source=Enrollment.Source.FREE,
    )
    api = APIClient()
    api.force_authenticate(student)
    assert _titles(api) == {"Mine"}


def test_trainer_still_sees_every_session(course, trainer):
    LiveSession.objects.create(trainer=trainer, course=course, title="Course only")
    LiveSession.objects.create(trainer=trainer, title="Open workshop")
    api = APIClient()
    api.force_authenticate(trainer)
    assert _titles(api) == {"Course only", "Open workshop"}


# -- 1:1 booking (PRD §3.6) ------------------------------------------------- #


@pytest.fixture
def slot(trainer):
    start = timezone.now() + timedelta(days=2)
    return TrainerAvailability.objects.create(
        trainer=trainer, start=start, end=start + timedelta(hours=1)
    )


def _book(api, trainer, slot, topic="React hooks deep dive"):
    return api.post(
        "/api/v1/individual-bookings/",
        {
            "trainer": trainer.id,
            "start": slot.start.isoformat(),
            "duration_minutes": 60,
            "topic": topic,
            "notes": "Tried useMemo already.",
        },
        format="json",
    )


def test_mentor_list_returns_approved_trainers_with_rate(trainer, student):
    set_profile(trainer, is_approved=True, hourly_rate=1500, expertise="React")
    api = APIClient()
    api.force_authenticate(student)
    resp = api.get("/api/v1/mentors/")
    assert resp.status_code == status.HTTP_200_OK
    mentor = resp.data["results"][0]
    assert mentor["id"] == trainer.id
    assert str(mentor["hourly_rate"]) == "1500.00"
    assert mentor["expertise"] == "React"


def test_unapproved_trainer_is_not_bookable(trainer, student):
    set_profile(trainer, is_approved=False)
    api = APIClient()
    api.force_authenticate(student)
    assert resp_ids(api.get("/api/v1/mentors/")) == []


def resp_ids(resp):
    return [m["id"] for m in resp.data["results"]]


def test_booking_stores_topic_and_notifies_trainer(trainer, student, slot):
    api = APIClient()
    api.force_authenticate(student)
    resp = _book(api, trainer, slot)

    assert resp.status_code == status.HTTP_201_CREATED
    assert resp.data["topic"] == "React hooks deep dive"
    assert resp.data["status"] == IndividualBooking.Status.PENDING
    slot.refresh_from_db()
    assert slot.is_booked is True
    assert trainer.notifications.filter(title="New 1:1 booking request").exists()


def test_booking_requires_topic(trainer, student, slot):
    api = APIClient()
    api.force_authenticate(student)
    resp = api.post(
        "/api/v1/individual-bookings/",
        {"trainer": trainer.id, "start": slot.start.isoformat()},
        format="json",
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "topic" in resp.data


def test_student_cancel_releases_the_slot(trainer, student, slot):
    api = APIClient()
    api.force_authenticate(student)
    booking_id = _book(api, trainer, slot).data["id"]

    resp = api.post(f"/api/v1/individual-bookings/{booking_id}/cancel/")
    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["status"] == IndividualBooking.Status.CANCELLED
    slot.refresh_from_db()
    assert slot.is_booked is False
    # The freed slot is bookable again.
    assert _book(api, trainer, slot).status_code == status.HTTP_201_CREATED


def test_trainer_confirms_and_completes(trainer, student, slot):
    api = APIClient()
    api.force_authenticate(student)
    booking_id = _book(api, trainer, slot).data["id"]

    api.force_authenticate(trainer)
    confirm = api.post(
        f"/api/v1/individual-bookings/{booking_id}/confirm/",
        {"meeting_url": "https://meet.example.com/abc"},
        format="json",
    )
    assert confirm.data["status"] == IndividualBooking.Status.CONFIRMED
    assert confirm.data["meeting_url"] == "https://meet.example.com/abc"
    assert student.notifications.filter(title="1:1 session confirmed 🎉").exists()

    done = api.post(f"/api/v1/individual-bookings/{booking_id}/complete/")
    assert done.data["status"] == IndividualBooking.Status.COMPLETED


def test_decline_frees_slot_and_blocks_double_transition(trainer, student, slot):
    api = APIClient()
    api.force_authenticate(student)
    booking_id = _book(api, trainer, slot).data["id"]

    api.force_authenticate(trainer)
    assert (
        api.post(f"/api/v1/individual-bookings/{booking_id}/decline/").data["status"]
        == IndividualBooking.Status.CANCELLED
    )
    slot.refresh_from_db()
    assert slot.is_booked is False
    # A second decline is rejected rather than silently re-freeing the slot.
    again = api.post(f"/api/v1/individual-bookings/{booking_id}/decline/")
    assert again.status_code == status.HTTP_400_BAD_REQUEST


def test_confirming_a_cancelled_booking_writes_nothing(trainer, student, slot):
    api = APIClient()
    api.force_authenticate(student)
    booking_id = _book(api, trainer, slot).data["id"]
    api.post(f"/api/v1/individual-bookings/{booking_id}/cancel/")

    api.force_authenticate(trainer)
    resp = api.post(
        f"/api/v1/individual-bookings/{booking_id}/confirm/",
        {"meeting_url": "https://meet.example.com/late"},
        format="json",
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    booking = IndividualBooking.objects.get(pk=booking_id)
    assert booking.status == IndividualBooking.Status.CANCELLED
    assert booking.meeting_url == ""


# -- pay-per-hour (PRD §3.6 "payment per hour") ----------------------------- #


@pytest.fixture
def paid_trainer(trainer):
    return set_profile(trainer, is_approved=True, hourly_rate=Decimal("1500"))


def _confirm(trainer, booking_id):
    api = APIClient()
    api.force_authenticate(trainer)
    return api.post(f"/api/v1/individual-bookings/{booking_id}/confirm/")


def test_confirm_prices_the_session_and_awaits_payment(paid_trainer, student, slot):
    api = APIClient()
    api.force_authenticate(student)
    booking_id = _book(api, paid_trainer, slot).data["id"]

    resp = _confirm(paid_trainer, booking_id)
    assert resp.data["status"] == IndividualBooking.Status.AWAITING_PAYMENT
    assert resp.data["order"] is not None
    # ₹1500/hr × 1h + 18% GST.
    assert Decimal(resp.data["amount_due"]) == Decimal("1770.00")
    assert resp.data["is_paid"] is False
    assert resp.data["payment_due_at"] is not None


def test_free_mentor_confirms_without_payment(trainer, student, slot):
    set_profile(trainer, is_approved=True, hourly_rate=0)
    api = APIClient()
    api.force_authenticate(student)
    booking_id = _book(api, trainer, slot).data["id"]

    resp = _confirm(trainer, booking_id)
    assert resp.data["status"] == IndividualBooking.Status.CONFIRMED
    assert resp.data["order"] is None
    assert resp.data["amount_due"] is None


def test_paying_the_order_confirms_the_booking(paid_trainer, student, slot):
    api = APIClient()
    api.force_authenticate(student)
    booking_id = _book(api, paid_trainer, slot).data["id"]
    order_id = _confirm(paid_trainer, booking_id).data["order"]

    api.force_authenticate(student)
    assert api.post(f"/api/v1/orders/{order_id}/pay/", {}, format="json").status_code == (
        status.HTTP_200_OK
    )

    booking = api.get(f"/api/v1/individual-bookings/{booking_id}/").data
    assert booking["status"] == IndividualBooking.Status.CONFIRMED
    assert booking["is_paid"] is True
    assert booking["amount_due"] is None


def test_trainer_cancelling_a_paid_booking_records_a_refund(paid_trainer, student, slot):
    api = APIClient()
    api.force_authenticate(student)
    booking_id = _book(api, paid_trainer, slot).data["id"]
    order_id = _confirm(paid_trainer, booking_id).data["order"]
    api.post(f"/api/v1/orders/{order_id}/pay/", {}, format="json")

    api.force_authenticate(paid_trainer)
    resp = api.post(f"/api/v1/individual-bookings/{booking_id}/cancel/")
    assert resp.data["status"] == IndividualBooking.Status.CANCELLED
    assert resp.data["refund_requested"] is True

    refund = Refund.objects.get(payment__order_id=order_id)
    assert refund.status == Refund.Status.REQUESTED
    assert refund.amount == Order.objects.get(pk=order_id).total
    # The money has not actually moved, so the order is still paid.
    assert Order.objects.get(pk=order_id).status == Order.Status.PAID
    slot.refresh_from_db()
    assert slot.is_booked is False


def test_student_cancelling_their_own_paid_booking_owes_nothing(
    paid_trainer, student, slot
):
    api = APIClient()
    api.force_authenticate(student)
    booking_id = _book(api, paid_trainer, slot).data["id"]
    order_id = _confirm(paid_trainer, booking_id).data["order"]
    api.force_authenticate(student)
    api.post(f"/api/v1/orders/{order_id}/pay/", {}, format="json")

    resp = api.post(f"/api/v1/individual-bookings/{booking_id}/cancel/")
    assert resp.data["refund_requested"] is False
    assert not Refund.objects.filter(payment__order_id=order_id).exists()


def test_cannot_pay_for_someone_elses_booking(paid_trainer, student, slot):
    api = APIClient()
    api.force_authenticate(student)
    booking_id = _book(api, paid_trainer, slot).data["id"]
    _confirm(paid_trainer, booking_id)

    intruder = User.objects.create_user(
        email="intruder@example.com", password="StrongPass123!", role=Role.STUDENT
    )
    api.force_authenticate(intruder)
    resp = api.post(
        "/api/v1/orders/",
        {"items": [{"item_type": "session", "object_id": booking_id}]},
        format="json",
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_open_requests_are_capped(trainer, student):
    api = APIClient()
    api.force_authenticate(student)
    base = timezone.now() + timedelta(days=3)
    for hours in range(4):
        start = base + timedelta(hours=hours)
        s = TrainerAvailability.objects.create(
            trainer=trainer, start=start, end=start + timedelta(hours=1)
        )
        resp = _book(api, trainer, s, topic=f"session {hours}")
        if hours < 3:
            assert resp.status_code == status.HTTP_201_CREATED
        else:
            assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_payment_deadline_never_runs_past_the_session_start(paid_trainer, student):
    start = timezone.now() + timedelta(hours=3)
    near = TrainerAvailability.objects.create(
        trainer=paid_trainer, start=start, end=start + timedelta(hours=1)
    )
    api = APIClient()
    api.force_authenticate(student)
    booking_id = _book(api, paid_trainer, near).data["id"]
    _confirm(paid_trainer, booking_id)

    booking = IndividualBooking.objects.get(pk=booking_id)
    # 24h window would overshoot the session, so the 2h-before cutoff wins.
    assert booking.payment_due_at == start - timedelta(hours=2)


def test_expired_unpaid_booking_is_swept_and_frees_the_slot(paid_trainer, student, slot):
    api = APIClient()
    api.force_authenticate(student)
    booking_id = _book(api, paid_trainer, slot).data["id"]
    _confirm(paid_trainer, booking_id)

    booking = IndividualBooking.objects.get(pk=booking_id)
    assert booking.payment_expired is False
    # Backdate the order: the deadline is derived from when the trainer accepted.
    Order.objects.filter(pk=booking.order_id).update(
        created_at=timezone.now() - timedelta(hours=48)
    )

    call_command("expire_unpaid_bookings")
    booking.refresh_from_db()
    assert booking.status == IndividualBooking.Status.CANCELLED
    slot.refresh_from_db()
    assert slot.is_booked is False


def test_other_trainer_cannot_confirm(trainer, student, slot):
    intruder = User.objects.create_user(
        email="other-trainer@example.com", password="StrongPass123!", role=Role.TRAINER
    )
    api = APIClient()
    api.force_authenticate(student)
    booking_id = _book(api, trainer, slot).data["id"]

    api.force_authenticate(intruder)
    resp = api.post(f"/api/v1/individual-bookings/{booking_id}/confirm/")
    assert resp.status_code == status.HTTP_404_NOT_FOUND
