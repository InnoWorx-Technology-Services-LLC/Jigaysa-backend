"""Payments routes (PRD §3.3, §3.4, §3.13). Mounted at ``/api/v1/``."""

from django.urls import path
from rest_framework.routers import DefaultRouter

from payments import admin_api, earnings_api, views, webhooks

app_name = "payments"

router = DefaultRouter()
router.register("pricing-plans", views.PricingPlanViewSet, basename="pricing-plan")
router.register("course-prices", views.CoursePriceViewSet, basename="course-price")
router.register("coupons", views.CouponViewSet, basename="coupon")
router.register("payment-methods", views.PaymentMethodViewSet, basename="payment-method")
router.register("orders", views.OrderViewSet, basename="order")
router.register("invoices", views.InvoiceViewSet, basename="invoice")
router.register("subscriptions", views.SubscriptionViewSet, basename="subscription")

# Admin console. Platform-wide reads, deliberately separate from the
# student-scoped resources above — see payments.admin_api.
router.register(
    "admin/payments", admin_api.AdminPaymentViewSet, basename="admin-payment"
)
router.register(
    "admin/refunds", admin_api.AdminRefundViewSet, basename="admin-refund"
)
router.register(
    "admin/payouts", admin_api.AdminPayoutViewSet, basename="admin-payout"
)

urlpatterns = [
    # Gateway → server callback. Unauthenticated by design (verified by HMAC).
    path(
        "payments/webhook/razorpay/",
        webhooks.RazorpayWebhookView.as_view(),
        name="razorpay-webhook",
    ),
    path(
        "billing/summary/",
        views.BillingSummaryView.as_view(),
        name="billing-summary",
    ),
    # --- The trainer's Earnings page (payments.earnings_api) -------------- #
    path(
        "trainer/earnings/summary/",
        earnings_api.EarningsSummaryView.as_view(),
        name="earnings-summary",
    ),
    path(
        "trainer/earnings/trend/",
        earnings_api.EarningsTrendView.as_view(),
        name="earnings-trend",
    ),
    path(
        "trainer/earnings/payouts/",
        earnings_api.TrainerPayoutListView.as_view(),
        name="earnings-payouts",
    ),
    path(
        "trainer/earnings/bank-account/",
        earnings_api.BankAccountView.as_view(),
        name="earnings-bank-account",
    ),
    path(
        "trainer/earnings/",
        earnings_api.EarningsLedgerView.as_view(),
        name="earnings-ledger",
    ),
    *router.urls,
]
