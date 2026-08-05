from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "accounts"

    def ready(self):
        # Keeps a TrainerProfile alongside every trainer account.
        from accounts import signals  # noqa: F401
