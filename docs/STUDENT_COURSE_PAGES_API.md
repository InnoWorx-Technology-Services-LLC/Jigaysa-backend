# Student Course Pages — API Calls

Frontend integration guide for the two student course screens:

1. **My courses** (grid of enrolled courses)
2. **Course player** (video + curriculum sidebar)

**Base URL:** `/api/v1` · **Auth:** every call needs `Authorization: Bearer <access_token>`.
All list endpoints are paginated (`?page=`, `?page_size=`, max 100).

---

## Page 1 — My courses (`/student/courses`)

Grid of the courses the student is enrolled in, each with a thumbnail, title,
progress bar and a **Continue** button.

### Call

```
GET /api/v1/enrollments/
Authorization: Bearer <token>
```

### Response `200 OK`

```json
{
  "count": 3,
  "next": null,
  "previous": null,
  "results": [
    {
      "id": 88,
      "student": 42,
      "course": {
        "id": 3,
        "slug": "intro-to-data-science",
        "title": "Intro to Data Science",
        "subtitle": "Python, pandas & visualisation",
        "trainer": { "id": 2, "full_name": "Dr. Kapoor", "email": "kapoor@…" },
        "category": "Data Science",
        "course_type": "self_paced",
        "skill_level": "beginner",
        "thumbnail": "",
        "is_free": true,
        "rating_avg": 4.7,
        "enrolled_count": 540
      },
      "batch": null,
      "status": "active",
      "source": "free",
      "progress_pct": 42,
      "enrolled_at": "2026-07-10T09:00:00Z",
      "completed_at": null
    }
  ]
}
```

### Field → UI mapping

| UI element              | Field                          |
|-------------------------|--------------------------------|
| Card title              | `course.title`                 |
| Thumbnail / banner      | `course.thumbnail` (empty → your placeholder colour) |
| Progress bar            | `progress_pct` (0–100)         |
| **Continue** button link| `course.slug` → open `/student/courses/{slug}` |

> **Continue vs Review:** if you later want a "Review" state for finished courses,
> use `status` (`"completed"` → Review, otherwise → Continue). For now the button
> is always **Continue** and just opens the course by `slug`.

---

## Page 2 — Course player (`/student/courses/{slug}`)

The player screen renders the **video** on the left and the **curriculum**
(modules → lessons, with completion ticks, durations and type icons) on the right
— all from a **single call**.

### Call

```
GET /api/v1/courses/{slug}/curriculum/
Authorization: Bearer <token>
```

### Response `200 OK`

```json
{
  "course": "intro-to-data-science",
  "has_access": true,
  "progress_pct": 55,
  "modules": [
    {
      "id": 10,
      "title": "01 · Foundations",
      "summary": "",
      "order": 1,
      "lessons": [
        {
          "id": 101,
          "module": 10,
          "title": "Welcome & how this course works",
          "content_type": "video",
          "order": 1,
          "duration_minutes": 6,
          "is_preview": true,
          "locked": false,
          "video_url": "https://…r2…/lesson-videos/…?X-Amz-Signature=…",
          "content": "",
          "resources": [],
          "completed": true,
          "watch_pct": 100,
          "last_position_seconds": 360
        },
        {
          "id": 104,
          "module": 10,
          "title": "Checkpoint quiz",
          "content_type": "quiz",
          "order": 4,
          "duration_minutes": 10,
          "is_preview": false,
          "locked": false,
          "video_url": "",
          "content": "",
          "resources": [],
          "completed": false,
          "watch_pct": 0,
          "last_position_seconds": 0
        }
      ]
    }
  ]
}
```

### Field → UI mapping

| UI element                          | Field                                   |
|-------------------------------------|-----------------------------------------|
| Module heading "01 · Foundations"   | `module.order` + `module.title`         |
| Lesson row title                    | `lesson.title`                          |
| Duration "6m"                       | `lesson.duration_minutes`               |
| Lesson type icon                    | `lesson.content_type` (`video`/`reading`/`quiz`/`assignment`/`live`) |
| **Green completion tick** ✅        | `lesson.completed` (`true`)             |
| Progress ring / partial state       | `lesson.watch_pct` (0–100)              |
| Locked lesson (not enrolled)        | `lesson.locked` (`true`)                |
| Video source for the player         | `lesson.video_url` (already a playable URL) |
| Resume position (seek to)           | `lesson.last_position_seconds`          |
| Reading lesson body                 | `lesson.content`                        |
| Downloadable resources              | `lesson.resources[]`                    |
| Overall course progress             | top-level `progress_pct`                |

### Notes for the frontend

- **Completion ticks** come straight from `lesson.completed` — no second call, no
  client-side merging.
- **Current lesson highlight**: drive this from the route (the `lesson.id` the
  student opened). The server doesn't pick a "current" lesson.
- **Video playback**: `video_url` is **ready to play as-is**:
  - For a **privately-stored** video (uploaded to R2) it's a **short-lived
    presigned URL** — it expires (~1h), so **request the curriculum again** (or
    just this lesson) if playback is attempted after a long idle.
  - For an externally-hosted video it's the stored URL.
  - When a lesson is `locked` (not enrolled / not preview), `video_url` is `""`.

---

## Tracking watch progress (drives the ticks)

As the student watches, **upsert** their progress. Re-posting for the same lesson
updates the existing row. Marking a lesson complete also recomputes the
enrollment's `progress_pct` (and auto-issues the certificate when the course hits
100%).

```
POST /api/v1/lesson-progress/
Authorization: Bearer <token>

{
  "enrollment": 88,
  "lesson": 104,
  "status": "completed",          // or "in_progress"
  "watch_pct": 100,
  "last_position_seconds": 600
}
```

Response `201/200`:

```json
{
  "id": 501,
  "enrollment": 88,
  "lesson": 104,
  "status": "completed",
  "watch_pct": 100,
  "last_position_seconds": 600,
  "completed_at": "2026-07-28T05:10:00Z"
}
```

> Get the `enrollment` id for the course from Page 1's `/enrollments/` response
> (`results[].id`), or from `/enrollments/?` filtered client-side by `course.slug`.

---

## Trainer side — attaching a lesson video (context)

Videos are uploaded direct-to-storage, then the **key** is saved on the lesson:

1. `POST /api/v1/uploads/presign/` → `{ "filename": "nb.mp4", "content_type": "video/mp4", "purpose": "lesson_video" }`
   → returns `{ method: "PUT", url, headers, key }`.
2. `PUT <url>` with the file bytes (send the `Content-Type` from `headers`).
3. `PATCH /api/v1/lessons/{id}/` → `{ "video_key": "<key>" }`.

The student player then serves that private video as a presigned URL automatically
(via `lesson.video_url`). For externally-hosted video, set `video_url` instead.

---

## Summary of what changed for these pages

- Curriculum lessons now include **`completed`**, **`watch_pct`**,
  **`last_position_seconds`** (the completion ticks + resume point).
- Curriculum response now includes top-level **`progress_pct`**.
- Lessons gained a **`video_key`** field; private videos are returned as a
  **presigned playback URL** in `video_url`.
