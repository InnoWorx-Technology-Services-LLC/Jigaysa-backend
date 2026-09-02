"""Checkout, pricing and fulfilment logic (PRD §3.3, §3.4, §3.13).

Kept out of the views so the money math (line items → discount → GST → total) and
the fulfilment side-effects (paid enrollment, subscription activation) live in one
auditable place.

Two payment paths converge on ``_settle``:

* **Razorpay** (production) — ``start_checkout`` creates the gateway order,
  then either ``confirm_checkout`` (browser handler payload) or the webhook
  (``confirm_webhook_payment``) settles it. Both verify a signature first.
* **Mock** (``pay_order``) — synchronous confirmation, allowed only when no
  Razorpay keys are configured, so dev and the test suite keep working.

Every settlement is idempotent: a second confirmation of the same order is a
no-op that returns the existing successful payment. That matters because
Razorpay retries webhooks and the browser handler can race the webhook.
"""

import logging
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db import models, transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from courses.models import Course
from payments import gateway
from payments.models import (
    Coupon,
    CoursePrice,
    Invoice,
    Order,
    OrderItem,
    Payment,
    PricingPlan,
    Refund,
    Subscription,
)

logger = logging.getLogger(__name__)

# Fallback GST rate applied to the discounted subtotal (PRD §3.3 Taxes/GST).
# The live rate comes from platform settings; this is what is used before the
# settings row exists.
GST_PERCENT = Decimal("18")


def gst_percent() -> Decimal:
    """The GST rate in force right now, from platform settings.

    Read per quote, never cached. An order snapshots ``tax_gst`` at creation, so
    changing the rate reprices future carts and leaves placed orders — and the
    invoices already issued against them — exactly as they were quoted.
    """
    from core.models import PlatformSetting  # lazy: avoids an app-load cycle

    value = PlatformSetting.get_solo().gst_percent
    return Decimal(value if value is not None else GST_PERCENT)

_PERIOD_DAYS = {
    PricingPlan.BillingPeriod.MONTHLY: 30,
    PricingPlan.BillingPeriod.QUARTERLY: 90,
    PricingPlan.BillingPeriod.ANNUAL: 365,
}


def money(value) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def session_amount(booking):
    """Price a 1:1 booking: the trainer's hourly rate × booked hours (PRD §3.6
    "payment per hour"). Read at order-creation time and snapshotted into the
    ``OrderItem``, so a later rate change cannot move a quoted price."""
    profile = getattr(booking.trainer, "trainer_profile", None)
    rate = Decimal(profile.hourly_rate) if profile else Decimal("0")
    return money(rate * Decimal(booking.duration_minutes) / Decimal("60"))


def resolve_line_item(item_type, object_id, user=None):
    """Resolve a requested line item to (title, amount, ref). Amount is taken
    from the server-side price, never the client.

    ``user`` is the buyer. It is optional for catalog items (a course costs the
    same for everyone) but **required** for a session, which is priced from —
    and payable only by — one specific booking's owner.
    """
    if item_type == OrderItem.ItemType.SESSION:
        from live.models import IndividualBooking  # lazy: avoid app-load cycle

        booking = IndividualBooking.objects.filter(pk=object_id).select_related(
            "trainer__trainer_profile"
        ).first()
        if booking is None:
            raise ValidationError(f"Booking {object_id} not found.")
        if user is None or booking.student_id != user.id:
            raise ValidationError("You can only pay for your own booking.")
        if booking.status != IndividualBooking.Status.AWAITING_PAYMENT:
            raise ValidationError(
                "This booking is not awaiting payment."
            )
        amount = session_amount(booking)
        if amount <= 0:
            raise ValidationError("This booking has nothing to pay.")
        title = f"1:1 with {booking.trainer.full_name or booking.trainer.email}"
        if booking.topic:
            title = f"{title} — {booking.topic}"
        return title[:255], amount, booking
    if item_type == OrderItem.ItemType.COURSE:
        course = Course.objects.filter(pk=object_id).first()
        if course is None:
            raise ValidationError(f"Course {object_id} not found.")
        if course.is_free:
            raise ValidationError(f"'{course.title}' is free — just enroll.")
        price = (
            CoursePrice.objects.filter(
                course=course, pricing_type=CoursePrice.PricingType.ONE_TIME
            )
            .order_by("amount")
            .first()
        )
        if price is None:
            raise ValidationError(f"'{course.title}' is not purchasable yet.")
        amount = price.amount - price.discount_amount
        if price.discount_percent:
            amount -= amount * price.discount_percent / 100
        return course.title, money(max(amount, 0)), course
    if item_type == OrderItem.ItemType.PLAN:
        plan = PricingPlan.objects.filter(pk=object_id, is_active=True).first()
        if plan is None:
            raise ValidationError(f"Plan {object_id} not found or inactive.")
        return plan.name, money(plan.price), plan
    raise ValidationError(f"Unsupported item type: {item_type}.")


