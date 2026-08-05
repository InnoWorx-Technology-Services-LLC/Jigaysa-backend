# Course Module API — Student · Trainer · Admin

The whole course lifecycle with payloads and responses (PRD §3.2 course
management, §2.1 course approval, §3.12 progress).

Base URL `/api/v1/` · `Authorization: Bearer <access>` on every call.
Payment is a separate document: [COURSE_PAYMENT_FLOW.md](COURSE_PAYMENT_FLOW.md).

---

## 0. Lifecycle

```
        trainer                      admin                    student
  ┌───────────────────┐
  │ create → draft    │
  │ build curriculum  │
  │ publish/  ────────┼──▶ pending_review ──publish/──▶ published ──▶ enroll
  └───────────────────┘         │                          │
                            reject/                   edit curriculum
                                ▼                          ▼
                              draft            has_unapproved_changes = true
                                                  (stays live, queued)
```

Statuses: `draft` · `pending_review` · `published` · `archived`.

**Who sees what** (enforced in the queryset, not just the UI):

| Role | Sees |
|---|---|
| Student / institution | `published` + `visibility: public` only |
| Trainer | the above **plus all their own**, any status |
| Admin | everything |

---

# PART 1 — TRAINER

## 1.1 My courses

### `GET /courses/?mine=true`

Add `&status=draft` · `pending_review` · `published` · `archived` for the tabs.

```json
{
  "count": 1,
  "results": [
    {
      "id": 3, "slug": "react-pro", "title": "React Pro", "subtitle": "",
      "trainer": { "id": 5, "full_name": "Dr. Kapoor", "email": "…" },
      "category": "Web", "course_type": "self_paced", "skill_level": "beginner",
      "language": "en", "duration_minutes": 0,
      "thumbnail": "", "thumbnail_color": "#8FD14F",
      "is_free": false, "status": "draft",
      "has_unapproved_changes": false,
      "rating_avg": "0.00", "rating_count": 0, "enrolled_count": 0,
      "published_at": null
    }
  ]
}
```

## 1.2 Create a course

### `POST /courses/`

```json
{
  "title": "React Pro",
  "category": 2,
  "skill_level": "beginner",
  "course_type": "self_paced"
}
```

`course_type`: `self_paced` · `live_batch` · `physical` · `hybrid` ·
`individual_coaching` · `group_coaching`.
`skill_level`: `beginner` · `intermediate` · `advanced`.

**`201`** → the full course object (§1.3). `slug` is generated from the title;
`status` starts at `draft`; `trainer` is taken from the token, never the body.

**`403`** for students — `Only trainers can create courses.`

## 1.3 Edit (autosave)

### `PATCH /courses/{slug}/`

Send only what changed — every section of the editor writes here.

```json
{
  "title": "React Pro",
  "subtitle": "Design products people actually use.",
  "description": "Master the design loop…",
  "category": 2,
  "tags": [4, 9],
  "skill_level": "beginner",
  "language": "en",
  "thumbnail": "https://cdn…/cover.png",
  "thumbnail_color": "#8FD14F",
  "intro_video_url": "https://…/intro.mp4",
  "outcomes": ["Run a usability test", "Ship a Figma prototype"],
  "welcome_message": "Welcome aboard!",
  "completion_message": "You did it!",
  "certificate_enabled": true,
  "visibility": "private",
  "is_free": false
}
```

| Field | Editor section |
|---|---|
| `title`, `subtitle`, `description`, `category`, `skill_level`, `language`, `tags` | Basics |
| `thumbnail`, `thumbnail_color`, `intro_video_url` | Media |
| `outcomes` (list of strings) | Outcomes |
| `welcome_message`, `completion_message`, `certificate_enabled`, `visibility` | Settings |
| `is_free` | Pricing |

`visibility`: `public` · `unlisted` · `private`.
**`status` and `published_at` are read-only** — they move only through the
publish/reject actions.

`certificate_enabled` is real: switch it off and no certificate is issued on
completion.

**`403`/`404`** if you don't own the course.

