"""Keep profile rows in step with every account.

Registered in ``AccountsConfig.ready()``.

Without this the profile rows only ever existed in the demo seed, so anyone who
signed up through the API had none.
"""

from django.db.models.signals import post_save
from django.dispatch import receiver

from accounts.models import Role, TrainerProfile, User, UserProfile


@receiver(post_save, sender=User, dispatch_uid="ensure_trainer_profile")
def ensure_trainer_profile(sender, instance, **kwargs):
    """Give every trainer a profile, whether they registered or were promoted.

    ``revenue_share_pct`` is deliberately left null: null means "follow the
    platform commission", so a new trainer tracks the platform default and keeps
    tracking it when an admin changes that default. Stamping a number here would
    freeze each trainer at whatever the rate happened to be on the day they
    signed up, which is precisely what platform-wide control is meant to avoid.
    """
    if instance.role != Role.TRAINER:
        return
    TrainerProfile.objects.get_or_create(user=instance)


@receiver(post_save, sender=User, dispatch_uid="ensure_user_profile")
def ensure_user_profile(sender, instance, **kwargs):
    """Every account gets the public profile shown on the dashboard header.

    Role-agnostic: a student's headline and a trainer's live in the same row,
    so ``/auth/me/`` has somewhere to write for whoever is signed in.
    """
    UserProfile.objects.get_or_create(user=instance)
