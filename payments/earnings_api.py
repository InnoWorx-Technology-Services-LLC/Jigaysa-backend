"""The trainer's **Earnings** page (PRD §3.3, §3.14).

Four tiles, a twelve-month chart, the revenue split, the bank account on file,
and the payout list.

Everything reads the ledger in ``payments.earnings`` and is **scoped to the
caller**. There is no parameter to widen it; the platform-wide view is the
admin-only ``/admin/payouts/``.

One thing to be honest about in the UI: a payout row means *recorded as owed*,
not *sent*. There is no payout processor integration, so nothing here moves
money — see ``TrainerPayoutListView``.
"""

from decimal import Decimal

from django.db.models import Avg, Count, Q, Sum
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from analytics.views import (
    DEFAULT_MONTHS,
    MAX_MONTHS,
    _month_bounds,
    _month_series,
    _months_param,
)
from core.pagination import DefaultPagination
from core.permissions import HasRole
from payments import earnings as earnings_service
from payments import services
from payments.models import TrainerEarning, TrainerPayout
from payments.serializers import (
    BankAccountSerializer,
    BankAccountUpdateSerializer,
    EarningLineSerializer,
    EarningsSummarySerializer,
    EarningsTrendSerializer,
    TrainerPayoutSerializer,
)

TRAINER_OR_ADMIN = HasRole("trainer", "admin")


def _mine(user):
    """This trainer's ledger, reversals excluded.

    A reversed line is kept for the audit trail and must never reach a total —
    counting refunded money as earned is the one arithmetic error on this page
    that a trainer would notice and never trust us again over.
    """
    return TrainerEarning.objects.filter(
        trainer=user, status=TrainerEarning.Status.PENDING
    )


class EarningsSummaryView(APIView):
    """GET ``/trainer/earnings/summary/`` — the four tiles and the split."""

    permission_classes = [TRAINER_OR_ADMIN]
    serializer_class = EarningsSummarySerializer
    api_roles = ("trainer", "admin")

    @extend_schema(responses=EarningsSummarySerializer)
    def get(self, request):
        ledger = _mine(request.user)
        bounds = _month_bounds(1)
        month_start = bounds[0][1]

        totals = ledger.aggregate(
            lifetime=Sum("net"),
            this_month=Sum("net", filter=Q(earned_at__gte=month_start)),
            pending=Sum(
                "net",
                filter=Q(payout__isnull=True)
                | Q(payout__status=TrainerPayout.Status.PENDING),
            ),
        )
        lifetime = totals["lifetime"] or Decimal("0")

        # Averaged over courses that actually earned, not over every course the
        # trainer has ever published. Dividing by courses with no sales
        # measures how much they publish, not how much they earn.
        earning_courses = (
            ledger.exclude(course__isnull=True)
            .values("course")
            .distinct()
            .count()
        )

        share = earnings_service._share_pct(request.user)
        return Response(
            EarningsSummarySerializer(
                {
                    "this_month": totals["this_month"] or Decimal("0"),
                    "lifetime": lifetime,
                    "pending_payout": totals["pending"] or Decimal("0"),
                    # Null, not zero, when nothing has earned yet — the same
                    # rule the assignment stats follow. "No average" and "an
                    # average of nothing" are different statements about
                    # somebody's income.
                    "average_per_course": (
                        services.money(lifetime / earning_courses)
                        if earning_courses
                        else None
                    ),
                    "earning_courses": earning_courses,
                    "share_pct": share,
                    "platform_fee_pct": Decimal("100") - share,
                    "currency": services.platform_currency(),
                }
            ).data
        )


class EarningsTrendView(APIView):
    """GET ``/trainer/earnings/trend/`` — the "Earnings (last 12 months)" chart.

    Dense: every month in the window, gaps as explicit zeros. A sparse series
    charted directly runs a line straight over a month with no sales and hides
    it — see ``analytics.views._month_series``.
    """

    permission_classes = [TRAINER_OR_ADMIN]
    serializer_class = EarningsTrendSerializer
    api_roles = ("trainer", "admin")

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "months", int, OpenApiParameter.QUERY,
                description=f"1–{MAX_MONTHS}, default {DEFAULT_MONTHS}.",
            )
        ],
        responses=EarningsTrendSerializer,
    )
    def get(self, request):
        months = _months_param(request)
        bounds = _month_bounds(months)
        ledger = _mine(request.user).filter(
            earned_at__gte=bounds[0][1], earned_at__lt=bounds[-1][2]
        )

        return Response(
            EarningsTrendSerializer(
                {
                    "months": months,
                    "earnings": _month_series(
                        ledger,
                        bounds,
                        lambda start, end: Sum(
                            "net",
                            filter=Q(earned_at__gte=start, earned_at__lt=end),
                        ),
                    ),
                    "currency": services.platform_currency(),
                }
            ).data
        )


