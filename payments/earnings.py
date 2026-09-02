"""Turning settled orders into trainer earnings, and earnings into payouts.

The piece that was missing: ``TrainerPayout`` existed as a table nothing wrote
to, so every earnings figure on every screen read zero regardless of revenue.

### When a trainer earns

**At capture.** The moment an order settles, the share is recorded. The
alternative — waiting out a refund window before recognising anything — makes
the Earnings page lie for a week to a trainer who just made a sale, and a
trainer who cannot see today's sale assumes the platform lost it.

The refund risk that recognition-at-capture creates is handled at the *payout*
boundary instead, where it actually matters: an earning must age
``TRAINER_PAYOUT_HOLD_DAYS`` before it can be swept into a payout. So a refund
inside the window reverses a line that was never sent anywhere, and the trainer
still saw their sale on the day it happened.

### What "gross" means

The line's amount **net of any coupon discount and excluding GST**:

* GST is the government's. Splitting it would have the platform and the trainer
  dividing money that belongs to neither.
* A coupon is a discount the platform chose to give. The student did not pay
  it, so nobody earned it — and it is allocated across lines in proportion to
  their amounts, because the coupon applied to the order, not to one course.

### Who earns

Course and batch lines pay the course's trainer; session lines pay the booked
trainer. **Plan lines pay nobody** — a platform subscription is not a sale of
any one trainer's work, and inventing an attribution for it would be worse than
recording none.
"""

import logging
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from payments.models import (
    CURRENCY_DEFAULT,
    OrderItem,
    TrainerEarning,
    TrainerPayout,
)

logger = logging.getLogger(__name__)

#: How long an earning must age before a payout can sweep it up. Covers the
#: window in which a refund is likely, so a reversal lands on money that has not
#: left yet. Configurable because a platform's refund policy is a business
#: decision, not a constant.
DEFAULT_HOLD_DAYS = 7


def hold_days() -> int:
    from django.conf import settings

    return int(getattr(settings, "TRAINER_PAYOUT_HOLD_DAYS", DEFAULT_HOLD_DAYS))


def _money(value) -> Decimal:
    from payments.services import money

    return money(value)


# --------------------------------------------------------------------------- #
# Recognition
# --------------------------------------------------------------------------- #


def _trainer_and_course(item):
    """Who earned from this line, and against which course.

    Returns ``(trainer, course)``; ``(None, None)`` when the line is not one
    trainer's work — a platform plan, or a reference that no longer resolves.
    """
    from courses.models import Batch, Course

    if item.item_type == OrderItem.ItemType.COURSE:
        course = Course.objects.filter(pk=item.object_id).first()
        return (course.trainer if course else None), course

    if item.item_type == OrderItem.ItemType.BATCH:
        batch = (
            Batch.objects.select_related("course")
            .filter(pk=item.object_id)
            .first()
        )
        if batch is None:
            return None, None
        # A batch can have its own trainer; otherwise the course's owner keeps it.
        return (batch.trainer or batch.course.trainer), batch.course

    if item.item_type == OrderItem.ItemType.SESSION:
        from live.models import IndividualBooking

        booking = IndividualBooking.objects.filter(pk=item.object_id).first()
        return (booking.trainer if booking else None), None

    # PLAN — a platform subscription. Nobody's individual sale.
    return None, None


def _share_pct(trainer) -> Decimal:
    """The trainer's cut at this moment, snapshotted onto the earning.

    Falls back to the platform default when the trainer has no profile yet —
    the same answer ``effective_revenue_share_pct`` would give, without
    creating a profile row as a side effect of someone buying a course.
    """
    from core.models import PlatformSetting

    profile = getattr(trainer, "trainer_profile", None)
    if profile is not None:
        return Decimal(profile.effective_revenue_share_pct)
    return Decimal("100") - PlatformSetting.get_solo().platform_commission_percent


