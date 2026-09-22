"""Jitsi as the live-session meeting provider (PRD §3.5).

Django never touches media. It does exactly two things: name the room, and sign
a short-lived token saying who may enter it and with what powers. The bridge at
``meet.jigaysa.com`` runs ``allow_empty_token = false``, so a join without one
of these tokens is refused by Prosody — the token *is* the access control, and
the registration check in the view is what decides whether to mint one.

Kept in its own module rather than inside the view so the provider is a seam:
swapping Jitsi for something else later is a new ``issue_join`` and nothing
more.
"""

import hashlib
import hmac
from datetime import timedelta

import jwt
from django.conf import settings
from django.utils import timezone

#: Recording is off for **everyone, including the trainer**, because Jibri is
#: not deployed. The `recorder.` vhost exists on the server, but the base
#: ``jitsi-meet`` install creates that whether or not a Jibri ever joins it —
#: its presence is not evidence that recording works.
#:
#: Setting this ``True`` without a Jibri behind it renders a record button that
#: looks functional and silently never records, which is a worse failure than
#: not offering it at all. Flip it to ``moderator`` in ``issue_join`` on the day
#: a Jibri instance exists, not before.
RECORDING_AVAILABLE = False


def is_enabled() -> bool:
    """Both a domain and a secret, or the integration is off.

    Mirrors ``RAZORPAY_KEY_ID``: an unconfigured deployment keeps the old
    behaviour instead of raising, so this ships safely before the secret is in
    place and turns itself on the moment it is.
    """
    return bool(settings.JITSI_DOMAIN and settings.JITSI_APP_SECRET)


def room_name(session) -> str:
    """A stable, unguessable room id for a session.

    Stable so a reconnecting learner lands back in the same room; unguessable
    because a predictable name (``session-42``) is a room anyone can walk into
    the moment token auth is ever relaxed. Derived rather than stored so it
    cannot drift from the session it names.

    Keyed on ``SECRET_KEY``: rotating that changes every *newly issued* room
    name. Sessions that already stored a ``meeting_id`` keep theirs.
    """
    digest = hmac.new(
        settings.SECRET_KEY.encode(),
        f"live-session:{session.pk}".encode(),
        hashlib.sha256,
    ).hexdigest()[:12]
    return f"jigyasa-{session.pk}-{digest}"


def issue_join(session, user):
    """``(join_url, room, token)`` for one person joining one session.

    The session's trainer is the **moderator**; everyone else is a plain
    participant. Without that split any learner can mute, kick, or end the
    class.

    The token is bound to a single room and expires — a join URL is a bearer
    credential and must not outlive the class it opens.
    """
    room = room_name(session)
    moderator = session.trainer_id == user.pk

    now = timezone.now()
    payload = {
        # Both must equal the server's ``app_id`` or Prosody rejects the token.
        "aud": settings.JITSI_APP_ID,
        "iss": settings.JITSI_APP_ID,
        # The vhost, matched exactly.
        "sub": settings.JITSI_DOMAIN,
        "room": room,
        # Backdated a little: a Django host whose clock runs seconds ahead of
        # the bridge would otherwise mint tokens that are "not yet valid".
        "nbf": int(now.timestamp()) - 10,
        "exp": int(
            (now + timedelta(minutes=settings.JITSI_TOKEN_TTL_MINUTES)).timestamp()
        ),
        "moderator": moderator,
        "context": {
            "user": {
                "id": str(user.pk),
                "name": user.full_name or user.email,
                "email": user.email,
                "affiliation": "owner" if moderator else "member",
            },
            "features": {
                "recording": RECORDING_AVAILABLE and moderator,
                "livestreaming": False,
                "transcription": False,
            },
        },
    }
    token = jwt.encode(payload, settings.JITSI_APP_SECRET, algorithm="HS256")
    return f"https://{settings.JITSI_DOMAIN}/{room}?jwt={token}", room, token
