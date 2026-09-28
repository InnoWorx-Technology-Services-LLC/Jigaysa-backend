# Trainer · Earnings — Frontend API

Backs `/trainer/earnings`: four tiles, the twelve-month chart, the revenue
split, the bank account card, and the payouts list.

Base URL: `/api/v1/` · Auth: `Authorization: Bearer <access token>`
Roles: `trainer` and `admin`. A student gets `403`. Always scoped to the
caller — there is no parameter to widen it.

---

## 1. The page is five calls

| Section | Call |
|---|---|
| Four tiles **and** the revenue split | `GET /trainer/earnings/summary/` |
| Earnings (last 12 months) | `GET /trainer/earnings/trend/` |
| Bank account on file | `GET`/`PUT`/`DELETE /trainer/earnings/bank-account/` |
| Payouts | `GET /trainer/earnings/payouts/` |
| The lines behind the totals | `GET /trainer/earnings/` |

The last one isn't in the mock. A trainer who disagrees with a total needs
somewhere to look, and "trust the number" is not an answer about someone's
income.

---

## 2. How a trainer earns

**At capture.** The moment an order settles, the share is recorded as a ledger
line. There is no waiting period before it appears — a trainer who cannot see
today's sale assumes the platform lost it.

Each line stores:

| | |
|---|---|
| `gross` | the line's amount, **net of any coupon discount and excluding GST** |
| `share_pct` | the trainer's rate **at the moment of sale**, snapshotted |
| `net` | `gross × share_pct / 100` — what the trainer earned |
| `platform_fee` | `gross − net` |

Three consequences worth knowing:

- **GST is not split.** Tax is the government's; dividing it would have the
  platform and the trainer sharing money belonging to neither.
- **A coupon discount is nobody's revenue**, and it is allocated across lines in
  proportion to their amounts — a coupon applies to the order, not to one
  course.
- **Changing a trainer's rate never restates history.** March keeps March's
  rate. That's what `share_pct` on each line is for.

**Plan purchases earn nobody.** A platform subscription isn't a sale of any one
trainer's work.

---

## 3. `GET /trainer/earnings/summary/` — tiles and split

```json
{ "this_month": "48000.00", "lifetime": "892000.00",
  "pending_payout": "48000.00", "average_per_course": "223000.00",
  "earning_courses": 4, "share_pct": "80.00",
  "platform_fee_pct": "20.00", "currency": "INR" }
```

Amounts are **decimal strings** — parse as decimals, never `parseFloat` a total.

| Field | Means |
|---|---|
| `this_month` | net earned since the 1st |
| `lifetime` | net earned, all time |
| `pending_payout` | earned and not yet paid — no payout, or a payout still `pending` |
| `average_per_course` | `lifetime ÷ earning_courses`, or **`null`** when nothing has earned |

**`average_per_course` divides by courses that actually earned**, not by every
course published. Dividing by all of them measures how much someone publishes,
not how much they earn.

`share_pct` + `platform_fee_pct` always sum to 100 — both come from the server,
so the two bars can't disagree with each other or with what the ledger used.

**Refunded money leaves every total here.** Reversed lines are excluded.

---

## 4. `GET /trainer/earnings/trend/` — the chart

```json
{ "months": 12,
  "earnings": [ { "month": "2025-10", "value": "0.00" }, … ],
  "currency": "INR" }
```

`?months=` accepts 1–36, default 12. Out of range is clamped; junk falls back
to the default.

**Dense** — every month in the window, gaps as explicit zeros. A sparse series
charted directly runs a line straight over a month with no sales and hides it.

---

## 5. `GET /trainer/earnings/` — the ledger

**Paginated.** One row per sale, for the life of the account.

```json
{ "id": 88, "order": 41, "course": 7, "course_title": "React 19 Pro",
  "gross": "1000.00", "share_pct": "80.00", "platform_fee": "200.00",
  "net": "800.00", "currency": "INR", "status": "pending",
  "payout": null, "payout_status": "", "earned_at": "2026-08-30T11:04:00Z",
  "reversed_at": null, "note": "" }
```

Filters: `?status=pending|reversed`, `?course=<id>`.

**Reversed lines appear here but not in the totals.** That's deliberate and it's
the only combination that lets a trainer see *why* a number went down. Render
them struck through with the `note`, not hidden.

`status: "pending"` means **earned**, not "awaiting something". Whether it has
been paid is `payout_status`.

---

## 6. `GET /trainer/earnings/payouts/` — the payouts list

**Paginated**, newest first. Filter `?status=pending|paid`.

```json
{ "id": 9, "period_start": "2026-06-01", "period_end": "2026-06-30",
  "gross": "80000.00", "platform_fee": "16000.00", "net": "64000.00",
  "status": "pending", "paid_at": null, "line_count": 12,
  "created_at": "2026-07-01T00:05:00Z" }
```

