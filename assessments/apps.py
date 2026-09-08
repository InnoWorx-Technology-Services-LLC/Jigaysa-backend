from django.apps import AppConfig


class AssessmentsConfig(AppConfig):
    name = 'assessments'

    def ready(self):
        # Rebuilds Assessment.total_questions whenever a question comes or goes.
        from assessments import signals  # noqa: F401
