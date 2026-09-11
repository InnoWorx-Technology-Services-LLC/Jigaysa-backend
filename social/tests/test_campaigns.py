"""Promote-wizard tests: templates, scheduling, the sweep, and publishing.

Focused on the things that are load-bearing and easy to get wrong: the claim
that stops a post going out twice, the retry policy that tells a bad minute
apart from a dead token, the per-network composition that keeps a URL out of an
Instagram caption, and the validation that refuses a campaign the network would
have refused hours later.

No network is ever touched — every adapter's ``publish`` is replaced at the
module boundary, the same way the connect tests stub ``exchange_code``.
"""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Role, User
from courses.models import Course
from social import promotions, publishing
from social.models import Campaign, CampaignPost, Provider, SocialAccount
from social.providers import linkedin as linkedin_adapter
from social.providers import meta as meta_adapter
from social.providers.base import (
    ProviderAuthError,
    ProviderError,
    PublishedPost,
)

pytestmark = pytest.mark.django_db

FERNET_KEY = "Zt7Vp3nQ8sX1yL4kR6mB9wC2dF5gH0jN3pS7uV1xY8A="


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def configured(settings):
    settings.LINKEDIN_CLIENT_ID = "test-client"
    settings.LINKEDIN_CLIENT_SECRET = "test-secret"
    settings.META_APP_ID = "test-app"
    settings.META_APP_SECRET = "test-secret"
    settings.SOCIAL_TOKEN_KEY = FERNET_KEY
    settings.FRONTEND_URL = "https://lms.example.com/student"
    settings.SOCIAL_COURSE_URL_PATH = "/courses/{slug}"
    return settings


@pytest.fixture
def trainer(db):
    return User.objects.create_user(
        email="trainer@example.com", password="StrongPass123!",
        role=Role.TRAINER, full_name="Rohan Deshpande",
    )


@pytest.fixture
def other_trainer(db):
    return User.objects.create_user(
        email="rival@example.com", password="StrongPass123!", role=Role.TRAINER
    )


@pytest.fixture
def student(db):
    return User.objects.create_user(
        email="student@example.com", password="StrongPass123!", role=Role.STUDENT
    )


@pytest.fixture
def course(trainer):
    return Course.objects.create(
        title="Full-Stack Web Development Masterclass",
        subtitle="Build and ship a real app in eight weeks.",
        trainer=trainer,
        is_free=True,
        thumbnail="https://cdn.example.com/cover.jpg",
        status=Course.Status.PUBLISHED,
    )


def _account(user, provider, **kwargs):
    defaults = {
        "provider_account_id": f"{provider}-1",
        "display_name": f"{provider.title()} destination",
        "handle": provider,
        "access_token": "token-123",
        "status": SocialAccount.Status.CONNECTED,
    }
    defaults.update(kwargs)
    return SocialAccount.objects.create(user=user, provider=provider, **defaults)


@pytest.fixture
def linkedin(trainer, configured):
    return _account(
        trainer, Provider.LINKEDIN, provider_meta={"author_urn": "urn:li:person:abc"}
    )


@pytest.fixture
def instagram(trainer, configured):
    return _account(
        trainer,
        Provider.INSTAGRAM,
        provider_meta={"ig_user_id": "ig-9", "page_id": "page-9"},
    )


@pytest.fixture
def facebook_page(trainer, configured):
    return _account(
        trainer,
        Provider.FACEBOOK,
        provider_account_id="page-9",
        provider_meta={"page_id": "page-9"},
    )


def auth(api, user):
    api.force_authenticate(user=user)
    return api


@pytest.fixture
def stub_publish(monkeypatch):
    """Replace both adapters' ``publish`` and record what they were asked to send."""
    sent = []

    def _make(name, outcome=None):
        def _publish(account, content):
            sent.append(
                {
                    "provider": account.provider,
                    "caption": content.caption,
                    "image_url": content.image_url,
                }
            )
            if callable(outcome):
                return outcome(account, content)
            return PublishedPost(
                provider_post_id=f"{name}-post-1",
                permalink=f"https://{name}.example.com/p/1",
            )

        return _publish

    def install(outcome=None):
        monkeypatch.setattr(linkedin_adapter, "publish", _make("linkedin", outcome))
        monkeypatch.setattr(meta_adapter, "publish", _make("meta", outcome))
        return sent

    install.sent = sent
    return install


