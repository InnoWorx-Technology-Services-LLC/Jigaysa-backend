# Trainer · Analytics — Frontend API

Backs `/trainer/analytics`: four tiles, the engagement trend, per-course
insights, and the doubt queue.

Base URL: `/api/v1/` · Auth: `Authorization: Bearer <access token>`
Roles: `trainer` and `admin`. A student gets `403`.

> ### Everything is scoped to your own courses
>
> Every number comes from courses where `course.trainer == you`. There is no
> query parameter to widen it, and no `if admin` branch — the platform-wide
> view is a different, admin-only API at `/admin/reports/`
> (`docs/ADMIN_REPORTS_API.md`).
>
> An admin calling these gets *their* own courses, which for most admins is
> none. That's correct, not a bug.

---

## 1. The page is four calls

| Section | Call |
|---|---|
| Four tiles | `GET /trainer/analytics/summary/` |
| Engagement trend | `GET /trainer/analytics/engagement/` |
| Per-course insights | `GET /trainer/analytics/courses/` |
| Doubt panel | `GET /trainer/analytics/doubts/` |

The last two are paginated; they're the ones whose row counts grow.

---

## 2. `GET /trainer/analytics/summary/` — the four tiles

```json
{ "completion": 64.0, "quiz_average": 78.0, "submission_rate": 82.0,
  "active_learners": 312, "courses": 4 }
```

| Field | Definition |
|---|---|
| `completion` | mean `progress_pct` across **enrolments** in your courses |
| `quiz_average` | mean `percent` across **graded** submissions |
| `submission_rate` | of every attempt started, the share actually handed in |
| `active_learners` | distinct **people** with an active enrolment |
| `courses` | how many courses you own |

> ### The three rates are `null`, never `0`, when there's nothing to average
>
> A trainer with no submissions has no quiz average. Printing **0%** says
> "everyone failed". Render a dash.

Three definitions worth knowing, because each had an alternative:

- **Completion averages over enrolments, not lessons.** A course where everyone
  finished half, and one where half the students finished all of it, are
  different stories — the enrolment view is the one a trainer acts on.
- **`quiz_average` ignores ungraded work.** A submitted-but-unmarked attempt
  sits at `percent: 0` in the database; counting it would drag the average down
  by exactly the amount of grading you haven't done yet.
- **`submission_rate` counts abandoned attempts against you.** Someone who
  opened an assessment and wandered off is the denominator's whole point.
- **`active_learners` counts people, not enrolments.** One student on three of
  your courses is one learner.

---

## 3. `GET /trainer/analytics/engagement/` — the line chart

```json
{
  "months": 12,
  "lessons_completed": [ { "month": "2025-10", "value": "58" }, … ],
  "submissions":       [ { "month": "2025-10", "value": "21" }, … ]
}
```

`?months=` accepts **1–36**, default 12. Out of range is clamped; junk falls
back to the default rather than `400`-ing a dashboard.

**"Engagement" is lessons completed per month**, with submissions as the second
series. Enrolments would measure marketing rather than teaching, and a login
count would flatter a course nobody is actually working through.

Both series are **dense** — every month in the window, gaps as explicit zeros —
and share one range, so index *n* is the same month in each. A sparse series
charted directly draws a line from November to January and hides the dip.

Note the field names differ from the admin trends endpoint: this one has no
`revenue` and no `currency`, because a trainer's engagement chart has neither.

---

## 4. `GET /trainer/analytics/courses/` — per-course insights

**Paginated** (default 20, max 100). Ordered by enrolment count, busiest first.

```json
{
  "count": 4,
  "next": null,
  "previous": null,
  "results": [
    { "course_id": 7, "course": "React 19 Pro", "course_slug": "react-19-pro",
      "learners": 120, "completion": 72.0, "quiz_average": 81.0 }
  ]
}
```

`learners` is active enrolments. `completion` and `quiz_average` are
percentages 0–100, or **`null`** when the course has no enrolments / nothing
graded — the same null-not-zero rule as the tiles.

---

## 5. `GET /trainer/analytics/doubts/` — the doubt panel

**Paginated.** Open doubts first, newest within that.

```json
{
  "count": 47,
  "results": [
    { "id": 91, "text": "How does useEffect cleanup work?", "status": "open",
      "asked_at": "2026-09-01T10:12:00Z", "student_name": "Riya Sharma",
      "session_title": "Hooks deep dive", "course": "React 19 Pro" }
  ]
}
```

Filters: `?status=open|answered`, `?course=<id>`. **`?status=open` is the
actionable one** — those are questions waiting on you.

> ### ⚠️ This is not the mock's "doubt frequency"
>
> That panel is labelled *AI-detected from forum & Q&A* and ranks clustered
> topics — "useEffect cleanup · 47", with a trend arrow. Clustering free text
> into topics needs a language model, and **this project has none**. There is no
> LLM dependency anywhere in the backend.
>
> Rather than fabricate a ranking, this returns the doubts themselves. That's
> real data and arguably more useful — a trainer can *answer* these, which a bar
> chart doesn't let them do.
>
> **Build the panel as a queue, not a bar chart**, until something exists behind
> the clustering. The trend arrows and the counts have no source.

Doubts come from `SessionDoubt` — questions raised in your live sessions. Forum
threads (`engagement.DiscussionThread`) are not included; that's a separate
surface with its own API.

---

## 6. Not in this release

- **Topic clustering** for doubts — see above.
- **Forum questions** in the doubt panel.
- **Per-student drill-down.** Insights stop at the course.
- **Date ranges** on the tiles or the course table. Only `engagement` takes a
  window, and only in whole months.
- **Export.** No CSV endpoint.
- **Caching.** Every number is computed live. `AnalyticsSnapshot` exists in the
  models and nothing writes to it; if these get slow, that's the place to fix
  it, and these response shapes are designed to survive the swap.
