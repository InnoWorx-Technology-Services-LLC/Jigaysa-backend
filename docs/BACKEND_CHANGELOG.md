# Backend changelog — what has been fixed

Everything built in this working session, newest first. Written for the frontend
team: each entry says what changed, what it unblocks, and what you now call.

Nothing here is committed yet — it is all in the working tree.

---

## 🐛 "Active users" read zero on a live server with 22 users

Found by calling the deployed `/api/v1/admin/reports/summary/` on
`devapi.jigaysa.com`, logging in successfully first &mdash; and still getting
`"active_users": 0`.

**Cause:** `SIMPLE_JWT` had no `UPDATE_LAST_LOGIN` key, and it defaults to
`False`. This platform authenticates *only* by JWT, so **nothing wrote
`last_login` at all**. Every one of the 22 accounts on that server had it null,
including the one that had just authenticated. The metric would have read zero
for ever regardless of real traffic.

**Fix:** `"UPDATE_LAST_LOGIN": True`, plus a composite index
`(is_active, last_login)` on the user table &mdash; the report filters on both,
so the count is served from the index without touching a row. Migration:
`accounts/0010`.

The alternative was counting distinct users out of `LoginActivity`, which is
already populated and needs no write. Rejected on efficiency: `last_login`
scales with **user count**, while the audit log scales with **login events**
&mdash; including failed attempts, which an attacker controls, in an append-only
table nobody prunes. It would also couple a dashboard read to a security log.
The write objection was weak anyway: a `LoginActivity` INSERT already happens on
that path, so a single-row UPDATE beside it is marginal.

**Note for whoever deploys:** accounts that existed before this stay uncounted
until their next sign-in, so the number climbs from zero over ninety days rather
than jumping to its true value. That is honest, not a second bug.

---

## Admin console — Institutions

Organisations could only be created in Django admin, and the user&rarr;organisation
link was settable nowhere else &mdash; which is why the platform has had **zero
organisations** since launch. Reference: `docs/ADMIN_INSTITUTIONS_API.md`.

### ✅ What you can call now

| Endpoint | Purpose |
|---|---|
| `GET`/`POST /api/v1/admin/organizations/` | list (**paginated**, `?search=` `?type=` `?status=`) and create |
| `PATCH`/`DELETE /api/v1/admin/organizations/{id}/` | rename, or deactivate |
| `POST /api/v1/admin/organizations/{id}/activate/` | undo a deactivation |
| `GET /api/v1/admin/organizations/{id}/members/` | the roll, **paginated**, `?role=` |
| `POST /api/v1/admin/organizations/{id}/members/add/` | attach existing users |
| `DELETE /api/v1/admin/organizations/{id}/members/{user_id}/` | detach one |

Onboarding is now three API calls instead of three Django-admin visits: create
the org, promote whoever runs it via the existing role endpoint, attach them.

### ⚠️ Membership is not a role

Adding someone to an institution **never changes what they may do**. A student at
a college and the administrator who bought the seats are both members; only the
second has `role: "institution"`, and that stays a separate call. Bundling the
two would make "add 400 students" silently able to mint 400 institution accounts.

### 🐛 Fixed on the way in: a duplicate name was a 500

`Organization.save()` derives a slug with `slugify(name)` and no de-duplication,
while the column is `unique=True`. Two institutions genuinely called "St. Xavier
College" &mdash; or any two names that slugify the same &mdash; would raise an
IntegrityError and surface as a server error.

The API now generates the slug explicitly and suffixes a counter
(`st-xavier-college-2`), so the model fallback never fires. Duplicate *names* are
rejected with a clean `400`. **The model itself is unchanged**, so anything
creating an `Organization` outside this endpoint still has the sharp edge.

### Two deliberate refusals

- **Renaming does not re-slug.** The slug is in URLs; re-slugging would break
  every link already shared. A rename is a label change, not a new tenant.