def validate_coupon(code, subtotal, item_types):
    """Return an applicable ``Coupon`` for this cart or raise. ``item_types`` is
    the set of item types in the order, used to enforce coupon scope."""
    from core.models import PlatformSetting  # lazy: avoids an app-load cycle

    # Enforced here rather than in the view so the kill switch covers order
    # creation and the "preview my discount" endpoint with one check.
    if not PlatformSetting.get_solo().allow_coupon_codes:
        raise ValidationError("Coupon codes are currently disabled.")
    coupon = Coupon.objects.filter(code=code, is_active=True).first()
    if coupon is None:
        raise ValidationError("Invalid or inactive coupon.")
    now = timezone.now()
    if coupon.valid_from and now < coupon.valid_from:
        raise ValidationError("This coupon is not active yet.")
    if coupon.valid_to and now > coupon.valid_to:
        raise ValidationError("This coupon has expired.")
    if coupon.max_redemptions and coupon.used_count >= coupon.max_redemptions:
        raise ValidationError("This coupon has been fully redeemed.")
    if subtotal < coupon.min_amount:
        raise ValidationError(
            f"Order must be at least {coupon.min_amount} to use this coupon."
        )
    if coupon.scope == Coupon.Scope.COURSE and OrderItem.ItemType.COURSE not in item_types:
        raise ValidationError("This coupon only applies to course purchases.")
    if coupon.scope == Coupon.Scope.PLAN and OrderItem.ItemType.PLAN not in item_types:
        raise ValidationError("This coupon only applies to plan purchases.")
    return coupon


def coupon_discount(coupon, subtotal) -> Decimal:
    if coupon is None:
        return money(0)
    if coupon.discount_type == Coupon.DiscountType.PERCENT:
        return money(min(subtotal, subtotal * coupon.value / 100))
    return money(min(subtotal, coupon.value))


def quote(items, coupon_code=None, user=None):
    """Price a cart without persisting: returns the resolved items + totals."""
    if not items:
        raise ValidationError("An order needs at least one item.")
    resolved = []
    subtotal = Decimal("0")
    item_types = set()
    for item in items:
        title, amount, ref = resolve_line_item(
            item["item_type"], item["object_id"], user=user
        )
        resolved.append(
            {"item_type": item["item_type"], "object_id": item["object_id"],
             "title": title, "amount": amount, "ref": ref}
        )
        subtotal += amount
        item_types.add(item["item_type"])

    coupon = None
    discount = money(0)
    if coupon_code:
        coupon = validate_coupon(coupon_code, subtotal, item_types)
        discount = coupon_discount(coupon, subtotal)

    taxable = subtotal - discount
    gst = money(taxable * gst_percent() / 100)
    total = money(taxable + gst)
    return {
        "items": resolved,
        "coupon": coupon,
        "subtotal": money(subtotal),
        "discount": discount,
        "tax_gst": gst,
        "total": total,
    }


