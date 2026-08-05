"""Keep a ``TrainerProfile`` in step with every trainer account.

Registered in ``AccountsConfig.ready()``.

Without this the profile row only ever existed in the demo seed, so a trainer
who signed up through the API had none — and since a mentor is discovered by
``trainer_profile__is_approved``, they could never be approved or booked. The
row is created unapproved: existing is not the same as being allowed to teach.
"""

from django.db.models.signals import post_save
from django.dispatch import receiver

from accounts.models import Role, TrainerProfile, User


@receiver(post_save, sender=User, dispatch_uid="ensure_trainer_profile")
def ensure_trainer_profile(sender, instance, **kwargs):
    """Give every trainer a profile, whether they registered or were promoted."""
    if instance.role != Role.TRAINER:
        return
    TrainerProfile.objects.get_or_create(user=instance)
