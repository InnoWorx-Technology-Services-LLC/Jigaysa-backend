# Student Community / Forum — Frontend API

Backs the **Community** pages: question list, question detail, Ask a question,
tag bar, and the Top contributors sidebar (PRD §3.12 Discussion Forum).

Base URL: `/api/v1/` · Auth: `Authorization: Bearer <access token>` on **every**
call — there is no anonymous access anywhere in this module.

---

## 1. Visibility — read this first

Threads carry a `visibility`, set on the Ask form:

| Value | Label in UI | Who can read it |
|---|---|---|
| `community` (default) | **My community (private)** | Users in the **same organization** as the author |
| `public` | **Public** | **Every logged-in user** on the platform |

> **`public` is not search-engine indexable.** It means platform-wide, not
> world-readable. An unauthenticated request gets **`401`** for every endpoint
> here, `public` threads included. Don't render "indexable" in the UI.

**Users with no organization share one implicit community.** Most learners sign
up without an organization, so scoping "my community" strictly to a non-null org
would make the forum look empty for them — every private thread visible only to
its own author. Real organizations remain isolated from each other and from this
default pool.

---

## 2. Question list

### `GET /discussion-threads/`

Only returns threads the caller may see (§1).

| Query param | Values |
|---|---|
| `sort` | `active` (default) · `votes` · `new` · `views` |
| `tag` | tag slug, e.g. `python` |
| `course` | course id |
| `scope` | `course` · `community` |
| `status` | `open` · `resolved` |
| `visibility` | `community` · `public` |
| `q` | free-text over title and body |

```json
{
  "id": 30,
  "title": "What is the difference between loc and iloc in pandas?",
  "body": "I keep mixing these up…",
  "author": { "id": 12, "full_name": "Anaya R.", "role": "student" },
  "course": 3,
  "scope": "community",
  "visibility": "public",
  "status": "resolved",
  "tags": ["python", "pandas"],
  "tag_names": ["Python", "Pandas"],
  "score": 12,
  "my_vote": 0,
  "reply_count": 2,
  "views_count": 340,
  "is_pinned": false,
  "last_activity_at": "2026-07-29T10:00:00Z",
  "created_at": "2026-07-20T08:00:00Z"
}
```

Everything the card needs: `score` for the vote number, `reply_count` for the
speech bubble, `views_count` for the eye, `status: "resolved"` for the green ✓,
and `visibility: "public"` for the green **public** chip.

---

## 3. Question detail

### `GET /discussion-threads/{id}/`

Same object plus nested `replies[]`. **Each call increments `views_count`** —
call it once per page view, not on every re-render.

```json
{
  "...thread fields...": "…",
  "replies": [
    {
      "id": 88, "thread": 30, "parent": null,
      "author": { "id": 7, "full_name": "Priya S.", "role": "student" },
      "body": "Figma is still the default…",
      "is_accepted_answer": true,
      "score": 3,
      "my_vote": 1,
      "created_at": "2026-07-21T09:00:00Z"
    }
  ]
}
```

**`404`** if the thread exists but the caller may not see it — deliberately not
`403`, so a private thread's existence isn't leaked.

---

## 4. Voting

### `POST /discussion-threads/{id}/vote/` · `POST /discussion-replies/{id}/vote/`

```json
{ "value": 1 }     // 1 = up, -1 = down
```

→ `{ "score": 13, "my_vote": 1 }`

Behaviour matches Stack Overflow:

- **Sending the value you already cast withdraws the vote** (`my_vote` → `0`).
  One endpoint powers pressing and un-pressing an arrow.
- Sending the opposite value flips it — the score moves by **2**.
- `my_vote` on any thread/reply tells you which arrow to highlight.

**`400`** `You cannot vote on your own question.` / `…your own answer.`
**`400`** `{"value": ["Send 1 to upvote or -1 to downvote."]}`

---

## 5. Reputation (Stack Overflow model)

Points are awarded automatically. Defaults:

