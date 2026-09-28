# Profile API — Student · Trainer · Admin · Institution

Everything behind the **Profile** and **Settings** screens.

Base URL `/api/v1/` · `Authorization: Bearer <access>` on every call.

---

## 0. There are two different "profiles"

This trips people up, so it is worth being blunt about it.

| | **Public profile** | **Teaching profile** |
|---|---|---|
| Endpoint | `GET`/`PATCH` `/auth/me/` | `GET`/`PATCH` `/trainer-profiles/me/` |
| Model | `User` + `UserProfile` | `TrainerProfile` |
| Who has one | **Everyone**, every role | Trainers (and admins) only |
| Holds | Name, phone, headline, about, location, skills, cover colour, avatar, links | Expertise, years of experience, hourly rate, approval status, revenue share |
| Screen | Profile page + Settings | Trainer onboarding / Earnings |

A trainer has **both**. Their headline and skills live on `/auth/me/` exactly
like a student's — `UserProfile` is role-agnostic by design. Only the
teaching-business fields are separate.

Everything on the Profile page is one `PATCH /auth/me/`. There is deliberately
no second endpoint for the profile block: the page saves the whole form at
once, and splitting it would give the client two calls to keep in step.

---

# PART 1 — THE PUBLIC PROFILE

## 1.1 Read it

### `GET /auth/me/`

Any authenticated user. Returns the account and its nested profile.

```json
{
  "id": 9,
  "email": "riya@example.com",
  "full_name": "Riya Sharma",
  "role": "student",
  "phone": "+919876543210",
  "phone_verified": false,
  "organization": null,
  "is_active": true,
  "date_joined": "2026-06-01T08:00:00Z",
  "profile": {
    "headline": "Aspiring Data Scientist",
    "bio": "Final-year BCA student.",
    "avatar": "",
    "location": "Pune, India",
    "website": "",
    "github_url": "",
    "linkedin_url": "",
    "language": "en",
    "timezone": "UTC",
    "skills": ["Python", "SQL"],
    "cover_color": "#8FD14F"
  }
}
```

The `profile` object is always present. An account that has never been edited
returns it with empty strings and `[]` — never `null`, and never absent.

## 1.2 Save it

### `PATCH /auth/me/`

Send only what changed. Account fields sit at the top level, profile fields
nest under `profile`.

```json
{
  "full_name": "Riya Sharma",
  "phone": "+919876543210",
  "profile": {
    "headline": "Aspiring Data Scientist",
    "location": "Pune, India",
    "bio": "Final-year BCA student.",
    "skills": ["Python", "SQL"],
    "cover_color": "#8FD14F"
  }
}
```

`200 OK` returns the full object from §1.1.

**Partial all the way down.** `profile` is merged, not replaced — sending
`{"profile": {"location": "Mumbai"}}` changes the location and leaves the
headline alone. Omitting `profile` entirely leaves the whole block untouched,
which is what the Settings screen does when it saves only name and phone.

`skills` is the exception: it is a list, so it is **replaced wholesale**. Send
the full list every time, not just the new entry.

## 1.3 Fields

| Field | Type | Notes |
|---|---|---|
| `full_name` | string | Top level, not in `profile` |
| `phone` | string | Top level. `phone_verified` is read-only |
| `headline` | string ≤ 255 | The line under the name |
| `bio` | text | The **About** box |
| `location` | string ≤ 255 | Free text, "City, Country" |
| `avatar` | URL | Empty means render initials |
| `website` `github_url` `linkedin_url` | URL | Must be a valid URL or `""` |
| `language` | string | Defaults `"en"` |
| `timezone` | string | Defaults `"UTC"` |
| `skills` | list of strings | See below |
| `cover_color` | hex string | See below |

**Read-only** — a `PATCH` naming them is ignored, not rejected: `id`, `email`,
`role`, `phone_verified`, `organization`, `is_active`, `date_joined`. Role and
suspension change through the admin endpoints; email is the login identity.

### `skills`

A plain list of free text the member types. Cleaned server-side, so send the
raw list and render what comes back:

