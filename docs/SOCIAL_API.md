# Social publishing — Frontend API

One feature, two screens. **Account → Social accounts** connects a trainer's
networks; the course editor's **Promote** wizard turns a course into scheduled
social posts through those connections (PRD §3.3).

Base URL: `/api/v1/` · Auth: `Authorization: Bearer <access token>` on every
call **except the OAuth callback**, which is a browser redirect target.

Roles: `trainer` and `admin`. A student gets `403`.

**LinkedIn, Facebook and Instagram are implemented.** `X` and `YouTube` return
`available: false`; naming one of their accounts in a campaign is a `400`.

> ### ⚠️ Not shippable to real trainers yet
>
> Publishing permissions (`w_member_social`, `pages_manage_posts`,
> `instagram_content_publish`) are review-gated. Until Meta and LinkedIn approve
> the apps, **posting works only for accounts listed as testers on the developer
> app**. The connect flow itself succeeds for anyone; posting is what's gated.

| | |
|---|---|
| **Part 1** — connecting | §1 – §4 |
| **Part 2** — promoting | §5 – §13 |
| **Part 3** — constraints and gaps | §14 – §15 |

---

# Part 1 — Connecting accounts

## 1. The one idea to build on

**A connection is a destination, not a network.** One row per place you can
publish to — so a trainer who admins two Facebook Pages has two Facebook
connections, each with its own name and avatar, and the Instagram account linked
to a Page is a connection of its own.

This is the single assumption everything else rests on, and it is where a
network-shaped UI breaks.

> ### ⚠️ Facebook and Instagram are one authorization
>
> They are two cards in your UI and a single Meta consent underneath. Connecting
> **either** card runs the same flow, and one round-trip commonly returns *both*
> a Facebook Page and its linked Instagram account.
>
> The callback tells you what actually landed: `?connected=facebook,instagram`.
> Re-fetch the account list after every return and render from that — never
> assume the card the user clicked is the only one that changed. Worth saying so
> in the UI copy, or it reads as a bug.

### What this means for the wizard's "Post to:" list

Render **one row per entry in every `accounts` array**, not one row per network.
A network whose `accounts` is empty is the "not connected — Connect" row.

There is no "primary Page" anywhere in the backend: no default, no first-match
fallback. If a trainer admins two Pages, a single "Facebook" row has no way to
say which one, and the server will not guess. Show the Page's `display_name` and
`avatar_url` and let them pick.

Selection is also **multi-select, not a radio group** — `accounts` takes a list,
so LinkedIn *and* Instagram *and* a Page can go out in one campaign.

---

## 2. `GET /social/accounts/` — the whole connect page in one call

Returns **every** provider, including the ones we haven't built. Render straight
from this array; don't keep your own list of networks.

```json
[
  {
    "provider": "linkedin",
    "label": "LinkedIn",
    "available": true,
    "configured": true,
    "accounts": [
      {
        "id": 7,
        "provider": "linkedin",
        "provider_label": "LinkedIn",
        "display_name": "Rohan Deshpande",
        "handle": "dr-kapoor",
        "avatar_url": "https://media.licdn.com/...",
        "status": "connected",
        "needs_reconnect": false,
        "last_error": "",
        "token_expires_at": "2026-10-26T10:31:00Z",
        "created_at": "2026-08-27T10:31:00Z",
        "updated_at": "2026-08-27T10:31:00Z"
      }
    ]
  },
  { "provider": "x", "label": "X", "available": false, "configured": false, "accounts": [] }
]
```

Providers come back in the card order of the mock: `linkedin`, `x`,
`instagram`, `facebook`, `youtube`.

### The states a card can be in

| Condition | Card shows | Button |
|---|---|---|
| `available: false` | "Coming soon" | disabled |
| `available: true`, `configured: false` | "Unavailable on this server" | disabled |
| `configured: true`, `accounts: []` | "Not connected" | **Connect** |
| `accounts` non-empty, `needs_reconnect: false` | "Connected" + `@handle` | **Disconnect** |
| `accounts` non-empty, `needs_reconnect: true` | "Reconnect needed" + `last_error` | **Reconnect** |

`available` and `configured` fail differently and deserve different copy:
`available: false` means we haven't built the adapter; `configured: false` means
this server has no credentials for it. Neither is the trainer's fault, but only
one of them will ever change for them.

**Use `needs_reconnect`.** It is one boolean that already accounts for the token
status — don't re-derive it from `status` on the client.

---

## 3. Connecting — the round-trip

### Step 1 · `POST /social/connect/{provider}/`