@transaction.atomic
def create_order(user, items, coupon_code=None):
    from core.models import PlatformSetting  # lazy: avoids an app-load cycle

    q = quote(items, coupon_code, user=user)
    order = Order.objects.create(
        user=user,
        status=Order.Status.PENDING,
        subtotal=q["subtotal"],
        discount=q["discount"],
        tax_gst=q["tax_gst"],
        total=q["total"],
        # Stamped from settings at creation, so a later currency change cannot
        # reinterpret the amount an order was already placed for.
        currency=PlatformSetting.get_solo().default_currency,
        coupon=q["coupon"],
    )
    OrderItem.objects.bulk_create(
        OrderItem(
            order=order,
            item_type=it["item_type"],
            object_id=it["object_id"],
            title=it["title"],
            amount=it["amount"],
        )
        for it in q["items"]
    )
    return order


def _next_invoice_number():
    year = timezone.now().year
    seq = Invoice.objects.filter(number__startswith=f"JIG-{year}-").count() + 1
    return f"JIG-{year}-{seq:04d}"


def _existing_success(order):
    return order.payments.filter(status=Payment.Status.SUCCESS).first()


def _assert_payable(order):
    if order.status == Order.Status.REFUNDED:
        raise ValidationError("This order was refunded and cannot be paid.")
    if order.total <= 0:
        raise ValidationError("This order has nothing to pay.")


@transaction.atomic
def _settle(order, payment):
    """Mark ``order`` paid, invoice it and grant what was bought.

    ``payment`` must already be saved with SUCCESS status. Callers are
    responsible for verifying the money actually moved — this function trusts
    them, so never call it straight from a request body.
    """
    order.status = Order.Status.PAID
    order.save(update_fields=["status", "updated_at"])

    Invoice.objects.create(
        order=order,
        number=_next_invoice_number(),
        user=order.user,
        description=", ".join(i.title for i in order.items.all())[:255],
        amount=order.subtotal - order.discount,
        gst_amount=order.tax_gst,
        status=Invoice.Status.PAID,
        issued_date=timezone.now().date(),
    )
    if order.coupon_id:
        Coupon.objects.filter(pk=order.coupon_id).update(
            used_count=order.coupon.used_count + 1
        )
    _fulfil_order(order)

    # The trainer's share is recognised here, at capture — see
    # ``payments.earnings`` for why, and for the hold period that keeps a
    # refund inside the window from chasing money that already left.
    from payments import earnings  # lazy: earnings imports back into services

    earnings.record_order_earnings(order)
    return payment


# --------------------------------------------------------------------------- #
# Razorpay path
# --------------------------------------------------------------------------- #


@transaction.atomic
def start_checkout(order):
    """Create (or reuse) the Razorpay order for ``order``.

    Returns ``(payment, razorpay_order)``. Reusing an existing pending Payment
    row means a student who abandons and reopens checkout doesn't accumulate
    orphan gateway orders — and the amount can't drift, because the row is
    keyed to this Order's total.
    """
    _assert_payable(order)
    if order.status == Order.Status.PAID:
        raise ValidationError("This order is already paid.")

    existing = (
        order.payments.filter(
            status=Payment.Status.CREATED, gateway=Payment.Gateway.RAZORPAY
        )
        .exclude(gateway_order_id="")
        .first()
    )
    if existing and existing.amount == order.total:
        return existing, existing.raw.get("gateway_order", {})

    rzp_order = gateway.create_order(
        amount=order.total,
        receipt=order.pk,
        notes={"order_id": str(order.pk), "user_id": str(order.user_id),
               "email": order.user.email},
        currency=order.currency,
    )
    payment = Payment.objects.create(
        order=order,
        gateway=Payment.Gateway.RAZORPAY,
        gateway_order_id=rzp_order["id"],
        amount=order.total,
        status=Payment.Status.CREATED,
        raw={"gateway_order": dict(rzp_order)},
    )
    return payment, rzp_order


