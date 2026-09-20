# Institution · Learners — Frontend API

Backs `/institution/learners`: four tiles, the roster table, the **View**
drawer, **Add learner** and **Import CSV**.

Sibling screens have their own docs — `docs/INSTITUTION_DASHBOARD_API.md`,
`docs/INSTITUTION_BATCHES_API.md`, `docs/INSTITUTION_COURSES_API.md`.

Base URL: `/api/v1/` · Auth: `Authorization: Bearer <access token>`
Role: **`institution` only.** A student, trainer *or admin* gets `403`.
An unlinked institution account gets `409` — see
`docs/INSTITUTION_DASHBOARD_API.md` §2.

---

## 1. The page

| Section | Call |
|---|---|
| Four tiles | `GET /institution/learners/summary/` |
| The table | `GET /institution/learners/` |
| **View** | `GET /institution/learners/{id}/` |
| **Add learner** | `POST /institution/learners/` |
| **Import CSV** | `POST /institution/learners/import_csv/` |

Your roster is `User.organization` — **everyone on your roll**, whether or not
they are in a cohort. Progress, status and batches are computed from **your
institution's training only**: a learner's unrelated personal course does not
move their bar here.

---

## 2. `GET /institution/learners/summary/` — the four tiles

```json
{ "total": 3820, "active": 2940, "active_window_days": 90,
  "avg_completion": 78.0,
  "certificates": 234, "certificates_recent": 18,
  "certificate_window_days": 30,
  "at_risk_idle_days": 14, "at_risk_progress_pct": 40 }
```

| Field | Definition |
|---|---|
| `total` | active accounts with `role=student` on your roll |
| `active` | of those, signed in within `active_window_days` |
| `avg_completion` | mean `progress_pct` across your institution's enrolments |
| `certificates` | issued certificates held by your learners |
| `certificates_recent` | of those, issued within `certificate_window_days` — the `+18` badge |

**Three definitions travel with their numbers.** "Active", "recently issued" and
"at risk" are definitions, not facts; one that lives only in the backend is one
the frontend eventually labels wrongly.

`active` uses the **platform's existing** active-user window
(`analytics.views.ACTIVE_WINDOW_DAYS`), not a second one invented for this
screen — so it means the same thing here as on the admin's Reports page.

`avg_completion` is `null`, not `0`, when there is nothing to average, and is
the same number as the Dashboard's tile.

---

## 3. `GET /institution/learners/` — the table

Paginated (`?page=`, `?page_size=`, max 100), ordered by name.

| Filter | Values |
|---|---|
| `?status=` | `active` \| `at-risk` \| `completed` |
| `?batch=` | batch id |
| `?q=` | matches name or email |

```json
{
  "id": 51, "full_name": "Riya Desai", "email": "riya@nexus.edu",
  "progress": 84.0,
  "last_active": "2026-09-16T10:02:11+05:30",
  "status": "active",
  "enrollments": 1, "active_enrollments": 1, "completed_enrollments": 0,
  "batch": "Batch A · CS 2025",
  "batches": [{ "id": 7, "name": "Batch A · CS 2025" }],
  "joined": "2026-02-01T09:00:00+05:30"
}
```

> ### `last_active` is `null` for someone who has never signed in
>
> Render **"never"**, not "today". It is `User.last_login`.
>
> `LearnerStats.last_active_date` looks like the better source and is **not**:
> nothing in the application maintains it — only the demo seeder writes it.

`batch` is the newest of `batches`, which carries them all. The table prints one
cell; a learner in three cohorts should not have two silently dropped.

---

## 4. `status` — the badge

| `status` | Rule |
|---|---|
| `completed` | no active enrolment left, and at least one completed |
| `at-risk` | has an active enrolment, **and** average progress < `at_risk_progress_pct`, **and** not seen for `at_risk_idle_days` |
| `active` | everyone else |

