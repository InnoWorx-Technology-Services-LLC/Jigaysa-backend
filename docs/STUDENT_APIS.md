# Student Section — Complete API Reference

**Project:** Jigyaasaa — Smart Hybrid LMS
**Base URL:** `/api/v1`
**Interactive docs:** `/api/docs/` (Swagger) · **Schema:** `/api/schema/`

Every endpoint below is one a **student** role can call. Trainer/admin-only
authoring routes are omitted (see Swagger for those).

**Auth:** all routes except §1.1–§1.5 require `Authorization: Bearer <access_token>`.

**Pagination:** every list endpoint is paginated — `?page=`, `?page_size=`
(default **20**, max **100**) — and wraps results as:

```json
{ "count": 42, "next": "…?page=2", "previous": null, "results": [ … ] }
```

Exceptions that return a **bare array** (not paginated): `GET /notification-preferences/`,
`GET /live-sessions/{id}/doubts/`, `GET /community-profile/my_badges/`.

**Errors:** DRF standard. `400` `{"detail": "…"}` or `{"field": ["…"]}` ·
`401` missing/expired token · `403` `{"detail": "…"}` permission ·
`404` not found (also returned when an object exists but is outside your
queryset scope) · `429` OTP rate limit · `501` unimplemented (social login).

---

## Table of contents

