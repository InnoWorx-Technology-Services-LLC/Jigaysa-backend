from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.utils.text import slugify


class TimeStampedModel(models.Model):
    """Abstract base giving every model audit timestamps.

    All future LMS models (courses, payments, live sessions, etc.) should
    inherit from this so created/updated tracking is uniform platform-wide.
    """

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Organization(TimeStampedModel):
    """A tenant: institution or corporate client (PRD §2.4).

    Users optionally belong to an Organization, giving us a multi-tenant
    seam (NFR: multi-tenant architecture) without re-modeling later. Bulk
    enrollments, custom batches and training analytics will hang off this.
    """

    class OrgType(models.TextChoices):
        INSTITUTION = "institution", "Institution"
        CORPORATE = "corporate", "Corporate"

    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=280, unique=True, blank=True)
    type = models.CharField(
        max_length=20, choices=OrgType.choices, default=OrgType.INSTITUTION
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)


class PlatformSetting(TimeStampedModel):
    """Platform-wide configuration an admin can change without a deploy.

    A **singleton**: exactly one row, ``pk=1``, created by a data migration that
    seeds it from the environment variables these fields replace. Nothing else
    may create one — ``save()`` pins the pk.

    Read through :meth:`get_solo` on every use rather than cached at import
    time. A settings row is a handful of columns behind a primary-key lookup,
    and paying for that query is what makes "change the GST rate and the next
    checkout uses it" true. Caching it in module state would make the value
    stale in every worker process that didn't serve the write.

    Two things deliberately *not* here:

    * Anything that must exist before the database does — ``SECRET_KEY``,
      ``DATABASES``, ``ALLOWED_HOSTS``. Those stay in the environment.
    * The Stripe keys the admin UI draws. There is no Stripe integration;
      storing a secret nothing reads is a liability with no upside.
    """

    #: Flag name in the API → the field backing it. Mirrors the same idiom as
    #: ``PricingPlan.ENTITLEMENT_FIELDS`` so both read the same way.
    FLAG_FIELDS = {
        "course_approval_required": "course_approval_required",
        "trainer_self_onboarding": "trainer_self_onboarding",
        "allow_coupon_codes": "allow_coupon_codes",
        "smart_classroom_module": "smart_classroom_module",
        "ai_suggestions": "ai_suggestions",
        "container_classrooms": "container_classrooms",
    }

    #: Flags the backend actually acts on. The rest are stored and returned so
    #: the admin screen and the frontend can round-trip them, but nothing reads
    #: them yet — those modules do not exist. Same honesty as
    #: ``payments.entitlements.ENFORCED``.
    ENFORCED_FLAGS = frozenset(
        {"course_approval_required", "trainer_self_onboarding", "allow_coupon_codes"}
    )

    # --- General ----------------------------------------------------------- #
    platform_name = models.CharField(max_length=120, default="Jigyasa")
    support_email = models.EmailField(blank=True)
    default_currency = models.CharField(
        max_length=8,
        default="INR",
        validators=[
            RegexValidator(
                r"^[A-Z]{3}$", "Use a 3-letter uppercase ISO code, e.g. INR."
            )
        ],
        help_text=(
            "Currency for new orders and billing totals. Does NOT convert "
            "existing prices — change your course prices and plans too."
        ),
    )

    # --- Payments & tax ---------------------------------------------------- #
    gst_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("18.00"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
        help_text=(
            "Applied to the discounted subtotal at checkout. Orders already "
            "placed keep the rate they were quoted at."
        ),
    )
    platform_commission_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("20.00"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
        help_text=(
            "The platform's default cut. Trainers keep (100 − this) unless a "
            "rate is set on their own profile, which overrides it. "
            "NOTHING COMPUTES PAYOUTS YET — changing this does not move money."
        ),
    )

    # Gateway credentials. The key id is public by design (it ships to the
    # browser to open Checkout); the two secrets never leave the server.
    razorpay_key_id = models.CharField(max_length=255, blank=True)
    razorpay_key_secret = models.CharField(max_length=255, blank=True)
    razorpay_webhook_secret = models.CharField(max_length=255, blank=True)

    # --- Feature flags ----------------------------------------------------- #
    course_approval_required = models.BooleanField(
        default=True,
        help_text="Off: a trainer's submission publishes immediately, no review.",
    )
    trainer_self_onboarding = models.BooleanField(
        default=True,
        help_text="Off: nobody can register as a trainer; an admin creates them.",
    )
    allow_coupon_codes = models.BooleanField(
        default=True, help_text="Off: coupon codes are rejected at checkout."
    )
    smart_classroom_module = models.BooleanField(
        default=False, help_text="Not enforced — the module does not exist yet."
    )
    ai_suggestions = models.BooleanField(
        default=False, help_text="Not enforced — the feature does not exist yet."
    )
    container_classrooms = models.BooleanField(
        default=False, help_text="Not enforced — Phase 2."
    )

    class Meta:
        verbose_name = "Platform settings"
        verbose_name_plural = "Platform settings"

    def __str__(self):
        return f"Platform settings ({self.platform_name})"

    def save(self, *args, **kwargs):
        """Force the singleton. A second row would make ``get_solo`` a coin toss."""
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):  # pragma: no cover - guarded in admin too
        raise NotImplementedError("The platform settings row cannot be deleted.")

    @classmethod
    def get_solo(cls):
        """The settings row, or an **unsaved** default instance.

        Never creates. A read path that writes is a read path that can deadlock
        or race two rows into existence, and this is called from inside the
        payment settlement transaction. If the seeding migration has not run,
        callers transparently get field defaults instead of an exception.
        """
        return cls.objects.filter(pk=1).first() or cls()

    def flags(self) -> dict:
        """Every flag → its current value, for the API."""
        return {key: getattr(self, field) for key, field in self.FLAG_FIELDS.items()}