## 1.4 Cover image upload

### `POST /uploads/presign/`

```json
{ "filename": "cover.png", "content_type": "image/png", "purpose": "course_thumbnail" }
```

Returns `{ method, url, headers, key, … }`. PUT the file to `url`, then save the
returned public URL onto `thumbnail`. Same flow with `purpose: "lesson_video"`
for private lesson video (stored on `Lesson.video_key`).

## 1.5 Pricing

### `POST /course-prices/`

```json
{
  "course": 3,
  "pricing_type": "one_time",
  "amount": "999.00",
  "currency": "INR",
  "discount_percent": "10.00",
  "discount_amount": "0.00",
  "valid_to": "2026-09-30T00:00:00Z"
}
```

Only `one_time` reaches checkout today. A paid course with **no** price row
cannot be bought — checkout returns `'React Pro' is not purchasable yet.`

## 1.6 Curriculum

### `POST /modules/`
```json
{ "course": 3, "title": "The design loop", "summary": "", "order": 0 }
```

### `POST /lessons/`
```json
{
  "module": 11,
  "title": "What UX really is",
  "content_type": "video",
  "order": 0,
  "duration_minutes": 9,
  "video_url": "https://…/lesson.mp4",
  "content": "",
  "is_preview": false
}
```

`content_type`: **`video` · `reading` · `quiz` · `assignment` · `live`** — exactly
the "Add lesson" menu. For `quiz`/`assignment` set `assessment: <id>`; for
`live` set `live_session: <id>`. `is_preview: true` makes a lesson watchable
without enrolling.

Reorder by PATCHing `order`. `PUT`/`PATCH`/`DELETE` on `/modules/{id}/` and
`/lessons/{id}/` work as normal.

### `POST /lesson-resources/`
```json
{ "lesson": 104, "title": "Heuristics cheatsheet", "url": "https://…", "resource_type": "pdf" }
```

> ⚠️ **Editing the curriculum of a *published* course sets
> `has_unapproved_changes: true`** and notifies admins. The course **stays
> live** — students are not interrupted — but it reappears in the admin queue.

## 1.7 Assessments

### `POST /assessments/`
```json
{
  "course": 3,
  "lesson": null,
  "title": "Module 1 checkpoint",
  "assessment_type": "quiz",
  "pass_percent": 70,
  "time_limit_minutes": 10,
  "max_attempts": 1,
  "is_published": true
}
```

`lesson: null` = "Whole course". `assessment_type`: `quiz` · `assignment` ·
`coding` · `descriptive`.

### `POST /assessments/{id}/questions/` — the "Save questions" button

**Replaces the entire question set in one call.** Trainer-only.

```json
{
  "questions": [
    {
      "question_type": "mcq",
      "text": "Which is a React hook?",
      "points": 1,
      "choices": [
        { "text": "useState", "is_correct": true },
        { "text": "componentDidMount", "is_correct": false }
      ]
    },
    {
      "question_type": "multi",
      "text": "Which are hooks?",
      "points": 2,
      "choices": [
        { "text": "useMemo", "is_correct": true },
        { "text": "useRef", "is_correct": true },
        { "text": "render", "is_correct": false }
      ]
    },
    { "question_type": "descriptive", "text": "Explain reconciliation.", "points": 5 }
  ]
}
```

`question_type`: `mcq` (single answer) · `multi` (multi answer) · `descriptive` ·
`coding` · `file`.

**`200`** → the saved questions **including `is_correct`**, plus generated ids.
`Assessment.total_questions` is updated.

**`GET /assessments/{id}/questions/`** returns the set with the answer key —
**trainer/admin only** (`403` for students, who get the key-free shape from
`GET /assessments/{id}/`).

**`400`** validation:

| Message | Cause |
|---|---|
| `Give the question at least two options.` | objective question with <2 choices |
| `Mark which option is correct.` | no `is_correct: true` |
| `A single-answer question can have only one correct option.` | `mcq` with 2+ correct |

## 1.8 Submit for review

### `POST /courses/{slug}/publish/`

