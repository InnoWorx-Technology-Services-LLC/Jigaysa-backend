# Jitsi Meet + Jibri — Dedicated Instance Deployment

Stand up `meet.jigaysa.com` on its **own server**, and wire it to the LMS so
only enrolled learners can join a live session.

> ### Why a separate instance
>
> A Jitsi install already exists on the shared application server
> (`103.174.103.210`) serving `meet.is3.jigaysa.com`. Adding a second vhost to
> it was the alternative, and it is the worse option:
>
> * **One Prosody, one jicofo.** Adding a vhost means editing config shared with
>   a live service and restarting it. Every change risks another client's
>   classes.
> * **That box is already saturated.** ~25 gunicorn services, MySQL, PostgreSQL,
>   Redis, nginx and Odoo, with swap nearly full. Jitsi is real-time — a
>   swapping videobridge means jitter and dropped audio.
> * **Jibri is the dealbreaker.** Each Jibri runs a full Xorg + Chrome + ffmpeg
>   and pins ~2 cores for the *entire* recording. On a 4-core shared box, one
>   recorded class degrades every other site on the machine.
>
> A dedicated instance removes all three problems and makes the media server
> independently restartable, upgradable and scalable.

---

## 1. Topology

```
                    ┌─────────────────────────────┐
  learners ────────▶│  meet.jigaysa.com  (NEW VM) │
   (browser)        │  nginx · prosody · jicofo   │
                    │  jitsi-videobridge2         │
                    └──────────┬──────────────────┘
                               │ XMPP 5222 (private / firewalled)
                    ┌──────────▼──────────────────┐
                    │  jibri-01  (NEW VM, later)  │
                    │  Xorg · Chrome · ffmpeg     │
                    └──────────┬──────────────────┘
                               │ finalize hook → S3/R2
                    ┌──────────▼──────────────────┐
                    │  Django LMS (existing box)  │
                    │  mints JWT · stores Recording│
                    └─────────────────────────────┘
```

Django never proxies media. It only issues signed tokens and records metadata.

## 2. Sizing

| Role | vCPU | RAM | Disk | Network |
|---|---|---|---|---|
| Jitsi (signalling + JVB) | 4 | 8 GB | 60 GB SSD | **1 Gbps, unmetered preferred** |
| Jibri (per concurrent recording) | 4 | 8 GB | 100 GB SSD | 100 Mbps |

**Bandwidth is the ceiling, not CPU.** Jitsi is an SFU — it forwards streams
rather than transcoding. A 100-participant class is roughly 50–100 Mbps of
egress. Confirm the provider's port speed *and* monthly transfer cap before
sizing anything else.

**One Jibri handles exactly one recording at a time.** Two simultaneous recorded
classes need two Jibri instances. Scale by adding VMs; they register themselves
with Prosody and jicofo hands work to whichever is idle.

Budget option: a single 8 vCPU / 16 GB VM running both. It works, but a
recording will measurably degrade call quality on that same box. Acceptable for
pilot, not for production.

## 3. Prerequisites

- [ ] Ubuntu 22.04 LTS (matches the existing estate) on a fresh VM
- [ ] Root / sudo
- [ ] **`meet.jigaysa.com` A record repointed** from `103.174.103.210` to the new VM.
      It currently resolves to the shared box — repoint it *before* running certbot.
- [ ] Firewall:

| Port | Proto | Source | Purpose |
|---|---|---|---|
| 22 | TCP | your IPs | SSH |
| 80 | TCP | any | certbot HTTP-01 |
| 443 | TCP | any | web + XMPP-over-WebSocket |
| **10000** | **UDP** | **any** | **JVB media — the one everybody forgets** |
| 5222 | TCP | **Jibri VM only** | Jibri ↔ Prosody. Never public. |

If UDP 10000 is blocked, calls connect and then have no audio or video. Test it
explicitly (§9).

---

## 4. Phase 1 — Base server

```bash
sudo apt update && sudo apt upgrade -y
sudo hostnamectl set-hostname meet.jigaysa.com
sudo apt install -y gnupg2 curl ca-certificates lsb-release apt-transport-https ufw

# Hostname must resolve locally or prosody's cert generation misbehaves
echo "127.0.0.1 meet.jigaysa.com" | sudo tee -a /etc/hosts

sudo ufw allow 22/tcp && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp
sudo ufw allow 10000/udp
sudo ufw enable
```

Jitsi is latency-sensitive; do not run a swap-heavy workload beside it.
Leave swappiness low:

