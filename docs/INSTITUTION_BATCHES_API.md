# Institution · Batches — Frontend API

Backs `/institution/batches`: the three tiles, the batch cards, **New batch**
and **Manage**.

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

## 1. The page is two calls

| Section | Call |
|---|---|
| Three tiles | `GET /institution/batches/summary/` |
| The cards | `GET /institution/batches/?status=all` |

plus the writes below for **New batch** and **Manage**.

---

## 2. Handle `409` before anything else

```json
{ "detail": "This account is not linked to an institution yet. A platform admin needs to add it to one before the institution console has anything to show." }
```

`User.organization` is nullable and nothing forces an `institution`-role account
to have one — an admin promotes someone and forgets the separate "add to
organisation" call (the two are deliberately separate; see
`docs/ADMIN_INSTITUTIONS_API.md`). **Every endpoint in the console returns
`409` in that state**, not empty tiles.

Render it as a "your account isn't linked yet — contact your administrator"
state, not as an empty dashboard. Zeros would say *"your institution has no
learners"*, which is a different and wrong story.

An institution with a real organisation but no data gets **`200` with zeros and
empty arrays** — never an error, never a gap.

---

## 3. `GET /institution/batches/summary/` — the three tiles

```json
{ "active": 3, "completing": 1, "upcoming": 1,
  "ended": 0, "total": 4, "learners": 280 }
```

| Field | Definition |
|---|---|
| `active` | cohorts whose date window contains today — the *Active batches* tile |
| `completing` | of those, the ones inside the closing window (see §4) |
| `upcoming` | `start_date` still in the future — the *Upcoming* tile |
| `ended` | `end_date` already past |
| `total` | every batch you have ever had |
| `learners` | distinct people with an active enrolment in one of your batches |

> ### This is not the same "learners" as the Dashboard's
>
> The Dashboard tile (`/institution/overview/` → `learners`) is your whole
> **roll** — everyone who belongs to the institution. This one counts **people
> actually in a cohort**. Someone on the roll who is not enrolled in any batch
> appears only in the first.
>
> They live on separate endpoints deliberately. One field meaning two things
> across two screens is how those screens start disagreeing in a review.

`learners` counts **distinct people**, so it can read *lower* than the cards add
up to when somebody is in two cohorts. That is correct, not a rounding bug — if
you want the tile to equal the sum of the cards exactly, sum the cards.

`active` **includes** `completing`: a cohort three weeks from its end date is
still running.

---

## 4. Endpoints

| | |
|---|---|
| `GET /institution/batches/` | the cards (paginated) |
| `POST /institution/batches/` | **New batch** |
| `GET /institution/batches/{id}/` | one batch |
| `PATCH` / `PUT` `/institution/batches/{id}/` | **Manage** |
| `DELETE` | **405 — there is no delete.** See below. |

### Reading

`?status=` `active` **(default)** | `upcoming` | `completing` | `ended` | `all`
`?course=<id>` to limit to one course. Paginated (`?page=`, `?page_size=`, max
100), ordered by start date, newest first.

The page shows every cohort, so it wants **`?status=all`** — the default is
`active` and would hide the upcoming card.

An unrecognised `status` **falls back to `active`** rather than `400`-ing — this
backs a panel, and a typo'd filter should show the running cohorts, not an error
page.

```json
{
  "id": 7, "name": "Batch A · CS 2025",
  "course": "React 19 Pro", "course_id": 3, "course_slug": "react-19-pro",
  "trainer": "Dr. Kapoor", "trainer_id": 12,
  "seats_taken": 120, "learners": 118, "capacity": 150,
  "completion": 72.0,
  "start_date": "2026-02-01", "end_date": "2026-06-30",
  "state": "active",
  "schedule": {}
}
```

> ### `seats_taken` and `learners` are not the same number
>
> `seats_taken` is the denormalised counter the enrolment flow maintains — seats
> sold. `learners` counts enrolments still `active` — people actually on the
> course. They diverge exactly when a cohort is emptying out, which is the thing
> worth seeing. Show `learners` on the card; the gap is the story.

