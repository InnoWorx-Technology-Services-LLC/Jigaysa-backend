# Admin · Users — Frontend API

Backs `/admin/users`: the trainer application queue, four counters, and a
searchable, filterable roster with per-row role changes and suspension.

Base URL: `/api/v1/` · Auth: `Authorization: Bearer <access token>`
**Admin only.** Every endpoint here returns `403` for any other role.

---

## 1. The page is four calls

| Section | Call |
|---|---|
| Trainer applications | `GET /trainer-profiles/?review=pending` |
| Approve / Reject | `POST /trainer-profiles/{id}/approve/` · `/reject/` |
| The four counters | `GET /admin/users/stats/` |
| The roster | `GET /admin/users/` |

---

## 2. `GET /admin/users/` — the roster

**Paginated.** This is the one list on the page that grows without bound — a row
per account ever created — so it is never returned whole.

```json
{
  "count": 1043,
  "next": "https://api.jigyaasaa.com/api/v1/admin/users/?page=2",
  "previous": null,
  "results": [
    {
      "id": 12,
      "email": "riya@jigyasa.local",
      "full_name": "Riya Sharma",
      "role": "student",
      "role_label": "Student / Learner",
      "status": "active",
      "is_active": true,
      "phone": "+91…",
      "phone_verified": false,
      "organization": null,
      "organization_name": "",
      "date_joined": "2026-08-12T09:20:00Z",
      "last_login": "2026-09-01T06:02:00Z"
    }
  ]
}
```

**Read the envelope, not an array.** A client that does `resp.data[0]` works
until the platform passes 20 users and then silently breaks.

| Query | Values |
|---|---|
| `page` | 1-based |
| `page_size` | default **20**, max **100** (asking for more is clamped, not rejected) |
| `search` | matches name **or** email, case-insensitive |
| `role` | `student` · `trainer` · `admin` · `institution` |
| `status` | `active` · `suspended` |

Newest first. `status` is the word your table prints — don't re-derive it from
`is_active`, or two screens will eventually disagree about what to call the same
row.

## 3. `GET /admin/users/stats/` — the counters

```json
{ "total_users": 10, "trainers": 3, "students": 4,
  "institutions": 2, "admins": 1, "suspended": 1 }
```

Not paginated — it is one object. Counted in a single grouped query so the
numbers are true *together*; four separate counts can disagree if a signup lands
between them.

`institutions` and `admins` are returned even though the mock only draws four
tiles. Use them or ignore them; they cost nothing.

---

## 4. `PATCH /admin/users/{id}/role/` — the Change role menu

```json
{ "role": "trainer" }
```

Returns the updated user object. Setting the role someone already has is a
no-op `200`, not an error — a double-click shouldn't produce a red toast.

**Promoting to trainer creates their teaching profile but leaves it
unapproved.** Appointing a trainer and listing them as a bookable mentor are two
different decisions; the second still goes through the application queue. Don't
show "Make Trainer" as if it puts someone on the mentors page.

`student` is assignable too, so a promotion can be undone — "Make Trainer" with
no way back is a one-way door.

## 5. `POST /admin/users/{id}/suspend/` and `/reactivate/`

```json
{ "reason": "Repeated chargebacks." }
```

`reason` is optional and is **shown to the suspended user verbatim**, so an
admin writing one is writing user-facing copy. Blank falls back to something
neutral. `reactivate` takes no body.

> ### Suspension is a lock, not a bouncer
>
> It sets `is_active=false`, which is what the auth backend checks — so it takes
> effect on the **next sign-in**, not mid-session. An admin expecting someone
> kicked out of a live session right now should be told otherwise, or they will
> file a bug.

### The two refusals — both `409`, not `403`

| Attempt | Response |
|---|---|
| Suspending your own account | `409` "You can't suspend your own account." |
| Changing your own role | `409` "You can't change your own role…" |

The caller has every right to the action; the platform's state is what makes it
a bad idea. Show the `detail` string as-is — it already says what to do instead.

The last active admin is protected by the same rules. You will not hit that
case through the UI (an admin acting on *another* admin always leaves
themselves behind), but the guard exists for sessions that outlive their own
suspension.

---

## 6. Trainer applications

The queue at the top of the page. These live on the existing trainer-profile
resource rather than a new one, because an application *is* a profile awaiting a
decision.

### `GET /trainer-profiles/?review=pending`

| `review` | Returns |
|---|---|
| `pending` | never reviewed — **this is the queue** |
| `approved` | approved trainers |
| `rejected` | reviewed and declined |

> ### ⚠️ `?review=pending` is not `?is_approved=false`
>
> A **rejected** application is also unapproved. Filtering on `is_approved`
> alone leaves every rejection sitting in the queue for ever, and the next admin
> re-reviews decisions their colleague already made. Use `review=pending`.

Each row carries `full_name`, `email`, `expertise`, `years_experience`, plus
`reviewed_at` and `review_note` (both empty while pending).

### `POST /trainer-profiles/{id}/approve/`

Approves and lists them on `GET /mentors/`. No body.

### `POST /trainer-profiles/{id}/reject/`

```json
{ "note": "Please reapply with links to recorded sessions." }
```

`note` is optional and is sent to the applicant verbatim — declining someone
without saying why is how a support ticket starts.

Mechanically this sets the same flag as `unapprove`. What differs is the queue:
**reject stamps `reviewed_at`, which is what removes the application from
`?review=pending`.** Use `reject` for the queue's button and `unapprove` for
withdrawing an already-approved trainer.

---

## 7. Not in this release

- **Bulk actions.** One user per request; there is no multi-select endpoint.
- **Deleting a user.** Suspension is the only removal. A hard delete would take
  their enrollments, orders and certificates with it.
- **An audit log.** Who changed whose role, and when, is not recorded anywhere a
  screen can read. The affected user gets a notification; nobody else does.
- **Inviting a user.** Accounts arrive by registration only.
