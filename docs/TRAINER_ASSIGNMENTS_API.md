# Trainer · Assignments — Frontend API

Backs `/trainer/assignments`: three tiles, the assignment list with per-row
submission counts, and the grading queue behind each **Review** button.

Base URL: `/api/v1/` · Auth: `Authorization: Bearer <access token>`
Roles: `trainer` and `admin`. A student gets `403` on the board and stats.

---

## 1. The page is two calls (plus grading)

| Section | Call |
|---|---|
| The three tiles | `GET /assessments/stats/` |
| The assignment list | `GET /assessments/board/?assessment_type=assignment` |
| **New assignment** / **Edit** | `POST` / `PATCH /assessments/{id}/` |
| **Review (N pending)** | `GET /submissions/?assessment=<id>&status=submitted` |
| Grading one submission | `POST /submissions/{id}/grade/` |

CRUD on assessments already existed. What's new is `board/` and `stats/` — the
numbers that make the page a dashboard instead of a list.

---

## 2. `GET /assessments/stats/` — the three tiles

```json
{ "open_assignments": 2, "pending_reviews": 33, "average_score": 74.0 }
```

Scoped to **your own** assessments. Not paginated — it's one object.

**Pass the same `?assessment_type=` your board is showing.** Without it the tiles
count every type — quizzes and coding tests included — while the list below shows
one, and the two disagree for no visible reason. The filter narrows
`open_assignments`, `pending_reviews` and `average_score` together.

| Field | Means |
|---|---|
| `open_assignments` | published, and either no deadline or a deadline still ahead |
| `pending_reviews` | submissions in `submitted` — handed in, not yet graded |
| `average_score` | mean `percent` across graded submissions, or **`null`** |

> ### `average_score` is `null`, never `0`, before anything is graded
>
> A new trainer has no average. Rendering that as **0%** says "everyone
> failed", which is a different and much worse message. Show a dash.

**`pending_reviews` counts across all your assignments, not just open ones.**
Closing an assignment does not grade what students already handed in, and that
work is still queued for you.

---

## 3. `GET /assessments/board/` — the list

**Paginated** (default 20, `?page_size=` up to 100). Always scoped to your own
work — a page with an Edit button on every row has no business showing a
colleague's assignment.

```json
{
  "count": 3,
  "next": null,
  "previous": null,
  "results": [
    {
      "id": 12,
      "title": "Build a pandas DataFrame pipeline",
      "course": 4,
      "course_title": "Intro to Data Science",
      "assessment_type": "assignment",
      "available_to": "2026-05-25T18:30:00Z",
      "is_published": true,
      "state": "open",
      "submitted_count": 28,
      "pending_review_count": 6,
      "enrolled_count": 34,
      "grading_type": "manual",
      "total_questions": 3,
      "created_at": "2026-04-02T09:00:00Z"
    }
  ]
}
```

Filters: `?assessment_type=assignment` (or `quiz`, `coding`, `descriptive`),
`?course=<id>`.

### The counts

| Field | Counts |
|---|---|
| `submitted_count` | submissions in `submitted`, `graded`, `passed` or `failed` — anything actually handed in |
| `pending_review_count` | just `submitted` — **the number on the Review button** |
| `enrolled_count` | active enrolments on the course — the denominator |

`28/34 submitted` in the mock is `submitted_count` / `enrolled_count`. A
submission that was started and abandoned (`in_progress`) counts in neither.

### `state` — the badge

One word, computed server-side so three fields don't have to be reassembled in
the client:

| `state` | When |
|---|---|
| `draft` | `is_published: false` |
| `closed` | published, and `available_to` is in the past |
| `open` | everything else |

**An assignment with no `available_to` never closes on its own.** If your UI
implies a deadline is mandatory, the backend disagrees.

---

## 4. The plain list is *not* scoped — that's deliberate

`GET /assessments/` still returns every **published** assessment plus your own
drafts. That's right for browsing, wrong for a page titled "your assignments".

Pass **`?mine=true`** to narrow the plain list the same way `board/` does. Use
`board/` for this page; `?mine=true` exists for anywhere else that needs the
narrow view without the counts.

---

## 5. Reviewing

The **Review (N pending)** button opens the grading queue for one assignment:

```
GET  /submissions/?assessment=12&status=submitted
POST /submissions/{id}/grade/     { "score", "percent", "feedback?", "passed?" }
```

Each submission carries **`student_name`, `student_email` and
`assessment_title`** inline — a grading queue that says "student: 12" cannot be
worked, and fetching a user per row would turn a page into N requests.

Both already existed. `N` is the row's `pending_review_count` — don't count the
queue client-side just to label the button.

---

## 6. Not in this release

- **Bulk grading.** One submission per request.
- **Reminders.** Nothing nudges students who haven't submitted.
- **Late-submission policy.** `available_to` decides the badge; it does not
  block or flag a late hand-in.
- **Rubric weighting in the score.** `GET`/`PUT /assessments/{id}/rubric/` now
  stores criteria (`[{name, max_points}]`), and grading a rubric assessment is
  rejected if the score exceeds the rubric total or the rubric is empty. What is
  still absent is per-criterion marking — you submit one total, not a breakdown.
