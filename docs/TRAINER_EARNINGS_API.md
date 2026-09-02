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
| Bank account on file | `GET`/`PUT /trainer/earnings/bank-account/` |
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
| `average_per_course` | `lifetime ÷ earning_courses` |

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

```json
{ "bank_name": "HDFC", "account_last4": "8821",
  "account_type": "Savings", "account_holder": "Dr. Kapoor", "is_set": true }
```

`PUT` takes the same four fields. Use `is_set` for the empty state rather than
guessing from four possibly-blank strings.

> ### ⚠️ This records where you *say* payouts should go
>
> It does not connect to a bank, and it **deliberately does not accept a full
> account number** — `account_last4` must be exactly four digits, and a longer
> value is a `400`, not a silent truncation.
>
> Storing a full number means holding a payout instrument: encryption at rest,
> an access trail, a breach story. There is no processor to hand it to, so the
> only thing storing it would achieve is the liability.
>
> **Say this in the form.** A trainer who believes they have connected a bank
> account, and has not, finds out at the worst possible moment. Label the field
> "Last 4 digits" and the card "For your records".

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
- **Marking a payout paid.** No endpoint; do it in Django admin.
- **Statements and tax forms.** The page blurb mentions both; neither exists.
- **Partial refund reversal.** A refund reverses the order's earnings in full.
  A partial refund currently reverses the whole line, which over-corrects — fine
  while partial refunds are rare, worth fixing before they aren't.
- **Per-course earnings breakdown.** Use `GET /trainer/earnings/?course=<id>`
  and total it yourself.