def confirm_checkout(order, razorpay_order_id, razorpay_payment_id, signature):
    """Settle ``order`` from the browser's Razorpay Checkout handler payload.

    Verifies, in this order: the signature is genuine, the gateway order id
    belongs to *this* order, and the captured amount matches what we billed.
    Any of those failing means the payload was tampered with or replayed.
    """
    paid = _existing_success(order)
    if paid:
        return paid  # webhook (or a double submit) already settled it
    _assert_payable(order)

    payment = order.payments.filter(gateway_order_id=razorpay_order_id).first()
    if payment is None:
        # The signature may well be valid — for somebody else's order. Refusing
        # here is what stops a cheap order's receipt paying for an expensive one.
        raise ValidationError("This payment does not belong to this order.")

    gateway.verify_checkout_signature(
        razorpay_order_id, razorpay_payment_id, signature
    )

    remote = gateway.fetch_payment(razorpay_payment_id)
    _assert_remote_matches(remote, payment)

    return _record_success(payment, remote, razorpay_payment_id)


def confirm_webhook_payment(razorpay_order_id, razorpay_payment_id, remote):
    """Settle from a verified ``payment.captured`` webhook. Returns the Payment,
    or ``None`` when the event is for an order we don't know about."""
    payment = (
        Payment.objects.select_related("order")
        .filter(gateway_order_id=razorpay_order_id)
        .first()
    )
    if payment is None:
        return None
    order = payment.order
    paid = _existing_success(order)
    if paid:
        return paid  # already settled by the browser handler or an earlier retry
    if order.status == Order.Status.REFUNDED:
        return None

    _assert_remote_matches(remote, payment)
    return _record_success(payment, remote, razorpay_payment_id)


def _assert_remote_matches(remote, payment):
    """Guard against a genuine-but-wrong payment being applied to this order."""
    if str(remote.get("status")) not in ("captured", "authorized"):
        raise ValidationError(
            f"Payment is not captured (status: {remote.get('status')})."
        )
    charged = gateway.from_minor_units(remote.get("amount") or 0)
    if charged != money(payment.amount):
        raise ValidationError(
            f"Paid amount {charged} does not match the order total {payment.amount}."
        )


@transaction.atomic
def _record_success(payment, remote, razorpay_payment_id):
    payment.gateway_payment_id = razorpay_payment_id
    payment.status = Payment.Status.SUCCESS
    payment.method = str(remote.get("method") or "")[:40]
    payment.paid_at = timezone.now()
    payment.raw = {**(payment.raw or {}), "payment": dict(remote)}
    payment.save(
        update_fields=[
            "gateway_payment_id", "status", "method", "paid_at", "raw", "updated_at"
        ]
    )
    return _settle(payment.order, payment)


def mark_failed(order, razorpay_order_id, remote=None):
    """Record a failed attempt without touching the order — the student can
    retry, and ``start_checkout`` will hand back the same gateway order."""
    payment = order.payments.filter(gateway_order_id=razorpay_order_id).first()
    if payment is None or payment.status == Payment.Status.SUCCESS:
        return payment
    payment.status = Payment.Status.FAILED
    payment.raw = {**(payment.raw or {}), "failure": dict(remote or {})}
    payment.save(update_fields=["status", "raw", "updated_at"])
    return payment


# --------------------------------------------------------------------------- #
# Mock path (no gateway configured)
# --------------------------------------------------------------------------- #


@transaction.atomic
def pay_order(order, gateway_name="mock", payment_method=None, gateway_payment_id=""):
    """Confirm an order synchronously, with no gateway involved.

    Only reachable when Razorpay is unconfigured — the view enforces that. Kept
    so local development and the test suite can exercise fulfilment without
    network calls. Idempotent.
    """
    if order.status == Order.Status.PAID:
        return _existing_success(order)
    _assert_payable(order)

    payment = Payment.objects.create(
        order=order,
        gateway=(
            gateway_name if gateway_name != "mock" else Payment.Gateway.RAZORPAY
        ),
        gateway_payment_id=(
            gateway_payment_id or f"mock_{order.pk}_{timezone.now():%H%M%S}"
        ),
        amount=order.total,
        method=(payment_method.type if payment_method else "mock"),
        status=Payment.Status.SUCCESS,
        paid_at=timezone.now(),
    )
    return _settle(order, payment)


