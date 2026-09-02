"""Serializers for pricing, checkout, invoices and subscriptions (§3.3/3.4/3.13)."""

from rest_framework import serializers

from payments.models import (
    Coupon,
    CoursePrice,
    Invoice,
    Order,
    OrderItem,
    Payment,
    PaymentMethod,
    PricingPlan,
    Refund,
    Subscription,
    TrainerEarning,
    TrainerPayout,
)
from accounts.models import TrainerProfile
from analytics.serializers import TrendPointSerializer


class PricingPlanSerializer(serializers.ModelSerializer):
    entitlements = serializers.SerializerMethodField()

    class Meta:
        model = PricingPlan
        fields = (
            "id", "name", "slug", "billing_period", "price", "currency",
            "features", "entitlements", "is_active",
            "includes_all_paid_courses", "includes_live_sessions",
            "includes_certificates", "priority_support",
        )

    def get_entitlements(self, obj):
        """The admin's ticks as a flat map — what the plan card renders."""
        return obj.entitlements()


class BillingSummarySerializer(serializers.Serializer):
    """Everything the Billing page shows, in one call (response shape only)."""

    total_spent = serializers.DecimalField(max_digits=12, decimal_places=2)
    currency = serializers.CharField()
    active_plan = PricingPlanSerializer(allow_null=True)
    subscription = serializers.DictField(allow_null=True)
    entitlements = serializers.DictField()
    invoice_count = serializers.IntegerField()


class CoursePriceSerializer(serializers.ModelSerializer):
    class Meta:
        model = CoursePrice
        fields = (
            "id", "course", "pricing_type", "amount", "currency",
            "discount_percent", "discount_amount", "valid_from", "valid_to",
        )


class CouponSerializer(serializers.ModelSerializer):
    class Meta:
        model = Coupon
        fields = (
            "id", "code", "discount_type", "value", "scope", "min_amount",
            "max_redemptions", "used_count", "valid_from", "valid_to", "is_active",
        )
        read_only_fields = ("used_count",)


class PaymentMethodSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentMethod
        fields = (
            "id", "type", "brand", "last4", "expiry", "is_default", "created_at",
        )
        read_only_fields = ("created_at",)


class SubscriptionSerializer(serializers.ModelSerializer):
    plan = PricingPlanSerializer(read_only=True)

    class Meta:
        model = Subscription
        fields = (
            "id", "plan", "status", "current_period_start", "current_period_end",
            "cancel_at", "created_at",
        )
        read_only_fields = fields


class OrderItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderItem
        fields = ("id", "item_type", "object_id", "title", "amount", "qty")


class PaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = (
            "id", "gateway", "gateway_order_id", "gateway_payment_id", "amount",
            "method", "status", "paid_at",
        )


class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, read_only=True)
    payments = PaymentSerializer(many=True, read_only=True)
    coupon_code = serializers.CharField(source="coupon.code", read_only=True)

    class Meta:
        model = Order
        fields = (
            "id", "status", "subtotal", "discount", "tax_gst", "total",
            "currency", "coupon_code", "items", "payments", "created_at",
        )
        read_only_fields = fields


class InvoiceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Invoice
        fields = (
            "id", "number", "order", "description", "amount", "gst_amount",
            "status", "issued_date", "pdf_url",
        )
        read_only_fields = fields


# --- action payloads --------------------------------------------------------


class OrderItemInputSerializer(serializers.Serializer):
    item_type = serializers.ChoiceField(choices=OrderItem.ItemType.choices)
    object_id = serializers.IntegerField(min_value=1)


class CheckoutSerializer(serializers.Serializer):
    """Create an order. Amounts are computed server-side from the catalog."""

    items = OrderItemInputSerializer(many=True)
    coupon_code = serializers.CharField(required=False, allow_blank=True)


class PaySerializer(serializers.Serializer):
    gateway = serializers.ChoiceField(
        choices=Payment.Gateway.choices, required=False
    )
    payment_method_id = serializers.IntegerField(required=False)
    gateway_payment_id = serializers.CharField(required=False, allow_blank=True)


