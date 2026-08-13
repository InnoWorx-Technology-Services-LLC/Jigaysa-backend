from django.contrib import admin

from core.models import Organization, PlatformSetting


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "type", "is_active", "created_at")
    list_filter = ("type", "is_active")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(PlatformSetting)
class PlatformSettingAdmin(admin.ModelAdmin):
    """The singleton settings row.

    Add and delete are switched off: there is exactly one row, seeded by a
    migration, and a second one would make ``get_solo`` arbitrary.
    """

    fieldsets = (
        (None, {"fields": ("platform_name", "support_email", "default_currency")}),
        (
            "Payments & tax",
            {
                "fields": ("gst_percent", "platform_commission_percent"),
                "description": (
                    "GST applies to carts priced from now on — orders already "
                    "placed keep the rate they were quoted at. "
                    "<b>Platform commission is stored but unused</b>: no payout "
                    "is computed anywhere yet."
                ),
            },
        ),
        (
            "Razorpay credentials",
            {
                "fields": (
                    "razorpay_key_id",
                    "razorpay_key_secret",
                    "razorpay_webhook_secret",
                ),
                "description": (
                    "Overrides the <code>RAZORPAY_*</code> environment "
                    "variables the moment they are set, with no restart. Leave "
                    "a field blank to keep using the environment value."
                ),
            },
        ),
        (
            "Feature flags",
            {
                "fields": (
                    "course_approval_required",
                    "trainer_self_onboarding",
                    "allow_coupon_codes",
                    "smart_classroom_module",
                    "ai_suggestions",
                    "container_classrooms",
                ),
                "description": (
                    "Only the first three change behaviour. The rest are stored "
                    "and returned by the API, but those modules do not exist yet."
                ),
            },
        ),
    )

    def has_add_permission(self, request):
        return not PlatformSetting.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False
