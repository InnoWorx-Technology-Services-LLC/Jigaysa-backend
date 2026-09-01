# Backend changelog — what has been fixed

Everything built in this working session, newest first. Written for the frontend
team: each entry says what changed, what it unblocks, and what you now call.

Nothing here is committed yet — it is all in the working tree.

---

## Course promotion — the Promote wizard, end to end

The **Schedule** step now has a backend. Templates, per-network copy, campaigns,
scheduled publishing and retries, for **LinkedIn, Facebook and Instagram**.
Full reference: `docs/SOCIAL_API.md`.

### ✅ What you can call now

| Endpoint | Who | Purpose |
|---|---|---|
| `GET /api/v1/social/templates/?course=<slug>` | trainer, admin | wizard steps 1 **and** 2 in one call — five cards, copy already rendered |
| `POST /api/v1/social/rewrite/` | trainer, admin | the Rewrite button; reshapes a caption per network |
| `GET/POST /api/v1/social/campaigns/` | trainer, admin | the Promotions tab; and step 4 — publish now or schedule |
| `GET/PATCH/DELETE /api/v1/social/campaigns/{id}/` | trainer, admin | detail, edit/move while scheduled, cancel |
| `POST /api/v1/social/campaigns/{id}/retry/` | trainer, admin | finish a partly-published campaign |

`X` and `YouTube` are still unimplemented; naming one of their accounts is a `400`.

### ⚠️ Four things that will surprise the frontend

1. **`publish_at` is the entire difference between the two radio buttons.**
   Omit it or send `null` for *Publish now*, send a timestamp for *Schedule for
   later*. No `mode` field, no second endpoint.
2. **`accounts` are `SocialAccount` ids, not provider names.** A connection is a
   destination — same rule as the connect page. Two Pages means two ids.
3. **`partial` is a real campaign status.** A launch that reached LinkedIn and
   not Instagram is half-shipped. Render "1 of 2 published" with a Retry; showing
   it as failed makes trainers post the LinkedIn copy twice.
4. **A post back in `pending` after a failure is not stuck.** A network having a
   bad minute gets retried by the sweep. `failed` means it has stopped trying.

### 🧠 What the wizard enforces before it schedules anything

Every one of these is a `400` at the Schedule button rather than a silent
failure at 2am: an account that isn't yours, an account needing a reconnect, an
`x`/`youtube` account, Instagram with no image, a thumbnail source on a course
with no cover, a `publish_at` in the past, a `publish_at` more than a year out.

### 🔁 Scheduling needs a cron entry

    python manage.py publish_due_campaigns

Every five minutes. Without it, **"Schedule for later" does nothing** — there is
no Celery on this deployment. Safe to overlap with itself and with an inline
publish: each post is claimed with a conditional UPDATE (`pending` →
`publishing`) whose row count decides ownership, so a second run finds nothing
to take and no post goes out twice.

The same sweep finishes a *Publish now* whose HTTP request timed out mid-fan-out
— those posts have no schedule and are due immediately, which makes a timeout a
delay rather than a lost post.

`--dry-run` lists what would go out; `--limit N` bounds a run.

### 🙈 Two honest gaps

- **"Auto promo card" is not built.** It needs a server-side raster pipeline
  that does not exist. `image_options` reports it with the same `available`
  idiom as the provider cards, so grey the button out; posting with
  `image_source: "promo_card"` is a `400`.
- **"Rewrite" is not AI.** There is no model in this project. It is a
  rule-based reshape: hashtag placement, the caption ceiling, and what happens
  to the link. Label the button accordingly — "Format for LinkedIn" is honest,
  anything implying generated copy is not.

### 🌐 What each network actually receives

Composed per destination at publish time, never stored pre-composed — one
campaign fans out to networks that disagree about links.

- **LinkedIn** — posts as the member. Image uploaded via their Images API
  (they will not fetch a URL); if that upload fails the text still goes out,
  because a post without its picture beats no post.
- **Facebook** — posts to the **Page**. With an image it goes through
  `/photos`, which fetches the URL itself.
- **Instagram** — container then publish, two calls. **The image is mandatory**
  and any URL in the caption is replaced with "Link in bio", because links in IG
  captions are not tappable.

### 🔒 Failure handling

A dead credential fails **once**, not three times, and marks the account so the
connect page shows "Reconnect" — retrying a revoked token is a loop, not
resilience. Because an Instagram row shares its Page's token, marking one marks
the other; otherwise the trainer fixes one card and the other keeps failing.

Disconnecting an account does **not** erase what was published through it
(`SET_NULL`, with the provider and label denormalised onto the post).

### Config

