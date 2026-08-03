"""Gamification: award points and badges for student activity (PRD §3.12).

Central so the point values and level curve live in one place. Called from the
notification/event signal handlers and the forum vote actions, not sprinkled
through the viewsets.

Point values follow **Stack Overflow's reputation model** and are stored in
``PointRule`` so an admin can retune them without a deploy. ``POINTS`` below is
the seed/fallback: it is what a fresh install starts with, and what a lookup
falls back to if a rule row is missing, so a deleted row degrades to the default
rather than silently paying zero.
"""

from django.db.models import F

from engagement.models import (
    Badge,
    CommunityProfile,
    DiscussionThread,
    PointRule,
    UserBadge,
    Vote,
)

# Default points per activity. The forum values are Stack Overflow's.
POINTS = {
    # Platform activity (pre-existing values, unchanged).
    "enroll": 10,
    "lesson_complete": 5,
    "assessment_pass": 25,
    "certificate": 50,
    "reply": 5,
    # Forum reputation, Stack Overflow's numbers.
    "question_upvote": 5,       # your question is upvoted
    "question_downvote": -2,    # your question is downvoted
    "answer_upvote": 10,        # your answer is upvoted
    "answer_downvote": -2,      # your answer is downvoted
    "accepted_answer": 15,      # your answer is accepted
    "accept_answer": 2,         # you accepted someone's answer
    "downvote_cast": -1,        # casting a downvote on an answer costs you
    # Not a Stack Overflow award — SO gives nothing for asking — but the Ask
    # page promises "you earn points for asking", so the promise is kept. Set it
    # to 0 in the admin to match SO exactly.
    "ask_question": 2,
}

# 500 points per level.
POINTS_PER_LEVEL = 500

# Reputation cannot go negative — Stack Overflow floors it at 1; we floor at 0
# because ``CommunityProfile.points`` is unsigned.
MIN_POINTS = 0


def points_for(activity) -> int:
    """Points for ``activity``: the admin's value, else the shipped default."""
    rule = PointRule.objects.filter(activity=activity).first()
    if rule is None:
        return POINTS.get(activity, 0)
    if not rule.is_active:
        return 0
    return rule.points


def sync_point_rules():
    """Materialise the defaults as editable rows. Idempotent.

    Only creates what is missing — an admin's retuned value is never
    overwritten by a later run.
    """
    for activity, points in POINTS.items():
        PointRule.objects.get_or_create(
            activity=activity,
            defaults={"points": points, "label": activity.replace("_", " ").title()},
        )


def award_points(user, activity, times=1):
    """Apply ``activity``'s points to ``user``. Handles negative rules.

    ``times`` scales the award — used when a vote flips (up → down is worth
    reversing the up *and* applying the down).
    """
    amount = points_for(activity) * times
    if not amount:
        return
    return adjust_points(user, amount)


def adjust_points(user, amount):
    """Move a user's reputation by ``amount``, never below ``MIN_POINTS``."""
    if not amount:
        return
    profile, _ = CommunityProfile.objects.get_or_create(user=user)
    profile.points = max(MIN_POINTS, profile.points + amount)
    profile.level = 1 + profile.points // POINTS_PER_LEVEL
    profile.save(update_fields=["points", "level", "updated_at"])
    return profile


def apply_vote(user, post, value):
    """Cast, change or clear ``user``'s vote on a thread or reply.

    Returns ``(score, my_vote)``. Semantics match Stack Overflow:

    * voting the same way twice **clears** the vote (the arrow toggles off)
    * voting the other way **flips** it, worth two points of score
    * the post's author gains or loses reputation by the difference, so a
      flipped or withdrawn vote is fully reversed rather than double-counted
    * downvoting an answer costs the *voter* a point, and withdrawing that
      downvote refunds it

    You cannot vote on your own post — enforced by the caller, which knows the
    right error type to raise.
    """
    is_thread = isinstance(post, DiscussionThread)
    existing = post.votes.filter(user=user).first()
    previous = existing.value if existing else 0

    if value == previous:  # same arrow again → withdraw
        value = 0
    if existing and value == 0:
        existing.delete()
    elif existing:
        existing.value = value
        existing.save(update_fields=["value", "updated_at"])
    elif value:
        post.votes.create(user=user, value=value)

    delta = value - previous
    if delta:
        type(post).objects.filter(pk=post.pk).update(score=F("score") + delta)
        post.refresh_from_db(fields=["score"])
        _apply_vote_reputation(user, post, is_thread, previous, value)
    return post.score, value


def _apply_vote_reputation(voter, post, is_thread, previous, value):
    """Reverse the old vote's reputation effect, then apply the new one."""
    author = post.author
    up = "question_upvote" if is_thread else "answer_upvote"
    down = "question_downvote" if is_thread else "answer_downvote"

    total = 0
    for vote_value, sign in ((previous, -1), (value, 1)):
        if vote_value == Vote.UP:
            total += sign * points_for(up)
        elif vote_value == Vote.DOWN:
            total += sign * points_for(down)
    # Self-votes are blocked in the view; this keeps the reputation path safe
    # even if some other caller forgets.
    if author.id != voter.id:
        adjust_points(author, total)

    # Casting a downvote on an answer costs the voter; withdrawing refunds it.
    if not is_thread:
        cost = points_for("downvote_cast")
        voter_delta = 0
        if previous == Vote.DOWN:
            voter_delta -= cost
        if value == Vote.DOWN:
            voter_delta += cost
        adjust_points(voter, voter_delta)


def award_badge(user, slug):
    """Grant a badge by slug if it exists and the user doesn't have it yet."""
    badge = Badge.objects.filter(slug=slug).first()
    if badge is None:
        return
    _, created = UserBadge.objects.get_or_create(user=user, badge=badge)
    if created:
        profile, _ = CommunityProfile.objects.get_or_create(user=user)
        profile.badges_count = UserBadge.objects.filter(user=user).count()
        profile.save(update_fields=["badges_count", "updated_at"])
