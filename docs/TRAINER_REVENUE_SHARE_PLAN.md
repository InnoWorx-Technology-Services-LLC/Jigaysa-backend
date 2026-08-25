# Trainer Revenue Share — Implementation Plan

**Status: not built.** A student pays, `_settle()` issues the invoice and grants
access, and the trainer earns nothing. Money stays in the platform's Razorpay
account with no record of who it was earned by.

This plan covers what to build, in what order, and the decisions that must be
made before writing the ledger.

---

## 1. What exists today

| Piece | State |
|---|---|
| `TrainerPayout` | Model only — `gross`, `platform_fee`, `net`, `status`. Written by `seed_demo` and nothing else |
| `TrainerProfile.revenue_share_pct` | ✅ **Live.** Null = follow the platform; a number pins that trainer |
| `TrainerProfile.effective_revenue_share_pct` | ✅ **Live.** The resolver a payout run must read |
| `PlatformSetting.platform_commission_percent` | ✅ **Live.** Changing it re-rates every unpinned trainer at once |
| `TrainerProfile.payout_account_ref` | Blank `CharField`. No bank details, no UPI, no fund-account id |
| Attribution from money → trainer | ❌ None |
| Per-sale earnings ledger | ❌ None |
| Trainer-facing earnings API | ❌ None |
| Admin payout API | ❌ None |
| Payout rails (money out) | ❌ None. `gateway.py` is Orders/Payments/Refunds only |

So the **rate** is solved. Everything that turns a rate into money is not.

---

## 2. Decisions needed before coding

These three change the ledger's shape. Guessing them bakes wrong money logic
into rows that later have to be migrated.

### 2.1 Who absorbs a coupon discount?

A ₹1,000 course sold with a 20% coupon nets ₹800 before GST.

| Option | Trainer gets (at 80%) | Reading |
|---|---|---|
| **Split the discounted amount** | ₹640 | The coupon is a joint marketing cost |
| Platform absorbs it | ₹800 | Trainer is owed their share of list price |

**Recommendation: split the discounted amount.** It is what the money actually
was, it can never pay out more than came in, and platform-funded discounts
otherwise let a generous coupon campaign cost more than the sale earned.

### 2.2 Do subscription plans earn trainers anything?

Today a `Pro` subscriber opens any paid course and **no order exists for that
course**, so there is nothing to attribute. Options:

| Option | Effort | Notes |
|---|---|---|
| **Nothing** (status quo) | none | Honest, but trainers fund the subscription tier for free |
| Pool + split by watch time | large | Needs reliable per-lesson watch data; `LessonProgress` has `watch_pct` but it is per-lesson, not per-period minutes |
| Pool + split by unique active learners | medium | Coarser, far simpler, defensible |

**Recommendation: ship phases 1–4 with course/session revenue only**, and treat
subscription pooling as a separate project. Say so in the trainer UI, or trainers
will assume subscription views are being paid.

### 2.3 TDS and GST on payouts

Payouts to Indian trainers generally attract **TDS under s.194J** (professional
fees), and a GST-registered trainer will invoice the platform back. The current
`gross / platform_fee / net` shape has nowhere to record either.

**This is an accountant's decision, not an engineering one.** Minimum viable:
add `tds_amount` and `tds_rate` to the payout, and a `gstin` +
`is_gst_registered` to `TrainerProfile`. Ask your CA before deciding the rate
and whether the platform is deducting at all.

---

## 3. The calculation

### Base amount — not `Order.total`

```
Order.total = subtotal − discount + GST
                └────────┬────────┘
                    the base
```

GST belongs to the government, never to the split. The base is
`subtotal − discount`, which is already stored as `Invoice.amount`.

Paying a share of `Order.total` would hand trainers 18% of money the platform
owes the tax authority.

### Per-item, with pro-rata discount

An order can hold several items by different trainers, but the discount is
order-level. Allocate it by each item's share of the subtotal:

```
item_base   = item.amount − (discount × item.amount / subtotal)
trainer_cut = item_base × effective_revenue_share_pct / 100
```

Rounding: compute each cut with `money()` (2dp, `ROUND_HALF_UP`, the existing
helper) and let the platform absorb the sub-paisa remainder — never a trainer.

### Which items attribute to whom