New in `.env` (see `.env.example`): `LINKEDIN_API_VERSION` (their REST API is
versioned by month and rejects a call without it) and `SOCIAL_COURSE_URL_PATH`
(the public course path a promoted post links to — joined to the *origin* of
`FRONTEND_URL`, for the same reason the OAuth redirect is). Migration:
`social/0002_campaign_campaignpost_and_more`. No new packages.

> **Still not shippable to real trainers.** Publishing permissions
> (`w_member_social`, `pages_manage_posts`, `instagram_content_publish`) remain
> review-gated. This works today only for accounts listed as testers on the
> developer apps.

---

## Social accounts — trainer panel connect page

Backs **Account → Social accounts** and the **Accounts** step of the Promote
wizard. New `social` app. Full reference: `docs/SOCIAL_API.md`.

### ✅ What you can call now

| Endpoint | Who | Purpose |
|---|---|---|
| `GET /api/v1/social/accounts/` | trainer, admin | every provider card + your connections, one call |
| `POST /api/v1/social/connect/{provider}/` | trainer, admin | returns the `authorize_url` to send the browser to |
| `GET /api/v1/social/callback/{provider}/` | *the network* | never called by you; always 302s back to the frontend |
| `DELETE /api/v1/social/accounts/{id}/` | trainer, admin | revoke upstream, then delete |

LinkedIn, Facebook and Instagram are implemented. `X` and `YouTube` return
`available: false` so their cards render through the same code path.

### ⚠️ Two things that will surprise the frontend

1. **Facebook and Instagram are one Meta authorization.** Two cards, one consent.
   One round-trip commonly returns a Facebook Page *and* its linked Instagram
   account — the callback reports `?connected=facebook,instagram`. Always
   re-fetch the account list on return rather than assuming only the clicked
   card changed.
2. **A connection is a destination, not a network.** Two Pages means two
   Facebook rows, each with its own name and avatar. The wizard's "Post to:"
   list is a list of destinations.

### 🔒 Security notes

- OAuth tokens are **encrypted at rest** (Fernet, `social/crypto.py`) and are
  never serialized — not write-only, not hinted. They are only ever written by
  the callback.
- The callback is unauthenticated because a browser redirect cannot carry a JWT.
  A **signed, timestamped `state`** stands in for auth: it names the user, can't
  be forged without `SECRET_KEY`, and expires after 10 minutes.
- `return_to` is validated as a site-relative path in two places — an open
  redirect wearing the platform's own domain is worth checking twice.

### 🐛 Fixed along the way

`FRONTEND_URL` is `https://lms.jigyaasaa.com/student` — it carries a path. Naive
concatenation would have sent every completed connection to
`/student/trainer/settings/social`, a 404 at the very end of a flow that
otherwise worked. The redirect builder now uses the **origin** of `FRONTEND_URL`
and discards its path.

### Config

New in `.env` (see `.env.example`): `LINKEDIN_CLIENT_ID` / `_SECRET`,
`META_APP_ID` / `META_APP_SECRET`, `META_GRAPH_VERSION`,
`SOCIAL_OAUTH_REDIRECT_BASE`, `SOCIAL_TOKEN_KEY`. New requirements: `requests`,
`cryptography`. Migration: `social/0001_initial`.

**Blank credentials are a supported state** — the card renders "not configured"
and connect returns `503`, rather than failing mid-flow.

> **Not shippable to real trainers yet.** `w_member_social`,
> `pages_manage_posts` and `instagram_content_publish` are all review-gated.
> Until Meta and LinkedIn approve the apps (weeks, and Meta also wants Business
> Verification), this works only for accounts listed as testers on the developer
> app. Campaigns, scheduling and publishing are a separate piece of work.

---

## Round 4 — blockers from `BLOCKERS-2.md`

### ✅ Anonymous catalog reads (their §F — "the single biggest launch blocker")

A logged-out visitor and every SEO crawler used to get `401` on the whole
catalog. Reads are now open; **writes are untouched.**

| Now public (no token) | Still authenticated |
|---|---|
| `GET /courses/` | every write, `publish`, `reject`, `review-queue`, `archive` |
| `GET /courses/{slug}/` | `POST /courses/{slug}/enroll/` |
| `GET /courses/{slug}/curriculum/` | `/enrollments/`, `/lesson-progress/`, `/lesson-notes/` |
| `GET /categories/`, `GET /tags/` | `/orders/`, `/invoices/`, `/billing/summary/` |
| `GET /library-resources/` | `/library-bookmarks/`, community, notifications |

