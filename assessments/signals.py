"""Keep ``Assessment.total_questions`` in step with the rows it counts.

The field is denormalised so the assessment list can print "12 questions"
without a COUNT per row. Anything denormalised drifts the moment a write path
forgets to update it, and two already had: the demo seed wrote a made-up
``total_questions`` for an assessment it never gave questions to, and saving an
*empty* question set deleted every question while skipping the re-count that
ran per created question. Both left assessments advertising questions that no
longer existed — the count said 2, the list came back empty.

So the count is not maintained by the write paths at all. It is rebuilt from
the question rows themselves whenever one is added or removed, which is the
only version of this that cannot fall out of step: a new endpoint, a shell
session, the admin and a cascading delete all go through here.

Two things go around it, neither of which anything does today. ``bulk_create``
and ``bulk_update`` do not send these signals at all. And moving an existing
question to a different assessment would leave the assessment it came from too
high — the API cannot do it (``QuestionAuthorSerializer`` marks ``assessment``
read-only) so it is not worth the ``pre_save`` bookkeeping to catch. Code that
starts doing either must call :func:`recount_questions` itself.
"""

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from assessments.models import Assessment, Question


def recount_questions(assessment_id):
    """Rewrite one assessment's ``total_questions`` from its question rows.

    Written with ``update()`` on a filtered queryset rather than
    ``instance.save()`` so it neither races with a concurrent save of the wider
    row nor fails when the assessment is already gone — a question deleted by
    its assessment cascading away matches zero rows and is a no-op.
    """
    Assessment.objects.filter(pk=assessment_id).update(
        total_questions=Question.objects.filter(assessment_id=assessment_id).count()
    )


@receiver(post_save, sender=Question, dispatch_uid="recount_questions_on_save")
def _recount_on_save(sender, instance, created, **kwargs):
    # Only a new row moves the count; editing a question's text or points
    # leaves it alone.
    if created:
        recount_questions(instance.assessment_id)


@receiver(post_delete, sender=Question, dispatch_uid="recount_questions_on_delete")
def _recount_on_delete(sender, instance, **kwargs):
    recount_questions(instance.assessment_id)