- **DELETE deactivates, never removes.** Courses, batches and classrooms hang off
  the row and members point at it, so deleting would either cascade through a
  course catalogue or orphan the roll. Same rule as user suspension, so the
  console has one mental model rather than two.

### 🙈 Gaps

No user creation or invite from this screen (you attach existing accounts), no
CSV roll import, no per-institution seat limits or billing, and `role:
"institution"` still does not scope what such a user can see. No migration &mdash;
the model already existed, only its API was missing.

---

## Trainer earnings — the payout gap, closed

`TrainerPayout` was a table nothing ever wrote to, so **every earnings figure on
three screens read zero regardless of revenue**: the trainer Earnings page, the
admin payout queue, and the Reports "Payouts" tile. There is now a ledger behind
them. Reference: `docs/TRAINER_EARNINGS_API.md`.

### The missing piece: a ledger, not a running total

New `TrainerEarning` — one line per paid order item, written the moment an order
settles. Payout rows are period aggregates and cannot say *which sales* made a
figure, tell a reversal from a sale that never happened, or stop an order being
counted twice. A ledger can, and three properties make it safe to compute money
from:

- **Idempotent** — one row per `order_item`, unique-constrained. Settlement runs
  from both the verify call *and* the webhook, so double-recording had to be
  impossible rather than unlikely.
- **Immutable** — a refund adds a `reversed` state; it never edits amounts.
- **Self-describing** — `share_pct` is snapshotted at the sale, so changing a
  trainer's rate in April cannot restate what March paid.

### What a trainer actually earns

`gross` is the line amount **net of coupon discount and excluding GST**:

- **GST is not split.** It is the government's; dividing it would have the
  platform and the trainer sharing money belonging to neither.
- **A coupon is nobody's revenue**, and it is allocated across lines *in
  proportion to their amounts* — a coupon applies to the order, not to one
  course. Charging it all to the first line would underpay one trainer and
  overpay another on the same order.
- **Plan purchases earn nobody.** A platform subscription is not a sale of any
  one trainer's work, and inventing an attribution would be worse than none.

### When — and the hold that makes it safe

**Recognised at capture.** Waiting out a refund window before showing anything
makes the page lie for a week to a trainer who just made a sale.

The refund risk is handled at the *payout* boundary instead, where it matters: an
earning must age `TRAINER_PAYOUT_HOLD_DAYS` (default 7, configurable) before a
payout can sweep it. So a refund inside the window reverses a line that has not
been committed anywhere — and the trainer still saw their sale on the day.

### ✅ What you can call now

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/trainer/earnings/summary/` | four tiles + the revenue split |
| `GET /api/v1/trainer/earnings/trend/` | the twelve-month chart, dense |
| `GET /api/v1/trainer/earnings/` | the ledger lines, **paginated** |
| `GET /api/v1/trainer/earnings/payouts/` | payouts, **paginated** |
| `GET`/`PUT /api/v1/trainer/earnings/bank-account/` | the bank card |

### 🔁 Needs a cron entry

    python manage.py generate_trainer_payouts        # monthly

Without it `pending_payout` grows for ever and the Payouts list stays empty. The
page copy promises payouts on the last day of each month; a cron entry on that
day is what makes it true. Safe to run twice.

### ⚠️ Two things to label honestly in the UI

1. **A payout row means "owed", not "sent".** There is no payout processor
   integration — the job records what is owed; nothing moves money. A payout
   becomes `paid` only when a human marks it after settling by other means.
2. **The bank card records where a trainer *says* payouts should go.** It
   **refuses a full account number** (`account_last4` must be exactly four
   digits; longer is a `400`, not a silent truncation). Storing a real number
   means holding a payout instrument — encryption at rest, an access trail, a
   breach story — with no processor to hand it to, so the only thing it would
   achieve is the liability. A trainer who thinks they have connected a bank
   account, and has not, finds out at the worst possible moment.

### Config

New: `TRAINER_PAYOUT_HOLD_DAYS` (default 7). Migrations:
`payments/0005_trainerearning`, `accounts/0009` (four bank-display fields on
`TrainerProfile`). No new packages.

### Known limitation

**A partial refund reverses the whole earning line**, which over-corrects. Fine
while partial refunds are rare; worth fixing before they are not.

---

## Trainer console — Assignments and Analytics

The two trainer pages that had nothing behind their dashboard layer. References:
`docs/TRAINER_ASSIGNMENTS_API.md`, `docs/TRAINER_ANALYTICS_API.md`.

### Assignments — the numbers that make it a dashboard

CRUD and grading already existed; the tiles and per-row counts did not.

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/assessments/board/` | your assignments, **paginated**, with counts |
| `GET /api/v1/assessments/stats/` | open assignments · pending reviews · avg score |
| `GET /api/v1/assessments/?mine=true` | the plain list, narrowed to your own |

