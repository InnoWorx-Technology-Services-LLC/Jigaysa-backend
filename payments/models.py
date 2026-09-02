"""Pricing, subscriptions, orders, invoices and payments (PRD §3.3, §3.4, §3.13).

Student billing screens read Subscription / Invoice / PaymentMethod. The full
purchase + gateway flow (Order → Payment → Refund) is modelled now so endpoints
slot in later without schema change. ``TrainerPayout`` is design-ready (PRD §3.14).
"""

from django.conf import settings
from django.db import models

from core.models import TimeStampedModel

CURRENCY_DEFAULT = "INR"


class PricingPlan(TimeStampedModel):
    """Platform-access subscription plan (PRD §3.4 minimum cost access)."""

    class BillingPeriod(models.TextChoices):
        MONTHLY = "monthly", "Monthly"
        QUARTERLY = "quarterly", "Quarterly"
        ANNUAL = "annual", "Annual"

    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140, unique=True)
    billing_period = models.CharField(
        max_length=20, choices=BillingPeriod.choices, default=BillingPeriod.MONTHLY
    )
    price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    currency = models.CharField(max_length=8, default=CURRENCY_DEFAULT)
    # Free-text marketing bullets shown on the pricing card. Display only —
    # the tick-boxes below are what the backend actually honours.
    features = models.JSONField(default=list, blank=True)

    # --- what a subscriber gets: ticked by an admin, read by the code ------ #
    includes_all_paid_courses = models.BooleanField(
        default=False,
        help_text="Access every paid course without buying them individually.",
    )
    includes_live_sessions = models.BooleanField(
        default=False, help_text="Join live classes and cohorts."
    )
    includes_certificates = models.BooleanField(
        default=False, help_text="Earn completion certificates."
    )
    priority_support = models.BooleanField(
        default=False,
        help_text="Support promise only — nothing in the API is gated on it.",
    )

    is_active = models.BooleanField(default=True)

    #: Tick-box name → the entitlement key the code checks.
    ENTITLEMENT_FIELDS = {
        "all_paid_courses": "includes_all_paid_courses",
        "live_sessions": "includes_live_sessions",
        "certificates": "includes_certificates",
        "priority_support": "priority_support",
    }

    def entitlements(self) -> dict:
        """This plan's ticks as a flat ``{key: bool}`` map for the API."""
        return {
            key: getattr(self, field)
            for key, field in self.ENTITLEMENT_FIELDS.items()
        }

    class Meta:
        ordering = ["price"]

    def __str__(self):
        return f"{self.name} ({self.billing_period})"


class Coupon(TimeStampedModel):
    """Discount coupon (PRD §3.3 discount coupons / promo campaigns)."""

    class DiscountType(models.TextChoices):
        PERCENT = "percent", "Percent"
        FLAT = "flat", "Flat"

    class Scope(models.TextChoices):
        COURSE = "course", "Course"
        PLAN = "plan", "Plan"
        ALL = "all", "All"

    code = models.CharField(max_length=40, unique=True)
    discount_type = models.CharField(
        max_length=10, choices=DiscountType.choices, default=DiscountType.PERCENT
    )
    value = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    scope = models.CharField(max_length=10, choices=Scope.choices, default=Scope.ALL)
    min_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    max_redemptions = models.PositiveIntegerField(default=0)  # 0 = unlimited
    used_count = models.PositiveIntegerField(default=0)
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_to = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.code


class CoursePrice(TimeStampedModel):
    """A price option attached to a course (PRD §3.3 pricing types)."""

    class PricingType(models.TextChoices):
        ONE_TIME = "one_time", "One-time"
        SUBSCRIPTION = "subscription", "Subscription"
        INSTALLMENT = "installment", "Installment / EMI"
        PER_SESSION = "per_session", "Pay per session"
        CORPORATE = "corporate", "Corporate"
        GROUP = "group", "Group"

    course = models.ForeignKey(
        "courses.Course", on_delete=models.CASCADE, related_name="prices"
    )
    pricing_type = models.CharField(
        max_length=20, choices=PricingType.choices, default=PricingType.ONE_TIME
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    currency = models.CharField(max_length=8, default=CURRENCY_DEFAULT)
    discount_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=0
    )
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_to = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.course} · {self.pricing_type} {self.amount}"