No body (trainer). → `status: "pending_review"`, admins notified.

**`400`**:
- `Add at least one module before submitting for review.`
- `Add at least one lesson before submitting for review.`
- `This course is already published. Curriculum edits are queued for review automatically.`

> The last one is deliberate: this call used to silently **unpublish** a live
> course and cut off enrolled students.

## 1.9 Archive

### `POST /courses/{slug}/archive/`

→ `status: "archived"`. Leaves the catalog; **enrollments, progress,
certificates and orders are all kept**. Use this instead of `DELETE`, which
would tear out rows students paid for.

---

# PART 2 — ADMIN

## 2.1 Review queue

### `GET /courses/review-queue/` — admin only (`403` otherwise)

```json
{
  "pending_review": [ { "id": 3, "slug": "react-pro", "title": "React Pro", "...": "…" } ],
  "changed_after_approval": [ { "id": 8, "title": "UX Fundamentals", "...": "…" } ]
}
```

Two piles in one call: fresh submissions, and live courses edited after approval.

## 2.2 Approve

### `POST /courses/{slug}/publish/` (as admin)

```json
{ "note": "Looks good" }
```

→ `status: "published"`, `published_at` stamped, `has_unapproved_changes` cleared,
trainer notified **"Course approved 🎉"**. `note` is optional and is stored on
`review_note`.

## 2.3 Reject

### `POST /courses/{slug}/reject/` — admin only

```json
{ "note": "Add more depth to module 2." }
```

→ `status: "draft"` (**not** `pending_review` — otherwise the trainer could
never resubmit), `review_note` saved, trainer notified **"Changes needed"**.

**`400`** `{"note": ["Tell the trainer what needs changing."]}` — a reason is
required. **`403`** for non-admins.

## 2.4 Taxonomy

`POST/PATCH/DELETE /categories/` and `/tags/` — **admin only**; everyone else
can read.

```json
{ "name": "Web Development", "parent": null, "icon": "" }
```

## 2.5 Everything else

Admins bypass the ownership check on every course endpoint above — same URLs,
no `mine=true` filter, all statuses visible.

---

# PART 3 — STUDENT

## 3.1 Browse the catalog

### `GET /courses/`

Filters: `category` · `tag` (slug) · `skill_level` · `course_type` ·
`is_free=true` · `q=<search>` · `trainer` ·
`ordering=-created_at|-rating_avg|-enrolled_count`.

Returns only `published` + `public` courses (§0).

## 3.2 Course detail

### `GET /courses/{slug}/`

```json
{
  "id": 3, "slug": "ux-fundamentals", "title": "UX Fundamentals",
  "subtitle": "Design products people actually use.",
  "description": "Master the design loop…",
  "trainer": { "id": 5, "full_name": "Dr. Kapoor", "email": "…" },
  "category": { "id": 2, "name": "Design", "slug": "design", "parent": null, "icon": "" },
  "tags": [{ "id": 4, "name": "UX", "slug": "ux" }],
  "course_type": "self_paced", "skill_level": "beginner", "language": "en",
  "duration_minutes": 190,
  "thumbnail": "", "thumbnail_color": "#8FD14F", "intro_video_url": "",
  "outcomes": ["Run a usability test"],
  "welcome_message": "Welcome aboard!", "completion_message": "You did it!",
  "certificate_enabled": true,
  "prerequisites": [], "status": "published", "visibility": "public",
  "is_free": false, "rating_avg": "4.80", "rating_count": 32,
  "enrolled_count": 210, "module_count": 3,
  "published_at": "2026-07-01T…", "created_at": "…", "updated_at": "…"
}
```

## 3.3 Enroll

### `POST /courses/{slug}/enroll/`

Body optional: `{ "batch": 12 }` for a live batch.

**`201`** → an Enrollment (§3.6). Works when the course is **free**, or when the
student holds a plan with `all_paid_courses` (enrollment is then tagged
`source: "subscription"`).