| `item_type` | Trainer | Notes |
|---|---|---|
| `course` | `Course.trainer` | |
| `session` | `IndividualBooking.trainer` | 1:1, priced from `hourly_rate` |
| `plan` | — | See §2.2. No trainer |
| `batch` | `Batch.course.trainer` | Confirm before relying on it |

---

## 4. Phased build

### Phase 1 — Ledger (no money moves)

New `payments.TrainerEarning`:

| Field | Why |
|---|---|
| `trainer` FK | who earned it |
| `order_item` FK (unique) | one earning per item — the idempotency guard |
| `course` / `booking` nullable FK | reporting without re-deriving |
| `base_amount` | post-discount, pre-GST |
| `share_pct` | **snapshot** of the rate applied |
| `amount` | `base_amount × share_pct / 100` |
| `status` | `pending` → `payable` → `paid` → `reversed` |
| `payout` FK nullable | set when swept into a `TrainerPayout` |

**Snapshotting `share_pct` is not optional here.** The rate is live — raising
the platform commission moves every unpinned trainer instantly — so an earnings
row that stored only a foreign key to the trainer would have last year's
earnings silently restated the moment an admin edits one field. Write the number
onto the row.

**Hook:** inside `services._settle()`, in the same transaction that marks the
order paid. Attribution is unambiguous exactly then, and the existing
idempotency guarantees extend to it for free. Unique `order_item` means a
replayed webhook cannot double-credit.

**Deliverable:** every paid order writes earnings rows. Nothing else changes.

### Phase 2 — Reversal on refund

**Hook:** `services._mark_refund_processed()`. Mark the order's earnings
`reversed`.

The rule that matters: a `paid` earning cannot be reversed — that money is gone.
It becomes a negative-balance debt carried into the trainer's next payout, the
same "record the obligation, chase it separately" discipline `request_refund()`
already uses.

### Phase 3 — Trainer-facing API

- `GET /trainer/earnings/` — the ledger, filterable by period
- `GET /trainer/earnings/summary/` — pending / payable / paid / lifetime

**This is the phase with the most value per unit of risk.** It moves no money,
but it makes the platform legible to trainers, and it surfaces attribution bugs
long before anyone is paid on them. Ship it and sit on it for a month.

### Phase 4 — Payout runs, paid manually

- `POST /admin/payouts/generate/` — sweep `payable` earnings for a period into
  one `TrainerPayout` per trainer
- `GET /admin/payouts/` — review the run
- `POST /admin/payouts/{id}/mark-paid/` — record a bank transfer, with reference

Bank transfer by hand, tracked in-app. This is how most platforms run for their
first year or two, and it needs no new gateway integration.

Also needed here: a payout **threshold** (skip trainers under ₹500 and roll
forward) and a hold period so a payout is not generated inside the refund window.

### Phase 5 — Automated payouts (a project, not a task)

RazorpayX Payouts is a **separate product** from the Orders/Payments API already
integrated: separate KYC, a funded balance, its own webhooks.

```
Contact  →  Fund Account (bank / VPA)  →  Payout  →  webhook settles it
```

`payout_account_ref` is the placeholder for the fund-account id. Collecting
trainer bank details also brings its own handling obligations. Do not start this
until phases 1–4 have run for a few real cycles.

---

## 5. Order of work

1. Answer §2.1 and §2.3
2. Phase 1 ledger + tests → **verify against real settled orders before paying anyone**
3. Phase 2 reversal
4. Phase 3 trainer API — run for a month, reconcile by hand
5. Phase 4 payout runs
6. Revisit §2.2 and Phase 5

Phases 1–4 are self-contained and touch no gateway code. Phase 1 alone is
roughly a day's work; the value is that it starts accruing an auditable record
immediately, so the first payout run is reconciling data you have been watching,
not numbers computed for the first time on the day money leaves.

---

## 6. Traps

- **Do not compute earnings from `Order.total`** — that includes GST (§3).
- **Do not recompute historical earnings from the current rate.** Snapshot
  `share_pct` on the row.
- **Do not generate a payout for an order still inside its refund window.**
- **Free and subscription-sourced enrollments earn nothing** — the trigger is a
  paid order, not an enrollment.
- **`_settle()` runs from three paths** (browser verify, webhook, mock pay).
  Hooking the ledger there covers all three; hooking a view covers one.
- **A trainer buying their own course** would credit themselves. Guard it.