```json
{ "return_to": "/trainer/courses/full-stack-web-development-masterclass/edit/promotions" }
```

`return_to` is optional and **must be a path on your site** — an absolute URL is
rejected with `400`. Omit it and the trainer lands on
`/trainer/settings/social`. Use it from the wizard's inline Connect link so the
user comes back to where they were.

Response:

```json
{
  "authorize_url": "https://www.linkedin.com/oauth/v2/authorization?...",
  "provider": "linkedin",
  "expires_in": 600
}
```

Send the browser to `authorize_url` — a full-page navigation or a popup, your
choice. **Nothing is saved at this point**, so an abandoned consent screen
leaves no trace.

The URL is valid for `expires_in` seconds (10 minutes). After that the callback
rejects it and the trainer starts over.

| Status | Meaning |
|---|---|
| `400` | provider has no adapter yet (`x`, `youtube`), or `return_to` wasn't a site path |
| `503` | provider has no credentials on this server |

### Step 2 · The network redirects to the callback

`GET /social/callback/{provider}/` — **you never call this.** The network does.
It always ends in a `302` back to the frontend; it never returns JSON.

### Step 3 · Read the outcome off your own URL

Success:

```
/trainer/settings/social?connected=linkedin&accounts=1
/trainer/settings/social?connected=facebook,instagram&accounts=3
```

Failure:

```
/trainer/settings/social?provider=facebook&error=No%20Facebook%20Page%20found...
```

`error` is written to be shown to the trainer as-is. Show it, then re-fetch
`GET /social/accounts/` either way — `connected` tells you what changed, but the
account list is the truth.

---

## 4. `DELETE /social/accounts/{id}/`

`204` on success. Revokes upstream where the network supports it, then removes
the row.

Succeeds **even if the network is unreachable** — a trainer who has decided to
disconnect shouldn't be trapped by someone else's outage. `404` if the account
isn't yours.

Disconnecting does **not** erase campaigns published through that account. The
posts keep their permalinks and read sensibly with the account gone.

---

# Part 2 — Promoting a course

## 5. The wizard is four calls

| Wizard step | Call |
|---|---|
| 1 Template **and** 2 Edit & preview | `GET /social/templates/?course=<slug>` |
| 2 Rewrite button | `POST /social/rewrite/` |
| 3 Accounts | `GET /social/accounts/` — §2, rendered as destinations per §1 |
| 4 Schedule / Publish now | `POST /social/campaigns/` |

Steps 1 and 2 are deliberately **one** request. The five cards come back with
their copy already rendered from the course, so moving from Template to Edit &
preview costs no round-trip and the textarea is never briefly empty.

Nothing is written until the trainer presses Publish or Schedule. An abandoned
wizard leaves no row — the same promise the connect flow makes about an
abandoned consent screen.

---

## 6. `GET /social/templates/?course=<slug>` — steps 1 and 2

`course` is required. You get `404` for a course that isn't yours (admins may
promote any course).

```json
{
  "course": {
    "slug": "full-stack-web-development-masterclass",
    "title": "Full-Stack Web Development Masterclass",
    "thumbnail": "https://cdn.example.com/cover.jpg",
    "link_url": "https://lms.jigyaasaa.com/courses/full-stack-web-development-masterclass"
  },
  "templates": [
    {
      "key": "launch",
      "badge": "Launch",
      "title": "Just launched",
      "blurb": "Announce a brand-new course.",
      "caption": "🚀 Just launched: Full-Stack Web Development Masterclass!\n\nA practical, project-led course you can start today. Learn by building, finish with proof.\n\nEnrol now for free.",
      "hashtags": "#learning #newcourse #upskill",
      "hint": ""
    }
  ],
  "image_options": [
    { "value": "thumbnail", "label": "Course thumbnail", "available": true,
      "url": "https://cdn.example.com/cover.jpg", "note": "" },
    { "value": "promo_card", "label": "Auto promo card", "available": false,
      "url": "", "note": "Not available on this server yet." },
    { "value": "none", "label": "No image", "available": true, "url": "",
      "note": "Instagram can't be a destination without an image." }
  ]
}
```

Templates come back in the card order of the mock: `launch`, `discount`,
`last_seats`, `testimonial`, `milestone`.

### `hint` is not an error

It says **which fallback the copy used**, so the trainer understands why the
testimonial card reads like a promise. Show it as a quiet note under the
textarea, not as a validation message.

| When | `hint` |
|---|---|
| No subtitle on the course | "Using default copy — add a course subtitle…" |
| No written reviews | "No reviews yet, so this reads as a promise rather than proof." |
| No batch with a capacity | "No batch with a capacity found, so the seat count stays vague." |
| No price set | "No price set on this course, so the offer line stays vague." |

