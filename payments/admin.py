"""Admin for pricing, plans and billing (PRD §3.3, §3.4, §3.13).

``PricingPlanAdmin`` is where a plan is created and its features are ticked —
the boxes under "What subscribers get" are read by ``payments.entitlements`` at
request time, so a change takes effect on the next API call with no deploy.
"""

from django.contrib import admin

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
    TrainerPayout,
)


@admin.register(PricingPlan)
class PricingPlanAdmin(admin.ModelAdmin):
    list_display = (
        "name", "billing_period", "price", "includes_all_paid_courses",
        "includes_live_sessions", "includes_certificates", "priority_support",
        "is_active",
    )
    list_editable = (
        "includes_all_paid_courses", "includes_live_sessions",
        "includes_certificates", "priority_support", "is_active",
    )
    list_filter = ("billing_period", "is_active")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    fieldsets = (
        (None, {"fields": ("name", "slug", "is_active")}),
        ("Pricing", {"fields": ("billing_period", "price", "currency")}),
        (
            "What subscribers get",
            {
                "fields": (
                    "includes_all_paid_courses",
                    "includes_live_sessions",
                    "includes_certificates",
                    "priority_support",
                ),
                "description": (
                    "Only <b>all paid courses</b> is enforced by the API today. "
                    "The others are shown on the pricing card and returned by "
                    "<code>/billing/summary/</code>, but those features are "
                    "currently open to every student."
                ),
            },
        ),
        (
            "Marketing copy",
            {
                "fields": ("features",),
                "description": "Extra bullet points for the pricing card. "
                "Display only — these grant nothing.",
            },
        ),
    )


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "status", "total", "currency", "created_at")
    list_filter = ("status",)
    search_fields = ("user__email",)
    inlines = [OrderItemInline]


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = (
        "user", "plan", "status", "current_period_start", "current_period_end",
    )
    list_filter = ("status", "plan")
    search_fields = ("user__email",)


@admin.register(Refund)
class RefundAdmin(admin.ModelAdmin):
    """Unsent refunds are money owed — watch the ``requested`` list."""

    list_display = (
        "id", "payment", "amount", "status", "gateway_refund_id", "created_at",
    )
    list_filter = ("status",)
    search_fields = ("payment__gateway_payment_id", "reason")
    readonly_fields = ("raw",)


admin.site.register(
    [Coupon, CoursePrice, Invoice, Payment, PaymentMethod, TrainerPayout]
)
