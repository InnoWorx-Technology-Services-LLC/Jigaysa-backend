# Institution · Reports — Frontend API

Backs `/institution/reports`: four tiles, the cohort-completion table, the
attendance trend, and **Export CSV**.

Sibling screens have their own docs — `docs/INSTITUTION_DASHBOARD_API.md`,
`docs/INSTITUTION_BATCHES_API.md`, `docs/INSTITUTION_COURSES_API.md`,
`docs/INSTITUTION_LEARNERS_API.md`.

Base URL: `/api/v1/` · Auth: `Authorization: Bearer <access token>`
Role: **`institution` only.** A student, trainer *or admin* gets `403`.
An unlinked institution account gets `409` — see
`docs/INSTITUTION_DASHBOARD_API.md` §2.

---

## 1. The page

| Section | Call |
|---|---|
| Four tiles | `GET /institution/reports/summary/` |
| Cohort completion | `GET /institution/reports/cohorts/` |
| Attendance trend | `GET /institution/reports/attendance/` |
| **Export CSV** | `GET /institution/reports/export/` |

> ### One rule runs through this whole page: `null` is not `0`
>
> Every rate here is `null` when it has no denominator. A cohort nobody has
> taken a register for has **no** attendance rate; painting that as 0% says
> everybody stayed away. Render a dash, and do not let a chart draw the line to
> the floor.

---

## 2. `GET /institution/reports/summary/` — the four tiles

```json
{ "cohorts": 12, "active_cohorts": 5,
  "completion": 78.0, "certified": 234, "attendance": 84.0 }
```

| Field | Definition |
|---|---|
| `cohorts` | every batch you have ever had |
| `active_cohorts` | those whose date window contains today |
| `completion` | mean `progress_pct` across your institution's enrolments |
| `certified` | issued certificates held by your learners — a **count** |
| `attendance` | present ÷ recorded, across every session of your cohorts |

`certified` is a count here and the table's `certified` is a count too, with
`certified_pct` as the rate. The mock shows a count in the tile and a percentage
per row, so **both are returned explicitly** rather than left for the UI to
derive one from the other.

---

## 3. `GET /institution/reports/cohorts/` — Cohort completion

Paginated (`?page=`, `?page_size=`, max 100), newest cohort first.

```json
{
  "batch_id": 7, "batch": "Batch A", "course": "CS 2025", "course_id": 3,
  "learners": 120, "capacity": 150,
  "completion": 72.0,
  "certified": 42, "certified_pct": 35.0,
  "engagement": 81.0,
  "attendance": 88.0,
  "start_date": "2026-02-01", "end_date": "2026-06-30"
}
```

| Column | Definition | `null` when |
|---|---|---|
| `completion` | mean `progress_pct` of the cohort's enrolments | no enrolments |
| `certified_pct` | issued certificates ÷ active learners | no learners |
| `engagement` | see below | no lessons, or nobody enrolled |
| `attendance` | present ÷ recorded for the cohort's sessions | no register taken |

### `engagement` — a chosen definition

**Completed lessons ÷ (enrolments × lessons in the course).**

> Nothing in the PRD defines "engagement". This counts **work done**.
>
> Logins were the obvious alternative and are worse: they measure showing up,
> not getting anywhere, and a cohort that opens the app daily without finishing
> anything would score 100%. Attendance is already its own column, so reusing
> it here would print the same number twice.
>
> A lesson in progress does **not** count. Opening a lesson is not doing it —
> that is exactly the difference between this and a login count.

The denominator is **every enrolment the cohort has ever had**, not the
`learners` column beside it, which counts only *active* ones. The numerator
counts those same people's completed lessons, so the halves must match — and
Reports is precisely where finished cohorts get read. A cohort whose enrolments
have all moved to `completed` shows `learners: 0` and still reports the work it
actually did.

The value is clamped to 100. That is a guard, not arithmetic:
`Enrollment.course` and `Enrollment.batch` are independent FKs, so an enrolment
filed under a batch but pointing at a different course can contribute
completions the denominator's lesson count knows nothing about. Rare and
arguably bad data — but a report that prints 140% is a report nobody trusts
again.

---

## 4. `GET /institution/reports/attendance/` — the trend

`?months=` (1–36, default 12).

```json
{ "months": 12,
  "attendance": [
    { "month": "2025-10", "value": null, "sessions_recorded": 0 },
    { "month": "2025-11", "value": 84.0, "sessions_recorded": 96 }
  ] }
```

**Dense by construction**: every month in the window is present, oldest first,
including the quiet ones. Grouping in SQL returns only months that had rows, and
charting that directly runs the line straight from October to December and hides
the dip.

Bucketed by the **session's scheduled date**, not when the register row was
written — a register filled in late belongs to the class it was taken for.

`value` is `null` for a month with no sessions. `sessions_recorded` is the
denominator, so the chart can show how much weight a point carries and tell
"nobody came" from "one person was marked".

An out-of-range or unparseable `months` is clamped, not rejected.

---

## 5. `GET /institution/reports/export/` — Export CSV

Returns `text/csv` as an attachment named
`<your-slug>-cohorts-<YYYY-MM-DD>.csv`.

```
Batch,Course,Learners,Capacity,Completion %,Certified,Certified %,Engagement %,Attendance %,Start,End
Batch A,CS 2025,120,150,72.0,42,35.0,81.0,88.0,2026-02-01,2026-06-30
```

**The whole table, not the current page.** An export that stopped at twenty rows
would be a quietly wrong spreadsheet, which is worse than no export at all.

Rates with no denominator are written as **empty cells**, not `0` — a
spreadsheet averaging a missing attendance figure in as a zero is exactly the
silent wrongness this page is built to avoid.

---

## 6. What this page does **not** have

- **No date-range filter.** The tiles and table are all-time; only the trend
  takes a window (`?months=`).
- **No per-learner export.** This is cohort-level. The roster is
  `docs/INSTITUTION_LEARNERS_API.md`.
- **No scheduled or emailed reports.**
- **Attendance comes from `live.Attendance`** — online session registers.
  `classrooms.SeatAttendance`, the physical seat-level register, is modelled and
  has no API at all, so it is not in these numbers.
