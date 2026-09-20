# Institution · Dashboard — Frontend API

Backs `/institution`: four tiles, the active-batch panel, upcoming classroom
bookings, and the recent activity feed.

Sibling screens have their own docs — `docs/INSTITUTION_DASHBOARD_API.md`,
`docs/INSTITUTION_BATCHES_API.md`, `docs/INSTITUTION_COURSES_API.md`,
`docs/INSTITUTION_LEARNERS_API.md`,
`docs/INSTITUTION_REPORTS_API.md`.

Base URL: `/api/v1/` · Auth: `Authorization: Bearer <access token>`
Role: **`institution` only.** A student, trainer *or admin* gets `403`.

> ### Everything is scoped to your own institution
>
> Every number comes from the organisation your account points at
> (`User.organization`). There is no query parameter to widen it and no
> `if admin` branch — one view that can answer for either the platform or a
> tenant is how the wrong institution's numbers eventually reach a customer.
>
> **Admins are excluded on purpose.** The trainer endpoints let an admin in
> because "your own courses" still means something for an admin. "Your own
> institution" does not — an admin has no organisation. The platform-wide views
> are `/admin/reports/` (`docs/ADMIN_REPORTS_API.md`) and
> `/admin/organizations/` (`docs/ADMIN_INSTITUTIONS_API.md`).

---

## 1. The page is four calls

| Section | Call | Paginated |
|---|---|---|
| Four tiles | `GET /institution/overview/` | no |
| Active batches + "View all" | `GET /institution/batches/` | yes |
| Upcoming bookings + "View all" | `GET /institution/bookings/` | yes |
| Recent activity | `GET /institution/activity/` | no — bounded by `?limit=` |

---

## 2. Handle `409` before anything else

```json
{ "detail": "This account is not linked to an institution yet. A platform admin needs to add it to one before the institution console has anything to show." }
```

`User.organization` is nullable and nothing forces an `institution`-role account
to have one — an admin promotes someone and forgets the separate "add to
organisation" call (the two are deliberately separate; see
`docs/ADMIN_INSTITUTIONS_API.md`). **All four endpoints return `409` in that
state**, not empty tiles.

Render it as a "your account isn't linked yet — contact your administrator"
state, not as an empty dashboard. Zeros would say *"your institution has no
learners"*, which is a different and wrong story.

An institution with a real organisation but no data gets **`200` with zeros and
empty arrays** — never an error, never a gap.

---

## 3. `GET /institution/overview/` — the four tiles

```json
{
  "organization": { "id": 1, "name": "Acme Institute", "slug": "acme-institute",
                    "type": "institution", "is_active": true },
  "active_batches": 3,
  "upcoming_batches": 1,
  "batches": 4,
  "learners": 6,
  "batch_learners": 5,
  "learners_joined_recently": 6,
  "new_learner_window_days": 30,
  "avg_completion": 53.7,
  "classrooms": 3
}
```

| Field | Definition |
|---|---|
| `active_batches` | batches of yours whose **date window contains today** |
| `upcoming_batches` | batches whose `start_date` is still in the future |
| `batches` | every batch of yours ever, the denominator for "3 of 4 running" |
| `learners` | **active** accounts with `role=student` on your roll |
| `batch_learners` | distinct people with an **active enrolment in one of your batches** |
| `learners_joined_recently` | of those, how many joined inside the window below |
| `new_learner_window_days` | always `30` today — the definition of the `+N` badge |
| `avg_completion` | mean `progress_pct` across your institution's enrolments |
| `classrooms` | rooms belonging to you (physical, smart and container alike) |

> ### `avg_completion` is `null`, never `0`, when there's nothing to average
>
> A college that starts on Monday has no completion rate. Printing **0%** says
> its learners are failing. Render a dash.

Four definitions worth knowing, because each had an alternative:

- **`active_batches` counts a window, not a table.** A batch with *no dates at
  all* counts as active — an unscheduled cohort is an open one, and skipping it
  would under-count against a list the institution can plainly see has rows in
  it.
- **`learners` is your roll, not your enrolments.** A student is a learner
  because they belong to your institution, not because they happen to be on a
  course. Suspended accounts (`is_active=false`) are excluded.
- **`learners` and `batch_learners` are different numbers on purpose.** The
  Dashboard's "Learners" tile is the roll (`learners`); the Batches page's
  "Total learners" tile is `batch_learners`. Someone on the roll who is not in
  any cohort appears only in the first. `batch_learners` counts **distinct
  people**, so it can read *lower* than the batch cards add up to when somebody
  is in two cohorts — that is correct, not a rounding bug. If you want the tile
  to equal the sum of the cards exactly, sum the cards.
- **`avg_completion` covers your training only** — enrolments in **your
  batches** *or* in **courses you own**. Batches alone would miss self-paced
  institutional content (whose enrolments carry no batch); courses alone would
  miss you running a cohort through somebody else's course, which is the common
  case. A learner on your roll taking an unrelated public course in their own
  time is **excluded** — that's theirs, not yours, and averaging it in makes the
  number unactionable.
