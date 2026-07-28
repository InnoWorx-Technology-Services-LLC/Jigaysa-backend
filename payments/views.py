"""Payments API: pricing, checkout, invoices and subscriptions (§3.3/3.4/3.13).

Student flow: read plans/prices → ``POST /orders/`` (server prices the cart) →
``POST /orders/{id}/pay/`` (confirms payment, issues the invoice and grants the
paid course/subscription). Coupons can be previewed before checkout. Every
read is scoped to the current user; plans/prices/coupons are admin-authored.
"""

from django.conf import settings
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.permissions import IsAdmin
from payments import gateway, services
from payments.models import (
    Coupon,
    CoursePrice,
    Invoice,
    Order,
    PaymentMethod,
    PricingPlan,
    Subscription,
)
from payments.serializers import (
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
        q = services.quote(payload.validated_data["items"], payload.validated_data["code"])
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
            services.mark_failed(order, data["razorpay_order_id"])
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
        subscription = self.get_object()
        subscription.status = Subscription.Status.CANCELLED
        subscription.save(update_fields=["status", "updated_at"])
        return Response(self.get_serializer(subscription).data)