> ### "At risk" requires **both** signals, deliberately
>
> Low progress alone flags everyone who enrolled this morning. Silence alone
> flags everyone on holiday. A learner who is behind *and* has stopped showing
> up is the one worth a phone call — and that is the only claim this label
> makes.
>
> Nothing in the PRD defines "at risk". The thresholds are chosen defaults:
> `AT_RISK_IDLE_DAYS` and `AT_RISK_PROGRESS_PCT` in
> `analytics/institution_api.py`. They ship in the summary response so the
> screen can say *why* someone is flagged rather than just colouring them red.
>
> A learner who has **finished** reads `completed`, never `at-risk` — with no
> active enrolment the idle clock is irrelevant.

The three statuses **partition the roll**: every learner is in exactly one, the
filter and the badge are built from the same expressions, and a row returned
under one label cannot render as another.

---

## 5. `GET /institution/learners/{id}/` — View

The row above, plus:

```json
{
  "courses": [
    { "enrollment_id": 88, "course": "React 19 Pro", "course_id": 4,
      "batch": "Batch A · CS 2025", "batch_id": 7,
      "status": "active", "progress": 84,
      "enrolled_at": "...", "completed_at": null }
  ],
  "certificates": 2
}
```

Another institution's learner is a **404**, not a 403.

---

## 6. `POST /institution/learners/` — Add learner

```jsonc
{ "email": "new@example.com",
  "full_name": "New Person",   // optional
  "batch": 7 }                 // optional: one of YOUR batches
```

Returns **201** with the full table row, so the client can drop it straight into
the list. Creates the account, puts it on your roll and enrols it — the three
things the button means — in one transaction, so a half-added learner is not a
state the page can reach.

> ### There is no password field
>
> The account is created with **no usable password**. The learner sets their own
> through `/auth/password-reset/` (a real implemented endpoint, not a stub). An
> institution choosing passwords for its learners is an institution that knows
> them.

> ### An existing email is refused, not absorbed
>
> `400` with *"An account with this email already exists. A platform admin can
> add it to your institution."*
>
> Quietly attaching an account somebody already created would hand the
> institution a view of a stranger's learning and a say in their account, off
> the back of typing their email address. Consent for that is not something this
> endpoint can obtain. Attaching an existing account stays a platform-admin
> action: `POST /admin/organizations/{id}/members/add/`.

`batch` is looked up **among your own batches only**, so a stray id reads "no
such batch in your institution" rather than enrolling somebody into another
institution's cohort.

---

## 7. `POST /institution/learners/import_csv/` — Import CSV

Send the file as multipart **`file`**, or its text as **`csv`**.
Header row required. Columns: `email` (required), `full_name`, `batch` (one of
your batch ids). Unknown columns are ignored. Max **500 rows**, 2 MB.

```
email,full_name,batch
ana@example.com,Ana Roy,7
bo@example.com,Bo Singh,7
cy@example.com,Cy Das,
```

**201** on success:

```json
{ "created": 3, "rejected": 0, "applied": true, "rows": [ ... ] }
```

**400** if *any* row fails — with the same shape, `applied: false`, and an
`error` on each bad row:

```json
{ "created": 0, "rejected": 2, "applied": false,
  "rows": [
    { "row": 2, "email": "good@example.com", "full_name": "Good Row" },
    { "row": 3, "email": "taken@example.com", "error": "email: An account with this email already exists..." },
    { "row": 4, "email": "not-an-email", "error": "email: Enter a valid email address." }
  ] }
```

> ### All rows or none
>
> Nothing is created unless every row passes. A partial import is the worst
> outcome available here: the institution cannot tell which half landed, and
> re-running to catch the rest collides with the half that already exists.
> Fix the file and send it again.
>
> `row` is the **1-based line number in the file** (the header is row 1), and
> **every** bad row is reported, not just the first — one round trip is enough
> to fix the file.

Duplicate emails *within* the file are caught too.

---

## 8. What this page does **not** have

- **No remove / offboard.** Detaching a learner is
  `DELETE /admin/organizations/{id}/members/{user_id}/`, a platform-admin
  action — same boundary as attaching one.
- **No edit.** Renaming a learner or changing their email is the account
  owner's or an admin's job.
- **No re-assign between batches.** Enrolment moves are not modelled here.
- **No invite email is sent.** The account is created; delivering the
  password-reset link is still the institution's job (or a mail integration
  that does not exist yet).
