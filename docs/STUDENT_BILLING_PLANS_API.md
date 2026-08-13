# Student Billing & Plans — Frontend API

Backs the **Billing** page: total spent, active plan, the plan card with its
feature ticks, payment methods and invoices (PRD §3.4 platform access pricing,
§3.13 payments).

Base URL: `/api/v1/` · Auth required on every call **except**
`GET /pricing-plans/`, which is public for the landing page (§2).

---

## 1. One call for the whole page

### `GET /billing/summary/`

```json
{
  "total_spent": "2269.00",
  "currency": "INR",
  "active_plan": {
    "id": 2, "name": "Pro", "slug": "pro",
    "billing_period": "monthly", "price": "499.00", "currency": "INR",
    "features": ["Priority support"],
    "entitlements": {
      "all_paid_courses": true,
      "live_sessions": true,
      "certificates": true,
      "priority_support": true
    },
    "includes_all_paid_courses": true,
    "includes_live_sessions": true,
    "includes_certificates": true,
    "priority_support": true,
    "is_active": true
  },
  "subscription": {
    "id": 9,
    "plan": { "id": 2, "name": "Pro", "…": "the same object as active_plan" },
    "status": "active",
    "current_period_start": "…", "current_period_end": "…",
    "cancel_at": null, "created_at": "…"
  },
  "entitlements": { "all_paid_courses": true, "live_sessions": true,
                    "certificates": true, "priority_support": true },
  "invoice_count": 3
}
```

`subscription.plan` is the **full plan object**, not an id — it's the same
payload as `active_plan`. `cancel_at` is non-null once the student has
cancelled; access continues until that timestamp (§4).

Maps straight onto the page:

| UI element | Field |
|---|---|
| **Total spent ₹0** | `total_spent` — sum of **paid** orders only |
| **Active plan: Pro** | `active_plan.name`, or render **Free** when `null` |
| Current plan card ticks | `active_plan.entitlements` |
| Invoices empty state | `invoice_count == 0` |

> `active_plan: null` **is** the free tier. Don't show a plan name when it's
> null — render "Free". Both the "Active plan" and "Current plan" labels must
> read from this one field, or they will disagree with each other.

> ⚠️ If an admin creates a **₹0 "Free" `PricingPlan` row**, it will list under
> `/pricing-plans/` but can never be bought: checkout rejects a zero total with
> *"This order has nothing to pay."* Keep Free as the absence of a plan, or hide
> it from the upgrade card.

---

## 2. Plans and their ticks

### `GET /pricing-plans/`

**No auth required** — this is the one payments endpoint that works logged-out,
so the pricing table can render on the landing page next to the public course
catalogue. Everything else under `/billing/` and `/subscriptions/` needs a token.

Paginated (`{count, next, previous, results}`, 20 per page) — read `results`,
not the body. Returns **active plans only**. `?active=all` adds retired plans but
is honoured for admins only; everyone else silently gets the active list.
`GET /pricing-plans/{id}/` is also public and *not* filtered by `is_active`, so a
subscriber on a withdrawn plan can still resolve it.

Admins create these in Django admin (`/admin/payments/pricingplan/`) and **tick
what each plan includes**; the ticks come back as `entitlements`. Name, price,
billing period, currency and the ticks are all editable there with no deploy —
the next API call reflects them. Plans can equally be driven over the API:
`POST` / `PATCH` / `DELETE /pricing-plans/{id}/` are open to admins.

| Key | What it does |
|---|---|
| `all_paid_courses` | **Enforced.** Opens every paid course without buying it |
| `live_sessions` | Advertised only — live classes are open to all students today |
| `certificates` | Advertised only — certificates issue on completion for everyone |
| `priority_support` | A support promise; nothing in the API is gated on it |

> Only `all_paid_courses` changes what the API allows. The other three render on
> the pricing card and are returned by the API, but restricting them would take
> away features students already have — say the word and they can be enforced.

`features` is a free-text list for extra marketing bullets. It grants nothing.

---

## 3. Buying a plan

Same checkout as courses:

1. `POST /orders/` with `{ "items": [{ "item_type": "plan", "object_id": 2 }] }`
2. `POST /orders/{id}/checkout/` → Razorpay options
3. `POST /orders/{id}/verify/` with the handler payload

On settlement the subscription activates for one period. See
[COURSE_PAYMENT_FLOW.md](COURSE_PAYMENT_FLOW.md).

**Renewing early extends, it doesn't restart.** Buying the same plan with 10
days still on the clock gives 40 days, not 30 — the new period stacks onto the
existing `current_period_end`, and `current_period_start` stays put. Re-buying
after a lapse starts fresh from today.

---

## 4. Subscription state

- `GET /subscriptions/current/` → `{ subscription, entitlements }` — lighter than
  the full summary when you only need to know what's unlocked