class RazorpayCheckoutSerializer(serializers.Serializer):
    """Everything the frontend needs to open Razorpay Checkout.

    Response-only — documents the shape of ``POST /orders/{id}/checkout/``.
    ``amount`` is in **paise** because that is what Checkout expects; ``key`` is
    the *public* key id and is safe to ship to the browser.
    """

    key = serializers.CharField()
    razorpay_order_id = serializers.CharField()
    amount = serializers.IntegerField(help_text="In paise (smallest currency unit).")
    amount_display = serializers.DecimalField(max_digits=12, decimal_places=2)
    currency = serializers.CharField()
    name = serializers.CharField()
    description = serializers.CharField()
    image = serializers.CharField(allow_blank=True)
    order_id = serializers.IntegerField(help_text="Our own Order id.")
    prefill = serializers.DictField()
    notes = serializers.DictField()
    callback_url = serializers.CharField()
    is_test_mode = serializers.BooleanField()


class RazorpayVerifySerializer(serializers.Serializer):
    """The payload Razorpay Checkout's ``handler`` hands back to the browser."""

    razorpay_order_id = serializers.CharField()
    razorpay_payment_id = serializers.CharField()
    razorpay_signature = serializers.CharField()


class CouponValidateSerializer(serializers.Serializer):
    code = serializers.CharField()
    items = OrderItemInputSerializer(many=True)


# --------------------------------------------------------------------------- #
# Admin console — the Payments page
# --------------------------------------------------------------------------- #


class AdminPaymentSerializer(serializers.ModelSerializer):
    """One row in the platform-wide transaction table.

    Carries the payer inline. The table's whole job is "who paid what", and
    making the client fetch a user per row to answer that is how a 20-row page
    becomes 21 requests.
    """

    payer_email = serializers.EmailField(source="order.user.email", read_only=True)
    payer_name = serializers.CharField(
        source="order.user.full_name", read_only=True, default=""
    )
    currency = serializers.CharField(source="order.currency", read_only=True)
    refunded_amount = serializers.SerializerMethodField()

    class Meta:
        model = Payment
        fields = (
            "id", "order", "payer_email", "payer_name", "gateway",
            "gateway_order_id", "gateway_payment_id", "amount", "currency",
            "method", "status", "refunded_amount", "paid_at", "created_at",
        )
        read_only_fields = fields

    def get_refunded_amount(self, obj) -> str:
        """Settled refunds only — a requested one is an intention, not a
        movement, and showing it as returned money overstates the refund column
        on every row that is still in flight."""
        total = sum(
            r.amount for r in obj.refunds.all()
            if r.status == Refund.Status.PROCESSED
        )
        return str(total)


class PaymentSummarySerializer(serializers.Serializer):
    """The four tiles. See ``AdminPaymentViewSet.summary`` for the definitions."""

    gross_volume = serializers.DecimalField(max_digits=14, decimal_places=2)
    refunds = serializers.DecimalField(max_digits=14, decimal_places=2)
    pending = serializers.DecimalField(max_digits=14, decimal_places=2)
    net = serializers.DecimalField(max_digits=14, decimal_places=2)
    currency = serializers.CharField()


class AdminRefundSerializer(serializers.ModelSerializer):
    """A refund, with enough of its payment to be actionable in a queue."""

    payer_email = serializers.EmailField(
        source="payment.order.user.email", read_only=True
    )
    order_id = serializers.IntegerField(source="payment.order_id", read_only=True)
    is_sent = serializers.BooleanField(read_only=True)

    class Meta:
        model = Refund
        fields = (
            "id", "payment", "order_id", "payer_email", "amount", "reason",
            "status", "gateway_refund_id", "is_sent", "processed_at",
            "created_at",
        )
        read_only_fields = fields


class RefundCreateSerializer(serializers.Serializer):
    """Body of ``POST /admin/refunds/``.

    Only successful payments can be refunded — there is nothing to send back
    from one that never captured, and letting an admin try produces a gateway
    error where a validation message belongs.
    """

    payment = serializers.PrimaryKeyRelatedField(queryset=Payment.objects.all())
    amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True,
        help_text="Omit to refund everything still refundable on this payment.",
    )
    reason = serializers.CharField(required=False, allow_blank=True, max_length=255)

    def validate_payment(self, payment):
        if payment.status != Payment.Status.SUCCESS:
            raise serializers.ValidationError(
                "Only a captured payment can be refunded."
            )
        return payment


