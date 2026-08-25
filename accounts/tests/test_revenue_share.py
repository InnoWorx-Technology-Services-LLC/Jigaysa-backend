"""Which revenue split is in force for a trainer.

Two fields describe the same split from opposite ends — the platform's cut and
this trainer's share — so what matters is that one resolver reconciles them,
that a platform-wide change really does move every ordinary trainer, and that a
negotiated rate is not collateral damage when it does.
"""

from decimal import Decimal

import pytest

from accounts.models import Role, TrainerProfile, User
from core.models import PlatformSetting

pytestmark = pytest.mark.django_db


def _commission(pct):
    row = PlatformSetting.get_solo()
    row.platform_commission_percent = Decimal(pct)
    row.save()


def _trainer(email):
    """A trainer account. The profile is created by the post_save signal."""
    user = User.objects.create_user(
        email=email, password="StrongPass123!", role=Role.TRAINER
    )
    return TrainerProfile.objects.get(user=user)


def test_a_new_trainer_follows_the_platform_commission():
    _commission("20")
    profile = _trainer("t-new@example.com")

    assert profile.revenue_share_pct is None  # nothing pinned
    assert profile.effective_revenue_share_pct == Decimal("80")


def test_changing_the_commission_moves_every_ordinary_trainer():
    """The whole point of setting it in one place."""
    _commission("20")
    early = _trainer("t-early@example.com")
    late = _trainer("t-late@example.com")

    _commission("30")

    early.refresh_from_db()
    late.refresh_from_db()
    assert early.effective_revenue_share_pct == Decimal("70")
    assert late.effective_revenue_share_pct == Decimal("70")


def test_a_negotiated_rate_survives_a_platform_wide_change():
    _commission("20")
    profile = _trainer("t-deal@example.com")
    profile.revenue_share_pct = Decimal("75")
    profile.save()

    _commission("50")

    profile.refresh_from_db()
    assert profile.effective_revenue_share_pct == Decimal("75")


def test_clearing_a_negotiated_rate_returns_the_trainer_to_the_platform():
    _commission("20")
    profile = _trainer("t-reset@example.com")
    profile.revenue_share_pct = Decimal("75")
    profile.save()

    profile.revenue_share_pct = None
    profile.save()

    assert profile.effective_revenue_share_pct == Decimal("80")


def test_a_deliberate_zero_is_not_treated_as_unset():
    """``0`` and ``None`` mean opposite things — an unpaid arrangement is not
    the same as "follow the platform"."""
    _commission("20")
    profile = _trainer("t-zero@example.com")
    profile.revenue_share_pct = Decimal("0")
    profile.save()

    assert profile.effective_revenue_share_pct == Decimal("0")


def test_a_full_commission_leaves_the_trainer_nothing():
    _commission("100")

    assert _trainer("t-all@example.com").effective_revenue_share_pct == Decimal("0")


def test_the_api_reports_the_rate_in_force():
    from accounts.serializers import TrainerProfileSerializer

    _commission("20")
    profile = _trainer("t-api@example.com")

    data = TrainerProfileSerializer(profile).data
    assert data["revenue_share_pct"] is None          # nothing negotiated
    assert Decimal(data["effective_revenue_share_pct"]) == Decimal("80")