> ### ⚠️ A payout row means "owed", not "sent"
>
> **There is no payout processor integration.** The scheduled job records what
> the platform owes and for which period; nothing transfers money. A payout
> becomes `paid` only when someone marks it so after settling it by other
> means.
>
> Label it honestly. "Payout scheduled · ₹64,000" is true. "Paid on 30 Jun" is
> not, unless `status` is `paid`.

---

## 7. `GET`/`PUT /trainer/earnings/bank-account/`

Payouts are settled by hand over NEFT/RTGS, so this takes the **full account
number and IFSC** — both are needed to actually send the money.

### `GET` — the card

```json
{ "bank_name": "HDFC", "account_last4": "8821", "ifsc": "HDFC0001234",
  "account_type": "Savings", "account_holder": "Dr. Kapoor", "is_set": true }
```

**The account number is never returned**, not even to the trainer who entered
it. They already know it; sending it back on every page load only widens where
it can leak. `account_last4` is what confirms which account is on file, and it
is derived server-side from the number — never sent by the client.

Use `is_set` for the empty state rather than guessing from several possibly-blank
strings.

### `PUT` — save it

```json
{ "bank_name": "HDFC", "account_number": "50100123458821",
  "ifsc": "HDFC0001234", "account_type": "Savings",
  "account_holder": "Dr. Kapoor" }
```

Returns the card shape above — again with no account number.

| Field | Rules |
|---|---|
| `bank_name` | required, ≤ 120 |
| `account_number` | required, **digits only**, 9–18. Spaces are stripped |
| `ifsc` | required, exactly 11: four letters, a `0`, then six letters/digits |
| `account_type` | optional, e.g. `Savings` / `Current` |
| `account_holder` | optional, ≤ 255 |

IFSC is upper-cased on the way in, so `hdfc0001234` is accepted and stored as
`HDFC0001234`. It is validated against the real format because a wrong IFSC
fails at the bank hours later, against money that has already left.

`400` examples:

```json
{ "account_number": ["An account number is digits only."] }
{ "ifsc": ["An IFSC is 11 characters: four letters, a zero, then six letters or digits — e.g. HDFC0001234."] }
```

### `DELETE` — clear them

Returns the now-empty card (`200`, not `204`) so the page can re-render from the
response. Clearing is its own verb rather than a `PUT` of blanks, so "remove my
details" cannot happen by accident from a half-filled form. It wipes the stored
number too — a trainer who asks for their details to be removed must not have
them kept.

> ### 🔒 This is a payout instrument — treat it as one
>
> With a number and an IFSC, money moves. So:
>
> * `payout_account_number` is **encrypted at rest** (Fernet, `enc:v1:` prefix
>   — see `accounts.crypto`). A database dump is not a fraud kit by itself.
> * It is keyed by **`PAYOUT_ENCRYPTION_KEY`, deliberately separate from
>   `SOCIAL_TOKEN_KEY`.** Rotating the social key just makes trainers reconnect
>   their accounts; rotating this one makes **every bank account on file
>   unreadable** and every trainer has to re-enter it. Do not share the keys,
>   and do not rotate this one casually.
> * The IFSC and last four are **not** encrypted: a branch code is public and
>   the last four are a label, so encrypting them would buy nothing and make
>   them unsearchable.
> * Only admins ever read the number back, one payout at a time, from
>   `GET /admin/payouts/{id}/`. It appears in no trainer-facing response.
>   The Django admin change form for a trainer profile also shows it
>   decrypted — that is staff-only, but it is a second way in, so keep Django
>   admin accounts tight.
>
> **Still say in the form that this does not connect to a bank.** Nothing is
> sent automatically — a human reads these details and makes the transfer.

---

## 8. Server-side requirement

Payouts are generated by a scheduled job:

```
python manage.py generate_trainer_payouts        # monthly
```

Without it, `pending_payout` grows for ever and the Payouts list stays empty.
The page copy says payouts land on the last day of each month — a cron entry on
that day is what makes that true. `--dry-run` reports; `--as-of YYYY-MM-DD` sets
the cut-off.

Only earnings older than `TRAINER_PAYOUT_HOLD_DAYS` (default **7**) are swept,
so a refund inside the usual window reverses a line that hasn't been committed
to a payout yet. Safe to run twice — a line already attached is never picked up
again.

---

## 9. Not in this release

- **Actually sending money.** No payout processor. See §6.
- **Statements and tax forms.** The page blurb mentions both; neither exists.
- **Partial refund reversal.** A refund reverses the order's earnings in full.
  A partial refund currently reverses the whole line, which over-corrects — fine
  while partial refunds are rare, worth fixing before they aren't.
- **Per-course earnings breakdown.** Use `GET /trainer/earnings/?course=<id>`
  and total it yourself.
