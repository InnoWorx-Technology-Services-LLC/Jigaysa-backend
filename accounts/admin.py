from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.utils.translation import gettext_lazy as _

from accounts.models import (
    LearnerStats,
    LoginActivity,
    TrainerProfile,
    User,
    UserProfile,
)


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    """Email-based admin (no username field)."""

    ordering = ("-created_at",)
    list_display = ("email", "full_name", "role", "is_active", "is_staff", "created_at")
    list_filter = ("role", "is_active", "is_staff", "is_superuser")
    search_fields = ("email", "full_name", "phone")
    readonly_fields = ("created_at", "updated_at", "last_login")

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        (_("Personal info"), {"fields": ("full_name", "phone", "phone_verified")}),
        (_("Role & tenancy"), {"fields": ("role", "organization")}),
        (
            _("Permissions"),
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        (_("Important dates"), {"fields": ("last_login", "created_at", "updated_at")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "full_name", "role", "password1", "password2"),
            },
        ),
    )


@admin.register(LoginActivity)
class LoginActivityAdmin(admin.ModelAdmin):
    list_display = ("email_attempted", "user", "success", "ip_address", "created_at")
    list_filter = ("success",)
    search_fields = ("email_attempted", "ip_address")
    readonly_fields = (
        "user",
        "email_attempted",
        "ip_address",
        "user_agent",
        "success",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False


@admin.register(TrainerProfile)
class TrainerProfileAdmin(admin.ModelAdmin):
    """Approve trainers as bookable mentors (PRD §2.1 trainer onboarding).

    ``is_approved`` is the gate ``GET /mentors/`` filters on — an unapproved
    trainer is invisible to students looking for a 1:1 session. Tick it here or
    use ``POST /api/v1/trainer-profiles/{id}/approve/``.
    """

    list_display = (
        "user", "expertise", "years_experience", "hourly_rate",
        "rating_avg", "is_approved",
    )
    list_editable = ("is_approved", "hourly_rate")
    list_filter = ("is_approved",)
    search_fields = ("user__email", "user__full_name", "expertise")
    autocomplete_fields = ("user",)

    @admin.action(description="Approve as bookable mentor")
    def approve(self, request, queryset):
        updated = queryset.update(is_approved=True)
        self.message_user(request, f"Approved {updated} trainer(s).")

    @admin.action(description="Remove mentor approval")
    def unapprove(self, request, queryset):
        updated = queryset.update(is_approved=False)
        self.message_user(request, f"Unapproved {updated} trainer(s).")

    actions = ["approve", "unapprove"]


admin.site.register([UserProfile, LearnerStats])
