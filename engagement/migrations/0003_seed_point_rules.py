"""Seed the editable point rules with the shipped defaults.

Without this an admin opens the Point Rules page and sees nothing, with no hint
of which activities exist or what they are worth. Awards would still work — the
lookup falls back to ``services.POINTS`` — but the values would be invisible and
therefore un-tunable, which defeats the point of the table.

Idempotent and non-destructive: only missing rows are created, so re-running it
never overwrites a value an admin has already tuned.
"""

from django.db import migrations

# Duplicated from ``engagement.services.POINTS`` on purpose: a migration must
# keep working against the code as it was when written, so it cannot import a
# constant that later edits may change or remove.
DEFAULTS = {
    "enroll": 10,
    "lesson_complete": 5,
    "assessment_pass": 25,
    "certificate": 50,
    "reply": 5,
    "question_upvote": 5,
    "question_downvote": -2,
    "answer_upvote": 10,
    "answer_downvote": -2,
    "accepted_answer": 15,
    "accept_answer": 2,
    "downvote_cast": -1,
    "ask_question": 2,
}


def seed(apps, schema_editor):
    PointRule = apps.get_model("engagement", "PointRule")
    for activity, points in DEFAULTS.items():
        PointRule.objects.get_or_create(
            activity=activity,
            defaults={
                "points": points,
                "label": activity.replace("_", " ").title(),
                "is_active": True,
            },
        )


def unseed(apps, schema_editor):
    PointRule = apps.get_model("engagement", "PointRule")
    PointRule.objects.filter(activity__in=DEFAULTS).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("engagement", "0002_pointrule_discussionreply_score_and_more"),
    ]

    operations = [migrations.RunPython(seed, unseed)]