```bash
echo "vm.swappiness=10" | sudo tee /etc/sysctl.d/99-jitsi.conf
sudo sysctl --system
```

## 5. Phase 2 — Jitsi Meet

```bash
curl -sL https://download.jitsi.org/jitsi-key.gpg.key \
  | sudo gpg --dearmor -o /usr/share/keyrings/jitsi-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/jitsi-keyring.gpg] https://download.jitsi.org stable/" \
  | sudo tee /etc/apt/sources.list.d/jitsi-stable.list
sudo apt update

sudo apt install -y jitsi-meet
```

The installer prompts for:
1. **Hostname** → `meet.jigaysa.com`
2. **Certificate** → *"Generate a new self-signed certificate"*, then replace it
   with Let's Encrypt in the next step.

```bash
sudo /usr/share/jitsi-meet/scripts/install-letsencrypt-cert.sh
```

Verify before going further — open `https://meet.jigaysa.com`, start a room,
join from a second device on a **different network** (not the same LAN; that
hides UDP problems).

### Advertise the public IP if behind NAT

Most cloud VMs are. In `/etc/jitsi/videobridge/sip-communicator.properties`:

```properties
org.ice4j.ice.harvest.NAT_HARVESTER_LOCAL_ADDRESS=<private IP>
org.ice4j.ice.harvest.NAT_HARVESTER_PUBLIC_ADDRESS=<public IP>
```

Skip both lines if the VM holds its public IP directly. Getting this wrong is
the single most common cause of "connects, then no video".

## 6. Phase 3 — JWT authentication

**This is the part that makes it an LMS feature rather than a public meeting
server.** Without it, the room URL is the only access control and anyone holding
a link joins any class.

```bash
sudo apt install -y jitsi-meet-tokens
```

The installer asks for an **app ID** and **app secret**. Use:

- App ID: `jigyasa`
- App secret: generate one and keep it — `openssl rand -hex 32`

Confirm `/etc/prosody/conf.d/meet.jigaysa.com.cfg.lua` contains:

```lua
VirtualHost "meet.jigaysa.com"
    authentication = "token"
    app_id = "jigyasa"
    app_secret = "<the secret>"
    allow_empty_token = false        -- no token, no entry
    modules_enabled = {
        "bosh";
        "websocket";
        "smacks";
    }
```

`allow_empty_token = false` is the line that matters. With it `true`, tokens are
optional and the whole exercise is decorative.

Do **not** add an anonymous `guest.` vhost. Every participant in this deployment
is a known LMS user.

```bash
sudo systemctl restart prosody jicofo jitsi-videobridge2
```

At this point `https://meet.jigaysa.com/anything` should **refuse** to let you
in. That refusal is the feature working.

---

## 7. Phase 4 — Django integration

No new dependency: **PyJWT 2.13.0** is already installed (via
`djangorestframework-simplejwt`).

### 7.1 Settings

Add to `Jigaysa/settings.py`, following the existing `env()` pattern:

```python
# Live sessions are hosted on a dedicated Jitsi instance. Leave
# JITSI_APP_SECRET empty to disable the integration; `join` then falls back to
# the stored join_url, exactly as RAZORPAY_KEY_ID gates the payment gateway.
JITSI_DOMAIN = env("JITSI_DOMAIN", default="")
JITSI_APP_ID = env("JITSI_APP_ID", default="jigyasa")
JITSI_APP_SECRET = env("JITSI_APP_SECRET", default="")
JITSI_TOKEN_TTL_MINUTES = env.int("JITSI_TOKEN_TTL_MINUTES", default=240)
```

And to `.env` / `.env.example`:

```ini
JITSI_DOMAIN=meet.jigaysa.com
JITSI_APP_ID=jigyasa
JITSI_APP_SECRET=
JITSI_TOKEN_TTL_MINUTES=240
```

### 7.2 New file — `live/meeting.py`