class PaymentMethod(TimeStampedModel):
    """A saved payment instrument (Billing "Visa •••• 4242")."""

    class MethodType(models.TextChoices):
        CARD = "card", "Card"
        UPI = "upi", "UPI"
        NETBANKING = "netbanking", "Net banking"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="payment_methods",
    )
    type = models.CharField(
        max_length=20, choices=MethodType.choices, default=MethodType.CARD
    )
    brand = models.CharField(max_length=40, blank=True)
    last4 = models.CharField(max_length=4, blank=True)
    expiry = models.CharField(max_length=7, blank=True)  # MM/YY
    gateway_token = models.CharField(max_length=255, blank=True)
    is_default = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.brand} ••••{self.last4}".strip()


class Subscription(TimeStampedModel):
    """A user's active platform subscription (Billing active-plan card)."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        CANCELLED = "cancelled", "Cancelled"
        EXPIRED = "expired", "Expired"
        PAST_DUE = "past_due", "Past due"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="subscriptions",
    )
    plan = models.ForeignKey(
        PricingPlan, on_delete=models.PROTECT, related_name="subscriptions"
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.ACTIVE
    )
    current_period_start = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    payment_method = models.ForeignKey(
        PaymentMethod,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="subscriptions",
    )
    gateway_subscription_id = models.CharField(max_length=255, blank=True)
    cancel_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user} · {self.plan} [{self.status}]"


class Order(TimeStampedModel):
    """A checkout order, possibly multi-item (PRD §3.13)."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"
        REFUNDED = "refunded", "Refunded"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="orders"
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_gst = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    currency = models.CharField(max_length=8, default=CURRENCY_DEFAULT)
    coupon = models.ForeignKey(
        Coupon,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="orders",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Order#{self.pk} · {self.user} [{self.status}]"


class OrderItem(TimeStampedModel):
    """A line item in an order (course / plan / session / batch)."""

    class ItemType(models.TextChoices):
        COURSE = "course", "Course"
        PLAN = "plan", "Plan"
        SESSION = "session", "Session"
        BATCH = "batch", "Batch"

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    item_type = models.CharField(
        max_length=20, choices=ItemType.choices, default=ItemType.COURSE
    )
    object_id = models.PositiveIntegerField(null=True, blank=True)
    title = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    qty = models.PositiveIntegerField(default=1)

    def __str__(self):
        return f"{self.title} x{self.qty}"


class Invoice(TimeStampedModel):
    """A GST invoice for an order (Billing invoices table, PRD §3.13)."""

    class Status(models.TextChoices):
        PAID = "paid", "Paid"
        PENDING = "pending", "Pending"
        REFUNDED = "refunded", "Refunded"

    order = models.ForeignKey(
        Order,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invoices",
    )
    number = models.CharField(max_length=40, unique=True)  # JIG-YYYY-NNNN
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="invoices"
    )
    description = models.CharField(max_length=255, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    gst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    issued_date = models.DateField(null=True, blank=True)
    pdf_url = models.URLField(blank=True)

    class Meta:
        ordering = ["-issued_date", "-created_at"]

    def __str__(self):
        return self.number


class Payment(TimeStampedModel):
    """A gateway payment attempt against an order (PRD §3.13 gateways)."""

    class Gateway(models.TextChoices):
        RAZORPAY = "razorpay", "Razorpay"
        STRIPE = "stripe", "Stripe"
        PAYPAL = "paypal", "PayPal"
        UPI = "upi", "UPI"

    class Status(models.TextChoices):
        CREATED = "created", "Created"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"

    order = models.ForeignKey(
        Order, on_delete=models.CASCADE, related_name="payments"
    )
    gateway = models.CharField(max_length=20, choices=Gateway.choices)
    # The gateway's own order handle (Razorpay ``order_xxx``), created before the
    # customer pays. Indexed because the webhook arrives with only this id and
    # has to find our row. Unique per row so a replayed webhook can't double-pay.
    gateway_order_id = models.CharField(
        max_length=255, blank=True, db_index=True
    )
    gateway_payment_id = models.CharField(max_length=255, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    method = models.CharField(max_length=40, blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.CREATED
    )
    paid_at = models.DateTimeField(null=True, blank=True)
    raw = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"{self.gateway}:{self.gateway_payment_id} [{self.status}]"


class Refund(TimeStampedModel):
    """A refund against a payment (PRD §3.13 refunds)."""

    class Status(models.TextChoices):
        REQUESTED = "requested", "Requested"
        PROCESSED = "processed", "Processed"
        FAILED = "failed", "Failed"

    payment = models.ForeignKey(
        Payment, on_delete=models.CASCADE, related_name="refunds"
    )
    # Razorpay's own handle (``rfnd_xxx``). Indexed because the refund webhook
    # arrives with only this id and has to find our row. Empty means the gateway
    # was never successfully called — the obligation is recorded but unsent.
    gateway_refund_id = models.CharField(max_length=255, blank=True, db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    reason = models.CharField(max_length=255, blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.REQUESTED
    )
    processed_at = models.DateTimeField(null=True, blank=True)
    # Gateway response, or the error text when a call failed. This is what an
    # admin reads to decide whether to retry or settle by hand.
    raw = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"Refund {self.amount} [{self.status}]"

    @property
    def is_sent(self):
        """The gateway accepted it; only the webhook can settle it now."""
        return bool(self.gateway_refund_id)


class TrainerPayout(TimeStampedModel):
    """Revenue-share payout to a trainer (PRD §3.3, §3.14 — design-ready)."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PAID = "paid", "Paid"

    trainer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="payouts"
    )
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    gross = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    platform_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    net = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    paid_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Payout {self.trainer} {self.net} [{self.status}]"


class TrainerEarning(TimeStampedModel):
    """One trainer's share of one paid order line (PRD §3.3, §3.14).

    A **ledger line, not a running total.** Payouts are period aggregates and
    cannot answer "which sales made this figure", tell a reversal from a sale
    that never happened, or stop the same order being counted twice. This can.

    The three properties that make it safe to compute money from:

    * **Idempotent.** One row per ``order_item``, enforced by a unique
      constraint. Settlement runs from both the verify call and the webhook, so
      "record it again" has to be impossible rather than unlikely.
    * **Immutable once written.** A refund adds a ``reversed`` state; it never
      edits the original amounts. What a trainer earned in March stays what
      they earned in March even if the rate changes in April.
    * **Self-describing.** ``share_pct`` is snapshotted at the moment of sale,
      so recomputing history is never necessary — and never possible by
      accident. See ``TrainerProfile.effective_revenue_share_pct``.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Earned"
        REVERSED = "reversed", "Reversed (refunded)"

    trainer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="earnings",
    )
    order = models.ForeignKey(
        Order, on_delete=models.CASCADE, related_name="trainer_earnings"
    )
    #: The line this share was computed from. Unique — see the class docstring.
    order_item = models.OneToOneField(
        OrderItem, on_delete=models.CASCADE, related_name="trainer_earning"
    )
    course = models.ForeignKey(
        "courses.Course",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="trainer_earnings",
    )

    #: What the student actually paid for this line: **net of any coupon
    #: discount and excluding GST.** Tax is the government's, not the
    #: platform's to split, and a discount the platform chose to give is not
    #: revenue anybody received.
    gross = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    #: The trainer's percentage at the moment of sale, snapshotted.
    share_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    platform_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    net = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    currency = models.CharField(max_length=8, default=CURRENCY_DEFAULT)

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    #: Set when this line is swept into a payout. ``NULL`` means "still owed"
    #: — whether it has actually been *paid* is then read from the payout's own
    #: status, so there is one source of truth rather than two that can drift.
    payout = models.ForeignKey(
        "payments.TrainerPayout",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="earnings",
    )
    earned_at = models.DateTimeField()
    reversed_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-earned_at", "-id"]
        indexes = [
            models.Index(fields=["trainer", "-earned_at"]),
            models.Index(fields=["trainer", "status", "payout"]),
        ]

    def __str__(self):
        return f"{self.trainer} earned {self.net} on order #{self.order_id}"

    @property
    def is_payable(self) -> bool:
        """Still owed: not refunded, and not yet swept into a payout."""
        return self.status == self.Status.PENDING and self.payout_id is None