Each row carries `submitted_count`, `pending_review_count` (**the number on the
Review button**), `enrolled_count`, and a one-word `state` — `draft`, `open` or
`closed` — so the client doesn't reassemble it from `is_published` plus
`available_to`.

> **The plain `/assessments/` list is not trainer-scoped, deliberately** — it
> shows every *published* assessment, which is right for browsing and wrong for
> a page with an Edit button on every row. `board/` is always scoped; `?mine=true`
> narrows the plain list.

### 🔒 Fixed while building: an access-control gap

`api_roles_by_action` is **schema metadata only** in this project — `core.schema`
reads it for the docs and nothing enforces it. `AssessmentViewSet` carries
`permission_classes = [IsAuthenticated]` and does its role checks by hand in
`perform_create`. New actions inheriting that attribute looked protected and were
not; `board/` and `stats/` now check explicitly, like the rest of the module.
Worth remembering when adding any future action to a viewset in this codebase.

### Analytics — new, and scoped to your own courses

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/trainer/analytics/summary/` | completion · quiz average · submission rate · active learners |
| `GET /api/v1/trainer/analytics/engagement/` | lessons completed and submissions, by month |
| `GET /api/v1/trainer/analytics/courses/` | per-course insights, **paginated** |
| `GET /api/v1/trainer/analytics/doubts/` | the doubt queue, **paginated** |

Every number comes from courses where `course.trainer == you`. There is no
parameter to widen it — the platform-wide view is the admin-only
`/admin/reports/`, and one view with an `if admin` branch is how the wrong
number reaches the wrong person.

**The rates are `null`, never `0`, when there is nothing to average.** A trainer
with no submissions has no quiz average; 0% says "everyone failed".

### 🙈 "AI-detected doubt frequency" cannot be built as designed

The mock ranks clustered topics — "useEffect cleanup · 47" with a trend arrow.
Clustering free text into topics needs a language model and **there is no LLM
anywhere in this backend**. Rather than fabricate the ranking, the endpoint
returns the doubts themselves, which a trainer can actually answer. Build that
panel as a queue, not a bar chart.

Same issue as the Promotions blurb's "let AI tailor the copy per network" — that
rewrite is rule-based too. Both strings are worth changing.

### 🐛 Two bugs the tests caught

- **The doubt queue was sorted backwards.** Ordering by `status` sorts
  alphabetically, and `"answered"` precedes `"open"` — the exact inverse of a
  queue, silently. Now ordered explicitly, open first.
- **Campaign history was not paginated** (from the earlier promotions work) — a
  bare array capped at 100. That is the failure mode that hides rather than
  breaks: correct until the 101st campaign quietly stops appearing. Now uses the
  standard envelope, and `docs/SOCIAL_API.md` §10 is updated.

---

## Admin console — Users, Payments and Reports

Three admin pages that had **no backend at all**. Each has its own reference:
`docs/ADMIN_USERS_API.md`, `docs/ADMIN_PAYMENTS_API.md`,
`docs/ADMIN_REPORTS_API.md`.

### Users — `docs/ADMIN_USERS_API.md`

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/admin/users/` | roster: **paginated**, `?search=` `?role=` `?status=` |
| `GET /api/v1/admin/users/stats/` | the four counters, one grouped query |
| `PATCH /api/v1/admin/users/{id}/role/` | the Change role menu |
| `POST /api/v1/admin/users/{id}/suspend/` · `/reactivate/` | suspension |
| `POST /api/v1/trainer-profiles/{id}/reject/` | **new** — the Reject button |