- **`new_learner_window_days` ships with the badge** for the reason
  `active_window_days` ships with the admin's active-user count: "+128" is
  meaningless without "in the last 30 days", and a window that lives only in the
  backend is one the frontend eventually mislabels.

`organization.is_active` is `false` for an institution an admin has
deactivated. It can still read its own console — deactivation keeps history —
so show a banner rather than blocking the page.

---

## 4. `GET /institution/bookings/` — upcoming classroom bookings

Paginated. `?when=` `upcoming` **(default, soonest first)** | `past` | `all`.
`?room=<id>` to limit to one room.

```json
{
  "id": 2, "room": "Room 101", "room_id": 4,
  "location": "Pune Campus · Block A",
  "starts_at": "2026-09-19T09:00:00+05:30",
  "ends_at":   "2026-09-19T12:00:00+05:30",
  "status": "scheduled",
  "batch": "Batch A · CS 2025", "batch_id": 7,
  "course": "React 19 Pro",
  "session_title": "Batch A · live lecture",
  "trainer": "Dr. Kapoor"
}
```

> ### The mock's `confirmed` / `pending` badge does not exist
>
> `status` is the real `ClassroomSession` lifecycle — `scheduled`, `live` or
> `completed`. That's a lifecycle, not an approval state, and **the platform
> models no booking-approval workflow at all.** Rather than invent two values to
> match the sample screen, this returns the truth. Label the badge from
> `status`. If approvals are genuinely wanted, that's a model change and a new
> endpoint, not a serializer tweak.

`ends_at` is derived from the attached session's `duration_minutes` and is
**`null` when no live session is attached** — an empty room booking has no
length to report, and guessing an hour would put a wrong time on a timetable.
`batch`, `course`, `session_title` and `trainer` are `""` in the same case.

Bookings with no date are excluded from `upcoming` — an unscheduled booking is
not upcoming, it's unscheduled. They appear under `?when=all`.

---

## 5. `GET /institution/activity/` — recent activity

`?limit=` (1–100, default 20), newest first. **Not paginated** — an offset over
four separately-ordered tables is not a coherent page 2, and the panel has no
"View all". An out-of-range or unparseable `limit` is clamped, not rejected.

```json
{
  "limit": 20,
  "events": [
    { "type": "enrolment", "at": "2026-09-16T10:02:11+05:30",
      "actor": "Meera Iyer", "target": "Batch A · CS 2025",
      "meta": { "batch_id": 7, "course_id": 3, "course": "React 19 Pro" } },
    { "type": "booking", "at": "2026-09-16T10:02:10+05:30",
      "actor": "", "target": "Room 101",
      "meta": { "room_id": 4, "starts_at": "2026-09-19T09:00:00+05:30",
                "status": "scheduled", "batch": "Batch A · CS 2025" } }
  ]
}
```

Every event has the **same five fields**, on purpose: `type` selects the icon
and the sentence template, `actor`/`target` fill it in, `meta` carries the ids
the row links to. A per-type shape would make the feed a switch statement on the
client and a breaking change every time a type is added.

| `type` | Source | `actor` | `target` |
|---|---|---|---|
| `enrolment` | an `Enrollment` was created | the student | batch name, else course |
| `completion` | an `Enrollment` reached `completed` | the student | course title |
| `booking` | a `ClassroomSession` was created | *(blank)* | room name |
| `batch` | a `Batch` was created | *(blank)* | batch name |

`actor` is `""` for events nobody performed — a batch being created isn't
attributable to a person the way an enrolment is.

> ### This is assembled from real events, not an audit log
>
> The platform has no activity table. Rather than leave the panel empty, this
> merges four things that genuinely happened, each a real row with a real
> timestamp.
>
> **Two kinds the mock shows are deliberately absent.** *"Flagged at-risk"* has
> no definition anywhere in the platform — inventing a threshold here would put
> that label on a named student on the strength of a made-up number.
> *"Monthly analytics report exported"* has no export log to read. Both want a
> real feature behind them, not a serializer that guesses.

---

## 6. What this does **not** cover

The sidebar's other items still have no backend:

| Sidebar item | State |
|---|---|
| Batches | **done** — `docs/INSTITUTION_BATCHES_API.md` |
| Courses | **done** — `docs/INSTITUTION_COURSES_API.md` (bar "Request custom training") |
| Learners | **done** — `docs/INSTITUTION_LEARNERS_API.md` |
| Reports | **done** — `docs/INSTITUTION_REPORTS_API.md` |
| Courses, Learners, Reports | models exist, no institution-scoped endpoints |
| Classrooms | `classrooms` models exist; **no API is mounted at all** |
| Classroom bookings | read-only through §4 above — no create/edit/cancel |
| Billing | no institution-scoped billing endpoints |

Seat-level data (`Seat`, `SeatAttendance`, `Device`, `SmartEvent`) is modelled
and unexposed — the `classrooms` app still has no `urls.py`.

---

## 7. Demo data

`python manage.py seed_demo` creates an institution login and a populated
console:

```
institute@jigyasa.local   role: institution   org: Acme Institute
```

Its six learners, four batches and two room bookings are dated **relative to the
day you seed**, so the tiles are never stale — unlike the fixed 2026 dates
elsewhere in that command.