- trimmed, and blank entries dropped
- de-duplicated case-insensitively, first spelling wins
  (`["  Python ", "python", "SQL"]` → `["Python", "SQL"]`)
- at most **30** skills, each at most **50** characters

These are deliberately **not** the `courses.Tag` taxonomy. Tags are the shared
vocabulary courses are filed under; a learner typing "Figma" should not mint a
row the whole catalog can be categorised by.

### `cover_color`

A hex colour, `#RGB` or `#RRGGBB`, or `""` for the default. Anything else is a
`400`. The swatches on the page are the suggested set, not a limit — any valid
hex is accepted.

## 1.4 Errors

`400` with the errors nested exactly where the fields were:

```json
{ "profile": { "cover_color": ["Use a hex colour such as #8FD14F."] } }
```

| Message | Cause |
|---|---|
| `Use a hex colour such as #8FD14F.` | `cover_color` is not hex |
| `At most 30 skills.` | More than 30 after cleaning |
| `'…' is longer than 50 characters.` | One skill too long |
| `Each skill must be text.` | A non-string in the list |
| `Skills must be a list.` | `skills` sent as a string or object |
| `Enter a valid URL.` | `website` / `github_url` / `linkedin_url` |

---

# PART 2 — THE TEACHING PROFILE (trainers)

Separate resource, separate screen. Full approval and revenue-share rules are
in [TRAINER_REVENUE_SHARE_PLAN.md](TRAINER_REVENUE_SHARE_PLAN.md).

### `GET`/`PATCH` `/trainer-profiles/me/`

Trainers and admins only — anyone else gets
`403 Only trainers have a teaching profile.` The row is created on first read
if it is missing.

```json
{
  "id": 4, "user_id": 12, "full_name": "Dr. Kapoor", "email": "kapoor@example.com",
  "expertise": "Machine Learning", "years_experience": 8, "hourly_rate": "2500.00",
  "rating_avg": "4.80", "rating_count": 326,
  "is_approved": true, "reviewed_at": "2026-05-02T10:00:00Z", "review_note": "",
  "revenue_share_pct": null, "effective_revenue_share_pct": "70.00",
  "created_at": "2026-04-28T09:00:00Z"
}
```

Writable: `expertise`, `years_experience`, `hourly_rate`. Everything else is
read-only — a trainer cannot approve themselves or set their own cut.

- **`rating_avg` / `rating_count`** are the *trainer's* rating, not any
  course's — see
  [COURSE_FEEDBACK_AND_RATING_API.md](COURSE_FEEDBACK_AND_RATING_API.md) for
  those. ⚠️ **Nothing writes these yet.** Unlike `Course.rating_avg`, which is
  recomputed on every review, no code path recalculates the trainer figure — it
  is `0.00 / 0` on every account outside the demo seed. Treat it as a
  placeholder until trainer ratings are actually collected; the mentor card
  should fall back to "New trainer" rather than print zero stars.
- **`effective_revenue_share_pct`** is the cut actually in force. Display this
  one. `revenue_share_pct: null` means "follow the platform default", which is
  the normal state.

---

## 3. Data model

```
User ──1:1──▶ UserProfile      (everyone — headline, about, skills, cover colour)
  │
  └──1:1──▶ TrainerProfile     (trainers only — expertise, rate, approval)
```

Both rows are created by `post_save` signals on `User`
([accounts/signals.py](../accounts/signals.py)), so every account has a
`UserProfile` from the moment it is registered. Accounts that predate the
signal are backfilled the first time `/auth/me/` is read, so no client ever
sees a missing `profile`.

---

## 4. Endpoint index

| Method | Path | Role |
|---|---|---|
| `GET` `PATCH` | `/auth/me/` | any authenticated — their own |
| `GET` `PATCH` | `/trainer-profiles/me/` | trainer, admin — their own |
| `GET` | `/trainer-profiles/` | admin — every trainer |
| `POST` | `/trainer-profiles/{id}/approve/` · `/unapprove/` | admin |
