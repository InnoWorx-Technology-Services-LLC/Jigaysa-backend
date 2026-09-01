from django.contrib import admin

from social.models import Campaign, CampaignPost, SocialAccount


@admin.register(SocialAccount)
class SocialAccountAdmin(admin.ModelAdmin):
    """Read-mostly. Connections are made through OAuth, not typed in.

    Tokens are excluded entirely — they decrypt on read, so putting them on a
    form would render a live publishing credential in a browser tab. Support
    staff need to see *that* an account is connected and why it broke, never
    the secret itself.
    """

    list_display = (
        "provider", "display_name", "handle", "user", "status", "updated_at",
    )
    list_filter = ("provider", "status")
    search_fields = ("display_name", "handle", "user__email", "provider_account_id")
    readonly_fields = (
        "user", "provider", "provider_account_id", "display_name", "handle",
        "avatar_url", "scopes", "token_expires_at", "provider_meta",
        "created_at", "updated_at",
    )
    fields = readonly_fields + ("status", "last_error")

    def has_add_permission(self, request):
        return False


class CampaignPostInline(admin.TabularInline):
    """The fan-out, read-only. Support needs to see which network refused and
    why; nothing here should be re-sent by hand — that is what Retry is for."""

    model = CampaignPost
    extra = 0
    can_delete = False
    readonly_fields = (
        "provider", "account_label", "status", "provider_post_id", "permalink",
        "attempts", "error", "published_at",
    )
    fields = readonly_fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    """Read-mostly, like the accounts above. Campaigns are made in the wizard.

    ``status`` stays editable so support can park a stuck campaign — setting it
    to ``cancelled`` is the only safe manual intervention, and it stops the
    sweep from picking the campaign up again.
    """

    list_display = (
        "id", "course", "trainer", "template", "status", "scheduled_for",
        "published_at",
    )
    list_filter = ("status", "template", "image_source")
    search_fields = ("course__title", "trainer__email", "caption")
    date_hierarchy = "created_at"
    inlines = [CampaignPostInline]
    readonly_fields = (
        "trainer", "course", "template", "caption", "hashtags", "image_source",
        "image_url", "link_url", "scheduled_for", "published_at", "created_at",
        "updated_at",
    )
    fields = readonly_fields + ("status",)

    def has_add_permission(self, request):
        return False