`completion` is `null` for a batch with no enrolments.

### `state` — the card badge

`upcoming` · `active` · `completing` · `ended`, derived **from the batch's dates
alone**:

| `state` | Rule |
|---|---|
| `upcoming` | `start_date` is in the future |
| `ended` | `end_date` is in the past |
| `completing` | active, and `end_date` is within **30 days** |
| `active` | anything else, including a batch with no dates at all |

> ### The 30 days is a chosen default, not a platform fact
>
> Nothing in the platform defined "completing" — the mock simply showed the
> badge. This is `COMPLETING_WINDOW_DAYS` in
> `analytics/institution_api.py`; change it there and the filter and the badge
> move together.
>
> It is deliberately **not** based on how far learners have got. A badge driven
> by completion percentage is a judgement about people; one driven by the
> calendar is a fact about the cohort. Batch C in the mock reads "completing" at
> 91% — if that is the intent you want, say so and it becomes a different rule.

`?status=completing` returns exactly the rows that label themselves
`completing`; the filter and the badge are kept in step by construction.

### Writing

```jsonc
POST /institution/batches/
{
  "course": 3,                    // required
  "name": "Batch D · UX 2026",    // required, non-blank
  "trainer": 12,                  // optional, must be a trainer account
  "start_date": "2026-07-01",     // optional
  "end_date": "2026-11-30",       // optional
  "capacity": 60,
  "schedule": {}
}
```

Returns **201** with the full card row above — the same shape `GET` returns, so
you can drop it straight into the list without refetching. `PATCH` returns
**200** in the same shape.

> ### `organization` is not a field — don't send it
>
> The tenant is taken from your account. `courses.BatchSerializer` (the
> trainer/admin endpoint at `/api/v1/batches/`) *does* expose `organization` as
> writable, which is exactly why the console does not reuse it: widening that
> endpoint's permissions would have let one institution file a batch under
> another institution's name. Sending `organization` here is ignored, on create
> and on update.

`enrolled_count` is not writable either — it is a counter the enrolment flow
maintains.

**Validation:**

| Rule | Response |
|---|---|
| `course` must be **published**, or owned by your institution | `400 course` |
| `trainer` must be an account with `role=trainer` | `400 trainer` |
| `end_date` cannot precede `start_date` | `400 end_date` |
| `capacity` cannot be cut below the learners already enrolled | `400 capacity` |
| `name` cannot be blank | `400 name` |

Two of those are worth the explanation:

- **Published courses, or your own.** Running a cohort through somebody else's
  *published* course is the common case and stays allowed. Building one on a
  stranger's unfinished **draft** is not: the syllabus can still change under
  your cohort and the trainer never offered it. Your own drafts are fine —
  your syllabus can't change under you.
- **`PATCH` is validated against the merged object.** A `PATCH` sends one field;
  checking only what was sent would let an `end_date` slide behind an untouched
  `start_date`. Each value falls back to the stored one.

### Another institution's batch is a `404`

Not a `403`. A `403` confirms the row exists, and you have no business learning
that. This applies to `GET`, `PATCH` and `PUT` on the detail route.

### There is no delete

`DELETE` returns **405**. `Enrollment.batch` is `SET_NULL`, so deleting a batch
would silently detach every learner's enrolment from the cohort they took while
leaving the enrolment behind — history rewritten with no warning. `Batch` has no
deactivation flag to use instead (the pattern `core.admin_api` uses to retire an
institution), so adding one is a model change and a deliberate decision, not
something to infer from a "Manage" button.

If the page needs a cancel action, that is the conversation to have.

---

---

## 5. What this page does **not** have

**No delete** — see above. Also no bulk enrolment into a batch, no per-batch
learner roster endpoint, and no schedule editor beyond the free-form `schedule`
JSON field. "Manage" currently means rename, retime, re-staff and resize.
