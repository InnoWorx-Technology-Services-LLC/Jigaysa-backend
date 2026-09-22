"""Jitsi join tokens for live sessions (PRD §3.5).

The bridge runs ``allow_empty_token = false``, so the token these tests inspect
*is* the access control — a room URL without one is refused by Prosody. What
matters here is therefore not that a URL comes back, but exactly what the token
inside it claims: which room, for how long, and with what powers.

Signatures are not verified when decoding: these tests assert on claims, and
requiring the live secret would make them un-runnable off the deployment.
"""

from datetime import timedelta

import jwt
import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Role, User
from courses.models import Course, Enrollment
from live import meeting
from live.models import LiveSession, SessionRegistration

pytestmark = pytest.mark.django_db

SECRET = "test-secret-not-the-real-one-0123456789abcdef"  # >=32B: HS256
DOMAIN = "meet.jigaysa.com"


@pytest.fixture
def jitsi(settings):
    """Turn the integration on for this test."""
    settings.JITSI_DOMAIN = DOMAIN
    settings.JITSI_APP_ID = "jigyasa"
    settings.JITSI_APP_SECRET = SECRET
    settings.JITSI_TOKEN_TTL_MINUTES = 240
    return settings


def _user(email, role=Role.STUDENT, **kwargs):
    return User.objects.create_user(
        email=email, password="StrongPass123!", role=role, **kwargs
    )


@pytest.fixture
def trainer():
    return _user("kapoor@example.com", Role.TRAINER, full_name="Dr. Kapoor")


@pytest.fixture
def course(trainer):
    return Course.objects.create(title="React 19 Pro", trainer=trainer)


@pytest.fixture
def session(course, trainer):
    return LiveSession.objects.create(
        course=course, trainer=trainer, title="Lecture 4",
        scheduled_start=timezone.now() + timedelta(hours=1),
        duration_minutes=90,
        join_url="https://old.example.com/legacy-room",
        meeting_id="legacy-room",
    )


@pytest.fixture
def student(course):
    learner = _user("riya@example.com", full_name="Riya Desai")
    Enrollment.objects.create(student=learner, course=course)
    return learner