```python
"""Jitsi as the live-session meeting provider (PRD §3.5).

Django never touches media. It does exactly two things: name the room, and
sign a short-lived token saying who may enter it and with what powers.

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


def is_enabled() -> bool:
    """Both a domain and a secret, or the integration is off.

    Mirrors ``RAZORPAY_KEY_ID``: an unconfigured deployment keeps the old
    behaviour instead of raising, so this can ship before the server exists.
    """
    return bool(settings.JITSI_DOMAIN and settings.JITSI_APP_SECRET)


def room_name(session) -> str:
    """A stable, unguessable room id for a session.

    Stable so a reconnecting learner lands back in the same room; unguessable
    because a predictable name (``session-42``) is a room anyone can walk into
    the moment token auth is ever relaxed. Derived, not stored, so it cannot
    drift from the session it names.
    """
    digest = hmac.new(
        settings.SECRET_KEY.encode(),
        f"live-session:{session.pk}".encode(),
        hashlib.sha256,
    ).hexdigest()[:12]
    return f"jigyasa-{session.pk}-{digest}"


def issue_join(session, user):
    """``(join_url, room, token)`` for one person joining one session.

    The trainer is the **moderator**; everyone else is a plain participant.
    Without that split any learner can mute, kick, or end the class.
    """
    room = room_name(session)
    moderator = session.trainer_id == user.pk

    now = timezone.now()
    payload = {
        "aud": settings.JITSI_APP_ID,
        "iss": settings.JITSI_APP_ID,
        "sub": settings.JITSI_DOMAIN,
        "room": room,
        # Small backdate absorbs clock skew between this host and Prosody.
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
                # Only the trainer may start a recording. A learner who can
                # record a class is a privacy incident waiting to happen.
                "recording": moderator,
                "livestreaming": False,
                "transcription": False,
            },
        },
    }
    token = jwt.encode(payload, settings.JITSI_APP_SECRET, algorithm="HS256")
    return f"https://{settings.JITSI_DOMAIN}/{room}?jwt={token}", room, token
```

### 7.3 Changed — `live/views.py`

The existing `join` action already refuses unregistered students. Keep that
check; it becomes real enforcement once the room itself is gated.

```python
    @action(detail=True, methods=["post"])
    def join(self, request, pk=None):
        """Mark the current user as joined and return the join URL."""
        session = self.get_object()
        registration = session.registrations.filter(student=request.user).first()
        if registration is None and getattr(request.user, "role", None) == "student":
            raise ValidationError("Register for this session before joining.")
        if registration and not registration.joined_at:
            registration.joined_at = timezone.now()
            registration.attended = True
            registration.save(update_fields=["joined_at", "attended", "updated_at"])

        if meeting.is_enabled():
            # Minted per call and short-lived: a join URL is a bearer
            # credential, so it must not outlive the class it opens.
            join_url, room, _ = meeting.issue_join(session, request.user)
            if session.meeting_id != room:
                session.meeting_id = room
                session.save(update_fields=["meeting_id", "updated_at"])
            return Response({"join_url": join_url, "meeting_id": room})

        return Response(
            {"join_url": session.join_url, "meeting_id": session.meeting_id}
        )
```

### 7.4 Tests worth writing

- a registered student gets a URL whose JWT carries `affiliation: member`
- the session's trainer gets `affiliation: owner` and `moderator: true`
- an unregistered student is still refused before any token is minted
- the token's `room` claim matches the returned room, and `exp` is in the future
- with `JITSI_APP_SECRET` empty, the response is byte-identical to today's

### 7.5 Frontend

Either redirect the browser to `join_url`, or embed with
`https://meet.jigaysa.com/external_api.js` and pass `jwt` in the options. The
backend contract is the same; embedding just keeps learners inside the LMS
shell.

---

## 8. Phase 5 — Jibri (recording)

> **Deferred.** Do this only after §4–§7 are working in production. Recording is
> the heaviest and most fragile component, and it is far easier to debug when
> plain calls are known good.

### 8.1 Audio loopback

Jibri captures audio through an ALSA loopback device. No loopback, no sound on
any recording.

```bash
echo "snd-aloop" | sudo tee -a /etc/modules
sudo modprobe snd-aloop
lsmod | grep snd_aloop     # must print a line
```

On some kernels this needs `linux-image-extra-virtual`.

### 8.2 Chrome + driver

```bash
curl -sS -o /tmp/google.pub https://dl.google.com/linux/linux_signing_key.pub
sudo gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg /tmp/google.pub
echo "deb [signed-by=/usr/share/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
  | sudo tee /etc/apt/sources.list.d/google-chrome.list
sudo apt update && sudo apt install -y google-chrome-stable ffmpeg curl unzip

# chromedriver MUST match the installed Chrome major version
google-chrome --version
```

A chromedriver/Chrome version mismatch is the most common Jibri failure and it
surfaces as a recording that silently never starts.

### 8.3 Install and register

```bash
sudo apt install -y jibri
sudo usermod -aG adm,audio,video,plugdev jibri
```

On the **Jitsi** host, create the two XMPP accounts Jibri needs — one to be
controlled, one to sit in the call:

```bash
sudo prosodyctl register jibri auth.meet.jigaysa.com <PASSWORD_CONTROL>
sudo prosodyctl register recorder recorder.meet.jigaysa.com <PASSWORD_CALL>
```