- `GET /subscriptions/` → history
- `POST /subscriptions/{id}/cancel/` → **ends at the end of the paid period**,
  not immediately. `status` flips to `cancelled` and `cancel_at` is set to
  `current_period_end`, but the entitlements keep paying out until `cancel_at`
  passes — those days are already paid for. `billing/summary/` still reports the
  plan during that window, so show "Pro · cancels on {cancel_at}" rather than
  dropping the student to Free
- Buying the plan again before `cancel_at` **withdraws the cancellation**:
  `cancel_at` clears and the period extends from where it was

> A `cancelled` row with `cancel_at: null` is a hard cancellation (admin or
> support) and grants nothing from that moment.

---

## 5. How course access is decided

A student can open a paid course's content when **any** of these hold:

1. they bought it (`Enrollment.source = "purchase"`)
2. it was granted (`free` / `bulk` / `institution`)
3. they hold a plan with `all_paid_courses`

Staff/admins and the course's own trainer always have access, regardless of
enrollment or plan.

A subscriber enrolling in a paid course gets `Enrollment.source = "subscription"`.
**That enrollment stops opening the course when the plan lapses** — the row
survives (so progress isn't lost) but `has_access` goes `false`. A course bought
outright is never revoked.

`GET /courses/{slug}/curriculum/` → `has_access` is the single source of truth;
gate the player on it rather than re-deriving from the subscription.

---

## 6. Stale copy to fix on the page

- **"Checkout runs on a test gateway for now, so no saved cards are needed —
  payments are confirmed instantly."** No longer true: Razorpay is live, and
  payment confirmation is asynchronous via webhook. The mock instant path
  (`POST /orders/{id}/pay/`) now returns **409** whenever Razorpay keys are
  configured.
- Saved cards genuinely aren't needed — Razorpay Checkout handles instrument
  selection in its own modal — but say that, not "test gateway".

---

## 7. Not built

- **Auto-renew.** A plan activates for one period and then simply expires;
  nothing charges again and `gateway_subscription_id` is never set (Razorpay
  Subscriptions API is not wired up). Students must re-buy manually — which does
  extend cleanly (§3), it just never happens on its own.
- **Expiry sweep.** Nothing rewrites `status` to `expired` when a period runs
  out. Access is correct either way (the date is checked live), but a lapsed row
  still reads `"status": "active"` in `GET /subscriptions/` — treat
  `current_period_end` as the truth, not `status`.
- **Proration / upgrades mid-period** — buying a *different* plan creates a
  second subscription; the one with the newest `current_period_start` wins, so a
  mid-period downgrade takes effect immediately and the old plan's remaining
  days are lost.
- **Platform commission / trainer payouts.** The commission rate is now
  configurable (§ below) but **nothing computes a payout from it**.
  `TrainerPayout.platform_fee` is a column only seed data ever fills.

- **Admin billing endpoints.** The API is student-scoped: orders, invoices,
  subscriptions and payment methods are all filtered to `request.user`, and
  refunds, payments and payouts have no endpoint at all. Django admin is
  currently the only place to issue a refund, inspect another user's order, or
  fix a subscription — see §8.
- **Plan → specific-course mapping.** It's all-paid-courses or nothing; there's
  no "this plan covers these 10 courses" tier.
- **Trainer revenue share for subscription access** — when a subscriber studies a
  course nobody bought, no payout is attributed to the trainer.

---

## 8. What still needs Django admin

The custom admin UI can replace Django admin for **plans, coupons and course
prices** — those are complete admin-gated REST resources today:

| Resource | Admin API |
|---|---|
| `PricingPlan` | ✅ `POST` / `PATCH` / `DELETE /pricing-plans/{id}/` |
| `Coupon` | ✅ full CRUD on `/coupons/` |
| `CoursePrice` | ✅ full CRUD on `/course-prices/` (trainer or admin) |

Everything else is student-scoped, so **an admin literally cannot see or touch
another user's billing over the API**:

| Resource | Gap |
|---|---|
| `Refund` | **No endpoint.** A `requested` refund is money owed to a student; the only places it surfaces are Django admin and `manage.py retry_refunds` |
| `Payment` | No endpoint — nothing to diagnose a stuck payment with |
| `Order` / `Invoice` | Read-only and filtered to `request.user` |
| `Subscription` | Filtered to `request.user`; no way to grant, extend or fix one |
| `TrainerPayout` | No endpoint |

**Keep Django admin until those exist.** Refunds are the one that matters — an
unsent refund with no UI is money quietly owed to a student that nobody is
looking at.

---

## 9. Platform-level knobs

The GST rate, the default currency and the Razorpay credentials are no longer
constants — an admin sets them at `/api/v1/platform-settings/`, and every read
here is live. See **[PLATFORM_SETTINGS_API.md](PLATFORM_SETTINGS_API.md)**.

Two consequences for this page:

- `billing/summary.currency` and `Order.currency` come from
  `default_currency`. It does **not** convert prices.
- The GST rate applies to new quotes only. An order's `tax_gst` and an invoice's
  `gst_amount` are snapshots of what was actually charged.
