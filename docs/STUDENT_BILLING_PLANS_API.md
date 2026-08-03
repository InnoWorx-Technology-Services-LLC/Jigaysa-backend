# Student Billing & Plans — Frontend API

Backs the **Billing** page: total spent, active plan, the plan card with its
feature ticks, payment methods and invoices (PRD §3.4 platform access pricing,
§3.13 payments).

Base URL: `/api/v1/` · Auth required on every call.

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
  "subscription": { "id": 9, "plan": 2, "status": "active",
                    "current_period_start": "…", "current_period_end": "…" },
  "entitlements": { "all_paid_courses": true, "live_sessions": true,
                    "certificates": true, "priority_support": true },
  "invoice_count": 3
}
```

Maps straight onto the page:

| UI element | Field |
|---|---|
| **Total spent ₹0** | `total_spent` — sum of **paid** orders only |
| **Active plan: Pro** | `active_plan.name`, or render **Free** when `null` |
| Current plan card ticks | `active_plan.entitlements` |
| Invoices empty state | `invoice_count == 0` |

> `active_plan: null` **is** the free tier — there is no `PricingPlan` row for
> "Free" unless an admin makes one. Don't show a plan name when it's null.

Your screenshot shows **"Active plan: Pro"** next to **"Current plan: Free"** —
those disagree. Both should come from `active_plan`.

---

## 2. Plans and their ticks

### `GET /pricing-plans/`

Every active plan, for the upgrade card. Admins create these in Django admin and
**tick what each plan includes**; the ticks come back as `entitlements`.

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
[STUDENT_PAYMENT_FLOW.md](STUDENT_PAYMENT_FLOW.md).

---

## 4. Subscription state

- `GET /subscriptions/current/` → `{ subscription, entitlements }` — lighter than
  the full summary when you only need to know what's unlocked
- `GET /subscriptions/` → history
- `POST /subscriptions/{id}/cancel/` → **ends at the end of the paid period**,
  not immediately. `cancel_at` is set to `current_period_end`; access continues
  until then

---

## 5. How course access is decided

A student can open a paid course's content when **any** of these hold:

1. they bought it (`Enrollment.source = "purchase"`)
2. it was granted (`free` / `bulk` / `institution`)
3. they hold a plan with `all_paid_courses`

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
  Subscriptions API is not wired up). Students must re-buy manually.
- **Proration / upgrades mid-period** — buying a second plan creates a second
  subscription; the newest active one wins.
- **Plan → specific-course mapping.** It's all-paid-courses or nothing; there's
  no "this plan covers these 10 courses" tier.
- **Trainer revenue share for subscription access** — when a subscriber studies a
  course nobody bought, no payout is attributed to the trainer.