class AdminPayoutSerializer(serializers.ModelSerializer):
    """A row in the trainer payout queue. Read-only — nothing writes these yet."""

    trainer_email = serializers.EmailField(source="trainer.email", read_only=True)
    trainer_name = serializers.CharField(
        source="trainer.full_name", read_only=True, default=""
    )

    class Meta:
        model = TrainerPayout
        fields = (
            "id", "trainer", "trainer_email", "trainer_name", "period_start",
            "period_end", "gross", "platform_fee", "net", "status", "paid_at",
            "created_at",
        )
        read_only_fields = fields


# --------------------------------------------------------------------------- #
# The trainer's Earnings page
# --------------------------------------------------------------------------- #


class EarningsSummarySerializer(serializers.Serializer):
    """The four tiles plus the revenue split.

    ``share_pct`` and ``platform_fee_pct`` always sum to 100 and are computed
    server-side, so the two bars cannot disagree with each other or with what
    the ledger actually used.
    """

    this_month = serializers.DecimalField(max_digits=14, decimal_places=2)
    lifetime = serializers.DecimalField(max_digits=14, decimal_places=2)
    pending_payout = serializers.DecimalField(max_digits=14, decimal_places=2)
    average_per_course = serializers.DecimalField(max_digits=14, decimal_places=2)
    earning_courses = serializers.IntegerField()
    share_pct = serializers.DecimalField(max_digits=5, decimal_places=2)
    platform_fee_pct = serializers.DecimalField(max_digits=5, decimal_places=2)
    currency = serializers.CharField()


class EarningsTrendSerializer(serializers.Serializer):
    """The twelve-month chart. Dense — every month, gaps as explicit zeros."""

    months = serializers.IntegerField()
    earnings = TrendPointSerializer(many=True)
    currency = serializers.CharField()


class EarningLineSerializer(serializers.ModelSerializer):
    """One line of the ledger — the evidence behind a total.

    ``share_pct`` is the rate **at the moment of sale**, not today's. That is
    the whole point of snapshotting it: a rate change in April must not restate
    what March paid.
    """

    course_title = serializers.CharField(
        source="course.title", read_only=True, default=""
    )
    payout_status = serializers.CharField(
        source="payout.status", read_only=True, default=""
    )

    class Meta:
        model = TrainerEarning
        fields = (
            "id", "order", "course", "course_title", "gross", "share_pct",
            "platform_fee", "net", "currency", "status", "payout",
            "payout_status", "earned_at", "reversed_at", "note",
        )
        read_only_fields = fields


class TrainerPayoutSerializer(serializers.ModelSerializer):
    """A payout as the trainer sees it. ``pending`` means owed, not sent."""

    line_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = TrainerPayout
        fields = (
            "id", "period_start", "period_end", "gross", "platform_fee",
            "net", "status", "paid_at", "line_count", "created_at",
        )
        read_only_fields = fields


class BankAccountSerializer(serializers.ModelSerializer):
    """The "Bank account on file" card. Never a full account number."""

    bank_name = serializers.CharField(source="payout_bank_name", read_only=True)
    account_last4 = serializers.CharField(
        source="payout_account_last4", read_only=True
    )
    account_type = serializers.CharField(
        source="payout_account_type", read_only=True
    )
    account_holder = serializers.CharField(
        source="payout_account_holder", read_only=True
    )
    is_set = serializers.SerializerMethodField()

    class Meta:
        model = TrainerProfile
        fields = (
            "bank_name", "account_last4", "account_type", "account_holder",
            "is_set",
        )
        read_only_fields = fields

    def get_is_set(self, obj) -> bool:
        """One flag, so the card doesn't guess from four possibly-blank strings."""
        return bool(obj.payout_bank_name and obj.payout_account_last4)


class BankAccountUpdateSerializer(serializers.Serializer):
    """Body of ``PUT /trainer/earnings/bank-account/``.

    Takes the **last four digits only**. A full account number is refused, not
    truncated — silently keeping four digits of a number someone believed they
    had registered is worse than telling them we do not take it.
    """

    bank_name = serializers.CharField(max_length=120)
    account_last4 = serializers.CharField(max_length=4, min_length=4)
    account_type = serializers.CharField(max_length=20, allow_blank=True, default="")
    account_holder = serializers.CharField(max_length=255, allow_blank=True, default="")

    def validate_account_last4(self, value):
        if not value.isdigit():
            raise serializers.ValidationError(
                "Enter the last four digits of the account number."
            )
        return value
