# Backend changelog — what has been fixed

Everything built in this working session, newest first. Written for the frontend
team: each entry says what changed, what it unblocks, and what you now call.

Nothing here is committed yet — it is all in the working tree.

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

## Round 3 — course module

- **Course approval workflow** (PRD §2.1): `publish/` submits (trainer) or
  approves (admin), new `reject/` with a **required** reason → back to `draft`
  so it can be resubmitted, `archive/`, and `GET /courses/review-queue/` for
  admins. Notifications both ways.
- **Submission is validated** — a course with no module, or no lesson, is
  rejected rather than reaching the admin queue as an empty shell.
- **Fixed a foot-gun**: `publish/` on an already-live course used to silently set
  it back to `pending_review`, pulling it out of the catalog and cutting off
  enrolled students. It now `400`s.
- **Curriculum edits after approval** set `has_unapproved_changes: true` and
  notify admins — the course **stays published**. Unpublishing over a typo fix
  would punish students mid-course.
- **Question authoring API** — `POST /assessments/{id}/questions/` replaces the
  whole set in one call (the editor's "Save questions"). Previously questions
  could only be created in Django admin, so the builder had nothing to save to.
  A separate trainer-only serializer carries `is_correct`, kept apart from the
  student shape so the answer key cannot leak.
- **New course fields**: `outcomes`, `welcome_message`, `completion_message`,
  `certificate_enabled`, `thumbnail_color`.
- **`certificate_enabled` is enforced** — switch it off and no certificate is
  issued on completion (`force=True` still lets an admin issue by hand).
- **Lesson notes** — `POST /lesson-notes/` upserts one private pad per lesson
  for the player's Notes tab. Trainers and admins cannot read them.

---

## Round 2 — plans & entitlements

- **`PricingPlan` gained tick-box features** an admin sets per plan:
  `includes_all_paid_courses`, `includes_live_sessions`,
  `includes_certificates`, `priority_support` — editable in Django admin, no
  deploy.
- **Only `all_paid_courses` is enforced.** The others are advertised on the
  pricing card and returned by the API, but those features are open to every
  student today; restricting them would remove access people already have.
- **Subscription access lapses correctly.** A subscriber's enrollment is tagged
  `source: "subscription"` and stops opening the course when the plan ends — the
  row survives so progress isn't lost. A **purchased** course is never revoked.
- Entitlements require an active status **and** an unexpired period. Nothing
  auto-renews, so a stale "active" row would otherwise grant access forever.
- **`GET /billing/summary/`** — total spent, active plan (`null` = Free),
  entitlements, invoice count. One call for the Billing page.
- **`POST /subscriptions/{id}/cancel/` now ends at period end**, not instantly —
  it was revoking days the student had paid for.

---

## Round 1 — 1:1 booking, payments, community

### 1:1 sessions (PRD §3.6)

- `GET /mentors/` — approved trainers with `hourly_rate`
- `topic` (required) + `notes` on a booking
- Lifecycle: `cancel` (either party) · `confirm` · `decline` · `complete`
- **Cancelling releases the slot** — `is_booked` was never cleared, so a
  cancelled slot was dead forever
- Slot booking is transactional with `SELECT … FOR UPDATE`, so two students
  racing the same slot can't both win
- Cap of 3 open requests per student

### Pay-per-hour, confirm-then-pay

Mentor accepts → the order is minted **at that moment**, so a later rate change
can't move the price → student pays through the existing Razorpay checkout →
booking auto-confirms on settlement. `payment_due_at` = 24h after acceptance or
2h before start, whichever is sooner. `expire_unpaid_bookings` releases lapsed
holds.

### Razorpay refunds

Called for real on cancellation, with a deliberate failure policy — **never
write off money on an ambiguous signal**:

| Outcome | Result |
|---|---|
| Insufficient balance (already settled to bank) | stays `requested` — debt open, retry after topping up |
| Network / timeout | stays `requested`; retry checks the gateway **before** re-sending |
| Permanently impossible | `failed` — settle out of band |
| Already fully refunded | treated as **success** |

Also handles refunds issued by hand in the Razorpay dashboard.

### Community / forum (PRD §3.12)

- Stack Overflow voting on questions and answers — same arrow withdraws,
  opposite flips, reputation exactly reversed
- Reputation values **admin-editable** (`PointRule`), no deploy
- `visibility`: `community` (same organization) vs `public` (platform-wide) —
  **both login-only**, nothing indexable
- Tags reusing `courses.Tag`, created on first use and slug-deduplicated
- `views_count`, `GET /forum-tags/`, `GET /community-profile/leaderboard/`

---

## Two bugs the tests caught that would have shipped

1. **Vote uniqueness silently absent on MySQL.** The first `Vote` model used
   conditional unique constraints, which MySQL refuses to create
   (`models.W036`) — nothing would have stopped double-voting in production.
   Rebuilt on a generic relation with a plain unique key.
2. **Community feed would have rendered empty.** Scoping "my community" to a
   non-null organization meant two orgless users couldn't see each other's
   threads — and most learners have no organization. Orgless users now share one
   implicit community; real organizations stay isolated.

---

## Operational requirements

Two scheduled commands — without them the platform leaks slots and money:

```bash
python manage.py expire_unpaid_bookings   # every 15 min
python manage.py retry_refunds            # hourly
```

Add **`refund.processed`** and **`refund.failed`** to the Razorpay dashboard
webhook alongside the existing `payment.*` events.

---

## Still not built

- **AI** — "Generate with AI", "Improve with AI", "AI lesson summary". No AI
  integration exists anywhere in the codebase.
- **Rewards catalog** — points accumulate but cannot be spent; the Community
  "Browse rewards" panel has no backend.
- **Subscription auto-renew** — a plan runs one period then expires; Razorpay
  Subscriptions API is not wired up.
- **Trainer payouts** — course sales, paid 1:1s and subscription access all
  create revenue-share obligations that `TrainerPayout` doesn't track.
- **Promotions / campaigns**, `analytics`, `classrooms`, `recordings`.
- **Certificate PDF** — `pdf_url` is always empty, but
  `GET /certificates/{id}/download/` returns printable HTML, which is the
  working path today.

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