**`400`**:
- `This is a paid course. Buy it via checkout (POST /api/v1/orders/ then /orders/{id}/pay/), or subscribe to a plan that includes all paid courses.`
- `Already enrolled in this course.`
- `Course is not open for enrollment.` (not published)

## 3.4 The player

### `GET /courses/{slug}/curriculum/`

```json
{
  "course": "ux-fundamentals",
  "has_access": true,
  "progress_pct": 18,
  "modules": [
    {
      "id": 11, "title": "The design loop", "summary": "", "order": 0,
      "lessons": [
        {
          "id": 101, "module": 11, "title": "What UX really is",
          "content_type": "video", "order": 0, "duration_minutes": 9,
          "is_preview": true, "locked": false,
          "video_url": "https://…signed…", "content": "",
          "resources": [],
          "completed": true, "watch_pct": 100, "last_position_seconds": 540
        }
      ]
    }
  ]
}
```

Everything the player needs: `completed` for the tick, `duration_minutes` for
`9m`, `content_type` for the icon, `last_position_seconds` to resume, and
`progress_pct` for "18% complete".

**`has_access` is the single source of truth** — gate the player on it, don't
re-derive it. It is `true` for admins, the owning trainer, anyone with a
purchased/free/bulk enrollment, or a current subscriber. A
**subscription-sourced enrollment stops granting access when the plan lapses**,
while a purchased one never does.

When `has_access` is `false`, non-preview lessons come back with
`locked: true` and empty `video_url`/`content`/`resources` — the outline is
visible, the content is not.

Private videos (`video_key`) are returned as a **short-lived presigned URL**;
re-fetch rather than caching it.

## 3.5 Track progress

### `POST /lesson-progress/` — upsert

```json
{
  "enrollment": 88,
  "lesson": 104,
  "status": "completed",
  "watch_pct": 100,
  "last_position_seconds": 600,
  "time_spent_seconds": 610
}
```

`status`: `not_started` · `in_progress` · `completed`. Posting again for the
same lesson **updates** the existing row. Use it both for "Mark complete" and
for periodic position saves during playback.

**`403`** `Not your enrollment.`

## 3.6 My courses

### `GET /enrollments/`

```json
{
  "id": 88, "student": 12,
  "course": { "id": 3, "slug": "ux-fundamentals", "title": "UX Fundamentals",
              "thumbnail_color": "#8FD14F", "...": "…" },
  "batch": null, "status": "active", "source": "purchase", "order": 301,
  "progress_pct": 18,
  "enrolled_at": "2026-07-02T…", "completed_at": null
}
```

`progress_pct` drives the card's bar; `status: "completed"` switches
**Continue** to **Review**.

## 3.7 Notes tab

### `POST /lesson-notes/` — upsert

```json
{ "lesson": 104, "body": "Heuristics: visibility of system status…" }
```

**`201`** first time, **`200`** on later saves — one pad per lesson, so the tab
can just save whatever is in the textarea.

`GET /lesson-notes/?lesson=104` → your note.
**Notes are private**: the queryset is always scoped to the caller, so a trainer
or admin cannot read a student's notes.

## 3.8 Q&A tab

Course discussion, not a separate system:
`GET /discussion-threads/?course=3` and `POST /discussion-threads/`.
Full detail in [STUDENT_COMMUNITY_API.md](STUDENT_COMMUNITY_API.md).

## 3.9 Resources tab

Comes back inside each lesson in §3.4 as `resources[]` — no extra call. Empty
list → "No downloadable resources for this lesson."

## 3.10 Reviews

### `POST /reviews/`
```json
{ "course": 3, "rating": 5, "comment": "Excellent." }
```
`GET /reviews/?course=3` to list. `Course.rating_avg` / `rating_count` update.

---



## 5. Two frontend badges to remove

Both are marked "Pending API" in the editor but **already exist**:

1. **Media → cover image upload** — §1.4, `purpose: "course_thumbnail"`
2. **Pricing → "amounts and plans are not stored by the API yet"** — §1.5, the
   whole form (one-time, price, discount %, valid until) maps field for field