def _fulfil_order(order):
    """Grant what the student paid for: enrollments and/or a subscription."""
    from courses.views import _create_enrollment  # lazy: avoid app-load cycle
    from courses.models import Enrollment

    for item in order.items.all():
        if item.item_type == OrderItem.ItemType.COURSE:
            course = Course.objects.filter(pk=item.object_id).first()
            if course and not Enrollment.objects.filter(
                student=order.user, course=course
            ).exists():
                enrollment = _create_enrollment(student=order.user, course=course)
                Enrollment.objects.filter(pk=enrollment.pk).update(
                    source=Enrollment.Source.PURCHASE, order=order
                )
        elif item.item_type == OrderItem.ItemType.PLAN:
            _activate_subscription(order, item.object_id)
        elif item.item_type == OrderItem.ItemType.SESSION:
            _confirm_paid_booking(order, item.object_id)


def _confirm_paid_booking(order, booking_id):
    """Money arrived for a 1:1 booking — confirm it, or owe it back.

    The booking can legitimately no longer be payable by the time this runs: the
    student may have paid a stale order after the payment window closed and the
    slot was released. Confirming then would hand out a slot the trainer has
    already re-sold, so we record the money as owed instead of silently keeping
    it.
    """
    from live.models import IndividualBooking  # lazy: avoid app-load cycle

    booking = IndividualBooking.objects.filter(
        pk=booking_id, student=order.user
    ).first()
    if booking is None:
        return
    if booking.status == IndividualBooking.Status.AWAITING_PAYMENT:
        booking.status = IndividualBooking.Status.CONFIRMED
        booking.order = order
        booking.save(update_fields=["status", "order", "updated_at"])
    else:
        request_refund(order, reason=f"Booking #{booking.pk} was no longer payable.")


def request_refund(order, reason="", send=True):
    """Record money owed back to the buyer, then try to actually send it.

    The ``Refund`` row is written **first and always**, even if the gateway call
    then fails. That ordering is the whole safety property: an unsent refund is
    a visible debt someone can chase, whereas a gateway call made without a row
    behind it is money that moved with no record.

    Never raises. A refund that cannot be sent right now is not a reason to fail
    the cancellation that triggered it — the student's session is cancelled
    either way, and the debt is on the books.
    """
    refunds = []
    for payment in order.payments.filter(status=Payment.Status.SUCCESS):
        # One open refund per payment. A previously *failed* one does not block
        # a fresh attempt, but an outstanding or settled one does — that is what
        # stops a double cancellation becoming a double refund.
        refund = Refund.objects.filter(
            payment=payment,
            status__in=(Refund.Status.REQUESTED, Refund.Status.PROCESSED),
        ).first()
        if refund is None:
            refund = Refund.objects.create(
                payment=payment, amount=payment.amount, reason=reason[:255]
            )
        if send and refund.status == Refund.Status.REQUESTED and not refund.is_sent:
            send_refund(refund)
        refunds.append(refund)
    return refunds