**Drafts do not leak.** The queryset now spells out the anonymous branch
explicitly — an unauthenticated caller sees `status=published` **and**
`visibility=public`, nothing else. A draft or `pending_review` course returns
`404`, not a redacted record.

**Preview curriculum works logged-out.** `curriculum` returns the same shape
with `has_access: false`; `is_preview` lessons keep their `video_url` and
`content`, everything else comes back `locked: true` with empty content. So the
public course page can play the free preview and lock the rest.

> One thing their proposal would have hit: `curriculum` looked up the caller's
> enrollment unconditionally, and filtering on an `AnonymousUser` **raises**.
> Adding `AllowAny` without guarding that would have turned a 401 into a 500.
> Guarded, with a test.

**Frontend action:** none. Remove the "Sign in to browse" fallback when you're
ready — these endpoints now return `200`.

### ✅ Trainer → mentor approval (their §2.2)

Was worse than reported: `is_approved` had **no API and no admin UI** — only a
database shell. And `TrainerProfile` was never created for anyone who registered
through the API, so `GET /mentors/` (which filters on that flag) could only ever
return an empty list. The entire 1:1 booking feature was unreachable.

Three things fixed:

1. **The profile now exists.** A `post_save` signal creates a `TrainerProfile`
   for every trainer account — on registration or on promotion to trainer. It is
   created **unapproved**; existing isn't the same as being allowed to teach.
2. **New API** at `/api/v1/trainer-profiles/`:

| Endpoint | Who | Purpose |
|---|---|---|
| `GET /trainer-profiles/me/` | trainer | own profile (created on first read) |
| `PATCH /trainer-profiles/me/` | trainer | set `expertise`, `years_experience`, `hourly_rate` |
| `GET /trainer-profiles/?is_approved=false` | admin | the pending-approval queue |
| `POST /trainer-profiles/{id}/approve/` | admin | make them bookable |
| `POST /trainer-profiles/{id}/unapprove/` | admin | delist them |

   `is_approved` is **read-only on the serializer** — a trainer PATCHing
   `{"is_approved": true}` onto their own profile is ignored. Approval only
   moves through the admin actions.
   The trainer gets a notification either way.

3. **Django admin** now registers `TrainerProfile` with an `is_approved` column,
   an inline tick-box, a filter and bulk approve/unapprove actions.

### ✅ Course media upload (their §2.1)

The presign endpoint hands back an object **key**, and its own docstring told you
to save it on `Course.thumbnail` — but that is a `URLField`, so saving a key
failed validation. `Lesson.video_key` was the only field modelled correctly.

Added `Course.thumbnail_key` and `Course.intro_video_key` (mirroring
`Lesson.video_key`). Serializers resolve them:

- **key set** → a plain **public CDN/bucket URL**
- **no key** → whatever URL was stored, unchanged

Cover art and intro video resolve to *public* URLs rather than presigned ones on
purpose: an anonymous visitor on the public catalog has no token to presign
with, and a signed URL would expire inside the page. Private teaching content
(`Lesson.video_key`) is still presigned per request.

**Flow:** `POST /uploads/presign/` (`purpose: course_thumbnail` /
`course_intro_video`) → PUT the bytes → `PATCH /courses/{slug}/`
`{"thumbnail_key": "<key>"}`.

### Not addressed from that document

- **C1 notification `link` paths** — confirmed inconsistent (`/certificates`,
  `/courses/{slug}`, `/live/{id}` are unprefixed; `/student/sessions`,
  `/trainer/sessions`, `/trainer/courses/{slug}` are prefixed). Not in scope this
  round.
- **C2 richer contributor stats** — no per-user reputation endpoint, no
  answer/question counts, no time-window ranking.
- **§3 `analytics` / `classrooms` / `recordings`** — still 0 paths.
  (`recordings` is written but deliberately unmounted in `Jigaysa/urls.py`.)

---



## Reference docs

| Doc | Covers |
|---|---|
| [COURSE_MODULE_API.md](COURSE_MODULE_API.md) | course lifecycle — student · trainer · admin |
| [COURSE_PAYMENT_FLOW.md](COURSE_PAYMENT_FLOW.md) | buying a course |
| [STUDENT_1TO1_BOOKING_API.md](STUDENT_1TO1_BOOKING_API.md) | mentor booking + pay-per-hour |
| [STUDENT_COMMUNITY_API.md](STUDENT_COMMUNITY_API.md) | forum, voting, reputation |
| [STUDENT_BILLING_PLANS_API.md](STUDENT_BILLING_PLANS_API.md) | plans, entitlements, billing |
| [STUDENT_PAYMENT_FLOW.md](STUDENT_PAYMENT_FLOW.md) | general payments |
