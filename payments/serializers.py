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
    Subscription,
)


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
