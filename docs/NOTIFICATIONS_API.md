# Notifications — Frontend API

Backs the **header bell**, the notification dropdown / full page, and the
**Settings → Notifications** matrix, for every role (PRD §3.12).

Base URL: `/api/v1/` · Auth: `Authorization: Bearer <access token>` on **every**
call. There is no anonymous access.

---

## 1. The one idea to build on

**Notifications are per-user, not per-role.** There is no `/admin/notifications/`
or `/trainer/notifications/`. Every role — student, trainer, admin, institution —
calls the *same* endpoints, and the backend scopes everything to the logged-in
user (`recipient = request.user`).

So the frontend needs **one bell component**, shared by all dashboards. What
differs between roles is only:

1. **Which events land in your bell** (an admin gets "Course submitted for
   review"; a trainer gets "New 1:1 booking request") — see §6.
2. **Where `link` points** (`/admin/courses/…` vs `/trainer/courses/…`) — the
   backend already writes the role-correct path into the notification, so the
   frontend just navigates to it blindly.

You never filter by role on the client. You cannot see another user's
notifications even by guessing an id — you get a `404`.

> **No WebSockets, no push delivery yet.** There is no Channels/ASGI socket layer
> and no Celery worker in this project. The bell is **poll-based** (§7). The
> `email` / `sms` / `whatsapp` / `push` channels are stored and respected in the
> preference matrix, but the sender is currently a console stub — nothing
> actually leaves the server on those channels.

---

## 2. The notification object

The single shape returned by every notification endpoint:

```json
{
  "id": 412,
  "category": "live_class",
  "title": "New 1:1 booking request",
  "body": "Anaya R. requested “React hooks deep dive”.",
  "link": "/trainer/sessions",
  "is_read": false,
  "created_at": "2026-08-05T09:14:22Z"
}
```

| Field | Type | Notes |
|---|---|---|
| `id` | int | |
| `category` | enum | `course` · `live_class` · `assessment` · `certificate` · `forum` · `payment` · `system`. Drives the icon + colour. |
| `title` | string | Bold line. May contain emoji (🎉, 🎓) — render as-is. |
| `body` | string | Sub-line. **Can be empty** (`""`), so don't reserve fixed height. |
| `link` | string | Relative in-app path, **or empty** (`""`). If empty, the row is not clickable. |
| `is_read` | bool | The only writable field. |
| `created_at` | ISO 8601 | Sort key; list is already `-created_at`. |

`category`, `title`, `body`, `link`, `created_at` are **read-only** — the client
can never author a notification. There is no `POST /notifications/`.

---

## 3. Bell endpoints

### 3.1 `GET /notifications/` — the list

Paginated (`page_size` 20, max 100), newest first.

**Query params**

| Param | Values | Use |
|---|---|---|
| `is_read` | `true` / `false` | The "Unread only" toggle |
| `category` | any category value | The category filter chips |
| `page` | int | |
| `page_size` | int ≤ 100 | Dropdown uses `page_size=10`; full page uses default |

`GET /api/v1/notifications/?is_read=false&page_size=10`

**Response `200`**

```json
{
  "count": 37,
  "next": "http://localhost:8000/api/v1/notifications/?is_read=false&page=2&page_size=10",
  "previous": null,
  "results": [
    {
      "id": 412,
      "category": "live_class",
      "title": "New 1:1 booking request",
      "body": "Anaya R. requested “React hooks deep dive”.",
      "link": "/trainer/sessions",
      "is_read": false,
      "created_at": "2026-08-05T09:14:22Z"
    },
    {
      "id": 408,
      "category": "course",
      "title": "Course approved: Advanced React 🎉",
      "body": "Your course is live and open for enrollment.",
      "link": "/trainer/courses/advanced-react",
      "is_read": true,
      "created_at": "2026-08-04T16:02:10Z"
    }
  ]
}
```

⚠️ `next` / `previous` are **absolute URLs built from the request host**. Don't
paste them into your API client blindly if you proxy through a different origin —
prefer incrementing `?page=` yourself.

### 3.2 `GET /notifications/unread_count/` — the red badge

The cheapest call in the module. This is what you poll.

**Response `200`**

```json
{ "unread": 5 }
```

Note the URL uses an **underscore**, not a hyphen: `unread_count`, not
`unread-count`. Same for `mark_all_read`.

### 3.3 `POST /notifications/{id}/read/` — mark one read

No request body. Idempotent — calling it on an already-read notification is a
no-op `200`, not an error.

**Response `200`** — the full updated object:

```json
{
  "id": 412,
  "category": "live_class",
  "title": "New 1:1 booking request",
  "body": "Anaya R. requested “React hooks deep dive”.",
  "link": "/trainer/sessions",
  "is_read": true,
  "created_at": "2026-08-05T09:14:22Z"
}
```

### 3.4 `POST /notifications/mark_all_read/` — "Mark all as read"

No request body. Marks **all** the caller's unread notifications, ignoring any
filter currently applied in the UI — if the user is looking at the `payment` tab,
this still clears `course` and `live_class` too. Label the button accordingly.

**Response `200`**

```json
{ "marked_read": 5 }
```

`marked_read` is the number that *changed*, so a second call returns
`{ "marked_read": 0 }`.

### 3.5 `PATCH /notifications/{id}/` — mark **unread** again

`read/` is one-way. To toggle back, PATCH:

**Request**

```json
{ "is_read": false }
```

Any other field in the body is silently ignored (all read-only). **Response
`200`** is the full object.

### 3.6 `DELETE /notifications/{id}/` — dismiss

**Response `204`**, empty body. Permanent — there is no undo endpoint, so confirm
in the UI or use an optimistic toast with a client-side re-`POST` you cannot
actually honour. Prefer *marking read* over deleting.

---

## 4. Settings — the preference matrix

This is the **Settings → Notifications** screen: 7 categories × 5 channels of
checkboxes.

### 4.1 `GET /notification-preferences/`

**⚠️ Not paginated.** Unlike every other list in the platform, this returns a
**bare JSON array** — no `count` / `results` wrapper. Don't reach for
`response.data.results` here or you'll get `undefined`.

It is also **self-seeding**: on first call the backend creates any missing rows,
so you always receive exactly **7 rows, one per category**, in a stable set. You
never have to handle "this category has no row yet".

**Response `200`**

```json
[
  { "id": 1, "category": "course",      "in_app": true, "email": true,  "sms": false, "whatsapp": false, "push": false },
  { "id": 2, "category": "live_class",  "in_app": true, "email": true,  "sms": false, "whatsapp": false, "push": false },
  { "id": 3, "category": "assessment",  "in_app": true, "email": true,  "sms": false, "whatsapp": false, "push": false },
  { "id": 4, "category": "certificate", "in_app": true, "email": true,  "sms": false, "whatsapp": false, "push": false },
  { "id": 5, "category": "forum",       "in_app": true, "email": false, "sms": false, "whatsapp": false, "push": true  },
  { "id": 6, "category": "payment",     "in_app": true, "email": true,  "sms": true,  "whatsapp": false, "push": false },
  { "id": 7, "category": "system",      "in_app": true, "email": true,  "sms": false, "whatsapp": false, "push": false }
]
```

Defaults for a fresh user are `in_app: true, email: true`, everything else
`false`.

### 4.2 `PATCH /notification-preferences/{id}/` — flip one checkbox

Use the row `id` from the list, **not** the category name.

**Request** — send only what changed:

```json
{ "sms": true }
```

**Response `200`**

```json
{ "id": 6, "category": "payment", "in_app": true, "email": true, "sms": true, "whatsapp": false, "push": false }
```

> **Don't use `POST /notification-preferences/`.** It exists, but a
> `(user, category)` pair is unique — posting a category the user already has
> (and after the GET above, they have all 7) hits the DB constraint and surfaces
> as a **`500`**, not a clean `400`. The GET seeds everything; always PATCH.

### 4.3 What `in_app: false` actually means

Turning `in_app` off for a category means the notification row is **never
created** — it is not hidden, it does not exist. Turning the toggle back on later
will **not** backfill anything the user missed. Say so in the UI copy
("You won't see new … alerts in your bell"), otherwise users will report the
setting as buggy.

---

## 5. Device tokens (push registration)

Register the browser/app push token so future push delivery can reach the device.
Wire it up now; delivery lands later.

### `POST /device-tokens/`

**Request**

```json
{ "token": "fcm-token-string…", "platform": "web" }
```

`platform`: `web` (default) · `android` · `ios`.

**Response `201`**

```json
{ "id": 9, "token": "fcm-token-string…", "platform": "web", "created_at": "2026-08-05T09:20:00Z" }
```

Safe to call on every app boot — it's an upsert on `(user, token)`, so
re-registering the same token updates the platform instead of erroring.

### `GET /device-tokens/` — paginated (`count`/`results`), the caller's tokens only.
### `DELETE /device-tokens/{id}/` — `204`. Call on logout so a shared device stops receiving another user's alerts.

---

## 6. Event catalogue — what each role actually receives

Everything below is emitted by the backend today. Use it to build the icon map,
write realistic empty states, and seed your mocks.

### 6.1 Student

| Trigger | `category` | `title` | `link` |
|---|---|---|---|
| Enrolls in a course | `course` | `Enrolled in {course}` | `/courses/{slug}` |
| Certificate issued | `certificate` | `Certificate issued 🎓` | `/certificates` |
| Passes an assessment | `assessment` | `You passed: {assessment}` | `/assessments/{id}` |
| Submission graded (not passed) | `assessment` | `Graded: {assessment}` | `/assessments/{id}` |
| Registers for a live session | `live_class` | `Registered: {session title}` | `/live/{session_id}` |
| Trainer accepts a paid 1:1 | `live_class` | `1:1 request accepted — payment needed` | `/student/sessions` |
| Trainer accepts a free 1:1 | `live_class` | `1:1 session confirmed 🎉` | `/student/sessions` |
| Trainer declines a 1:1 | `live_class` | `1:1 request declined` | `/student/sessions` |
| Trainer cancels a 1:1 | `live_class` | `1:1 booking cancelled` | `/student/sessions` |
| Unpaid 1:1 lapses (nightly job) | `live_class` | `1:1 booking expired` | `/student/sessions` |

The "accepted — payment needed" body carries the amount inline
(`Pay ₹1500 to lock the slot.`) — it's a **call to action**, so give
`live_class` + unread a prominent style rather than burying it in the list.

### 6.2 Trainer

| Trigger | `category` | `title` | `link` |
|---|---|---|---|
| Student requests a 1:1 | `live_class` | `New 1:1 booking request` | `/trainer/sessions` |
| Student cancels a 1:1 | `live_class` | `1:1 booking cancelled` | `/trainer/sessions` |
| Admin approves their course | `course` | `Course approved: {title} 🎉` | `/trainer/courses/{slug}` |
| Admin rejects their course | `course` | `Changes needed: {title}` | `/trainer/courses/{slug}` |
| Admin approves them as mentor | `system` | `You're approved as a mentor 🎉` | `/trainer/settings` |
| Admin withdraws approval | `system` | `Mentor approval withdrawn` | `/trainer/settings` |

Course rejection puts the admin's reason in `body` (falling back to
"An admin sent your course back…"). Render `body` with full wrapping here — it is
the actionable content, not decoration.

