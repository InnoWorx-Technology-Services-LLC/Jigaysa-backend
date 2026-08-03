import pytest
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Role, User
from core.models import Organization
from courses.models import Tag
from engagement.models import (
    CommunityProfile,
    DiscussionReply,
    DiscussionThread,
    PointRule,
    Vote,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def student():
    return User.objects.create_user(
        email="stu@example.com", password="StrongPass123!", role=Role.STUDENT
    )


@pytest.fixture
def other():
    return User.objects.create_user(
        email="o@example.com", password="StrongPass123!", role=Role.STUDENT
    )


def test_thread_create_reply_and_accept(student, other):
    api = APIClient()
    api.force_authenticate(student)
    thread_resp = api.post(
        "/api/v1/discussion-threads/",
        {"title": "loc vs iloc?", "body": "help", "scope": "community"},
        format="json",
    )
    assert thread_resp.status_code == status.HTTP_201_CREATED
    thread_id = thread_resp.data["id"]

    # Another student replies.
    api.force_authenticate(other)
    reply_resp = api.post(
        "/api/v1/discussion-replies/",
        {"thread": thread_id, "body": "loc is label-based"},
        format="json",
    )
    assert reply_resp.status_code == status.HTTP_201_CREATED
    reply_id = reply_resp.data["id"]
    assert DiscussionThread.objects.get(id=thread_id).reply_count == 1

    # The thread author accepts the answer → thread resolved.
    api.force_authenticate(student)
    accept = api.post(f"/api/v1/discussion-replies/{reply_id}/accept/")
    assert accept.status_code == status.HTTP_200_OK
    assert DiscussionReply.objects.get(id=reply_id).is_accepted_answer is True
    assert DiscussionThread.objects.get(id=thread_id).status == "resolved"


def test_non_author_cannot_accept(student, other):
    thread = DiscussionThread.objects.create(
        author=student, title="q", scope=DiscussionThread.Scope.COMMUNITY
    )
    reply = DiscussionReply.objects.create(thread=thread, author=other, body="a")
    api = APIClient()
    api.force_authenticate(other)
    resp = api.post(f"/api/v1/discussion-replies/{reply.id}/accept/")
    assert resp.status_code == status.HTTP_403_FORBIDDEN


def test_community_profile_me(student):
    api = APIClient()
    api.force_authenticate(student)
    resp = api.get("/api/v1/community-profile/me/")
    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["points"] == 0
    assert resp.data["badges"] == []


# -- voting & reputation (Stack Overflow rules) ----------------------------- #


def _thread(author, **kwargs):
    kwargs.setdefault("title", "q")
    kwargs.setdefault("scope", DiscussionThread.Scope.COMMUNITY)
    return DiscussionThread.objects.create(author=author, **kwargs)


def points_of(user):
    profile = CommunityProfile.objects.filter(user=user).first()
    return profile.points if profile else 0


def test_upvoting_a_question_pays_the_author(student, other):
    thread = _thread(student)
    api = APIClient()
    api.force_authenticate(other)

    resp = api.post(
        f"/api/v1/discussion-threads/{thread.id}/vote/", {"value": 1}, format="json"
    )
    assert resp.data == {"score": 1, "my_vote": 1}
    assert points_of(student) == 5  # SO: question upvote = +5


def test_upvoting_an_answer_pays_more_than_a_question(student, other):
    thread = _thread(student)
    reply = DiscussionReply.objects.create(thread=thread, author=other, body="a")
    api = APIClient()
    api.force_authenticate(student)

    api.post(
        f"/api/v1/discussion-replies/{reply.id}/vote/", {"value": 1}, format="json"
    )
    assert points_of(other) == 10  # SO: answer upvote = +10


def test_voting_the_same_way_twice_withdraws_the_vote(student, other):
    thread = _thread(student)
    api = APIClient()
    api.force_authenticate(other)
    url = f"/api/v1/discussion-threads/{thread.id}/vote/"

    api.post(url, {"value": 1}, format="json")
    resp = api.post(url, {"value": 1}, format="json")

    assert resp.data == {"score": 0, "my_vote": 0}
    # The reputation is handed back, not kept.
    assert points_of(student) == 0
    assert Vote.objects.filter(thread=thread).count() == 0


def test_flipping_a_vote_reverses_the_old_one(student, other):
    thread = _thread(student)
    api = APIClient()
    api.force_authenticate(other)
    url = f"/api/v1/discussion-threads/{thread.id}/vote/"

    api.post(url, {"value": 1}, format="json")   # +5, score 1
    resp = api.post(url, {"value": -1}, format="json")

    assert resp.data == {"score": -1, "my_vote": -1}  # score moves by 2
    # +5 reversed, then -2 applied → floored at 0 rather than going negative.
    assert points_of(student) == 0
    assert Vote.objects.get(thread=thread).value == -1


def test_downvoting_an_answer_costs_the_voter(student, other):
    thread = _thread(student)
    reply = DiscussionReply.objects.create(thread=thread, author=other, body="a")
    # Give the voter reputation to lose.
    CommunityProfile.objects.create(user=student, points=100)
    api = APIClient()
    api.force_authenticate(student)

    api.post(
        f"/api/v1/discussion-replies/{reply.id}/vote/", {"value": -1}, format="json"
    )
    assert points_of(student) == 99  # SO: casting a downvote costs -1


def test_withdrawing_a_downvote_refunds_the_voter(student, other):
    thread = _thread(student)
    reply = DiscussionReply.objects.create(thread=thread, author=other, body="a")
    CommunityProfile.objects.create(user=student, points=100)
    api = APIClient()
    api.force_authenticate(student)
    url = f"/api/v1/discussion-replies/{reply.id}/vote/"

    api.post(url, {"value": -1}, format="json")
    api.post(url, {"value": -1}, format="json")
    assert points_of(student) == 100


def test_cannot_vote_on_your_own_post(student):
    thread = _thread(student)
    api = APIClient()
    api.force_authenticate(student)
    resp = api.post(
        f"/api/v1/discussion-threads/{thread.id}/vote/", {"value": 1}, format="json"
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert points_of(student) == 0


def test_vote_value_is_validated(student, other):
    thread = _thread(student)
    api = APIClient()
    api.force_authenticate(other)
    resp = api.post(
        f"/api/v1/discussion-threads/{thread.id}/vote/", {"value": 7}, format="json"
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_accepting_an_answer_pays_both_sides(student, other):
    thread = _thread(student)
    reply = DiscussionReply.objects.create(thread=thread, author=other, body="a")
    api = APIClient()
    api.force_authenticate(student)

    api.post(f"/api/v1/discussion-replies/{reply.id}/accept/")
    assert points_of(other) == 15    # SO: accepted answer = +15
    assert points_of(student) == 2   # SO: accepting = +2


def test_admin_can_retune_point_values(student, other):
    """The whole reason PointRule exists — no deploy to change the economy."""
    PointRule.objects.filter(activity="question_upvote").update(points=50)
    thread = _thread(student)
    api = APIClient()
    api.force_authenticate(other)

    api.post(
        f"/api/v1/discussion-threads/{thread.id}/vote/", {"value": 1}, format="json"
    )
    assert points_of(student) == 50


def test_deactivated_rule_awards_nothing(student, other):
    PointRule.objects.filter(activity="question_upvote").update(is_active=False)
    thread = _thread(student)
    api = APIClient()
    api.force_authenticate(other)

    api.post(
        f"/api/v1/discussion-threads/{thread.id}/vote/", {"value": 1}, format="json"
    )
    assert points_of(student) == 0


def test_asking_a_question_awards_points(student):
    """The Ask page promises this — so it has to be true."""
    api = APIClient()
    api.force_authenticate(student)
    api.post(
        "/api/v1/discussion-threads/",
        {"title": "how do I structure a data project?", "scope": "community"},
        format="json",
    )
    assert points_of(student) == 2


# -- visibility ------------------------------------------------------------- #


@pytest.fixture
def org():
    return Organization.objects.create(name="Acme Institute")


def test_private_thread_is_hidden_from_other_communities(org, student, other):
    student.organization = org
    student.save(update_fields=["organization"])
    mine = _thread(student, visibility=DiscussionThread.Visibility.COMMUNITY)

    api = APIClient()
    api.force_authenticate(other)  # no organization
    listed = [t["id"] for t in api.get("/api/v1/discussion-threads/").data["results"]]
    assert mine.id not in listed
    assert api.get(f"/api/v1/discussion-threads/{mine.id}/").status_code == (
        status.HTTP_404_NOT_FOUND
    )


def test_same_community_can_read_a_private_thread(org, student, other):
    for user in (student, other):
        user.organization = org
        user.save(update_fields=["organization"])
    mine = _thread(student, visibility=DiscussionThread.Visibility.COMMUNITY)

    api = APIClient()
    api.force_authenticate(other)
    assert api.get(f"/api/v1/discussion-threads/{mine.id}/").status_code == (
        status.HTTP_200_OK
    )


def test_public_threads_are_visible_platform_wide_but_need_auth(student, other):
    public = _thread(student, visibility=DiscussionThread.Visibility.PUBLIC)

    anon = APIClient()
    assert anon.get(f"/api/v1/discussion-threads/{public.id}/").status_code == (
        status.HTTP_401_UNAUTHORIZED
    )

    api = APIClient()
    api.force_authenticate(other)
    assert api.get(f"/api/v1/discussion-threads/{public.id}/").status_code == (
        status.HTTP_200_OK
    )


# -- tags, views, leaderboard ----------------------------------------------- #


def test_tags_are_created_on_first_use_and_deduplicated(student):
    api = APIClient()
    api.force_authenticate(student)
    resp = api.post(
        "/api/v1/discussion-threads/",
        {"title": "viz help", "scope": "community", "tags": ["Data Viz", "careers"]},
        format="json",
    )
    assert resp.status_code == status.HTTP_201_CREATED
    assert sorted(resp.data["tags"]) == ["careers", "data-viz"]

    # "data viz" must land on the same tag, not create a near-duplicate.
    api.post(
        "/api/v1/discussion-threads/",
        {"title": "more viz", "scope": "community", "tags": ["data viz"]},
        format="json",
    )
    assert Tag.objects.filter(slug="data-viz").count() == 1


def test_forum_tags_endpoint_counts_visible_threads(student):
    api = APIClient()
    api.force_authenticate(student)
    api.post(
        "/api/v1/discussion-threads/",
        {"title": "q1", "scope": "community", "tags": ["python"]},
        format="json",
    )
    resp = api.get("/api/v1/forum-tags/")
    assert resp.status_code == status.HTTP_200_OK
    python = next(t for t in resp.data if t["slug"] == "python")
    assert python["thread_count"] == 1


def test_reading_a_question_counts_a_view(student, other):
    thread = _thread(student)
    api = APIClient()
    api.force_authenticate(other)

    api.get(f"/api/v1/discussion-threads/{thread.id}/")
    api.get(f"/api/v1/discussion-threads/{thread.id}/")
    thread.refresh_from_db()
    assert thread.views_count == 2


def test_leaderboard_ranks_and_locates_the_caller(student, other):
    CommunityProfile.objects.create(user=student, points=8420)
    CommunityProfile.objects.create(user=other, points=1240)
    api = APIClient()
    api.force_authenticate(other)

    resp = api.get("/api/v1/community-profile/leaderboard/")
    assert [e["points"] for e in resp.data["results"]] == [8420, 1240]
    assert resp.data["results"][0]["rank"] == 1
    assert resp.data["my_rank"] == 2
    assert resp.data["my_points"] == 1240
