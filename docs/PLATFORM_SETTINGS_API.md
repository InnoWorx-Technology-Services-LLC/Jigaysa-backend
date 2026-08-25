# Platform Settings — Admin API

Backs the **Platform settings** screen: general branding, GST and commission,
Razorpay credentials, and the feature flags.

Base URL: `/api/v1/` · Admin role required, except `platform-settings/public/`.

One **singleton** row, so there is no id in the path and no list endpoint.

---

## 1. The two endpoints

| Endpoint | Who | What |
|---|---|---|
| `GET`/`PATCH` `/platform-settings/` | admin only | everything, secrets write-only |
| `GET` `/platform-settings/public/` | **anyone, logged out** | branding + flags only |

### `GET /platform-settings/`

```json
{
  "platform_name": "Jigyasa",
  "support_email": "hello@jigyasa.app",
  "default_currency": "INR",

  "gst_percent": "18.00",
  "platform_commission_percent": "20.00",

  "razorpay_key_id": "rzp_live_5xK2…",
  "razorpay_key_secret_set": true,
  "razorpay_webhook_secret_set": true,
  "razorpay_key_secret_hint": "••••a1b2",
  "razorpay_key_source": "environment",
  "gateway_configured": true,

  "course_approval_required": true,
  "trainer_self_onboarding": true,
  "allow_coupon_codes": true,
  "smart_classroom_module": false,
  "ai_suggestions": false,
  "container_classrooms": false,

  "flags": { "course_approval_required": true, "…": "all six, as a map" },
  "enforced_flags": [
    "allow_coupon_codes", "course_approval_required", "trainer_self_onboarding"
  ],
  "updated_at": "2026-08-13T11:53:00Z"
}
```

### `PATCH /platform-settings/`

Send only what changed. `PATCH {"gst_percent": "5"}` cannot disturb your
gateway keys — an omitted secret is left exactly as it was.

### `GET /platform-settings/public/`

```json
{
  "platform_name": "Jigyasa",
  "support_email": "hello@jigyasa.app",
  "default_currency": "INR",
  "flags": { "allow_coupon_codes": true, "…": "all six" }
}
```

No tax rate, no commission, no credentials. The landing page needs the platform
name before anyone signs in, and the student UI needs the flags to know whether
to draw the coupon box — neither needs to know the platform's margin.

---

## 2. Secrets

`razorpay_key_secret` and `razorpay_webhook_secret` are **write-only**. They are
accepted on `PATCH` and never returned by any `GET`.

That is not paranoia about the field, it is about the blast radius: a settings
screen that reads its own secrets back turns one stolen admin session into a
stolen payment gateway. And it costs nothing — an admin changing a key is
pasting a new one from the Razorpay dashboard, never reading the old one off the
form.

What the UI gets instead:

| Field | Renders as |
|---|---|
| `razorpay_key_secret_set` | ✓/✗ next to the field |
| `razorpay_key_secret_hint` | `••••a1b2` — enough to tell two keys apart |
| `razorpay_key_source` | `settings` · `environment` · `none` |
| `gateway_configured` | whether checkout will work at all right now |

`razorpay_key_id` **is** returned — it ships to the browser to open Checkout, so
it was never secret.

Sending `""` clears a secret. Omitting the field leaves it untouched.

### Where credentials actually come from

```
platform settings row  →  environment (RAZORPAY_*)  →  not configured
```

A blank field in the settings row means *"use the environment"*, not *"no
gateway"*. The migration that creates the row leaves the credential fields empty
on purpose: the env fallback already makes them work, and copying live secrets
into a second store — one that gets dumped, backed up and replicated — buys no
behaviour. `razorpay_key_source` is what tells the admin which store is live, so
an empty box never reads as broken.

Typing a key into the settings screen overrides the environment **immediately,
with no restart**. Clearing it falls back to the environment again. That means
an operator locked out of the admin UI can still fix a broken gateway from the
environment, and an admin can rotate a key without a deploy.

> Stripe fields are **not** implemented. There is no Stripe integration —
> `Payment.Gateway.STRIPE` is a label with no code behind it — and storing a
> credential nothing reads is a liability with no upside.

---

## 3. What each setting actually does

Every value below is read **live**, on each use. There is no cache and no
restart: change it, and the next request uses it.

| Setting | Effect |
|---|---|
| `platform_name` | Returned to the frontend for branding |
| `support_email` | Returned to the frontend |
| `default_currency` | Stamped onto **new** orders; `billing/summary.currency` |
| `gst_percent` | Applied to the discounted subtotal in every new quote |
| `platform_commission_percent` | Sets every trainer's split live, unless pinned per-trainer. Nothing computes payouts yet |

