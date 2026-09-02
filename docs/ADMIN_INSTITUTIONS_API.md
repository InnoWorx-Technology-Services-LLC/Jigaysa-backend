# Admin · Institutions — Frontend API

Creates and manages **organisations** — institutions and corporate clients —
and the people inside them.

Base URL: `/api/v1/` · Auth: `Authorization: Bearer <access token>`
**Admin only.** Every endpoint here returns `403` for any other role.

> ### Why this exists
>
> Before this, an `Organization` could only be created in Django admin, and the
> link between a user and their institution was settable nowhere else. Onboarding
> one institution meant three visits to a screen built for developers — which is
> why the platform has had **zero organisations** since launch.

---

## 1. Two things, kept apart

| | |
|---|---|
| **The organisation** | The tenant record. Courses, batches, classrooms and the community feed all scope by it. |
| **Its members** | Ordinary users pointed at it by `User.organization`. |

**Membership is not a role.** A student at a college and the administrator who
bought the seats are both members; only the second has `role: "institution"`.

Adding someone to an institution never changes what they may do — that stays a
separate call to `PATCH /admin/users/{id}/role/`. Bundling the two would make
"add 400 students" silently able to mint 400 institution accounts.

---

## 2. Onboarding an institution — three calls

```
POST  /admin/organizations/                 → { "id": 3, … }
PATCH /admin/users/{head_id}/role/          { "role": "institution" }
POST  /admin/organizations/3/members/add/   { "users": [head_id] }
```

Create the tenant, promote whoever runs it, attach them. Order doesn't matter.
Then add the students with a second `members/add/` call.

---

## 3. `GET /admin/organizations/` — the list

**Paginated** (default 20, `?page_size=` up to 100). Ordered by name.

```json
{
  "count": 12,
  "next": "…?page=2",
  "previous": null,
  "results": [
    {
      "id": 3,
      "name": "St. Xavier College",
      "slug": "st-xavier-college",
      "type": "institution",
      "type_label": "Institution",
      "is_active": true,
      "status": "active",
      "member_count": 412,
      "course_count": 9,
      "created_at": "2026-09-02T10:00:00Z",
      "updated_at": "2026-09-02T10:00:00Z"
    }
  ]
}
```

| Query | Values |
|---|---|
| `search` | matches name **or** slug |
| `type` | `institution` · `corporate` |
| `status` | `active` · `inactive` |

`status` is the word your table prints — don't re-derive it from `is_active`.

`member_count` and `course_count` are annotated server-side in one query. Don't
compute them per row.

---

## 4. `POST /admin/organizations/` — create

```json
{ "name": "St. Xavier College", "type": "institution" }
```

`type` defaults to `institution`; the other value is `corporate`. `is_active`
defaults to true.

Returns `201` with the **same shape the list returns**, counts included, so the
table can render the new row without refetching.

### The slug is generated, not accepted

Derived from the name, and **de-duplicated**: two organisations whose names
slugify identically get `st-xavier-college` and `st-xavier-college-2`. Worth
knowing because the model's own slug generation does *not* de-duplicate and the
column is unique — going around this endpoint can raise an IntegrityError.

Duplicate **names** are rejected outright with a `400` (case-insensitive).

---

## 5. `PATCH /admin/organizations/{id}/` — edit

```json
{ "name": "St. Xavier University", "type": "institution", "is_active": true }
```

**Renaming does not change the slug.** The slug appears in URLs, so re-slugging
would break every link already shared — a rename is a label change, not a new
tenant. If your UI shows the slug, mark it as fixed after creation.

## 6. `DELETE /admin/organizations/{id}/` — deactivate

`204`. **The row is never removed.**

Courses, batches, classrooms and community scoping all hang off it, and members
point at it — deleting would either cascade through somebody's course catalogue
or quietly orphan every member. Deactivating keeps the history and takes it out
of the active list.

This is the same rule the Users page follows with suspension, so the console has
one mental model rather than two. Undo with:

```
POST /admin/organizations/{id}/activate/
```

---

## 7. Members

### `GET /admin/organizations/{id}/members/`

**Paginated.** An institution's roll grows without bound — this is the list on
the page that must never come back whole. Optional `?role=student|institution|…`.

```json
{ "id": 12, "email": "riya@college.edu", "full_name": "Riya Sharma",
  "role": "student", "role_label": "Student / Learner", "is_active": true }
```

### `POST /admin/organizations/{id}/members/add/`

```json
{ "users": [12, 19, 23] }
```

```json
{ "added": 3,
  "reassigned_from_another_organization": ["riya@college.edu"],
  "members": [ … ] }
```

**All-or-nothing.** One unknown id rejects the whole batch with a `400` naming
it — nothing is attached. That matters when you're bulk-adding a roll and one
row of a spreadsheet is stale.

Moving someone who already belongs elsewhere is allowed and is a plain
reassignment; `reassigned_from_another_organization` lists who moved, so you can
say so rather than silently poaching them.

**This never touches anyone's role.** See §1.

### `DELETE /admin/organizations/{id}/members/{user_id}/`

`204`, or `404` if they aren't a member. Clears the link only — the account
keeps its role, its enrolments and its history. Leaving an institution is not
leaving the platform, and a student whose college stops paying still owns the
courses they took.

---

## 8. Not in this release

- **Creating a user from this screen.** You attach *existing* accounts. Invite
  or bulk-create flows don't exist; people register themselves or an admin
  creates them in Django admin.
- **Bulk CSV import** of a member roll.
- **Per-institution settings** — seat limits, contracts, billing. `Organization`
  has a name, a type and an active flag, and nothing else.
- **Institution-scoped admin login.** `role: "institution"` marks the account;
  no endpoint yet restricts what such a user sees to their own organisation.
- **Hard delete.** See §6.