Two `409`s guard the console against locking the platform out of itself: you
cannot suspend or demote **yourself**.

> **`?review=pending` is not `?is_approved=false`.** A rejected application is
> also unapproved, so filtering on the flag alone left every rejection in the
> queue for ever. `reject` stamps `reviewed_at`, and that is what takes it out.
> Migration: `accounts/0008`.

**Suspension takes effect on the next sign-in, not mid-session** — it is a lock
on the door, not a bouncer.

### Payments — `docs/ADMIN_PAYMENTS_API.md`

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/admin/payments/` | platform-wide transactions, **paginated** |
| `GET /api/v1/admin/payments/summary/` | gross / refunds / pending / net |
| `GET`+`POST /api/v1/admin/refunds/` | the refund queue, and issuing one |
| `GET /api/v1/admin/payouts/` | trainer payout queue (read-only) |

> **Do not build this page on `/orders/`.** That endpoint scopes reads to the
> caller *including for admins*, so an admin sees their own test orders and
> concludes the platform has no revenue. Rather than weaken a student-facing
> endpoint with an `if admin` branch, the wide reads live behind `IsAdmin` in
> `payments/admin_api.py`.

`summary` honours the same filters as the table — tiles that ignore the filter
beside a table that honours it is the classic dashboard lie. Partial refunds are
supported; omitting `amount` refunds the remainder.

### Reports — `docs/ADMIN_REPORTS_API.md`

`GET /api/v1/admin/reports/` `summary/`, `trends/`, `users-by-role/`,
`attendance/` (the last **paginated**). The `analytics` app was an empty shell
and was not mounted at all; it now is.

Trend series are **dense** — every month in the window, gaps as explicit zeros.
A sparse aggregate charted directly draws a line from November to January and
hides the dip. Both series share one month range, so index *n* is the same month
in each.

Attendance rate is `null`, never `0`, when no register has been taken. They mean
opposite things and colouring the first as the second paints a healthy batch as
a total failure.

### 🐛 Found while building: MySQL has no timezone tables

`CONVERT_TZ` returns `NULL` on this server and `mysql.time_zone` is empty, so
any `__date` lookup or `TruncMonth` on a timezone-aware column **silently
matches nothing** — no error, just an honest-looking chart of all zeros. The
first version of the trends endpoint read empty for exactly this reason.

Both new modules now compare **aware datetimes computed in Python** instead, so
they do not depend on that data load. No pre-existing code used `__date` or
`Trunc*`, so nothing else was affected — but anything added later will hit it.
Worth running `mysql_tzinfo_to_sql` on the server regardless.

### Pagination

Every list endpoint above uses the platform default: `?page`, `?page_size`
(default 20, max 100), and a `{count, next, previous, results}` envelope. **A
client that does `resp.data[0]` will break on the first full page.**

### 🙈 Gaps worth knowing

- **Nothing generates trainer payouts.** `TrainerPayout` is written by no code
  path, so the payout queue is empty and the Reports "Payouts" tile reads 0 on a
  platform with real revenue. Endpoint and shape are real; the data is not.
- **No bulk actions, no user deletion, no audit log** on Users.
- **No reconciliation, no export, no cohort analysis** — all three appear in
  page blurbs and none is computed.

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