An empty `hint` means the copy was built from real course data.

### ⚠️ "Auto promo card" is not built

`image_options` uses the **same `available` idiom as the provider cards** in §2.
Grey the button out when `available` is `false` — posting with
`image_source: "promo_card"` is a `400` while it stays that way. Generating one
needs a server-side raster pipeline that does not exist yet.

---

## 7. `POST /social/rewrite/` — the Rewrite button

```json
{ "caption": "…", "provider": "instagram", "hashtags": "#learning",
  "link_url": "https://lms.jigyaasaa.com/courses/x" }
```

```json
{
  "provider": "instagram",
  "caption": "Launching today.",
  "preview": "Launching today.\n\nLink in bio\n\n#learning",
  "limit": 2200
}
```

**`caption` goes back in the textarea; `preview` is what would actually post.**
They differ because the link and the hashtags are appended at publish time, per
network — the preview pane needs to show that, the textarea must not swallow it.

> ### This is not AI
>
> It is a rule-based reshape: hashtag placement, the caption ceiling, and what
> happens to the link. There is no model behind it. Label the button honestly —
> "Format for LinkedIn" reads better than anything implying generated copy.

What each network actually gets:

| Network | Reshape | Ceiling |
|---|---|---|
| **LinkedIn** | Long form as written. Link on its own line, clickable. | 3000 |
| **Facebook** | Trimmed to lead + CTA when longer than three paragraphs, so the ask survives the "See more" fold. Link clickable. | 5000 |
| **Instagram** | URLs stripped from the body and replaced with **"Link in bio"** — links in IG captions are not tappable. | 2200 |

A link the trainer already pasted into the caption is not appended a second
time.

---

## 8. `POST /social/campaigns/` — step 4

```json
{
  "course": "full-stack-web-development-masterclass",
  "template": "launch",
  "caption": "🚀 Just launched: …",
  "hashtags": "#learning #newcourse #upskill",
  "image_source": "thumbnail",
  "accounts": [7, 12],
  "publish_at": "2026-09-05T09:00:00Z"
}
```

**`publish_at` is the whole difference between the two radio buttons.** Omit it
or send `null` for *Publish now*; send a timestamp for *Schedule for later*.
There is no separate endpoint and no `mode` field.

`accounts` are **`SocialAccount` ids from `GET /social/accounts/`** — destination
ids, not provider names (§1). A trainer with two Facebook Pages has two ids to
choose between, and the campaign posts to exactly the ones listed.

Returns `201` with the campaign and its per-destination posts.

### Publish now blocks until the networks answer

Several seconds is normal — keep the spinner up. Each destination is a separate
API round-trip (Instagram is two), each capped at 15s.

If your request times out anyway, **nothing is lost**: posts left `pending` are
picked up by the server-side sweep within five minutes. Treat a timeout as
"still going", re-fetch the campaign, and don't offer to submit again.

### What gets rejected before anything is scheduled

Every one of these is a `400` at the Schedule button rather than a silent
failure at 2am:

| Body | Reason |
|---|---|
| An account that isn't yours | it belongs to another login |
| An account with `needs_reconnect: true` | the token is dead; reconnect first |
| An `x` or `youtube` account | no adapter |
| Instagram + no image | Instagram has no text-only post |
| `image_source: "thumbnail"` with no course cover | nothing to post |
| `image_source: "promo_card"` | not available on this server yet |
| `publish_at` in the past | (2 minutes of grace for clock skew) |
| `publish_at` more than a year out | almost always a mistyped year |

---

## 9. The campaign object

```json
{
  "id": 41,
  "course": "full-stack-web-development-masterclass",
  "course_title": "Full-Stack Web Development Masterclass",
  "template": "launch",
  "caption": "…",
  "hashtags": "#learning #newcourse #upskill",
  "image_source": "thumbnail",
  "image_url": "https://cdn.example.com/cover.jpg",
  "link_url": "https://lms.jigyaasaa.com/courses/full-stack-…",
  "scheduled_for": "2026-09-05T09:00:00Z",
  "status": "scheduled",
  "published_at": null,
  "can_edit": true,
  "posts": [
    {
      "id": 88,
      "account": 7,
      "provider": "linkedin",
      "provider_label": "LinkedIn",
      "account_label": "Rohan Deshpande",
      "status": "pending",
      "published_caption": "",
      "provider_post_id": "",
      "permalink": "",
      "error": "",
      "attempts": 0,
      "can_retry": false,
      "published_at": null
    }
  ]
}
```