# --------------------------------------------------------------------------- #
# Step 1 & 2 — templates
# --------------------------------------------------------------------------- #


def test_templates_render_all_five_from_the_course(api, trainer, course):
    resp = auth(api, trainer).get(
        reverse("social:template-list"), {"course": course.slug}
    )
    assert resp.status_code == status.HTTP_200_OK

    keys = [t["key"] for t in resp.data["templates"]]
    assert keys == ["launch", "discount", "last_seats", "testimonial", "milestone"]

    launch = resp.data["templates"][0]
    assert course.title in launch["caption"]
    assert course.subtitle in launch["caption"]
    assert "Enrol now for free." in launch["caption"]
    assert launch["hashtags"] == "#learning #newcourse #upskill"
    # A course with a subtitle needs no apology for its copy.
    assert launch["hint"] == ""


def test_templates_hint_at_the_fallback_they_used(api, trainer, course):
    course.subtitle = ""
    course.save(update_fields=["subtitle"])

    resp = auth(api, trainer).get(
        reverse("social:template-list"), {"course": course.slug}
    )
    by_key = {t["key"]: t for t in resp.data["templates"]}
    assert "subtitle" in by_key["launch"]["hint"]
    assert "reviews" in by_key["testimonial"]["hint"]


def test_promo_card_is_declared_unavailable_not_silently_aliased(
    api, trainer, course
):
    resp = auth(api, trainer).get(
        reverse("social:template-list"), {"course": course.slug}
    )
    options = {o["value"]: o for o in resp.data["image_options"]}
    assert options["thumbnail"]["available"] is True
    assert options["thumbnail"]["url"] == course.thumbnail
    assert options["promo_card"]["available"] is False
    assert options["promo_card"]["note"]