Then in `/etc/jitsi/jibri/jibri.conf` on the Jibri host, set the XMPP
environment to point at `meet.jigaysa.com` with those credentials, and the
recording directory:

```hocon
jibri {
  recording {
    recordings-directory = "/srv/recordings"
    finalize-script = "/etc/jitsi/jibri/finalize_recording.sh"
  }
}
```

```bash
sudo mkdir -p /srv/recordings && sudo chown jibri:jibri /srv/recordings
sudo systemctl enable --now jibri
```

### 8.4 Enable the recording button

In `/etc/jitsi/meet/meet.jigaysa.com-config.js`:

```javascript
fileRecordingsEnabled: true,
hiddenDomain: 'recorder.meet.jigaysa.com',
```

`hiddenDomain` is what keeps the recorder from appearing as a ghost participant
in the call.

## 9. Phase 6 — recordings back into the LMS

The models already exist and are **not currently routed** —
[`recordings/urls.py`](../recordings/urls.py) is commented out at
[`Jigaysa/urls.py:32`](../Jigaysa/urls.py#L32). Mount it when this phase ships.

`recordings.Recording` already has `session`, `video_url`, `cdn_url`,
`duration_seconds` and `status` (`processing` → `ready`), and
`live.LiveSession.recording` points at it. Nothing new is needed modelling-wise.

The pipeline:

1. Jibri finishes and runs `finalize_recording.sh` with the recording directory.
2. The script uploads the `.mp4` to the **existing** S3-compatible bucket —
   `AWS_STORAGE_BUCKET_NAME` / `AWS_S3_ENDPOINT_URL` are already configured for
   R2 in `settings.py`.
3. The script POSTs to a new internal endpoint, e.g.
   `POST /api/v1/internal/recordings/`, with the room name, object key and
   duration.
4. Django maps the room back to the session — the room name embeds the session
   pk (`jigyasa-<pk>-<hmac>`), and `LiveSession.meeting_id` stores it — creates
   the `Recording` with `status=ready`, and links it.

**Secure that endpoint with a shared secret header, not IP allowlisting.**
It creates rows on behalf of a machine; treat it as a webhook. Jibri also
retries, so make it idempotent on the room + start-time pair or one class ends
up with three recordings.

---

## 10. Verification checklist

| # | Check | Expected |
|---|---|---|
| 1 | `https://meet.jigaysa.com` loads | Jitsi UI |
| 2 | Join a room **without** a token | refused |
| 3 | `nc -zvu <public-ip> 10000` from outside | open |
| 4 | Two devices on **different networks** join | audio + video both ways |
| 5 | `POST /api/v1/live-sessions/{id}/join/` as a registered student | 200 with `join_url` |
| 6 | Same as an **unregistered** student | 400, no token minted |
| 7 | Decode the JWT at jwt.io | `room`, `exp`, correct `affiliation` |
| 8 | Trainer joins | has moderator controls |
| 9 | `sudo systemctl status jitsi-videobridge2 jicofo prosody` | all active |
| 10 | (Jibri) start a recording | appears in `/srv/recordings` |

Test 4 is the one that catches NAT and firewall mistakes. Two tabs on the same
machine will pass even when UDP 10000 is completely blocked.

## 11. Operations

**Watch:** JVB conference/participant counts (`/colibri/stats` on the bridge),
UDP 10000 packet loss, and load average during classes.

**Back up:** `/etc/prosody/`, `/etc/jitsi/`, `/etc/nginx/sites-available/`.
Certs renew via certbot's timer — verify with `systemctl list-timers | grep certbot`.

**Scale:** add JVB instances before touching signalling. One Prosody/jicofo pair
handles many bridges; jicofo load-balances across them.

**Rotate `JITSI_APP_SECRET`** if it ever leaks — it is the only thing standing
between a URL and a classroom. Rotating invalidates live tokens, so do it
between classes.

## 12. Cutover

`meet.jigaysa.com` currently resolves to the shared box (`103.174.103.210`),
which has no vhost for it. Nothing in the LMS points at it yet, so there is no
live traffic to migrate:

1. Build the new VM and get it working on a temporary name or by `/etc/hosts`.
2. Repoint the `meet.jigaysa.com` A record; wait out the TTL.
3. Run certbot on the new host.
4. Set `JITSI_APP_SECRET` in the LMS `.env` and restart Django — this is the
   switch that turns the integration on.

`meet.is3.jigaysa.com` on the shared box is untouched throughout. Nothing in
this runbook modifies that server.