### 6.3 Admin

Admin notifications are **fan-out**: the event is written once per active admin,
so every admin sees it independently and one admin marking it read does **not**
clear it for the others. Design the admin queue around that — the bell is a
personal inbox, not a shared work queue.

| Trigger | `category` | `title` | `link` |
|---|---|---|---|
| Trainer submits a course for review | `course` | `Course submitted for review` | `/admin/courses/{slug}` |
| Published course's curriculum edited | `course` | `Published course changed` | `/admin/courses/{slug}` |

Both point at admin-only routes, so the target page must be role-guarded — a
notification `link` is not an authorization grant.

### 6.4 Institution

**No events currently target the `institution` role.** The bell will be
permanently empty for those users. Ship the component (so it lights up when
events are added) but make sure the empty state reads sensibly rather than like a
loading failure.

### 6.5 Categories with no producer yet

`payment` and `forum` are valid values, appear in the Settings matrix, and are
filterable — but **nothing emits them yet**. Payment receipts and forum replies
do not currently notify. Build the icons and filters; expect zero results.

---

## 7. Implementation notes for the frontend

**Polling.** No socket layer exists, so:

- Poll `GET /notifications/unread_count/` every **30–60s** while the tab is
  visible. It's a single indexed `COUNT` — cheap.
- **Pause polling on `document.hidden`** and fire once immediately on
  `visibilitychange` back to visible. Otherwise background tabs multiply load for
  no user benefit.