| Event | Who gets it | Points |
|---|---|---|
| Your question is upvoted | author | **+5** |
| Your question is downvoted | author | **−2** |
| Your answer is upvoted | author | **+10** |
| Your answer is downvoted | author | **−2** |
| Your answer is accepted | answerer | **+15** |
| You accept an answer | asker | **+2** |
| You downvote an answer | **voter** | **−1** |
| You ask a question | asker | **+2** |

Reversal is exact: withdrawing or flipping a vote undoes the reputation it gave.
Reputation floors at **0** — it never goes negative.

> **Asking is +2, which Stack Overflow does not do** (SO gives nothing for
> asking). It's here because the Ask page promises "you earn points for asking".
> An admin can set it to `0` to match SO exactly.

**All values are admin-editable** in Django admin → *Engagement → Point rules*,
no deploy needed. Changes apply to the *next* award; existing reputation is not
recalculated. Setting a rule inactive makes it worth 0.

---

## 6. Tag bar

### `GET /forum-tags/`

Unpaginated. Counts only threads the caller can see, so a number never
advertises questions they can't open.

```json
[ { "id": 4, "name": "Python", "slug": "python", "thread_count": 42 } ]
```

Sorted by count, so slicing the first 6 gives you the chip row directly.

---

## 7. Ask a question

### `POST /discussion-threads/`

```json
{
  "title": "How do I structure my first data project for a portfolio?",
  "body": "I want my capstone to actually impress recruiters…",
  "course": 3,
  "scope": "community",
  "visibility": "community",
  "tags": ["careers", "data-viz"]
}
```

| Field | Required | Notes |
|---|---|---|
| `title` | ✅ | |
| `body` | — | the "Details" box |
| `course` | — | omit or `null` for the "General" option |
| `visibility` | — | defaults to `community` |
| `tags` | — | **list of names**, max 5 |

**Tags are created on first use** — send whatever the student typed. Matching is
by slug, so `"Data Viz"`, `"data viz"` and `"data-viz"` all resolve to the same
tag rather than making three. The response echoes normalised slugs in `tags` and
display labels in `tag_names`.

Posting awards **+2** reputation.

---

## 8. Answering

`POST /discussion-replies/` — `{ "thread": 30, "body": "…", "parent": null }`
(`parent` for a threaded reply.) Bumps `reply_count` and `last_activity_at`.

`POST /discussion-replies/{id}/accept/` — thread author, course trainer or admin
only. Marks the ✓, sets the thread `resolved`, and pays both sides (§5).
**`403`** for anyone else.

---

## 9. Top contributors

### `GET /community-profile/leaderboard/`

`?limit=` (default 5, max 100) · `?scope=community` to rank only the caller's
own organization.

```json
{
  "results": [
    { "rank": 1, "user": { "id": 5, "full_name": "Dr. Kapoor", "role": "trainer" },
      "points": 8420, "level": 17, "badges_count": 4 }
  ],
  "my_rank": 4,
  "my_points": 1240
}
```

Sidebar uses `limit=5`; the "Full leaderboard" page uses a larger limit.
`my_rank` / `my_points` let you show "you're #4" without paging to find them.

Own card: `GET /community-profile/me/` → `{ points, level, badges_count, badges[] }`.

---

## 10. Not built — don't build UI for it

- **The Rewards panel.** "Turn your points into real gifts — swag, free courses,
  mentorship" and **Browse rewards** have **no backend at all** — no catalog, no
  redemption, no stock or fulfilment tracking. Points accumulate but cannot be
  spent on anything. Either hide the panel or treat it as a coming-soon card.
- **Comments on answers** (SO's third level) — replies nest via `parent`, but
  there's no separate comment type or its own voting.
- **Editing history, close/reopen, duplicate marking, moderation queue.**
- **Reputation history** — a user sees their total, not a per-event breakdown of
  where it came from.
- **Bounties, privileges by reputation level** (SO unlocks abilities at
  thresholds; `level` is computed but gates nothing).
