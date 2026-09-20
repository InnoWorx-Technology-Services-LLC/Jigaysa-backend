# Institution · Courses — Frontend API

Backs `/institution/courses`: the three tiles and the course cards.

Sibling screens have their own docs — `docs/INSTITUTION_DASHBOARD_API.md`,
`docs/INSTITUTION_BATCHES_API.md`.

Base URL: `/api/v1/` · Auth: `Authorization: Bearer <access token>`
Role: **`institution` only.** A student, trainer *or admin* gets `403`.

> ### Everything is scoped to your own institution
>
> The catalogue is the same for every institution, but **`assigned`,
> `batches` and every tile are yours alone**. Another institution's cohort on
> the same course is never visible on your card.

---

## 1. The page is two calls

| Section | Call |
|---|---|
| Three tiles | `GET /institution/courses/summary/` |
| The cards | `GET /institution/courses/` |

## 2. Handle `409` before anything else

An `institution`-role account not linked to an organisation gets `409` from
every endpoint in the console, not empty tiles. See
`docs/INSTITUTION_DASHBOARD_API.md` §2 — the handling is identical.

---

## 3. `GET /institution/courses/summary/` — the three tiles

```json
{ "available": 6, "assigned": 3, "active": 3, "learners": 280 }
```

| Field | Definition |
|---|---|
| `available` | courses in your catalogue — the denominator |
| `assigned` | distinct courses that have at least one of **your** batches |
| `active` | of those, the ones with a **currently running** batch |
| `learners` | distinct people with an active enrolment in one of your batches |

> ### `active` can never exceed `assigned`
>
> A course cannot be running for you without being assigned to you. The sample
> screen shows **4 active against 3 assigned**, which no real data can produce
> — don't build the tile expecting that shape.

`learners` is the **same** definition as the Batches screen's tile, and
deliberately **not** the Dashboard's `learners`, which is your whole roll. See
`docs/INSTITUTION_BATCHES_API.md` §3.

---

## 4. `GET /institution/courses/` — the cards

Paginated (`?page=`, `?page_size=`, max 100), newest first.

| Filter | Values |
|---|---|
| `?assigned=` | `true` — only courses already on one of your batches; `false` — only unassigned |
| `?category=` | category id |
| `?type=` | `Course.CourseType`: `live_batch`, `self_paced`, `hybrid`, `physical`, `individual_coaching`, `group_coaching` |
| `?level=` | `beginner` \| `intermediate` \| `advanced` |
| `?q=` | matches title or subtitle |

```json
{
  "id": 3, "title": "React 19 Professional", "slug": "react-19-professional",
  "subtitle": "Modern React with Server Components",
  "category": "Web Development", "category_id": 2,
  "trainer": "Priya Sharma", "trainer_id": 12,
  "course_type": "live_batch",
  "skill_level": "intermediate",
  "duration_minutes": 2400, "duration_hours": 40.0,
  "thumbnail": "", "is_free": false, "is_own": false,
  "assigned": true,
  "batches": [
    { "id": 7, "name": "Batch A · CS 2025", "learners": 120, "capacity": 150 }
  ]
}
```

- **The badge** (`live` / `self-paced` / `hybrid`) is `course_type`.
- **`duration_hours`** is derived from `duration_minutes` server-side. The card
  prints "40 h"; six clients dividing by 60 is six chances to round
  differently.
- **`batches` is *your* cohorts only.** `assigned` is just whether that list is
  non-empty, shipped as its own boolean so the card doesn't have to know the
  rule.
- **A course can be on more than one of your cohorts.** The mock shows a single
  "Assigned to"; the array returns them all, so the card can say "+2 more"
  rather than make a course look free when it's already running twice.
- **`is_own`** distinguishes your own programme from one out of the shared
  catalogue.

### What's in your catalogue

Published **and** public courses, plus **everything your institution owns**
whatever its state — your own drafts included, because your syllabus can't
change underneath you the way a stranger's can.

Someone else's draft, pending-review, archived or private course is not in it.

---

## 5. "Assign to batch" — it's the batches endpoint

There is **no** `POST /institution/courses/`. A `Batch` carries exactly one
required `course`, so assigning a course to a cohort *is* creating the cohort:

```
POST /institution/batches/   { "course": 3, "name": "Batch D · UX 2026", ... }
```

See `docs/INSTITUTION_BATCHES_API.md` §4. A second endpoint doing the same
write through different words would be two code paths for one action, and two
places for the tenant check to be got wrong.

> ### Browsing and assigning share one queryset
>
> `GET /institution/courses/` and the `course` validation on
> `POST /institution/batches/` run **the same** filter
> (`institution_courses()` in `analytics/institution_api.py`). Every card the
> page shows can be assigned; anything it doesn't show is rejected with
> `400 course`.
>
> They were two similar-but-separate rules at first. That is exactly how you
> end up with a card carrying a live "Assign to batch" button that 400s when
> pressed — found by a customer, not a test.

Courses themselves are not editable here. They belong to their trainer;
authoring lives at `/api/v1/courses/`.

---

## 6. What this page does **not** have

**"Request custom training" has no backend.** Nothing in the platform models a
training enquiry — there is no table to write it to and no admin screen to read
it from. Building it means a new `TrainingRequest` model (a migration), an
institution-side `POST`, and an admin-side queue, or the button posts into a
black hole.

Also absent: unassigning a course (that is deleting a batch, which
`docs/INSTITUTION_BATCHES_API.md` explains is deliberately not offered), and
per-course learner rosters.
