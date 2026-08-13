"""Create the singleton settings row.

Seeds the non-secret defaults only. The gateway credential fields are left
**blank on purpose**, which the resolver in ``payments.gateway`` reads as "use
the environment" — so a server already taking payments from ``.env`` keys keeps
working, unchanged, with nothing copied anywhere.

Not copying the secrets in is the whole point. The env fallback already makes
them work, so seeding would buy no behaviour and would duplicate live gateway
credentials into a second store that gets dumped, backed up and replicated. A
secret is safest in one place. The admin screen reports which source is in force
(``razorpay_key_source``) so a blank box never reads as "not configured".
"""

from django.db import migrations


def seed(apps, schema_editor):
    from django.conf import settings

    PlatformSetting = apps.get_model("core", "PlatformSetting")
    if PlatformSetting.objects.filter(pk=1).exists():
        return
    PlatformSetting.objects.create(
        pk=1,
        platform_name=getattr(settings, "RAZORPAY_CHECKOUT_NAME", "") or "Jigyasa",
        support_email="",
        default_currency="INR",
    )


def unseed(apps, schema_editor):
    PlatformSetting = apps.get_model("core", "PlatformSetting")
    PlatformSetting.objects.filter(pk=1).delete()


class Migration(migrations.Migration):

    dependencies = [("core", "0002_platformsetting")]

    operations = [migrations.RunPython(seed, unseed)]