### Two things that are snapshotted, not retroactive

**GST.** An order stores `tax_gst` and `total` at creation. Raising the rate to
28% reprices future carts and leaves placed orders — and the invoices already
issued against them — exactly as they were quoted. That is the correct
behaviour: an invoice is a record of what was charged, not a live calculation.

**Currency.** Stamped onto `Order.currency` at creation for the same reason.

> ⚠️ `default_currency` does **not** convert prices. Set it to `USD` and a ₹499
> plan becomes a $499 plan. Change your course prices and pricing plans too.

### Platform commission is live for every trainer

`platform_commission_percent` is the platform's cut, so trainers keep
`100 − commission`. Changing it **re-rates every trainer immediately** — that is
the point of setting it in one place.

| | |
|---|---|
| Commission `20` | every ordinary trainer keeps `80%` |
| Admin sets commission `30` | **all of them move to `70%` at once**, existing included |
| New trainer signs up | `revenue_share_pct` stays null → follows the platform |
| Negotiating with one trainer | set `revenue_share_pct` on their profile — that **pins** them |
| Undoing a negotiated rate | clear the field back to null → they follow the platform again |

`TrainerProfile.revenue_share_pct` is therefore an **override, not a copy**:

- `null` → follow the platform commission, now and whenever it changes
- a number → this trainer's agreed rate, immune to platform-wide changes

Blank means "follow the platform", **not zero**. A deliberate `0` is a real rate
and is honoured as one.

Read `TrainerProfile.effective_revenue_share_pct` — never either field alone.

> ⚠️ **Consequence for payouts:** because the rate is live, an earnings row must
> snapshot the rate at the moment it is earned. Otherwise raising the commission
> would silently restate every historical earning. See
> [TRAINER_REVENUE_SHARE_PLAN.md](TRAINER_REVENUE_SHARE_PLAN.md) §4.

> **Still does not move money.** No payout is computed anywhere.

---

## 4. Feature flags

`enforced_flags` in the response is the honest list — what the backend acts on.

| Flag | Enforced | What it does |
|---|---|---|
| `course_approval_required` | ✅ | Off: a trainer's submission publishes immediately, skipping the admin review queue |
| `trainer_self_onboarding` | ✅ | Off: `POST /auth/register/` with `role=trainer` returns 400. Students unaffected |
| `allow_coupon_codes` | ✅ | Off: coupon codes are rejected at both order creation and `POST /coupons/validate/` |
| `smart_classroom_module` | ❌ | Stored and returned. The module does not exist |
| `ai_suggestions` | ❌ | Stored and returned. The feature does not exist |
| `container_classrooms` | ❌ | Stored and returned. Phase 2 |

The bottom three are round-tripped so the admin screen works and the frontend
can pre-build against them — but nothing reads them. Don't gate anything real on
them yet. (Same convention as `entitlements` on pricing plans: the API tells you
which keys are enforced rather than implying all of them are.)

**Turning approval off does not skip the content checks.** A course still needs
at least one module and one lesson to publish. The flag decides *who signs off*,
not whether an empty shell can go live.

---

## 5. Validation

| Field | Rule |
|---|---|
| `gst_percent` | 0–100 |
| `platform_commission_percent` | 0–100 |
| `default_currency` | 3 uppercase letters (ISO 4217), e.g. `INR` |
| `support_email` | valid email, or blank |

---

## 6. Notes for the frontend

- The **Save** and **Save payment settings** buttons can both `PATCH` the same
  endpoint with their own subset of fields. Partial updates are safe.
- Render secret inputs as empty with a "configured ••••a1b2 — change" affordance
  driven by `*_set` and `_hint`. Never expect the value back.
- Show `razorpay_key_source: "environment"` as *"Using environment keys"* rather
  than as an empty/unconfigured state.
- Drop the **Stripe secret key** field — there is nothing behind it.
- The commission field should carry a "not yet used for payouts" note, or it
  reads as a live setting that silently does nothing.
- For the flags, drive the disabled/"coming soon" styling off `enforced_flags`
  rather than hardcoding which three are real.

---

## 7. Not built

- **No audit trail.** Nothing records who changed the GST rate or rotated a key,
  beyond `updated_at`. Worth adding before more than one person has admin.
- **No per-organization overrides.** These are platform-wide; a multi-tenant
  install cannot vary them per `Organization` yet.
- **Everything on the "Platform config" nav item other than this screen** —
  Users, Payments, Reports, Classrooms and Devices are still `SOON`.