| § | Area | Base path |
|---|------|-----------|
| 1 | [Auth & profile](#1-auth--profile) | `/auth/` |
| 2 | [Course catalog](#2-course-catalog) | `/courses/`, `/categories/`, `/tags/` |
| 3 | [Enrollment](#3-enrollment) | `/enrollments/` |
| 4 | [Course player & progress](#4-course-player--progress) | `/courses/{slug}/curriculum/`, `/lesson-progress/` |
| 5 | [Reviews](#5-reviews) | `/reviews/` |
| 6 | [Assessments & assignments](#6-assessments--assignments) | `/assessments/`, `/submissions/` |
| 7 | [Live classes & 1:1](#7-live-classes--11-sessions) | `/live-sessions/`, `/session-registrations/`, `/trainer-availability/`, `/individual-bookings/` |
| 8 | [Free library](#8-free-library) | `/library-resources/`, `/library-bookmarks/` |
| 9 | [Discussions & community](#9-discussions--community) | `/discussion-threads/`, `/discussion-replies/`, `/community-posts/`, `/badges/`, `/community-profile/` |
| 10 | [Notifications](#10-notifications) | `/notifications/`, `/notification-preferences/`, `/device-tokens/` |
| 11 | [Payments & purchase](#11-payments--purchase) | `/pricing-plans/`, `/course-prices/`, `/coupons/validate/`, `/orders/`, `/invoices/`, `/subscriptions/`, `/payment-methods/` |
| 12 | [Certificates](#12-certificates) | `/certificates/` |
| 13 | [File uploads](#13-file-uploads) | `/uploads/` |
| 14 | [Known gaps](#14-known-gaps--caveats) | — |

---

## 1. Auth & profile

### 1.1 `POST /auth/register/` — sign up *(public)*

```json
{ "email": "asha@example.com", "full_name": "Asha R", "role": "student",
  "phone": "+919876543210", "password": "Passw0rd!123" }
```

`role` accepts only `student` or `trainer`. Password runs Django's validators.

**`201`** → `{ "id": 42, "email": "…", "full_name": "…", "role": "student", "phone": "…" }`

### 1.2 `POST /auth/login/` — JWT login *(public)*

```json
{ "email": "asha@example.com", "password": "Passw0rd!123" }
```

**`200`**

```json
{
  "refresh": "eyJ…", "access": "eyJ…",
  "user": { "id": 42, "email": "…", "full_name": "Asha R", "role": "student",
            "phone": "+91…", "phone_verified": false, "organization": null,
            "is_active": true, "date_joined": "2026-07-01T10:00:00Z" }
}
```

The access token carries `role` and `email` claims. Every login attempt (success
or failure) is recorded as a `LoginActivity` row with IP + user-agent.

### 1.3 `POST /auth/token/refresh/` — `{ "refresh": "…" }` → `{ "access": "…" }`

### 1.4 Mobile OTP login *(public)*

| Endpoint | Body | Response |
|---|---|---|
| `POST /auth/otp/request/` | `{ "phone": "+919876543210" }` | `200 {"detail": "If that number is registered, an OTP has been sent."}` |
| `POST /auth/otp/verify/` | `{ "phone": "…", "code": "482913" }` | `200 { refresh, access, user }` — same shape as login |

OTP is 6 digits, **5-minute TTL**, **5 attempts** then invalidated. Verify errors:
`400` expired / incorrect · `429` too many attempts · `404` no account for that number.
Successful verify also flips `phone_verified` to `true`.

### 1.5 Password reset *(public)*

| Endpoint | Body |
|---|---|
| `POST /auth/password-reset/` | `{ "email": "asha@example.com" }` |
| `POST /auth/password-reset/confirm/` | `{ "uid": "NDI", "token": "c1x-…", "new_password": "…" }` |

The request endpoint **always** returns `200` with a generic message
(anti-enumeration). In dev the `uid`/`token` pair is printed to the server console —
email delivery is not wired yet.

### 1.6 `GET` / `PATCH` `/auth/me/` — profile

`PATCH` accepts only `full_name` and `phone`. `email`, `role`, `phone_verified`,
`organization`, `is_active`, `date_joined` are read-only.

### 1.7 `POST /auth/logout/` — `{ "refresh": "…" }` → **`205`**, blacklists the token.

### 1.8 `POST /auth/social/` — **`501 Not Implemented`** (no OAuth provider wired).

---

## 2. Course catalog

### `GET /courses/` — browse

Students see **only** courses that are `status=published` **and**
`visibility=public`.

| Query param | Values |
|---|---|
| `q` | keyword — matches `title` or `subtitle` (icontains) |
| `category` | category **id** |
| `tag` | tag **slug** |
| `skill_level` | `beginner` \| `intermediate` \| `advanced` |
| `course_type` | `self_paced` \| `live_batch` \| `physical` \| `hybrid` \| `individual_coaching` \| `group_coaching` |
| `is_free` | `true` \| `false` |
| `trainer` | trainer user id |
| `ordering` | `created_at`, `rating_avg`, `enrolled_count` (prefix `-` for desc). Anything else is ignored. |

**`200`** — `results[]` uses the light catalog card:

```json
{
  "id": 3, "slug": "intro-to-data-science",
  "title": "Intro to Data Science", "subtitle": "Python, pandas & visualisation",
  "trainer": { "id": 2, "full_name": "Dr. Kapoor", "email": "kapoor@…" },
  "category": "Data Science",
  "course_type": "self_paced", "skill_level": "beginner",
  "language": "en", "duration_minutes": 480,
  "thumbnail": "", "is_free": true, "status": "published",
  "rating_avg": "4.70", "rating_count": 118, "enrolled_count": 540,
  "published_at": "2026-06-01T00:00:00Z"
}
```

> **No price on the card.** `is_free` is the only pricing signal here. To show an
> amount you must call `GET /course-prices/?course=<id>` separately (§11.2).

### `GET /courses/{slug}/` — detail

Adds `description`, `organization`, nested `category` object, `tags[]`,
`intro_video_url`, `prerequisites[]`, `visibility`, `module_count`,
`created_at`, `updated_at`.

### `GET /categories/` · `GET /tags/`

Read-only for students. Category: `{ id, name, slug, parent, icon }`.
Tag: `{ id, name, slug }`.

---

## 3. Enrollment

### 3.1 `POST /courses/{slug}/enroll/` — self-enroll (free courses)

Body optional: `{ "batch": 7 }`.

**`201`** → the enrollment object (see §3.3).

**`400`** cases:

| Condition | Message |
|---|---|
| Course not published | `Course is not open for enrollment.` |
| Already enrolled | `Already enrolled in this course.` |
| Course is paid | `This is a paid course. Purchase it via checkout (POST /api/v1/orders/ then /orders/{id}/pay/) to enrol.` |

### 3.2 `POST /enrollments/` — same thing by id

`{ "course": 3, "batch": null }` — identical validation and response.

### 3.3 `GET /enrollments/` — "My courses"

Scoped to the caller. Each row:

```json
{
  "id": 88, "student": 42,
  "course": { …CourseListSerializer, see §2… },
  "batch": null,
  "status": "active",          // active | completed | cancelled | refunded
  "source": "free",            // free | purchase | bulk | institution
  "order": null,
  "progress_pct": 42,
  "enrolled_at": "2026-07-10T09:00:00Z",
  "completed_at": null
}
```

All fields are read-only. `GET /enrollments/{id}/` returns one row.

---

## 4. Course player & progress

### 4.1 `GET /courses/{slug}/curriculum/` — the whole player in one call

**`200`**

```json
{
  "course": "intro-to-data-science",
  "has_access": true,
  "progress_pct": 55,
  "modules": [
    {
      "id": 10, "title": "01 · Foundations", "summary": "", "order": 1,
      "lessons": [
        {
          "id": 101, "module": 10,
          "title": "Welcome & how this course works",
          "content_type": "video",        // video | reading | quiz | assignment | live
          "order": 1, "duration_minutes": 6,
          "is_preview": true, "locked": false,
          "video_url": "https://…r2…/lesson-videos/…?X-Amz-Signature=…",
          "content": "",
          "resources": [
            { "id": 9, "lesson": 101, "title": "Slides", "url": "https://…",
              "file": null, "resource_type": "pdf" }
          ],
          "completed": true, "watch_pct": 100, "last_position_seconds": 360
        }
      ]
    }
  ]
}
```

**Gating.** `has_access` is `true` for admins, the owning trainer, or an enrolled
student. A lesson is unlocked when `is_preview` is `true` **or** `has_access` is
`true`. When `locked` is `true` the server blanks `video_url` (`""`),
`content` (`""`) and `resources` (`[]`) — the title, duration and type still show,
so you can render a locked curriculum outline to non-enrolled visitors.

**Video.** `video_url` is ready to play as-is. For privately-stored videos
(`video_key` set on the lesson) it's a **presigned URL that expires (~1h)** —
re-fetch the curriculum if playback starts after a long idle. If storage is
unconfigured or presigning fails, it silently falls back to the stored
`video_url` rather than erroring.

**Progress fields** (`completed`, `watch_pct`, `last_position_seconds`) are folded
in from the caller's own `LessonProgress`. With no enrollment they are
`false`/`0`/`0`.

### 4.2 `POST /lesson-progress/` — upsert watch progress

Posting for an already-tracked lesson **updates** the existing row (no 409).

```json
{ "enrollment": 88, "lesson": 104,
  "status": "completed",            // not_started | in_progress | completed
  "watch_pct": 100,
  "time_spent_seconds": 610,
  "last_position_seconds": 600 }
```

**`201`**

```json
{ "id": 501, "enrollment": 88, "lesson": 104, "status": "completed",
  "watch_pct": 100, "time_spent_seconds": 610,
  "last_position_seconds": 600, "completed_at": "2026-07-28T05:10:00Z" }
```

Side effects when `status` becomes `completed`:

1. `completed_at` is stamped and `watch_pct` forced to **100**.
2. `Enrollment.progress_pct` is recomputed as
   `round(completed_lessons / total_lessons × 100)`.
3. At 100% the enrollment flips to `completed` and a **certificate is auto-issued**
   (failures there are swallowed — progress still saves).

**`403`** `Not your enrollment.` if the enrollment belongs to someone else.

### 4.3 `GET /lesson-progress/?enrollment=<id>` — your own rows only.

Also supports `PUT`/`PATCH`/`DELETE` on `/lesson-progress/{id}/`.

---

## 5. Reviews

### `POST /reviews/`

```json
{ "course": 3, "rating": 5, "comment": "Clear and practical." }
```

`rating` must be **1–5**. Two `400` guards: `You must be enrolled to review this
course.` and `You have already reviewed this course.` Creating/updating/deleting
recomputes the course's `rating_avg` and `rating_count`.

**`201`**

```json
{ "id": 77, "course": 3,
  "student": { "id": 42, "full_name": "Asha R", "email": "asha@…" },
  "rating": 5, "comment": "Clear and practical.",
  "created_at": "2026-07-28T06:00:00Z" }
```

### `GET /reviews/?course=3` — all reviews for a course (public to any logged-in user).

`PATCH`/`DELETE /reviews/{id}/` — own review only, else `403`
`You can only modify your own review.`

---

## 6. Assessments & assignments

### 6.1 `GET /assessments/?course=<id>&lesson=<id>&assessment_type=<type>`

Students see **published assessments only**. `assessment_type` is
`quiz` | `assignment` | `coding` | `descriptive`.

```json
{ "id": 12, "course": 3, "lesson": 104, "trainer": 2,
  "title": "Module 1 checkpoint", "assessment_type": "quiz", "description": "",
  "time_limit_minutes": 15, "pass_percent": 60, "max_attempts": 2,
  "available_from": null, "available_to": null,
  "grading_type": "auto",          // auto | manual | rubric
  "is_published": true, "total_questions": 10,
  "created_at": "…" }
```

### 6.2 `GET /assessments/{id}/` — with questions

Adds `questions[]`. **Answer keys are never exposed** — choices carry only
`{ id, text, order }`, no `is_correct`.

```json
"questions": [
  { "id": 300, "assessment": 12, "question_type": "mcq",
    "text": "Which library provides DataFrames?", "points": 5, "order": 1,
    "choices": [ { "id": 900, "text": "pandas", "order": 1 },
                 { "id": 901, "text": "requests", "order": 2 } ] }
]
```

`question_type`: `mcq` (single choice) · `multi` · `descriptive` · `coding` · `file`.

### 6.3 `POST /assessments/{id}/submit/` — attempt in one call

```json
{
  "answers": [
    { "question": 300, "selected_choices": [900] },
    { "question": 301, "text_answer": "Because …" },
    { "question": 302, "code": "def solve(): …" },
    { "question": 303, "file_key": "submissions/42/2026/07/ab12-report.pdf" }
  ],
  "time_taken_seconds": 540
}
```

`file_key` comes from the presign flow (§13). Answers for questions not on this
assessment are silently skipped.

**Grading rules**

- `mcq` / `multi` are graded **immediately**: full `points` only if the selected
  set **exactly equals** the correct set — no partial credit.
- `descriptive` / `coding` / `file` score 0 and mark the submission as awaiting
  review.
- `percent = round(earned / total_points × 100)`.
- If any subjective answer exists → `status: "submitted"`, `passed: false`
  (pending trainer grading). Otherwise → `passed`/`failed` vs `pass_percent`.

**`201`**

```json
{ "id": 700, "assessment": 12, "student": 42, "enrollment": 88,
  "attempt_no": 1, "status": "passed",
  "started_at": "…", "submitted_at": "…",
  "score": "45.00", "percent": 90, "passed": true,
  "time_taken_seconds": 540, "feedback": "", "graded_at": null,
  "answers": [
    { "id": 1, "question": 300, "selected_choices": [900], "text_answer": "",
      "code": "", "file_key": "", "is_correct": true, "points_awarded": "5.00" }
  ] }
```

**`400`** cases: `This assessment is not open.` (unpublished) ·
`This assessment is not yet available.` / `This assessment is closed.` (outside
the `available_from`/`available_to` window) · `You have used all attempts for this
assessment.` (`max_attempts`; `0` means unlimited).

### 6.4 `GET /submissions/?assessment=<id>&status=<status>`

Your own attempts. `status`: `in_progress` | `submitted` | `graded` | `passed` | `failed`.
`GET /submissions/{id}/` returns one with nested `answers[]` (now including
`is_correct` and `points_awarded` — so **review-after-submit** works without
leaking keys before the attempt).

---

## 7. Live classes & 1:1 sessions

> Conferencing is **provider-agnostic**: `join_url` / `meeting_id` are plain
> fields a trainer fills in (a Google Meet / Zoom / Jitsi link). There is **no**
> Meet/Zoom API integration, no auto-provisioned room, no calendar event.

### 7.1 `GET /live-sessions/`

Filters: `course=<id>`, `batch=<id>`, `trainer=<id>`,
`status=scheduled|live|completed|cancelled`, `upcoming=true`.

```json
{
  "id": 12, "course": 3, "batch": null,
  "trainer": { "id": 5, "full_name": "Dr. Kapoor", "email": "…" },
  "title": "Live Q&A — week 2", "description": "",
  "session_type": "group",        // group | individual | workshop | qa
  "scheduled_start": "2026-08-01T10:00:00Z",
  "duration_minutes": 60, "capacity": 0, "registration_limit": 50,
  "status": "scheduled",
  "join_url": "https://meet.google.com/abc-defg-hij",
  "meeting_id": "abc-defg-hij",
  "attendees_count": 0, "registrations_count": 14,
  "my_registration_status": "registered",   // registered | waitlisted | cancelled | null
  "created_at": "…"
}
```

### 7.2 `POST /live-sessions/{id}/register/`

No body. **Auto-waitlists** when active registrations reach
`registration_limit` (`0` = unlimited). Re-registering after cancelling
reactivates the same row.

**`201`** (new) / **`200`** (existing) → a `SessionRegistration` (§7.5).
**`400`** `This session is not open for registration.` if completed/cancelled.

### 7.3 `POST /live-sessions/{id}/join/`

Stamps `joined_at` + `attended=true` on first call, then:

```json
{ "join_url": "https://meet.google.com/abc-defg-hij", "meeting_id": "abc-defg-hij" }
```

**`400`** `Register for this session before joining.` — students only; trainers
and admins bypass.

### 7.4 Doubts

- `POST /live-sessions/{id}/raise-doubt/` — `{ "text": "Can you re-explain joins?" }`
  → **`201`** `{ id, session, student, student_name, text, status: "open", asked_at }`.
  `400` if `text` is blank.
- `GET /live-sessions/{id}/doubts/` — **bare array**. Students see only their own;
  the owning trainer/admin sees all.
- `GET /session-doubts/?session=<id>` — paginated equivalent, your own doubts.

### 7.5 `GET /session-registrations/` — your registrations

```json
{ "id": 88, "session": 12,
  "session_detail": { …full LiveSession, §7.1… },
  "status": "registered", "registered_at": "…",
  "joined_at": null, "attended": false }
```

`POST /session-registrations/{id}/cancel/` — cancels **and promotes the earliest
waitlisted student** to `registered`.

### 7.6 1:1 booking

> **Full frontend guide: [STUDENT_1TO1_BOOKING_API.md](STUDENT_1TO1_BOOKING_API.md)**
> — statuses, payment, deadlines and UI labels. Summary below.

The three steps of the **Book a session** modal, in order.

**Step 1 — choose a mentor.** `GET /mentors/?q=<search>` — approved, active
trainers only; `q` matches name or expertise.

```json
{ "id": 5, "full_name": "Dr. Kapoor", "email": "…",
  "headline": "Senior data scientist", "avatar": "",
  "expertise": "React", "years_experience": 8,
  "hourly_rate": "1500.00", "rating_avg": "4.80", "rating_count": 32 }
```

**Step 2 — pick a slot.** `GET /trainer-availability/?trainer=<id>&available=true`
— open future slots. `available=true` filters `is_booked=false` **and**
`start >= now`. → `{ id, trainer: {…}, start, end, slot_minutes, is_booked }`

> Each bookable time is **its own row**. `start` in the booking must equal a
> slot's `start` exactly — don't split one long slot into hours client-side.

**Step 3 — submit.** `POST /individual-bookings/`

```json
{ "trainer": 5, "start": "2026-08-02T11:00:00Z", "duration_minutes": 60,
  "topic": "React hooks deep dive", "notes": "Tried useMemo already." }
```

**`201`** → `{ id, trainer, trainer_name, student, student_name, topic, notes,
start, duration_minutes, status: "pending", meeting_url: "", created_at }`

`topic` is **required**; `notes` is optional. The matching availability slot is
locked (`SELECT … FOR UPDATE`) and marked booked, so two students racing for the
same slot can't both win. **`400`** cases: `You cannot book a session with
yourself.` · `No open availability for this trainer at that time.`
Omitting `start` skips the slot check entirely and books nothing on the calendar.
The trainer gets a "New 1:1 booking request" notification.

**Lifecycle.** `pending` → `awaiting_payment` → `confirmed` → `completed`;
`cancel` from either side while still open. All return the updated booking; all
`400` with `This booking is already <status>.` on an invalid transition.

| Endpoint | Who | Effect |
|---|---|---|
| `POST /individual-bookings/{id}/cancel/` | student **or** trainer | → `cancelled`, **frees the slot**, notifies the other party. Adds `refund_requested` / `paid` to the response |
| `POST /individual-bookings/{id}/confirm/` | trainer | → `awaiting_payment` + creates the order (or `confirmed` when the mentor is free). Optional body `{ "meeting_url": "https://…" }` |
| `POST /individual-bookings/{id}/decline/` | trainer | → `cancelled`, **frees the slot**, notifies the student |
| `POST /individual-bookings/{id}/complete/` | trainer | `confirmed` → `completed` (moves it to "Past sessions") |

- `GET /individual-bookings/` — bookings you made (or, for trainers, received).
  Render `pending` as **requested**; derive Upcoming / Completed / Total-hours
  counters client-side from `status` + `duration_minutes`.

**Payment (PRD §3.6 payment per hour).** Accepting a request prices it at the
mentor's `hourly_rate × duration` (+18% GST) and mints an order, exposed on the
booking as `order` / `amount_due` / `payment_due_at` / `is_paid`. Pay it through
the normal `POST /orders/{id}/checkout/` → `verify/` flow (§13); the booking
flips to `confirmed` on settlement. Miss `payment_due_at` (24h after acceptance,
or 2h before start — whichever is sooner) and the booking is cancelled and the
slot released.

> `meeting_url` is written **only** by `confirm/`. It stays empty otherwise.
> A trainer cancelling a **paid** booking records a refund obligation; nothing
> calls Razorpay's refund API. See §15.

---

## 8. Free library

### `GET /library-resources/`

Filters: `q` (title/description), `format`, `category` (id), `access_level`,
`author` (trainer id), `course` (id).
`ordering`: `popularity_score`, `published_at`, `views_count`, `created_at`
(prefix `-` for desc).

`format`: `video` | `ebook` | `notes` | `webinar` | `sample_lesson` | `cert_resource`
`access_level`: `free` | `premium`

```json
{ "id": 20, "title": "Pandas cheat-sheet", "slug": "pandas-cheat-sheet",
  "description": "…", "format": "notes",
  "category": 4, "category_name": "Data Science",
  "author": 2, "author_name": "Dr. Kapoor", "course": null,
  "file_url": "https://…", "video_url": "",
  "duration_minutes": 0, "pages": 6,
  "access_level": "free", "views_count": 812, "popularity_score": 812,
  "thumbnail": "", "published_at": "…", "is_bookmarked": true,
  "created_at": "…" }
```

### `GET /library-resources/{slug}/`

Same shape; **increments `views_count` and `popularity_score` by 1** on every
retrieve.

> `access_level` is **advisory only** — `file_url` / `video_url` are returned to
> any authenticated user even for `premium` items. Gating is not implemented.

### `POST /library-resources/{slug}/bookmark/` — toggle

→ **`201`** `{ "bookmarked": true }` or **`200`** `{ "bookmarked": false }`.

### `GET /library-bookmarks/` — the "Saved" tab

```json
{ "id": 5, "resource": 20, "resource_detail": { …resource above… },
  "saved_at": "…" }
```

`POST /library-bookmarks/` `{ "resource": 20 }` (idempotent, `201` either way) ·
`DELETE /library-bookmarks/{id}/`.

---

## 9. Discussions & community

> **Full frontend guide: [STUDENT_COMMUNITY_API.md](STUDENT_COMMUNITY_API.md)**
> — visibility rules, voting, Stack-Overflow reputation, tags and the
> leaderboard. Summary below.
>
> New since this section was written: `visibility`, `tags`, `views_count`,
> `score`/`my_vote`, `POST .../vote/` on threads and replies,
> `GET /forum-tags/`, and `GET /community-profile/leaderboard/`.

### 9.1 Threads

`GET /discussion-threads/?course=<id>&scope=course|community&status=open|resolved&q=<search>`

```json
{ "id": 30, "course": 3, "batch": null,
  "author": { "id": 42, "full_name": "Asha R", "role": "student" },
  "title": "Confused about groupby", "body": "…",
  "scope": "course", "status": "open", "is_pinned": false,
  "reply_count": 4, "last_activity_at": "…", "created_at": "…" }
```

`POST /discussion-threads/` — send `title`, `body`, `scope`, optional `course`/`batch`.
`author`, `status`, `is_pinned`, `reply_count`, `last_activity_at` are server-set.

`GET /discussion-threads/{id}/` adds nested `replies[]`.

`PATCH`/`DELETE` — own thread only (`403 You can only modify your own thread.`).

### 9.2 Replies

`POST /discussion-replies/` — `{ "thread": 30, "body": "…", "parent": null }`
→ bumps the thread's `reply_count` and `last_activity_at`.

```json
{ "id": 61, "thread": 30,
  "author": { "id": 7, "full_name": "Dr. Kapoor", "role": "trainer" },
  "parent": null, "body": "…", "is_accepted_answer": false, "created_at": "…" }
```

`GET /discussion-replies/?thread=30` · `POST /discussion-replies/{id}/accept/` —
marks the accepted answer (clears any previous one) and sets the thread to
`resolved`. Allowed for the **thread author**, the **course trainer**, or an admin;
otherwise `403`.

### 9.3 Community feed

`GET`/`POST /community-posts/` → `{ id, author, body, post_type, likes_count, created_at }`
`POST /community-posts/{id}/like/` → `{ "likes_count": 13 }`

> `like` is an **unconditional increment** — there's no per-user like record, so it
> can be called repeatedly and cannot be un-liked.

### 9.4 Gamification

- `GET /badges/` — the earnable catalog: `{ id, name, slug, icon, description }`.
- `GET /community-profile/me/` — auto-creates the profile on first call:

  ```json
  { "points": 185, "level": 1, "badges_count": 2,
    "badges": [ { "id": 3, "badge": { "id": 1, "name": "First steps",
                  "slug": "first-steps", "icon": "🎯", "description": "…" },
                 "earned_at": "…" } ] }
  ```

- `GET /community-profile/my_badges/` — **bare array** of `{ id, badge, earned_at }`.

Points accrue automatically from activity signals (enroll, assessment pass,
certificate issued); level is `points / 500`.

---

## 10. Notifications

### `GET /notifications/?is_read=false&category=<category>`

`category`: `course` | `live_class` | `assessment` | `certificate` | `forum` |
`payment` | `system`

```json
{ "id": 900, "category": "certificate",
  "title": "Your certificate is ready",
  "body": "Intro to Data Science — download it from your dashboard.",
  "link": "/student/certificates/12", "is_read": false, "created_at": "…" }
```

Only `is_read` is writable (`PATCH /notifications/{id}/`).

| Endpoint | Response |
|---|---|
| `GET /notifications/unread_count/` | `{ "unread": 7 }` |
| `POST /notifications/mark_all_read/` | `{ "marked_read": 7 }` |
| `POST /notifications/{id}/read/` | the notification object |
| `DELETE /notifications/{id}/` | `204` |

### `GET /notification-preferences/` — the Settings matrix

Returns a **bare array**, auto-seeding one row per category on first call:

```json
[ { "id": 1, "category": "course", "in_app": true, "email": true,
    "sms": false, "whatsapp": false, "push": true } ]
```

`PATCH /notification-preferences/{id}/` to toggle a channel. Preferences are
honored when notifications fire; only the **in-app** channel actually delivers
today — email/SMS/WhatsApp/push dispatch is not wired.

### Device tokens

`GET`/`POST`/`DELETE /device-tokens/` — `{ "token": "fcm…", "platform": "web" }`
(`android` | `ios` | `web`). `POST` is an upsert on `(user, token)`.

---

## 11. Payments & purchase

> 💳 **Razorpay (test mode).** Real gateway, real signature verification, real
> webhook — running on `rzp_test_` keys, so no money moves until live keys are
> swapped in. Cart pricing, GST, coupons, invoicing and access-granting are
> unchanged.

**The flow:**

```
POST /orders/                 → local order, priced server-side
POST /orders/{id}/checkout/   → Razorpay order + Checkout params
  ↓ browser opens Razorpay Checkout
POST /orders/{id}/verify/     → handler payload, verified server-side → paid
  ↕ (in parallel, authoritative)
POST /payments/webhook/razorpay/  ← Razorpay → server
```

`verify/` and the webhook both settle the order and are **idempotent** — whichever
lands first wins, the other is a no-op. That matters: a student can pay and close
the tab before `verify/` fires, and the webhook still grants the enrollment.

### 11.1 `GET /pricing-plans/` — platform subscription plans

Only `is_active=true` unless `?active=all`.

```json
{ "id": 1, "name": "Annual", "slug": "annual", "billing_period": "annual",
  "price": "4999.00", "currency": "INR",
  "features": ["All courses", "Certificates"], "is_active": true }
```

### 11.2 `GET /course-prices/?course=<id>` — a course's price options

```json
{ "id": 4, "course": 3,
  "pricing_type": "one_time",   // one_time | subscription | installment | per_session | corporate | group
  "amount": "1999.00", "currency": "INR",
  "discount_percent": "10.00", "discount_amount": "0.00",
  "valid_from": null, "valid_to": null }
```

> Only `one_time` prices are purchasable through checkout. `valid_from`/`valid_to`
> are **not enforced** — an expired price still prices a cart. See §14.

### 11.3 `POST /coupons/validate/` — preview a discount

```json
{ "code": "LAUNCH20",
  "items": [ { "item_type": "course", "object_id": 3 } ] }
```

→ `{ "code": "LAUNCH20", "subtotal": "1999.00", "discount": "399.80",
     "tax_gst": "287.86", "total": "1887.06" }`

`item_type` is `course` or `plan`.

### 11.4 `POST /orders/` — create a checkout order

```json
{ "items": [ { "item_type": "course", "object_id": 3 } ],
  "coupon_code": "LAUNCH20" }
```

**Amounts are computed server-side** from the catalog — anything you send for
price is ignored. Per course: the **cheapest `one_time` price**, minus
`discount_amount`, then minus `discount_percent`, clamped at 0. Then coupon, then
**18% GST**.

**`201`**

```json
{ "id": 55, "status": "pending",
  "subtotal": "1999.00", "discount": "399.80", "tax_gst": "287.86",
  "total": "1887.06", "currency": "INR", "coupon_code": "LAUNCH20",
  "items": [ { "id": 1, "item_type": "course", "object_id": 3,
               "title": "Intro to Data Science", "amount": "1799.10", "qty": 1 } ],
  "payments": [], "created_at": "…" }
```

**`400`** cases: `Course {id} not found.` · `'{title}' is free — just enroll.` ·
`'{title}' is not purchasable yet.` (no `one_time` price row) ·
`Plan {id} not found or inactive.`

### 11.5 `POST /orders/{id}/checkout/` — open Razorpay Checkout

No body. Creates a Razorpay order (or reuses the existing one for this unpaid
order — safe to call repeatedly, it will not create duplicates).

**`200`**

```json
{
  "key": "rzp_test_TJ0Xg4q0cMu4vL",
  "razorpay_order_id": "order_TJ0mlTna615ecg",
  "amount": 118000,
  "amount_display": "1180.00",
  "currency": "INR",
  "name": "Jigyaasaa",
  "description": "Intro to Data Science",
  "image": "",
  "order_id": 55,
  "prefill": { "name": "Asha R", "email": "asha@…", "contact": "+91…" },
  "notes": { "order_id": "55" },
  "callback_url": "https://lms.jigyaasaa.com/student/orders/55",
  "is_test_mode": true
}
```

> `amount` is in **paise** (Razorpay's unit); `amount_display` is the rupee value
> for your UI. `key` is the **public** key id — safe in the browser. The API
> secret never leaves the server.

**`503`** gateway not configured · **`502`** Razorpay unreachable.

**Frontend wiring** (include `https://checkout.razorpay.com/v1/checkout.js`):

```js
const cfg = await api.post(`/orders/${orderId}/checkout/`).then(r => r.data);

new window.Razorpay({
  key: cfg.key,
  order_id: cfg.razorpay_order_id,
  amount: cfg.amount,
  currency: cfg.currency,
  name: cfg.name,
  description: cfg.description,
  image: cfg.image || undefined,
  prefill: cfg.prefill,
  notes: cfg.notes,
  theme: { color: '#4f46e5' },
  handler: async (res) => {
    // res = { razorpay_order_id, razorpay_payment_id, razorpay_signature }
    const order = await api.post(`/orders/${orderId}/verify/`, res);
    // order.status === 'paid' → enrollment granted, invoice issued
  },
  modal: {
    ondismiss: () => {
      // Student closed the modal. The order stays 'pending' — re-open by
      // calling checkout/ again; it returns the same razorpay_order_id.
    },
  },
}).open();
```

**Test cards** (test mode only): card `4111 1111 1111 1111`, any future expiry,
any CVV, OTP `1234`. UPI success: `success@razorpay`.

### 11.6 `POST /orders/{id}/verify/` — confirm the payment

Post the Checkout `handler` payload verbatim:

```json
{ "razorpay_order_id": "order_TJ0mlTna615ecg",
  "razorpay_payment_id": "pay_TJ0nQ2xY8kLmNo",
  "razorpay_signature": "9f8c…" }
```

Server-side this checks, in order: the **HMAC signature** is genuine → the
gateway order belongs to **this** order → the payment is **captured** → the
**amount matches** the order total. Only then does it issue the invoice and grant
access.

**`200`** → the updated order, `status: "paid"`, `payments[]` populated.

| Code | Cause |
|---|---|
| `400` | `Payment signature verification failed.` |
| `400` | `This payment does not belong to this order.` |
| `400` | `Paid amount X does not match the order total Y.` |
| `400` | `Payment is not captured (status: …).` |
| `502` | Razorpay unreachable while re-fetching the payment |

**Idempotent** — if the webhook already settled the order, this returns the paid
order unchanged.

### 11.7 `POST /payments/webhook/razorpay/` — gateway callback

**Not called by your frontend.** Configure it in Razorpay → Settings → Webhooks
with events `payment.captured` and `payment.failed`, then put the webhook secret
in `RAZORPAY_WEBHOOK_SECRET`.

Unauthenticated by design (Razorpay holds no credentials of ours) — authenticity
comes from an HMAC over the **raw body** in `X-Razorpay-Signature`, verified
before the payload is parsed.

| Response | Meaning |
|---|---|
| `200 {"status":"ok"}` | settled |
| `200 {"status":"unknown_order"}` | no matching order — acked so Razorpay stops retrying |
| `200 {"status":"rejected"}` | amount/capture mismatch — permanent, logged for manual reconciliation |
| `200 {"status":"ignored"}` | event we don't handle |
| `400` | bad signature or malformed body |
| `500` | transient failure — Razorpay retries |

> **Until `RAZORPAY_WEBHOOK_SECRET` is set this endpoint returns `503`** and only
> the browser `verify/` path can confirm payments. That's the one remaining setup
> step before checkout is production-safe.

### 11.8 `POST /orders/{id}/pay/` — mock confirmation *(dev/test only)*

```json
{ "gateway": "mock", "payment_method_id": 2, "gateway_payment_id": "" }
```

Marks the order paid with no money involved. **Returns `409`** when Razorpay keys
are configured — which they now are, so this is effectively disabled outside
local dev and the test suite. Idempotent.

### 11.9 Reads

| Endpoint | Shape |
|---|---|
| `GET /orders/` · `/orders/{id}/` | as above, your own only |
| `GET /invoices/` | `{ id, number, order, description, amount, gst_amount, status, issued_date, pdf_url }` |
| `GET /subscriptions/` | `{ id, plan: {…}, status, current_period_start, current_period_end, cancel_at, created_at }` |
| `POST /subscriptions/{id}/cancel/` | sets `status: "cancelled"` |
| `GET`/`POST` `/payment-methods/` | `{ id, type, brand, last4, expiry, is_default, created_at }` — setting `is_default` unsets the others |

---

## 12. Certificates

### `GET /certificates/` — your certificates

```json
{ "id": 12, "serial_number": "JG-2026-000012",
  "student": { "id": 42, "full_name": "Asha R", "email": "…" },
  "course": 3, "course_title": "Intro to Data Science",
  "course_slug": "intro-to-data-science",
  "enrollment": 88, "template": null,
  "issued_date": "2026-07-28", "grade": "A", "total_hours": 8,
  "pdf_url": "", "verification_code": "a1b2c3d4e5",
  "verification_url": "http://…/api/v1/certificates/verify/a1b2c3d4e5/",
  "status": "issued" }
```

All fields read-only. Certificates are **auto-issued** when an enrollment hits
100% (§4.2).

### `POST /certificates/claim/` — `{ "course": 3 }`

Fallback if auto-issue was missed. **`201`** (new) / **`200`** (already issued).
**`400`**: `You are not enrolled in this course.` ·
`Complete the course before claiming a certificate.`

### `GET /certificates/{id}/download/`

Returns **`text/html`** (not JSON, not a PDF) — a printable certificate page the
browser can "Save as PDF". Scoped to the holder / course trainer / admin.

### `GET /certificates/verify/{code}/` — **public, no auth** (QR target)

```json
{ "valid": true, "serial_number": "JG-2026-000012", "holder": "Asha R",
  "course_title": "Intro to Data Science", "issued_date": "2026-07-28",
  "total_hours": 8, "grade": "A", "status": "issued" }
```

**`404`** `{ "valid": false, "detail": "Certificate not found." }`.
`valid` is `true` only when `status == "issued"` (revoked certificates resolve but
report `valid: false`).

### `GET /certificate-templates/` — read-only for students.

---

## 13. File uploads

Used by students for **assignment file answers** (§6.3) and avatars. Bytes never
pass through Django.

### Step 1 — `POST /uploads/presign/`

```json
{ "filename": "report.pdf", "content_type": "application/pdf",
  "purpose": "assignment" }
```

`purpose` (student-relevant): `assignment`, `avatar`, `message_attachment`.
Full set also includes `course_thumbnail`, `course_intro_video`, `lesson_video`,
`lesson_resource`, `library_video`, `library_file`, `library_thumbnail`, `recording`.

**`201`**

```json
{ "method": "PUT",
  "url": "https://<bucket>.r2.cloudflarestorage.com/…?X-Amz-Signature=…",
  "headers": { "Content-Type": "application/pdf" },
  "key": "submissions/42/2026/07/ab12cd34-report.pdf",
  "public_url": "https://…", "expires_in": 900, "max_bytes": 52428800 }
```

**`503`** `Object storage is not configured on this server.` ·
**`502`** `Could not generate an upload URL. Try again.`

### Step 2 — `PUT <url>` with the raw file body

Send **exactly** the `Content-Type` from `headers` — a mismatch breaks the
signature. No `Authorization` header.

### Step 3 — save the `key`

Send it as `file_key` in the assessment submit payload (§6.3).

### Reading a private file back — `GET /uploads/download/?key=<key>`

→ `{ "download_url": "https://…?X-Amz-Signature=…", "expires_in": 3600 }`

> **No ownership check.** Any authenticated user who knows a key can presign a
> download for it. See §14.

---

## 14. Known gaps & caveats

Things a frontend integrator will hit. None of these are speculative — each is
visible in the current code.

| # | Gap | Impact |
|---|---|---|
| 1 | **No price on the course card.** `CourseListSerializer`/`CourseDetailSerializer` expose no price field. | Catalog needs a second call to `/course-prices/?course=<id>` per course, or an N+1 fan-out on a grid. |
| 2 | **`join_url` is exposed on every live-session read**, not just `join/`. | The registration check on `POST /join/` is cosmetic — anyone who can list sessions has the meeting link. |
| 3 | **No Google Meet / Zoom integration.** `join_url` + `meeting_id` are manually-entered fields; there's no `provider` field. | Trainers must create and paste the meeting link themselves. Attendance is self-reported by the client calling `join/`. |
| 4 | **`IndividualBooking.meeting_url` is never written** (and is `read_only` in the serializer). | Confirmed 1:1 bookings have no link at all. |
| 5 | **Only `one_time` prices are purchasable.** `resolve_line_item()` hardcodes it. | `subscription`/`installment`/`per_session`/`corporate`/`group` prices can be read but not checked out. |
| 6 | **`CoursePrice.valid_from`/`valid_to` are inert.** Nothing filters on them. | An expired promotional price still prices a cart. |
| 7 | **Library `access_level` is advisory.** `file_url`/`video_url` are returned for `premium` items to any authenticated user. | Premium gating must be enforced client-side, which means it isn't enforced. |
| 8 | **`/uploads/download/` has no ownership check.** Any authenticated user can presign any key. | Assignment submissions and private lesson videos are readable by key. |
| 9 | **`community-posts/{id}/like/` is an unconditional counter bump.** No `UserLike` model. | Can't show "liked by me", can't unlike, and repeated calls inflate the count. |
| 10 | **Notification channels beyond in-app don't dispatch.** Preferences are stored and honored in the matrix, but only the in-app bell delivers. | Email/SMS/WhatsApp/push toggles are inert. |
| 11 | **Razorpay is in test mode**, and `RAZORPAY_WEBHOOK_SECRET` is not set yet. | Payments verify correctly but the webhook returns `503` until the secret is configured — so a student who closes the tab mid-payment won't be enrolled until then. Swap to `rzp_live_` keys for production. |
| 12 | **R2 bucket CORS is unconfirmed** — the browser `PUT` to the presigned URL was blocked. | Direct-to-storage uploads (assignment files, avatars) fail from the browser until the bucket's CORS policy allows `PUT` from `https://lms.jigyaasaa.com`. See below. |
| 13 | **Recordings module (§3.11) is unmounted.** | `Jigaysa/urls.py` has the include commented out — no `/recordings/` routes exist. |
| 14 | **Social / SSO login returns `501`.** | Email+password and mobile OTP are the only working login paths. |

### R2 CORS policy (fixes #12)

The presign endpoint is server-side and unaffected — what's blocked is the
browser's `PUT` straight to Cloudflare R2. Apply this in the Cloudflare
dashboard → R2 → `codexindia` → Settings → CORS Policy:

```json
[
  {
    "AllowedOrigins": [
      "https://lms.jigyaasaa.com",
      "http://localhost:3000"
    ],
    "AllowedMethods": ["PUT", "GET", "HEAD"],
    "AllowedHeaders": ["Content-Type"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3600
  }
]
```

`AllowedHeaders` must include `Content-Type` — the presigned PUT signature covers
that header, so the browser sends it and the preflight fails without it.
`AllowedOrigins` are **origins**, not paths: `https://lms.jigyaasaa.com`, not
`https://lms.jigyaasaa.com/student`.

Verify from the browser console on the deployed frontend:

```js
const p = await api.post('/uploads/presign/', {
  filename: 'test.txt', content_type: 'text/plain', purpose: 'assignment',
}).then(r => r.data);
await fetch(p.url, {
  method: 'PUT', headers: p.headers, body: new Blob(['hello']),
});  // → 200 means CORS is fixed
```

---

## Appendix — the student journey as a call sequence

```
POST /auth/register/                          → account
POST /auth/login/                             → access + refresh
GET  /courses/?q=data&is_free=true            → browse
GET  /courses/{slug}/                         → detail
GET  /course-prices/?course={id}              → price (paid courses only)

  free  → POST /courses/{slug}/enroll/
  paid  → POST /coupons/validate/   (optional)
          POST /orders/                       → priced server-side
          POST /orders/{id}/checkout/         → Razorpay Checkout params
          (browser pays via Razorpay)
          POST /orders/{id}/verify/           → enrollment + invoice
          [webhook settles it too, if the tab closed]

GET  /enrollments/                            → My courses
GET  /courses/{slug}/curriculum/              → player (video + ticks)
POST /lesson-progress/                        → as they watch
GET  /assessments/?course={id}                → quizzes
POST /assessments/{id}/submit/                → auto-graded
GET  /live-sessions/?course={id}&upcoming=true
POST /live-sessions/{id}/register/
POST /live-sessions/{id}/join/                → join_url
POST /reviews/                                → rate the course
GET  /certificates/                           → auto-issued at 100%
GET  /certificates/{id}/download/             → printable HTML
```
