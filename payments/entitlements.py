"""What an active platform subscription unlocks (PRD §3.4 platform access).

One place answers "may this user do X because they subscribe?", so course
access, enrollment and the billing page can never disagree about it.

An entitlement needs **two** things to hold: a subscription row marked active
*and* a period that has not run out. Both matter, because nothing renews
subscriptions automatically — a plan bought last year still has
``status="active"`` forever, and trusting that flag alone would hand out free
access indefinitely.
"""

from django.utils import timezone

from payments.models import PricingPlan, Subscription

#: Entitlement keys, mirroring ``PricingPlan.ENTITLEMENT_FIELDS``.
ALL_PAID_COURSES = "all_paid_courses"
LIVE_SESSIONS = "live_sessions"
CERTIFICATES = "certificates"
PRIORITY_SUPPORT = "priority_support"

#: Entitlements the backend actually enforces today. The rest are advertised on
#: the pricing card and returned by the API, but gate nothing yet — those
#: features are currently open to every student, and quietly restricting them
#: would take away access people already have.
ENFORCED = frozenset({ALL_PAID_COURSES})


def active_subscription(user):
    """The user's current subscription, or ``None``.

    "Current" means active *and* inside its paid period. Newest first, so a
    user who upgraded mid-period gets the plan they most recently bought.
    """
    if not getattr(user, "is_authenticated", False):
        return None
    now = timezone.now()
    return (
        Subscription.objects.filter(
            user=user, status=Subscription.Status.ACTIVE
        )
        .filter(current_period_end__gte=now)
        .select_related("plan")
        .order_by("-current_period_start", "-created_at")
        .first()
    )


def entitlements_for(user) -> dict:
    """Every entitlement key → whether ``user`` currently has it."""
    subscription = active_subscription(user)
    if subscription is None:
        return {key: False for key in PricingPlan.ENTITLEMENT_FIELDS}
    return subscription.plan.entitlements()


def has_entitlement(user, key) -> bool:
    """Does ``user`` currently hold entitlement ``key``?"""
    return bool(entitlements_for(user).get(key))


def can_access_paid_courses(user) -> bool:
    """Shorthand for the one entitlement that gates content today."""
    return has_entitlement(user, ALL_PAID_COURSES)
