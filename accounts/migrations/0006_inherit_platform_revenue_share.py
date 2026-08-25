"""Let trainers follow the platform commission instead of a frozen number.

``revenue_share_pct`` used to default to ``70`` and was read by nothing, so every
profile carries that number without anyone having chosen it — and it contradicts
a 20% platform commission, which implies 80. Now that null means "follow the
platform", those rows have to be cleared or they would pin every existing
trainer to a rate nobody agreed, permanently out of step with the default.

Cleared: rows at the shipped default (``70``), and rows exactly equal to the
current platform-derived share. Neither was ever a decision — the first is a
Django default, the second is what a trainer following the platform already
resolves to, so nulling it changes nothing today and lets them track the default
tomorrow.

Kept: every other value. A profile someone moved to 65 or 75 is a real
negotiated deal, and a platform-wide change must not silently overwrite it.

One knowingly-accepted edge: a rate negotiated at *exactly* the current platform
share is indistinguishable from an unset one and gets cleared. It resolves to
the same number today and only diverges if the platform default later moves —
re-pin that trainer if it matters.

Safe today because nothing has ever read the field: no trainer has been paid on
it, so no historical payout can be re-interpreted by the change.
"""

from decimal import Decimal

from django.db import migrations

SHIPPED_DEFAULT = Decimal("70.00")
FALLBACK_COMMISSION = Decimal("20.00")


def follow_platform_default(apps, schema_editor):
    TrainerProfile = apps.get_model("accounts", "TrainerProfile")
    PlatformSetting = apps.get_model("core", "PlatformSetting")

    row = PlatformSetting.objects.filter(pk=1).first()
    commission = row.platform_commission_percent if row else FALLBACK_COMMISSION
    platform_share = Decimal("100") - commission

    TrainerProfile.objects.filter(
        revenue_share_pct__in={SHIPPED_DEFAULT, platform_share}
    ).update(revenue_share_pct=None)


def restore_shipped_default(apps, schema_editor):
    """Irreversible in principle — an inherited rate is indistinguishable from a
    negotiated one once written. Restores the shipped default so the schema
    change can roll back; it does not recover which rows were which."""
    TrainerProfile = apps.get_model("accounts", "TrainerProfile")
    TrainerProfile.objects.filter(revenue_share_pct__isnull=True).update(
        revenue_share_pct=SHIPPED_DEFAULT
    )


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0005_alter_trainerprofile_revenue_share_pct"),
        # Needs the settings row so the platform share can be computed.
        ("core", "0003_seed_platform_settings"),
    ]

    operations = [
        migrations.RunPython(follow_platform_default, restore_shipped_default)
    ]