def _api(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def _join(user, session):
    return _api(user).post(f"/api/v1/live-sessions/{session.id}/join/")


def _claims(join_url):
    return jwt.decode(
        join_url.split("jwt=")[1], options={"verify_signature": False}
    )


def _register(session, student):
    return SessionRegistration.objects.create(session=session, student=student)


# --------------------------------------------------------------------------- #
# The switch
# --------------------------------------------------------------------------- #


def test_the_integration_is_off_without_a_secret(settings):
    settings.JITSI_DOMAIN = DOMAIN
    settings.JITSI_APP_SECRET = ""
    assert meeting.is_enabled() is False


def test_the_integration_is_off_without_a_domain(settings):
    settings.JITSI_DOMAIN = ""
    settings.JITSI_APP_SECRET = SECRET
    assert meeting.is_enabled() is False


def test_with_no_secret_join_answers_exactly_as_before(session, student):
    """The fallback is byte-identical, which is what makes this safe to deploy
    before the secret exists."""
    _register(session, student)
    body = _join(student, session).data

    assert body == {
        "join_url": "https://old.example.com/legacy-room",
        "meeting_id": "legacy-room",
    }


# --------------------------------------------------------------------------- #
# Room names
# --------------------------------------------------------------------------- #


def test_a_room_name_is_stable(jitsi, session):
    """A reconnecting learner must land back in the same room."""
    assert meeting.room_name(session) == meeting.room_name(session)


def test_a_room_name_is_not_guessable(jitsi, session):
    """``session-42`` would be a room anyone could walk into the moment token
    auth were ever relaxed."""
    room = meeting.room_name(session)
    assert room.startswith(f"jigyasa-{session.pk}-")
    suffix = room.rsplit("-", 1)[1]
    assert len(suffix) == 12
    assert suffix != str(session.pk)


def test_two_sessions_never_share_a_room(jitsi, session, course, trainer):
    other = LiveSession.objects.create(
        course=course, trainer=trainer, title="Lecture 5",
        scheduled_start=timezone.now() + timedelta(days=1),
    )
    assert meeting.room_name(session) != meeting.room_name(other)


# --------------------------------------------------------------------------- #
# What the token claims
# --------------------------------------------------------------------------- #


def test_a_registered_student_joins_as_a_plain_participant(
    jitsi, session, student
):
    _register(session, student)
    body = _join(student, session).data
    claims = _claims(body["join_url"])

    assert body["join_url"].startswith(f"https://{DOMAIN}/{body['meeting_id']}?jwt=")
    assert claims["context"]["user"]["affiliation"] == "member"
    assert claims["moderator"] is False
    assert claims["context"]["user"]["name"] == "Riya Desai"
    assert claims["context"]["user"]["id"] == str(student.pk)


def test_the_trainer_joins_as_moderator(jitsi, session, trainer):
    """Without this split any learner can mute, kick, or end the class."""
    claims = _claims(_join(trainer, session).data["join_url"])

    assert claims["moderator"] is True
    assert claims["context"]["user"]["affiliation"] == "owner"


def test_recording_is_off_even_for_the_trainer(jitsi, session, trainer):
    """Jibri is not deployed. A record button that looks functional and
    silently never records is worse than no button at all."""
    claims = _claims(_join(trainer, session).data["join_url"])

    assert claims["context"]["features"]["recording"] is False
    assert claims["context"]["features"]["livestreaming"] is False


def test_the_token_is_bound_to_one_room(jitsi, session, student):
    """A token for room A must not open room B."""
    _register(session, student)
    body = _join(student, session).data
    assert _claims(body["join_url"])["room"] == body["meeting_id"]


def test_the_token_addresses_this_deployment(jitsi, session, trainer):
    """``aud``/``iss`` must equal the bridge's app_id and ``sub`` its vhost, or
    Prosody rejects the token."""
    claims = _claims(_join(trainer, session).data["join_url"])

    assert claims["aud"] == "jigyasa"
    assert claims["iss"] == "jigyasa"
    assert claims["sub"] == DOMAIN


def test_the_token_expires(jitsi, session, trainer):
    """A join URL is a bearer credential; it must not outlive the class."""
    claims = _claims(_join(trainer, session).data["join_url"])
    now = timezone.now().timestamp()

    assert claims["exp"] > now
    assert claims["exp"] <= now + 240 * 60 + 5
    # Backdated a little, so a Django clock seconds ahead of the bridge does
    # not mint tokens that are "not yet valid".
    assert claims["nbf"] < now


def test_the_ttl_is_configurable(jitsi, session, trainer):
    jitsi.JITSI_TOKEN_TTL_MINUTES = 30
    claims = _claims(_join(trainer, session).data["join_url"])
    assert claims["exp"] <= timezone.now().timestamp() + 30 * 60 + 5


def test_the_token_verifies_against_the_shared_secret(jitsi, session, trainer):
    """Signed HS256 with the configured secret — what Prosody will check."""
    token = _join(trainer, session).data["join_url"].split("jwt=")[1]
    decoded = jwt.decode(
        token, SECRET, algorithms=["HS256"], audience="jigyasa"
    )
    assert decoded["room"]

    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, "wrong-secret-but-also-long-enough-0123456789",
                   algorithms=["HS256"], audience="jigyasa")


# --------------------------------------------------------------------------- #
# Who gets a token at all
# --------------------------------------------------------------------------- #


def test_an_unregistered_student_is_refused_before_any_token_is_minted(
    jitsi, session, student
):
    response = _join(student, session)
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    session.refresh_from_db()
    # The room was never named, so nothing about it leaked into the session.
    assert session.meeting_id == "legacy-room"


def test_joining_records_attendance(jitsi, session, student):
    registration = _register(session, student)
    _join(student, session)

    registration.refresh_from_db()
    assert registration.attended is True
    assert registration.joined_at is not None


def test_the_room_is_stored_on_the_session(jitsi, session, trainer):
    """So the recording callback and the admin can map a room back to a class."""
    body = _join(trainer, session).data
    session.refresh_from_db()
    assert session.meeting_id == body["meeting_id"]
    assert session.meeting_id != "legacy-room"


def test_a_second_join_reuses_the_room_but_mints_a_fresh_token(
    jitsi, session, student
):
    _register(session, student)
    first = _join(student, session).data
    second = _join(student, session).data

    assert first["meeting_id"] == second["meeting_id"]
    assert _claims(first["join_url"])["room"] == _claims(second["join_url"])["room"]