### Campaign status

| Status | Means |
|---|---|
| `scheduled` | waiting for its time, or for the sweep |
| `publishing` | due, and posts are being sent |
| `published` | every destination succeeded |
| `partial` | **some succeeded, some didn't** |
| `failed` | none succeeded |
| `cancelled` | every destination was cancelled |

**`partial` is a real outcome, not a rounding of `failed`.** A launch that
reached LinkedIn and not Instagram is half-shipped; showing it as failed makes
trainers post the LinkedIn copy a second time. Render it as "1 of 2 published"
with a Retry for the rest.

### Post status

`pending` → `publishing` → `published` / `failed`, plus `cancelled`.

**`pending` after a failure is normal, not stuck.** A network having a bad
minute returns the post to `pending` for the next sweep. `failed` means it has
stopped trying: either three attempts are gone, or the credential is dead.

`account_label` is a snapshot — it is what tells the trainer *which Page* a post
went to, and it survives disconnecting that account.

`published_caption` is what was actually sent to that network — the only record
of the Instagram variant of a LinkedIn caption. Empty until it publishes.

`permalink` is best effort. Instagram only reveals it on a separate call, and a
post that published is not failed over a missing link.

---

## 10. `GET /social/campaigns/` — the Promotions tab

**Paginated** — `?page`, `?page_size` (default 20, max 100), with a
`{count, next, previous, results}` envelope. A trainer accumulates a campaign
per promotion for the life of their account, so this list only grows.

Optional `?course=<slug>` and `?status=<status>`. Newest first. Scoped to your
own campaigns; admins see all.

## 11. `PATCH /social/campaigns/{id}/` — edit or move

```json
{ "caption": "…", "hashtags": "#learning", "publish_at": "2026-09-06T09:00:00Z" }
```

Only while `can_edit` is `true` (status `scheduled`). Anything else is `409` —
once a post has reached a network the copy is public, and editing our row would
only make the record disagree with what people can already read.

**`publish_at: null` here means "publish it now"**, and the response comes back
already published. That is the "Post it now" affordance on a scheduled campaign.

Destinations are not editable. Changing them means reconciling posts a sweep may
already have claimed; cancel and recreate instead.

## 12. `DELETE /social/campaigns/{id}/` — cancel

`204`, and the row is gone. `409` if any destination already published — we
cannot unpublish someone's LinkedIn post, and a row that quietly vanished would
take the permalinks with it. Tell the trainer to delete it on the network.

## 13. `POST /social/campaigns/{id}/retry/` — finish a partial campaign

Re-runs **only** the failed destinations that have attempts left. A destination
that already published is never re-sent. `400` when nothing is retryable —
check `can_retry` on the posts before showing the button.

If a post failed because the token died, the account will show
`needs_reconnect: true` on the connect page. **Reconnect first, then retry** —
retrying before that fails again immediately.

---

# Part 3 — Constraints and gaps

## 14. What the trainer needs to know

Platform constraints, not bugs. Putting them in the UI saves support tickets.

| | |
|---|---|
| **Facebook** | Posting happens as a **Page**, never a personal profile. No Page → the connect fails with a message saying exactly that. |
| **Facebook** | Two Pages means two destinations. Nothing is selected by default — say which Page a post is going to before they press Publish. |
| **Instagram** | Must be a **Business or Creator** account **linked to a Facebook Page**. A personal IG account simply won't appear in the list. |
| **Instagram** | Every post **requires an image**, and the link is stripped for "Link in bio". Worth saying in the UI the moment Instagram is ticked. |
| **LinkedIn** | Posts as the person, not a company page. Token lasts ~60 days, then `needs_reconnect` goes true. |
| **Scheduling** | Times are sent and returned in **UTC**. The picker is minute-granular; the sweep runs every five minutes, so a post can go out up to five minutes after its time. Say "around 9:00", not "at 9:00:00". |
| **Review gating** | See the banner at the top: posting works only for developer-app testers until the app reviews land. |

### Server-side requirement

Scheduling depends on a cron entry running `python manage.py
publish_due_campaigns` every five minutes. Without it, **"Schedule for later"
silently does nothing** — worth confirming with whoever owns the deployment
before the frontend ships the feature.

---

## 15. Not in this release

- **`X` and `YouTube`** — no adapter. Their cards render through the same code
  path with `available: false`; adding an adapter is the only change needed.
- **Auto promo card** — see §6.
- **Analytics.** Nothing reads back likes, impressions or clicks. `permalink` is
  the whole reporting story for now.
- **Recurring campaigns.** One campaign is one moment. A weekly drip is a
  separate feature.
