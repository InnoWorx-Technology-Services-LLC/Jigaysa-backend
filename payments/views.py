"""Payments API: pricing, checkout, invoices and subscriptions (§3.3/3.4/3.13).

Student flow: read plans/prices → ``POST /orders/`` (server prices the cart) →
``POST /orders/{id}/pay/`` (confirms payment, issues the invoice and grants the
paid course/subscription). Coupons can be previewed before checkout. Every
read is scoped to the current user; plans/prices/coupons are admin-authored.
"""

from decimal import Decimal

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import IsAdmin
from payments import entitlements, gateway, services
from payments.models import (
    CURRENCY_DEFAULT,
    Coupon,
    CoursePrice,
    Invoice,
    Order,
    PaymentMethod,
    PricingPlan,
    Subscription,
)
from payments.serializers import (
    BillingSummarySerializer,
    CheckoutSerializer,
    CouponSerializer,
    CouponValidateSerializer,
    CoursePriceSerializer,
    InvoiceSerializer,
    OrderSerializer,
    PaymentMethodSerializer,
    PaySerializer,
    PricingPlanSerializer,
    RazorpayCheckoutSerializer,
    RazorpayVerifySerializer,
    SubscriptionSerializer,
)

ALL_ROLES = ("student", "trainer", "admin", "institution")
ADMIN_ONLY = ("admin",)


def _filter_by(qs, request, param, field=None):
    value = request.query_params.get(param)
    if value:
        qs = qs.filter(**{field or param: value})
    return qs


class PricingPlanViewSet(viewsets.ModelViewSet):
    """Platform-access plans (PRD §3.4). Anyone reads; admins author."""

    queryset = PricingPlan.objects.all()
    serializer_class = PricingPlanSerializer
    api_roles = ALL_ROLES
    api_roles_by_action = {
        "create": ADMIN_ONLY, "update": ADMIN_ONLY,
        "partial_update": ADMIN_ONLY, "destroy": ADMIN_ONLY,
    }

    def get_queryset(self):
        qs = PricingPlan.objects.all()
        if self.action == "list" and self.request.query_params.get("active") != "all":
            qs = qs.filter(is_active=True)
        return qs

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated()]
        return [IsAdmin()]


class CoursePriceViewSet(viewsets.ModelViewSet):
    """Course price options (PRD §3.3). Filter by ``?course=<id>``."""

    serializer_class = CoursePriceSerializer
    api_roles = ALL_ROLES
    api_roles_by_action = {
        "create": ("trainer", "admin"), "update": ("trainer", "admin"),
        "partial_update": ("trainer", "admin"), "destroy": ("trainer", "admin"),
    }

    def get_permissions(self):
        return [IsAuthenticated()]

    def get_queryset(self):
        qs = CoursePrice.objects.select_related("course")
        return _filter_by(qs, self.request, "course", "course_id")


class CouponViewSet(viewsets.ModelViewSet):
    """Coupons (PRD §3.3). Admin-managed; students use ``validate`` to preview a
    discount for their cart before checkout."""

    queryset = Coupon.objects.all()
    serializer_class = CouponSerializer
    api_roles = ADMIN_ONLY
    api_roles_by_action = {"validate": ALL_ROLES}

    def get_permissions(self):
        if self.action == "validate":
            return [IsAuthenticated()]
        return [IsAdmin()]

    @action(detail=False, methods=["post"])
    def validate(self, request):
        payload = CouponValidateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        q = services.quote(
            payload.validated_data["items"],
            payload.validated_data["code"],
            user=request.user,
        )
        return Response(
            {
                "code": q["coupon"].code,
                "subtotal": q["subtotal"],
                "discount": q["discount"],
                "tax_gst": q["tax_gst"],
                "total": q["total"],
            }
        )


class PaymentMethodViewSet(viewsets.ModelViewSet):
    """The current user's saved payment methods."""

    serializer_class = PaymentMethodSerializer
    permission_classes = [IsAuthenticated]
    api_roles = ALL_ROLES

    def get_queryset(self):
        return PaymentMethod.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        method = serializer.save(user=self.request.user)
        if method.is_default:
            PaymentMethod.objects.filter(user=self.request.user).exclude(
                pk=method.pk
            ).update(is_default=False)


class OrderViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """Checkout orders. Students see only their own.

    Razorpay flow: ``POST /orders/`` (prices the cart server-side) →
    ``POST /orders/{id}/checkout/`` (creates the gateway order) → open Razorpay
    Checkout in the browser → ``POST /orders/{id}/verify/`` with the handler
    payload. The ``payment.captured`` webhook settles the order independently,
    so a closed browser doesn't lose a paid enrollment.
    """

    permission_classes = [IsAuthenticated]
    api_roles = ALL_ROLES

    def get_serializer_class(self):
        if self.action == "create":
            return CheckoutSerializer
        if self.action == "checkout":
            return RazorpayCheckoutSerializer
        if self.action == "verify":
            return RazorpayVerifySerializer
        return OrderSerializer

    def get_queryset(self):
        return (
            Order.objects.filter(user=self.request.user)
            .prefetch_related("items", "payments")
            .select_related("coupon")
        )

    def create(self, request, *args, **kwargs):
        payload = CheckoutSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        order = services.create_order(
            user=request.user,
            items=payload.validated_data["items"],
            coupon_code=payload.validated_data.get("coupon_code") or None,
        )
        return Response(OrderSerializer(order).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        summary="Open Razorpay Checkout for this order",
        description=(
            "Creates a Razorpay order and returns the parameters to pass to "
            "`Razorpay(options)` in the browser. Safe to call again — an "
            "unpaid order reuses its existing gateway order rather than "
            "creating a duplicate.\n\n"
            "`amount` is in **paise**; `key` is the public key id."
        ),
        responses=RazorpayCheckoutSerializer,
        request=None,
    )
    @action(detail=True, methods=["post"])
    def checkout(self, request, pk=None):
        order = self.get_object()
        if not gateway.is_configured():
            return Response(
                {"detail": "Payment gateway is not configured on this server."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        try:
            payment, rzp_order = services.start_checkout(order)
        except gateway.GatewayError as exc:
            return Response(
                {"detail": f"Could not reach the payment gateway: {exc}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        user = request.user
        return Response(
            {
                "key": settings.RAZORPAY_KEY_ID,
                "razorpay_order_id": payment.gateway_order_id,
                "amount": rzp_order.get("amount")
                or gateway.to_minor_units(order.total),
                "amount_display": order.total,
                "currency": order.currency,
                "name": settings.RAZORPAY_CHECKOUT_NAME,
                "description": ", ".join(i.title for i in order.items.all())[:255],
                "image": settings.RAZORPAY_CHECKOUT_LOGO,
                "order_id": order.pk,
                "prefill": {
                    "name": user.full_name or "",
                    "email": user.email,
                    "contact": user.phone or "",
                },
                "notes": {"order_id": str(order.pk)},
                "callback_url": f"{settings.FRONTEND_URL.rstrip('/')}/orders/{order.pk}",
                "is_test_mode": gateway.is_test_mode(),
            }
        )

    @extend_schema(
        summary="Verify a Razorpay payment and fulfil the order",
        description=(
            "Post the payload Razorpay Checkout's `handler` gives you. The "
            "signature is verified server-side, the payment is re-fetched from "
            "Razorpay, and the amount is checked against the order total before "
            "anything is granted. Idempotent — if the webhook already settled "
            "the order this returns the paid order unchanged."
        ),
        responses=OrderSerializer,
    )
    @action(detail=True, methods=["post"])
    def verify(self, request, pk=None):
        order = self.get_object()
        payload = RazorpayVerifySerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        try:
            services.confirm_checkout(
                order,
                razorpay_order_id=data["razorpay_order_id"],
                razorpay_payment_id=data["razorpay_payment_id"],
                signature=data["razorpay_signature"],
            )
        except gateway.SignatureMismatch:
            # Deliberately does NOT mark the payment failed: a bad signature
            # means this *request* was garbage, not that the payment failed.
            # Burning the row here would make the next checkout/ mint a second
            # gateway order, and a real in-flight payment could then be paid
            # twice against one fulfilment. Only the payment.failed webhook is
            # authoritative about failure.
            return Response(
                {"detail": "Payment signature verification failed."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except gateway.GatewayNotConfigured:
            return Response(
                {"detail": "Payment gateway is not configured on this server."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except gateway.GatewayError as exc:
            return Response(
                {"detail": f"Could not reach the payment gateway: {exc}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        order.refresh_from_db()
        return Response(OrderSerializer(order).data)

    @extend_schema(
        summary="Confirm an order without a gateway (dev/test only)",
        description=(
            "⚠️ Mock confirmation: marks the order paid, issues the invoice and "
            "grants access with no money involved. Returns **409** when Razorpay "
            "keys are configured — use `checkout/` + `verify/` instead. "
            "Idempotent."
        ),
    )
    @action(detail=True, methods=["post"])
    def pay(self, request, pk=None):
        if gateway.is_configured():
            return Response(
                {
                    "detail": (
                        "Mock payment is disabled because a real gateway is "
                        "configured. Use POST /orders/{id}/checkout/ then "
                        "POST /orders/{id}/verify/."
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )
        order = self.get_object()
        payload = PaySerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        method = None
        if payload.validated_data.get("payment_method_id"):
            method = PaymentMethod.objects.filter(
                pk=payload.validated_data["payment_method_id"], user=request.user
            ).first()
        services.pay_order(
            order,
            gateway_name=payload.validated_data.get("gateway", "mock"),
            payment_method=method,
            gateway_payment_id=payload.validated_data.get("gateway_payment_id", ""),
        )
        order.refresh_from_db()
        return Response(OrderSerializer(order).data)


class InvoiceViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    """The current user's GST invoices (PRD §3.13)."""

    serializer_class = InvoiceSerializer
    permission_classes = [IsAuthenticated]
    api_roles = ALL_ROLES

    def get_queryset(self):
        return Invoice.objects.filter(user=self.request.user).select_related("order")


class SubscriptionViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    """The current user's platform subscriptions. ``cancel/`` ends auto-renew."""

    serializer_class = SubscriptionSerializer
    permission_classes = [IsAuthenticated]
    api_roles = ALL_ROLES

    def get_queryset(self):
        return Subscription.objects.filter(user=self.request.user).select_related("plan")

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        """End the subscription at the end of the period already paid for.

        Cancelling does not revoke access mid-period — the student paid for
        those days. ``cancel_at`` records when it lapses; the entitlement check
        stops granting anything once ``current_period_end`` passes.
        """
        subscription = self.get_object()
        subscription.cancel_at = subscription.current_period_end or timezone.now()
        subscription.status = Subscription.Status.CANCELLED
        subscription.save(update_fields=["status", "cancel_at", "updated_at"])
        return Response(self.get_serializer(subscription).data)

    @action(detail=False, methods=["get"])
    def current(self, request):
        """The active subscription and what it unlocks, or ``null`` when free."""
        subscription = entitlements.active_subscription(request.user)
        return Response(
            {
                "subscription": (
                    self.get_serializer(subscription).data if subscription else None
                ),
                "entitlements": entitlements.entitlements_for(request.user),
            }
        )


class BillingSummaryView(APIView):
    """GET /api/v1/billing/summary/ — one call for the whole Billing page.

    Total spent, the active plan (``null`` = the free tier), what it unlocks,
    and how many invoices exist.
    """

    permission_classes = [IsAuthenticated]
    api_roles = ALL_ROLES

    @extend_schema(responses=BillingSummarySerializer)
    def get(self, request):
        user = request.user
        paid = Order.objects.filter(user=user, status=Order.Status.PAID)
        total_spent = paid.aggregate(total=Sum("total"))["total"] or Decimal("0.00")
        subscription = entitlements.active_subscription(user)
        return Response(
            {
                "total_spent": total_spent,
                "currency": CURRENCY_DEFAULT,
                "active_plan": (
                    PricingPlanSerializer(subscription.plan).data
                    if subscription
                    else None
                ),
                "subscription": (
                    SubscriptionSerializer(subscription).data if subscription else None
                ),
                "entitlements": entitlements.entitlements_for(user),
                "invoice_count": Invoice.objects.filter(user=user).count(),
            }
        )