def send_refund(refund):
    """Push one recorded refund to Razorpay. Returns the updated ``Refund``.

    Outcomes, and why each is handled the way it is:

    * **no gateway configured** — leave it ``requested``; an admin settles it.
      This is also the dev/test path.
    * **already refunded at Razorpay** — not an error. Our row simply hadn't
      caught up (a timed-out call that actually succeeded, or a dashboard
      refund). Mark it processed.
    * **insufficient balance** — the takings are already settled to the bank, so
      there is no float to refund from. Stays ``requested`` so the debt is still
      owed, and an admin retries after topping up. *Not* marked failed: failed
      would read as "written off".
    * **permanently impossible** (payment too old, uncaptured, wrong amount) —
      marked ``failed`` with the reason, because no retry will ever fix it. Pay
      the student back out of band.
    * **network / unknown** — stays ``requested`` with no gateway id. The retry
      command asks Razorpay what actually happened before trying again, so a
      timed-out-but-successful call is never refunded twice.
    """
    payment = refund.payment
    if not payment.gateway_payment_id or not gateway.is_configured():
        return refund

    try:
        remote = gateway.refund_payment(
            payment.gateway_payment_id,
            refund.amount,
            notes={"order_id": str(payment.order_id), "reason": refund.reason},
        )
    except gateway.RefundNotPossible as exc:
        # "Fully refunded already" is a success in disguise — the money is back.
        if "refunded" in str(exc).lower():
            return _mark_refund_processed(refund, {"detail": str(exc)})
        return _mark_refund_failed(refund, str(exc))
    except gateway.InsufficientBalance as exc:
        return _hold_refund(refund, str(exc), "insufficient_balance")
    except (gateway.GatewayError, gateway.GatewayNotConfigured) as exc:
        return _hold_refund(refund, str(exc), "unknown")

    refund.gateway_refund_id = remote.get("id") or ""
    refund.raw = remote
    if remote.get("status") == "processed":
        return _mark_refund_processed(refund, remote)
    refund.save(update_fields=["gateway_refund_id", "raw", "updated_at"])
    return refund


def _mark_refund_processed(refund, remote):
    refund.status = Refund.Status.PROCESSED
    refund.processed_at = timezone.now()
    refund.raw = remote if isinstance(remote, dict) else {"detail": str(remote)}
    if isinstance(remote, dict) and remote.get("id"):
        refund.gateway_refund_id = remote["id"]
    refund.save(
        update_fields=[
            "status", "processed_at", "raw", "gateway_refund_id", "updated_at"
        ]
    )
    order = refund.payment.order
    if order.status != Order.Status.REFUNDED:
        order.status = Order.Status.REFUNDED
        order.save(update_fields=["status", "updated_at"])
    Invoice.objects.filter(order=order).update(status=Invoice.Status.REFUNDED)

    # The trainer did not keep money the student got back. Reversed, never
    # deleted — a March statement has to still say what March was.
    from payments import earnings  # lazy: earnings imports back into services

    earnings.reverse_order_earnings(
        order, note=f"Refund #{refund.pk} processed."
    )
    return refund


def _mark_refund_failed(refund, message):
    refund.status = Refund.Status.FAILED
    refund.raw = {"error": message, "outcome": "permanent"}
    refund.save(update_fields=["status", "raw", "updated_at"])
    logger.error(
        "Refund %s permanently rejected (payment=%s): %s",
        refund.pk, refund.payment.gateway_payment_id, message,
    )
    return refund


def _hold_refund(refund, message, outcome):
    """Keep the debt open and retryable, and say loudly that it needs a human."""
    refund.raw = {"error": message, "outcome": outcome}
    refund.save(update_fields=["raw", "updated_at"])
    logger.error(
        "Refund %s not sent (%s, payment=%s): %s — money still owed",
        refund.pk, outcome, refund.payment.gateway_payment_id, message,
    )
    return refund


def retry_refund(refund):
    """Re-attempt a recorded-but-unsent refund, without ever double-paying.

    Asks Razorpay for the payment's existing refunds first: a call that timed
    out may well have succeeded, and re-sending on that assumption is how you
    refund a student twice.
    """
    if refund.status != Refund.Status.REQUESTED or refund.is_sent:
        return refund
    payment = refund.payment
    if not payment.gateway_payment_id or not gateway.is_configured():
        return refund
    try:
        existing = gateway.fetch_refunds(payment.gateway_payment_id)
    except (gateway.GatewayError, gateway.GatewayNotConfigured) as exc:
        return _hold_refund(refund, str(exc), "unknown")
    if existing:
        # Something is already out there — adopt it rather than making another.
        remote = existing[0]
        if remote.get("status") == "failed":
            return _mark_refund_failed(refund, "Gateway refund failed.")
        if remote.get("status") == "processed":
            return _mark_refund_processed(refund, remote)
        refund.gateway_refund_id = remote.get("id") or ""
        refund.raw = remote
        refund.save(update_fields=["gateway_refund_id", "raw", "updated_at"])
        return refund
    return send_refund(refund)


