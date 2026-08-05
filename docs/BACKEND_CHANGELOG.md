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



## Reference docs

| Doc | Covers |
|---|---|
| [COURSE_MODULE_API.md](COURSE_MODULE_API.md) | course lifecycle — student · trainer · admin |
| [COURSE_PAYMENT_FLOW.md](COURSE_PAYMENT_FLOW.md) | buying a course |
| [STUDENT_1TO1_BOOKING_API.md](STUDENT_1TO1_BOOKING_API.md) | mentor booking + pay-per-hour |
| [STUDENT_COMMUNITY_API.md](STUDENT_COMMUNITY_API.md) | forum, voting, reputation |
| [STUDENT_BILLING_PLANS_API.md](STUDENT_BILLING_PLANS_API.md) | plans, entitlements, billing |
| [STUDENT_PAYMENT_FLOW.md](STUDENT_PAYMENT_FLOW.md) | general payments |