class EarningsLedgerView(APIView):
    """GET ``/trainer/earnings/`` — the individual lines behind the totals.

    **Paginated.** One row per sale, for the life of the account.

    Not in the mock, but a trainer who disagrees with a total needs somewhere
    to look, and "trust the number" is not an answer about someone's income.
    Reversed lines are **included here** — excluded from totals, visible in the
    ledger, which is the only combination that lets a refund be explained.
    """

    permission_classes = [TRAINER_OR_ADMIN]
    serializer_class = EarningLineSerializer
    api_roles = ("trainer", "admin")

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "status", str, OpenApiParameter.QUERY,
                description="pending | reversed",
            ),
            OpenApiParameter("course", int, OpenApiParameter.QUERY),
        ],
        responses=EarningLineSerializer(many=True),
    )
    def get(self, request):
        ledger = TrainerEarning.objects.filter(
            trainer=request.user
        ).select_related("course", "order", "payout")

        state = request.query_params.get("status", "").strip()
        if state in dict(TrainerEarning.Status.choices):
            ledger = ledger.filter(status=state)
        course = request.query_params.get("course", "").strip()
        if course.isdigit():
            ledger = ledger.filter(course_id=int(course))

        paginator = DefaultPagination()
        page = paginator.paginate_queryset(ledger, request, view=self)
        return paginator.get_paginated_response(
            EarningLineSerializer(page, many=True).data
        )


class TrainerPayoutListView(APIView):
    """GET ``/trainer/earnings/payouts/`` — the Payouts list.

    **Paginated**, newest first.

    > ### A payout row means "owed", not "sent"
    >
    > There is no payout processor integration. `generate_trainer_payouts`
    > records what the platform owes and for which period; nothing transfers
    > money, and a payout only becomes `paid` when someone marks it so after
    > paying by other means.
    >
    > Label the status honestly. "Payout scheduled" is true; "Paid on 30 Jun"
    > is not, unless the row says `paid`.
    """

    permission_classes = [TRAINER_OR_ADMIN]
    serializer_class = TrainerPayoutSerializer
    api_roles = ("trainer", "admin")

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "status", str, OpenApiParameter.QUERY,
                description="pending | paid",
            )
        ],
        responses=TrainerPayoutSerializer(many=True),
    )
    def get(self, request):
        payouts = TrainerPayout.objects.filter(trainer=request.user).annotate(
            line_count=Count("earnings")
        ).order_by("-period_end", "-created_at")

        state = request.query_params.get("status", "").strip()
        if state in dict(TrainerPayout.Status.choices):
            payouts = payouts.filter(status=state)

        paginator = DefaultPagination()
        page = paginator.paginate_queryset(payouts, request, view=self)
        return paginator.get_paginated_response(
            TrainerPayoutSerializer(page, many=True).data
        )


class BankAccountView(APIView):
    """GET/PUT ``/trainer/earnings/bank-account/`` — "Bank account on file".

    > ### This records where you *say* payouts should go
    >
    > It does not connect to a bank, and it deliberately **does not accept a
    > full account number**. Storing one means holding a payout instrument —
    > encryption at rest, an access trail, a breach story — and there is no
    > processor to hand it to, so the only thing storing it would achieve is
    > the liability.
    >
    > The four fields here are exactly what the card renders. When a payout
    > processor is integrated, the real number is registered there and its token
    > lands in ``payout_account_ref``.
    >
    > Say this in the UI. A trainer who believes they have connected a bank
    > account, and has not, finds out at the worst possible moment.
    """

    permission_classes = [TRAINER_OR_ADMIN]
    serializer_class = BankAccountSerializer
    api_roles = ("trainer", "admin")

    def _profile(self, request):
        from accounts.models import TrainerProfile

        profile, _ = TrainerProfile.objects.get_or_create(user=request.user)
        return profile

    @extend_schema(responses=BankAccountSerializer)
    def get(self, request):
        return Response(BankAccountSerializer(self._profile(request)).data)

    @extend_schema(
        request=BankAccountUpdateSerializer, responses=BankAccountSerializer
    )
    def put(self, request):
        profile = self._profile(request)
        body = BankAccountUpdateSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        for field, value in body.validated_data.items():
            setattr(profile, f"payout_{field}", value)
        profile.save(
            update_fields=[
                "payout_bank_name", "payout_account_last4",
                "payout_account_type", "payout_account_holder", "updated_at",
            ]
        )
        return Response(
            BankAccountSerializer(profile).data, status=status.HTTP_200_OK
        )

    @extend_schema(responses=BankAccountSerializer)
    def delete(self, request):
        """Clear the bank details.

        ``PUT`` requires a bank name and four digits, which is right for setting
        one and wrong for removing one — it left the card write-once, with no way
        for a trainer to take their details off the platform. Deleting is its own
        verb rather than a ``PUT`` of blanks, so "remove this" cannot happen by
        accident from a half-filled form.

        Returns the now-empty card (``200``) rather than ``204``, so the page can
        re-render from the response instead of refetching.
        """
        profile = self._profile(request)
        profile.payout_bank_name = ""
        profile.payout_account_last4 = ""
        profile.payout_account_type = ""
        profile.payout_account_holder = ""
        profile.save(
            update_fields=[
                "payout_bank_name", "payout_account_last4",
                "payout_account_type", "payout_account_holder", "updated_at",
            ]
        )
        return Response(
            BankAccountSerializer(profile).data, status=status.HTTP_200_OK
        )