def _activate_subscription(order, plan_id):
    """Start — or extend — the plan the student just paid for.

    Renewing *before* the current period runs out stacks the new period onto the
    end of the old one. Restarting from today instead would silently confiscate
    the days already bought: renew a monthly plan with ten days left and you'd
    have paid twice for thirty days rather than once for forty.

    Deliberately not ``update_or_create``: nothing enforces uniqueness on
    ``(user, plan)`` at the database level, so a stray duplicate row would make
    that raise ``MultipleObjectsReturned`` — and this runs *after* the money has
    moved, inside the settlement transaction. Picking the furthest-dated row is
    both duplicate-tolerant and the right answer when there is only one. The
    lock closes the other end of it: the browser handler and the webhook can
    settle two orders for the same plan concurrently, and without it both would
    see "no subscription yet" and create one each.
    """
    plan = PricingPlan.objects.filter(pk=plan_id).first()
    if plan is None:
        return
    now = timezone.now()
    days = _PERIOD_DAYS.get(plan.billing_period, 30)

    existing = (
        Subscription.objects.select_for_update()
        .filter(user=order.user, plan=plan)
        .order_by(models.F("current_period_end").desc(nulls_last=True))
        .first()
    )
    if existing is None:
        Subscription.objects.create(
            user=order.user,
            plan=plan,
            status=Subscription.Status.ACTIVE,
            current_period_start=now,
            current_period_end=now + timedelta(days=days),
        )
        return

    still_running = bool(
        existing.current_period_end and existing.current_period_end > now
    )
    existing.status = Subscription.Status.ACTIVE
    # Paying again withdraws a pending cancellation — otherwise the renewal
    # would be billed and then still lapse on the old cancel date.
    existing.cancel_at = None
    if not still_running:
        existing.current_period_start = now
    existing.current_period_end = (
        existing.current_period_end if still_running else now
    ) + timedelta(days=days)
    existing.save(
        update_fields=[
            "status", "cancel_at", "current_period_start",
            "current_period_end", "updated_at",
        ]
    )


def platform_currency() -> str:
    """The currency admin totals are quoted in.

    Amounts are stored per order, so a deployment that switched currency
    mid-life has rows in both — this is the label for *today's* numbers, not a
    conversion. Mixing currencies in one total would be wrong either way; this
    at least names which one the tiles mean.
    """
    from core.models import PlatformSetting  # lazy: avoids an app-load cycle

    return PlatformSetting.get_solo().default_currency


def refund_payment(payment, amount=None, reason=""):
    """Refund one payment, in whole or in part (admin console).

    The per-payment sibling of :func:`request_refund`, which refunds an order's
    payments in full. Both keep the same ordering — **the row is written before
    the gateway is called and survives the call failing** — because an unsent
    refund is a visible debt someone can chase, while a gateway call with no row
    behind it is money that moved with no record.

    An outstanding or settled refund on the same payment blocks a second one; a
    previously *failed* one does not, so a retry is still possible. Passing no
    ``amount`` refunds whatever remains.
    """
    already = (
        Refund.objects.filter(
            payment=payment,
            status__in=(Refund.Status.REQUESTED, Refund.Status.PROCESSED),
        )
        .aggregate(total=models.Sum("amount"))["total"]
        or Decimal("0")
    )
    remaining = money(payment.amount) - money(already)
    if remaining <= 0:
        raise ValidationError("This payment has already been fully refunded.")

    amount = money(amount) if amount is not None else remaining
    if amount <= 0:
        raise ValidationError("A refund has to be for more than zero.")
    if amount > remaining:
        raise ValidationError(
            f"That is more than the {remaining} still refundable on this payment."
        )

    refund = Refund.objects.create(
        payment=payment, amount=amount, reason=reason[:255]
    )
    if not refund.is_sent:
        send_refund(refund)
    return refund