def _line_gross(item, order) -> Decimal:
    """The line's amount, less its share of the order discount, excluding GST.

    The discount is allocated in proportion to line amounts because a coupon
    applies to the order, not to one course. Charging the whole discount to
    whichever line happens to be first would quietly underpay one trainer and
    overpay another on the same order.
    """
    amount = _money(item.amount)
    subtotal = _money(order.subtotal)
    discount = _money(order.discount)
    if discount <= 0 or subtotal <= 0:
        return amount
    return _money(amount - (amount / subtotal * discount))


@transaction.atomic
def record_order_earnings(order):
    """Write a ledger line for every attributable item on a settled order.

    Safe to call more than once — settlement runs from both the verify call and
    the webhook, so this **must** be idempotent rather than merely careful. The
    unique constraint on ``order_item`` is what guarantees it; the pre-check
    below just avoids the exception in the common case.
    """
    created = []
    for item in order.items.select_related("order"):
        if TrainerEarning.objects.filter(order_item=item).exists():
            continue

        trainer, course = _trainer_and_course(item)
        if trainer is None:
            continue

        gross = _line_gross(item, order)
        if gross <= 0:
            continue

        share = _share_pct(trainer)
        net = _money(gross * share / 100)
        created.append(
            TrainerEarning(
                trainer=trainer,
                order=order,
                order_item=item,
                course=course,
                gross=gross,
                share_pct=share,
                net=net,
                platform_fee=_money(gross - net),
                currency=order.currency or CURRENCY_DEFAULT,
                earned_at=timezone.now(),
            )
        )

    if created:
        TrainerEarning.objects.bulk_create(created, ignore_conflicts=True)
    return created


@transaction.atomic
def reverse_order_earnings(order, note=""):
    """Mark this order's earnings reversed after a refund.

    Reverses rather than deletes, and never edits the original amounts: a
    trainer's March statement must still say what March was, even once a refund
    lands in April. A line already swept into a payout is still marked — the
    money left, and the record has to say so for anyone reconciling it.
    """
    return TrainerEarning.objects.filter(
        order=order, status=TrainerEarning.Status.PENDING
    ).update(
        status=TrainerEarning.Status.REVERSED,
        reversed_at=timezone.now(),
        note=(note or "Order refunded.")[:255],
        updated_at=timezone.now(),
    )


# --------------------------------------------------------------------------- #
# Payouts
# --------------------------------------------------------------------------- #


def payable_earnings(as_of=None):
    """Earnings old enough to pay out and not already in a payout."""
    from datetime import timedelta

    as_of = as_of or timezone.now()
    return TrainerEarning.objects.filter(
        status=TrainerEarning.Status.PENDING,
        payout__isnull=True,
        earned_at__lte=as_of - timedelta(days=hold_days()),
    )


@transaction.atomic
def generate_payouts(as_of=None):
    """Sweep every trainer's payable earnings into one payout each.

    Returns the payouts created. A trainer with nothing payable gets no row —
    an empty payout is noise in a queue someone has to work through.

    The payout is created **pending**: this records what is owed and for which
    period. Nothing here moves money, because there is no payout processor
    integration; marking one paid is a separate, deliberate act.
    """
    as_of = as_of or timezone.now()
    earnings = payable_earnings(as_of).select_related("trainer")

    by_trainer = {}
    for earning in earnings:
        by_trainer.setdefault(earning.trainer_id, []).append(earning)

    payouts = []
    for trainer_id, lines in by_trainer.items():
        gross = sum((line.gross for line in lines), Decimal("0"))
        net = sum((line.net for line in lines), Decimal("0"))
        payout = TrainerPayout.objects.create(
            trainer_id=trainer_id,
            period_start=min(line.earned_at for line in lines).date(),
            period_end=as_of.date(),
            gross=_money(gross),
            net=_money(net),
            platform_fee=_money(gross - net),
            status=TrainerPayout.Status.PENDING,
        )
        TrainerEarning.objects.filter(
            pk__in=[line.pk for line in lines]
        ).update(payout=payout, updated_at=timezone.now())
        payouts.append(payout)

    return payouts