- Fetch `GET /notifications/?page_size=10` **only when the dropdown opens**, plus
  once when `unread` increases while the dropdown is already open.

**Reading.** Mark read optimistically: flip `is_read` locally, decrement the
badge, then `POST …/read/`. On failure, roll back. Do the same for
`mark_all_read/` using the returned `marked_read` to reconcile.

**Navigation.** Clicking a row does two things: `POST …/read/` and navigate to
`link`. Guard the empty case — `link` can be `""`, in which case the row is
informational only. Treat `link` as a relative app route (it always is), never as
an external URL.

**One component, four dashboards.** Since the API is role-agnostic, the bell,
the dropdown, the full list page and the Settings matrix are all built once and
mounted in the student, trainer, admin and institution shells alike. The only
role-aware code is the router that resolves `/admin/courses/{slug}` etc. — which
is the app's normal route guarding, not notification logic.

**Category → icon map** (suggested, matches what the backend actually emits):

| Category | Icon | Colour cue |
|---|---|---|
| `course` | book | blue |
| `live_class` | video camera | purple |
| `assessment` | clipboard-check | amber |
| `certificate` | award / medal | green |
| `forum` | message-circle | slate |
| `payment` | credit card | teal |
| `system` | bell / info | grey |

---

## 8. Errors

| Status | When | Body |
|---|---|---|
| `401` | Missing/expired token | `{"detail": "Authentication credentials were not provided."}` |
| `404` | `{id}` doesn't exist **or** belongs to another user | `{"detail": "No Notification matches the given query."}` |
| `405` | `POST /notifications/` — creating is not allowed | `{"detail": "Method \"POST\" not allowed."}` |
| `500` | `POST /notification-preferences/` with a duplicate category (§4.2) | — |

Note the `404`-not-`403` on someone else's notification: existence is never
leaked. Don't write client logic that distinguishes "deleted" from "not yours" —
you can't.

---