def test_templates_are_scoped_to_your_own_courses(api, other_trainer, course):
    resp = auth(api, other_trainer).get(
        reverse("social:template-list"), {"course": course.slug}
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND


def test_templates_need_a_course(api, trainer):
    resp = auth(api, trainer).get(reverse("social:template-list"))
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_students_cannot_open_the_wizard(api, student, course):
    resp = auth(api, student).get(
        reverse("social:template-list"), {"course": course.slug}
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN


# --------------------------------------------------------------------------- #
# Step 2 — rewrite
# --------------------------------------------------------------------------- #


def test_instagram_rewrite_drops_the_link_and_says_where_it_went(api, trainer):
    resp = auth(api, trainer).post(
        reverse("social:rewrite"),
        {
            "caption": "Launching today. Details: https://lms.example.com/courses/x",
            "provider": "instagram",
            "hashtags": "#learning",
            "link_url": "https://lms.example.com/courses/x",
        },
        format="json",
    )
    assert resp.status_code == status.HTTP_200_OK
    assert "https://" not in resp.data["caption"]
    assert "https://" not in resp.data["preview"]
    assert promotions.IG_LINK_TEXT in resp.data["preview"]
    assert resp.data["limit"] == 2200


def test_linkedin_rewrite_keeps_the_link_clickable(api, trainer):
    link = "https://lms.example.com/courses/x"
    resp = auth(api, trainer).post(
        reverse("social:rewrite"),
        {"caption": "Launching today.", "provider": "linkedin", "link_url": link},
        format="json",
    )
    assert link in resp.data["preview"]
    assert resp.data["limit"] == 3000


def test_a_link_on_its_own_line_is_still_stripped_for_instagram():
    """The common shape: a trainer puts the URL on a line by itself."""
    caption = (
        "Launching today.\n\nDetails:\nhttps://lms.example.com/x\n\nEnrol now."
    )
    composed = promotions.compose(
        caption, "", "https://lms.example.com/x", "instagram"
    )
    assert "https://" not in composed
    assert promotions.IG_LINK_TEXT in composed
    assert "Launching today." in composed
    assert "Enrol now." in composed


def test_a_link_already_in_the_caption_is_not_appended_twice():
    link = "https://lms.example.com/x"
    composed = promotions.compose(f"Enrol: {link}", "", link, "linkedin")
    assert composed.count(link) == 1


def test_compose_clips_to_the_network_ceiling():
    long_caption = "x" * 5000
    composed = promotions.compose(long_caption, "", "", "instagram")
    assert len(composed) <= promotions.CAPTION_LIMIT["instagram"]


# --------------------------------------------------------------------------- #
# Step 4 — scheduling
# --------------------------------------------------------------------------- #


def _create(api, user, course, accounts, **extra):
    body = {
        "course": course.slug,
        "template": "launch",
        "caption": "Just launched something good.",
        "hashtags": "#learning",
        "accounts": [a.pk for a in accounts],
    }
    body.update(extra)
    return auth(api, user).post(
        reverse("social:campaign-list"), body, format="json"
    )


def test_scheduling_writes_pending_posts_and_calls_nothing(
    api, trainer, course, linkedin, stub_publish
):
    sent = stub_publish()
    when = timezone.now() + timedelta(days=2)

    resp = _create(api, trainer, course, [linkedin], publish_at=when.isoformat())
    assert resp.status_code == status.HTTP_201_CREATED
    assert resp.data["status"] == Campaign.Status.SCHEDULED
    assert resp.data["can_edit"] is True
    assert [p["status"] for p in resp.data["posts"]] == [
        CampaignPost.Status.PENDING
    ]
    assert sent == []  # nothing reaches a network until it is due


def test_publish_now_goes_out_inline_and_reports_each_destination(
    api, trainer, course, linkedin, instagram, stub_publish
):
    sent = stub_publish()

    resp = _create(api, trainer, course, [linkedin, instagram])
    assert resp.status_code == status.HTTP_201_CREATED
    assert resp.data["status"] == Campaign.Status.PUBLISHED
    assert resp.data["can_edit"] is False

    posts = {p["provider"]: p for p in resp.data["posts"]}
    assert posts["linkedin"]["permalink"] == "https://linkedin.example.com/p/1"
    assert posts["instagram"]["provider_post_id"] == "meta-post-1"
    assert {s["provider"] for s in sent} == {"linkedin", "instagram"}


def test_each_network_gets_its_own_caption(
    api, trainer, course, linkedin, instagram, stub_publish
):
    sent = stub_publish()
    link = promotions.course_url(course)

    _create(api, trainer, course, [linkedin, instagram])

    by_provider = {s["provider"]: s["caption"] for s in sent}
    assert link in by_provider["linkedin"]
    assert link not in by_provider["instagram"]
    assert promotions.IG_LINK_TEXT in by_provider["instagram"]


def test_instagram_without_an_image_is_refused_up_front(
    api, trainer, course, instagram, stub_publish
):
    stub_publish()
    course.thumbnail = ""
    course.save(update_fields=["thumbnail"])

    resp = _create(api, trainer, course, [instagram], image_source="none")
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "Instagram" in str(resp.data)
    assert not Campaign.objects.exists()


def test_promo_card_is_refused_while_unavailable(api, trainer, course, linkedin):
    resp = _create(api, trainer, course, [linkedin], image_source="promo_card")
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_you_cannot_post_through_someone_elses_account(
    api, trainer, other_trainer, course, configured
):
    theirs = _account(other_trainer, Provider.LINKEDIN)
    resp = _create(api, trainer, course, [theirs])
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_an_account_needing_reconnect_is_refused_before_scheduling(
    api, trainer, course, linkedin
):
    linkedin.mark_unusable(SocialAccount.Status.EXPIRED, "Token expired.")
    resp = _create(api, trainer, course, [linkedin])
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "reconnect" in str(resp.data).lower()


def test_a_schedule_in_the_past_is_refused(api, trainer, course, linkedin):
    past = (timezone.now() - timedelta(hours=1)).isoformat()
    resp = _create(api, trainer, course, [linkedin], publish_at=past)
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_a_schedule_a_decade_out_is_refused(api, trainer, course, linkedin):
    far = (timezone.now() + timedelta(days=4000)).isoformat()
    resp = _create(api, trainer, course, [linkedin], publish_at=far)
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_x_and_youtube_are_still_refused(api, trainer, course, configured):
    x_account = _account(trainer, Provider.X)
    resp = _create(api, trainer, course, [x_account])
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


# --------------------------------------------------------------------------- #
# Editing and cancelling
# --------------------------------------------------------------------------- #


def test_a_scheduled_campaign_can_be_moved(api, trainer, course, linkedin, stub_publish):
    stub_publish()
    when = timezone.now() + timedelta(days=1)
    created = _create(api, trainer, course, [linkedin], publish_at=when.isoformat())
    later = timezone.now() + timedelta(days=3)

    resp = auth(api, trainer).patch(
        reverse("social:campaign-detail", args=[created.data["id"]]),
        {"publish_at": later.isoformat(), "caption": "Reworded."},
        format="json",
    )
    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["caption"] == "Reworded."
    assert resp.data["status"] == Campaign.Status.SCHEDULED


def test_moving_a_schedule_to_now_publishes_it(
    api, trainer, course, linkedin, stub_publish
):
    sent = stub_publish()
    when = timezone.now() + timedelta(days=1)
    created = _create(api, trainer, course, [linkedin], publish_at=when.isoformat())
    assert sent == []

    resp = auth(api, trainer).patch(
        reverse("social:campaign-detail", args=[created.data["id"]]),
        {"publish_at": None},
        format="json",
    )
    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["status"] == Campaign.Status.PUBLISHED
    assert len(sent) == 1


def test_a_published_campaign_cannot_be_edited(
    api, trainer, course, linkedin, stub_publish
):
    stub_publish()
    created = _create(api, trainer, course, [linkedin])

    resp = auth(api, trainer).patch(
        reverse("social:campaign-detail", args=[created.data["id"]]),
        {"caption": "Too late."},
        format="json",
    )
    assert resp.status_code == status.HTTP_409_CONFLICT


def test_a_scheduled_campaign_can_be_cancelled(
    api, trainer, course, linkedin, stub_publish
):
    stub_publish()
    when = timezone.now() + timedelta(days=1)
    created = _create(api, trainer, course, [linkedin], publish_at=when.isoformat())

    resp = auth(api, trainer).delete(
        reverse("social:campaign-detail", args=[created.data["id"]])
    )
    assert resp.status_code == status.HTTP_204_NO_CONTENT
    assert not Campaign.objects.exists()


def test_a_campaign_that_is_already_public_is_not_deleted(
    api, trainer, course, linkedin, stub_publish
):
    stub_publish()
    created = _create(api, trainer, course, [linkedin])

    resp = auth(api, trainer).delete(
        reverse("social:campaign-detail", args=[created.data["id"]])
    )
    assert resp.status_code == status.HTTP_409_CONFLICT
    assert Campaign.objects.filter(pk=created.data["id"]).exists()


def test_campaign_history_is_paginated(api, trainer, course, linkedin, stub_publish):
    """It grows for the life of the account, so it is never returned whole."""
    stub_publish()
    for _ in range(25):
        _create(api, trainer, course, [linkedin])

    resp = auth(api, trainer).get(reverse("social:campaign-list"))
    assert set(resp.data) >= {"count", "next", "previous", "results"}
    assert resp.data["count"] == 25
    assert len(resp.data["results"]) == 20
    assert resp.data["next"]


def test_campaigns_are_scoped_to_their_trainer(
    api, trainer, other_trainer, course, linkedin, stub_publish
):
    stub_publish()
    created = _create(api, trainer, course, [linkedin])

    resp = auth(api, other_trainer).get(
        reverse("social:campaign-detail", args=[created.data["id"]])
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND

    listed = auth(api, other_trainer).get(reverse("social:campaign-list"))
    assert listed.data["count"] == 0
    assert listed.data["results"] == []


# --------------------------------------------------------------------------- #
# The sweep
# --------------------------------------------------------------------------- #


def _schedule(course, trainer, account, when):
    campaign = Campaign.objects.create(
        trainer=trainer,
        course=course,
        template="launch",
        caption="Scheduled copy.",
        hashtags="#learning",
        image_url=course.thumbnail,
        link_url=promotions.course_url(course),
        scheduled_for=when,
    )
    publishing.build_posts(campaign, [account])
    return campaign


def test_the_sweep_publishes_what_is_due(
    trainer, course, linkedin, stub_publish
):
    sent = stub_publish()
    campaign = _schedule(
        course, trainer, linkedin, timezone.now() - timedelta(minutes=5)
    )

    attempted = publishing.run_due()
    assert len(attempted) == 1
    assert len(sent) == 1

    campaign.refresh_from_db()
    assert campaign.status == Campaign.Status.PUBLISHED
    assert campaign.published_at is not None


def test_the_sweep_leaves_the_future_alone(trainer, course, linkedin, stub_publish):
    sent = stub_publish()
    campaign = _schedule(
        course, trainer, linkedin, timezone.now() + timedelta(hours=2)
    )

    assert publishing.run_due() == []
    assert sent == []
    campaign.refresh_from_db()
    assert campaign.status == Campaign.Status.SCHEDULED


def test_the_sweep_finishes_a_publish_now_that_never_completed(
    trainer, course, linkedin, stub_publish
):
    """A request that times out mid-fan-out leaves pending rows with no
    schedule. Those are due immediately — that is what makes a timeout a delay
    rather than a lost post."""
    sent = stub_publish()
    campaign = _schedule(course, trainer, linkedin, None)

    publishing.run_due()
    assert len(sent) == 1
    campaign.refresh_from_db()
    assert campaign.status == Campaign.Status.PUBLISHED


def test_a_post_is_never_sent_twice(trainer, course, linkedin, stub_publish):
    sent = stub_publish()
    campaign = _schedule(course, trainer, linkedin, None)
    post = campaign.posts.get()

    assert publishing.publish_post(post) is True
    # A second worker holding a stale copy of the same row finds it claimed.
    assert publishing.publish_post(post) is False
    assert len(sent) == 1


# --------------------------------------------------------------------------- #
# Failure handling
# --------------------------------------------------------------------------- #


def test_a_bad_minute_is_retried_then_given_up_on(
    trainer, course, linkedin, stub_publish
):
    def boom(account, content):
        raise ProviderError("LinkedIn is having a moment.")

    stub_publish(boom)
    campaign = _schedule(course, trainer, linkedin, None)
    post = campaign.posts.get()

    for expected in range(1, CampaignPost.MAX_ATTEMPTS):
        publishing.publish_post(post)
        post.refresh_from_db()
        assert post.attempts == expected
        # Still pending, so the next sweep will pick it up again.
        assert post.status == CampaignPost.Status.PENDING

    publishing.publish_post(post)
    post.refresh_from_db()
    assert post.attempts == CampaignPost.MAX_ATTEMPTS
    assert post.status == CampaignPost.Status.FAILED
    assert "having a moment" in post.error

    publishing.refresh_status(campaign)
    campaign.refresh_from_db()
    assert campaign.status == Campaign.Status.FAILED


def test_a_dead_token_fails_once_and_asks_for_a_reconnect(
    trainer, course, linkedin, stub_publish
):
    def denied(account, content):
        raise ProviderAuthError("The token has been revoked.")

    stub_publish(denied)
    campaign = _schedule(course, trainer, linkedin, None)
    post = campaign.posts.get()

    publishing.publish_post(post)
    post.refresh_from_db()
    # One attempt, not three: retrying a revoked token is a loop, not resilience.
    assert post.attempts == 1
    assert post.status == CampaignPost.Status.FAILED

    linkedin.refresh_from_db()
    assert linkedin.status == SocialAccount.Status.EXPIRED
    assert linkedin.is_usable is False
    assert "revoked" in linkedin.last_error


def test_a_dead_page_token_also_marks_the_instagram_that_shares_it(
    trainer, course, instagram, facebook_page, stub_publish
):
    def denied(account, content):
        raise ProviderAuthError("Session expired.")

    stub_publish(denied)
    campaign = _schedule(course, trainer, instagram, None)
    publishing.publish_post(campaign.posts.get())

    facebook_page.refresh_from_db()
    assert facebook_page.status == SocialAccount.Status.EXPIRED


def test_one_network_failing_does_not_stop_the_others(
    api, trainer, course, linkedin, instagram, monkeypatch, stub_publish
):
    stub_publish()

    def only_meta_fails(account, content):
        raise ProviderError("Instagram rejected the image.")

    monkeypatch.setattr(meta_adapter, "publish", only_meta_fails)

    resp = _create(api, trainer, course, [linkedin, instagram])
    assert resp.status_code == status.HTTP_201_CREATED
    # Still "publishing", not "partial": Instagram hit a transient error and is
    # back in the queue, so the campaign is in flight rather than half-finished.
    assert resp.data["status"] == Campaign.Status.PUBLISHING

    posts = {p["provider"]: p for p in resp.data["posts"]}
    assert posts["linkedin"]["status"] == CampaignPost.Status.PUBLISHED
    assert posts["instagram"]["status"] == CampaignPost.Status.PENDING
    assert posts["instagram"]["attempts"] == 1


def test_retry_finishes_a_partial_campaign_without_reposting(
    api, trainer, course, linkedin, instagram, monkeypatch, stub_publish
):
    sent = stub_publish()

    def meta_fails(account, content):
        raise ProviderAuthError("Instagram needs a reconnect.")

    monkeypatch.setattr(meta_adapter, "publish", meta_fails)
    created = _create(api, trainer, course, [linkedin, instagram])
    assert created.data["status"] == Campaign.Status.PARTIAL
    assert len(sent) == 1  # only LinkedIn got through

    # Fix the account and the adapter, then retry.
    instagram.status = SocialAccount.Status.CONNECTED
    instagram.last_error = ""
    instagram.save(update_fields=["status", "last_error"])
    stub_publish()

    resp = auth(api, trainer).post(
        reverse("social:campaign-retry", args=[created.data["id"]])
    )
    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["status"] == Campaign.Status.PUBLISHED

    # LinkedIn published before the retry and must not be sent again. The stub
    # records into the same list across re-installation, so this counts every
    # call either adapter has ever received.
    assert [s["provider"] for s in sent].count("linkedin") == 1


def test_retry_refuses_when_there_is_nothing_to_retry(
    api, trainer, course, linkedin, stub_publish
):
    stub_publish()
    created = _create(api, trainer, course, [linkedin])

    resp = auth(api, trainer).post(
        reverse("social:campaign-retry", args=[created.data["id"]])
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_a_disconnected_account_fails_its_post_without_crashing(
    trainer, course, linkedin, stub_publish
):
    stub_publish()
    campaign = _schedule(
        course, trainer, linkedin, timezone.now() + timedelta(days=1)
    )
    linkedin.delete()

    campaign.scheduled_for = None
    campaign.save(update_fields=["scheduled_for"])
    publishing.run_due()

    post = campaign.posts.get()
    assert post.account_id is None
    assert post.provider == Provider.LINKEDIN  # the row still reads sensibly
    assert "disconnected" in post.error


# --------------------------------------------------------------------------- #
# Custom (uploaded) campaign image
# --------------------------------------------------------------------------- #

BUCKET_URL = "https://pub-abc123.r2.dev/promo-banners/1/2026/09/deadbeef-banner.png"


@pytest.fixture
def own_bucket(settings):
    settings.AWS_STORAGE_BUCKET_NAME = "test-bucket"
    settings.AWS_S3_CUSTOM_DOMAIN = "pub-abc123.r2.dev"
    return settings


def test_custom_image_url_is_used_as_the_posted_art(
    api, trainer, course, linkedin, own_bucket, stub_publish
):
    stub_publish()
    resp = _create(
        api, trainer, course, [linkedin],
        image_source="custom", image_url=BUCKET_URL,
    )
    assert resp.status_code == status.HTTP_201_CREATED
    assert resp.data["image_source"] == "custom"
    assert resp.data["image_url"] == BUCKET_URL


def test_custom_image_satisfies_instagrams_image_requirement(
    api, trainer, instagram, own_bucket, stub_publish, trainer_course_without_cover
):
    """The whole point of the feature: a cover-less course can reach Instagram."""
    stub_publish()
    resp = _create(
        api, trainer, trainer_course_without_cover, [instagram],
        image_source="custom", image_url=BUCKET_URL,
    )
    assert resp.status_code == status.HTTP_201_CREATED


@pytest.fixture
def trainer_course_without_cover(trainer):
    return Course.objects.create(
        title="Course With No Cover",
        trainer=trainer,
        is_free=True,
        thumbnail="",
        status=Course.Status.PUBLISHED,
    )


def test_instagram_still_refused_when_there_is_no_image_at_all(
    api, trainer, instagram, own_bucket, trainer_course_without_cover
):
    resp = _create(
        api, trainer, trainer_course_without_cover, [instagram], image_source="none"
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_foreign_image_url_is_refused(
    api, trainer, course, linkedin, own_bucket
):
    """An arbitrary host would be an SSRF and would let a caller put anybody's
    content on our trainer's feed."""
    resp = _create(
        api, trainer, course, [linkedin],
        image_source="custom", image_url="https://evil.example.com/x.png",
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "image_url" in resp.data


def test_custom_source_without_a_url_is_refused(
    api, trainer, course, linkedin, own_bucket
):
    resp = _create(api, trainer, course, [linkedin], image_source="custom")
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "image_url" in resp.data


def test_stray_image_url_cannot_override_a_thumbnail_choice(
    api, trainer, course, linkedin, own_bucket, stub_publish
):
    stub_publish()
    resp = _create(
        api, trainer, course, [linkedin],
        image_source="thumbnail", image_url=BUCKET_URL,
    )
    assert resp.status_code == status.HTTP_201_CREATED
    assert resp.data["image_url"] == course.thumbnail
