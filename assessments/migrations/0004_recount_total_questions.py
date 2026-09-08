"""Rebuild ``Assessment.total_questions`` from the question rows.

Two paths had been writing the field without the questions to back it: the demo
seed invented a count for an assessment it gave no questions to, and saving an
empty question set deleted every question without re-counting. Both are fixed
(see ``assessments.signals``), but the rows they already wrote are still wrong —
"Final quiz" advertises 2 questions and returns none — so the stored value is
recomputed here rather than left for someone to notice.

Safe to re-run against correct data: it only writes rows that disagree.
"""

from django.db import migrations
from django.db.models import Count


def recount(apps, schema_editor):
    Assessment = apps.get_model("assessments", "Assessment")
    stale = []
    for assessment in (
        Assessment.objects.annotate(actual=Count("questions")).iterator()
    ):
        if assessment.total_questions != assessment.actual:
            assessment.total_questions = assessment.actual
            stale.append(assessment)
    Assessment.objects.bulk_update(stale, ["total_questions"], batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ("assessments", "0003_answer_file_key"),
    ]

    operations = [
        # No reverse: the previous values were wrong, and restoring them is not
        # something anyone would want.
        migrations.RunPython(recount, migrations.RunPython.noop),
    ]
