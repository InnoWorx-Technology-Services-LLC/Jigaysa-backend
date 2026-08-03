"""Payments routes (PRD §3.3, §3.4, §3.13). Mounted at ``/api/v1/``."""

from django.urls import path
from rest_framework.routers import DefaultRouter

from payments import views, webhooks

app_name = "payments"

router = DefaultRouter()
router.register("pricing-plans", views.PricingPlanViewSet, basename="pricing-plan")
router.register("course-prices", views.CoursePriceViewSet, basename="course-price")
router.register("coupons", views.CouponViewSet, basename="coupon")
router.register("payment-methods", views.PaymentMethodViewSet, basename="payment-method")
router.register("orders", views.OrderViewSet, basename="order")
router.register("invoices", views.InvoiceViewSet, basename="invoice")
router.register("subscriptions", views.SubscriptionViewSet, basename="subscription")

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
    *router.urls,
]
