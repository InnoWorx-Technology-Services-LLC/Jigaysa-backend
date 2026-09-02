"""The admin console's **Payments** page (PRD §3.13).

Four counters, a platform-wide transaction table, refund management, and the
trainer payout queue.

Kept out of ``payments.views`` because of one hard rule that module enforces and
this one deliberately breaks: **``OrderViewSet`` scopes every read to
``request.user``**, including for admins. That is correct there — it is a
student's own billing history — but it means an admin using those endpoints sees
their own three test orders and concludes the platform has no revenue. Rather
than weaken the scoping on a student-facing endpoint with an `if admin` branch,
the platform-wide reads live here, behind `IsAdmin`, where the wide scope is the
whole point and cannot leak into a student's request by accident.

Every list is paginated. A transaction table is the one screen guaranteed to
outgrow its page — it gains a row per payment attempt for ever, including the
failures — so returning it whole is not a thing that works for six months and
then stops; it is a thing that works in staging and never in production.
"""

from datetime import datetime, time, timedelta

from django.db.models import Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_date
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from core.permissions import IsAdmin
from payments import services
from payments.models import Payment, Refund, TrainerPayout
from payments.serializers import (
    AdminPaymentSerializer,
    AdminPayoutSerializer,
    AdminRefundSerializer,
    PaymentSummarySerializer,
    RefundCreateSerializer,
)


def _date_window(request, queryset, field="created_at"):
    """Apply optional ``?from=`` / ``?to=`` (inclusive) to a queryset.

    Bad dates are ignored rather than rejected. This filter exists to narrow a
    report, and a typo in a URL should show the unfiltered page, not a `400`
    where the numbers used to be.

    Compares **aware datetimes rather than using ``__date``**. A ``__date``
    lookup emits ``CONVERT_TZ`` on MySQL, which returns ``NULL`` unless the
    server's timezone tables are loaded — and a ``NULL`` comparison does not
    raise, it silently matches nothing. A date filter that quietly empties the
    table is worse than one that fails loudly, so this avoids needing that data
    load at all. ``to`` is inclusive, hence the half-open bound on the next day.
    """
    tz = timezone.get_current_timezone()
    start = parse_date(request.query_params.get("from", "") or "")
    end = parse_date(request.query_params.get("to", "") or "")
    if start:
        queryset = queryset.filter(
            **{
                f"{field}__gte": timezone.make_aware(
                    datetime.combine(start, time.min), tz
                )
            }
        )
    if end:
        queryset = queryset.filter(
            **{
                f"{field}__lt": timezone.make_aware(
                    datetime.combine(end + timedelta(days=1), time.min), tz
                )
            }
        )
    return queryset


class AdminPaymentViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    """Every payment attempt on the platform, newest first."""

    serializer_class = AdminPaymentSerializer
    permission_classes = [IsAdmin]
    api_roles = ("admin",)

    def get_queryset(self):
        queryset = (
            Payment.objects.select_related("order", "order__user")
            .prefetch_related("refunds")
            .order_by("-created_at")
        )

        state = self.request.query_params.get("status", "").strip()
        if state in dict(Payment.Status.choices):
            queryset = queryset.filter(status=state)

        gateway = self.request.query_params.get("gateway", "").strip()
        if gateway in dict(Payment.Gateway.choices):
            queryset = queryset.filter(gateway=gateway)

        search = self.request.query_params.get("search", "").strip()
        if search:
            queryset = queryset.filter(
                Q(order__user__email__icontains=search)
                | Q(order__user__full_name__icontains=search)
                | Q(gateway_payment_id__icontains=search)
                | Q(gateway_order_id__icontains=search)
            )

        return _date_window(self.request, queryset)

    @extend_schema(
        parameters=[
            OpenApiParameter("status", str, OpenApiParameter.QUERY,
                             description="created | success | failed"),
            OpenApiParameter("gateway", str, OpenApiParameter.QUERY,
                             description="razorpay | stripe | paypal | upi"),
            OpenApiParameter("search", str, OpenApiParameter.QUERY,
                             description="Payer email/name, or a gateway id."),
            OpenApiParameter("from", str, OpenApiParameter.QUERY,
                             description="YYYY-MM-DD, inclusive."),
            OpenApiParameter("to", str, OpenApiParameter.QUERY,
                             description="YYYY-MM-DD, inclusive."),
        ],
        responses=AdminPaymentSerializer(many=True),
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(responses=PaymentSummarySerializer)
    @action(detail=False, methods=["get"])
    def summary(self, request):
        """Gross, refunds, pending and net — the four tiles.

        **Computed over the same filtered window as the table**, so changing the
        date range moves the tiles with it. Tiles that ignore the filter beside
        a table that honours it is the classic dashboard lie.

        The definitions, stated because "revenue" is ambiguous enough to be
        worth pinning down:

        * **gross** — captured payments (`success`). What actually arrived.
        * **refunds** — refunds in state `processed`. Money genuinely returned;
          a `requested` refund is an intention, not a movement.
        * **pending** — payments still `created`. Started, not yet captured.
          Most of these will end up abandoned rather than paid, so it is a
          measure of exposure, not of expected income.
        * **net** — gross − refunds.
        """
        payments = self.get_queryset()
        gross = payments.filter(status=Payment.Status.SUCCESS).aggregate(
            total=Sum("amount")
        )["total"] or 0
        pending = payments.filter(status=Payment.Status.CREATED).aggregate(
            total=Sum("amount")
        )["total"] or 0
        refunded = Refund.objects.filter(
            payment__in=payments, status=Refund.Status.PROCESSED
        ).aggregate(total=Sum("amount"))["total"] or 0

        return Response(
            PaymentSummarySerializer(
                {
                    "gross_volume": gross,
                    "refunds": refunded,
                    "pending": pending,
                    "net": gross - refunded,
                    "currency": services.platform_currency(),
                }
            ).data
        )


class AdminRefundViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """The refund queue, and the endpoint that starts one."""

    permission_classes = [IsAdmin]
    api_roles = ("admin",)

    def get_serializer_class(self):
        if self.action == "create":
            return RefundCreateSerializer
        return AdminRefundSerializer

    def get_queryset(self):
        queryset = (
            Refund.objects.select_related(
                "payment", "payment__order", "payment__order__user"
            )
            .order_by("-created_at")
        )
        state = self.request.query_params.get("status", "").strip()
        if state in dict(Refund.Status.choices):
            queryset = queryset.filter(status=state)
        return _date_window(self.request, queryset)

    @extend_schema(request=RefundCreateSerializer, responses=AdminRefundSerializer)
    def create(self, request, *args, **kwargs):
        """Refund a payment, in whole or in part.

        The row is written **before** the gateway is called and survives the
        call failing — a refund we owe is a fact about our books, not about
        whether Razorpay answered. A failed attempt lands in `failed` with the
        reason in `raw`, where `manage.py retry_refunds` can pick it up, rather
        than vanishing and leaving an admin sure they refunded someone.
        """
        body = RefundCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        refund = services.refund_payment(
            payment=body.validated_data["payment"],
            # ``.get``: the field is optional, and omitting it means "refund
            # the remainder" — which the service decides, not the view.
            amount=body.validated_data.get("amount"),
            reason=body.validated_data.get("reason", ""),
        )
        return Response(
            AdminRefundSerializer(refund).data, status=status.HTTP_201_CREATED
        )


class AdminPayoutViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    """The trainer payout queue.

    > **Read-only, and nothing computes these rows yet.** ``TrainerPayout`` is
    > populated by no code path in this release — the revenue-share percentage
    > is recorded on trainer profiles, but nothing turns settled orders into
    > payout rows. This endpoint reads a table that is empty in production, and
    > says so rather than implying a queue that is merely quiet.
    """

    serializer_class = AdminPayoutSerializer
    permission_classes = [IsAdmin]
    api_roles = ("admin",)

    def get_queryset(self):
        queryset = TrainerPayout.objects.select_related("trainer").order_by(
            "status", "-period_end", "-created_at"
        )
        state = self.request.query_params.get("status", "").strip()
        if state in dict(TrainerPayout.Status.choices):
            queryset = queryset.filter(status=state)
        return _date_window(self.request, queryset)
