# Admin · Payments — Frontend API

Backs `/admin/payments`: four counters, the platform-wide transaction table,
refund management, and the trainer payout queue.

Base URL: `/api/v1/` · Auth: `Authorization: Bearer <access token>`
**Admin only.** Every endpoint here returns `403` for any other role.

> ### ⚠️ Don't build this page on `/orders/`
>
> `GET /orders/` scopes every read to the caller — **including for admins**.
> That is correct there (it is a student's own billing history) but it means an
> admin building the Payments page on it sees their own test orders and reports
> a platform with no revenue.
>
> The `admin/` endpoints below are the platform-wide ones.

---

## 1. The page is three calls

| Section | Call |
|---|---|
| The four tiles | `GET /admin/payments/summary/` |
| Transactions table | `GET /admin/payments/` |
| Payout schedule | `GET /admin/payouts/` |

Refunds add `GET`/`POST /admin/refunds/`.

---

## 2. `GET /admin/payments/summary/` — the four tiles

```json
{ "gross_volume": "1428000.00", "refunds": "32000.00",
  "pending": "18500.00", "net": "1396000.00", "currency": "INR" }
```

Amounts are **decimal strings**, not floats — parse them as decimals, and never
with `parseFloat` for anything you then add up.

### What each one actually means

"Revenue" is ambiguous enough to be worth pinning down:

| Tile | Definition |
|---|---|
| `gross_volume` | captured payments (`status: success`). What actually arrived. |
| `refunds` | refunds in state `processed`. Money genuinely returned — a `requested` refund is an intention, not a movement. |
| `pending` | payments still `created`. Started, not captured. **Most of these end up abandoned rather than paid**, so it is a measure of exposure, not of expected income — don't label it "incoming". |
| `net` | `gross_volume − refunds`. |

Failed payments appear in the table but in **none** of the tiles.

### It honours the same filters as the table

`summary` accepts every query parameter the list does. Pass the same ones to
both — tiles that ignore the filter beside a table that honours it is the
classic dashboard lie.

---

## 3. `GET /admin/payments/` — the transaction table

**Paginated.** This table gains a row per payment *attempt* for ever, including
the failures, so it is the screen guaranteed to outgrow its page.

```json
{
  "count": 812,
  "next": "https://api.jigyaasaa.com/api/v1/admin/payments/?page=2",
  "previous": null,
  "results": [
    {
      "id": 501,
      "order": 233,
      "payer_email": "riya@jigyasa.local",
      "payer_name": "Riya Sharma",
      "gateway": "razorpay",
      "gateway_order_id": "order_Nx…",
      "gateway_payment_id": "pay_Nx…",
      "amount": "1499.00",
      "currency": "INR",
      "method": "upi",
      "status": "success",
      "refunded_amount": "0",
      "paid_at": "2026-08-30T11:04:00Z",
      "created_at": "2026-08-30T11:03:12Z"
    }
  ]
}
```

The payer is inlined. The table's whole job is "who paid what", and fetching a
user per row to answer that turns a 20-row page into 21 requests.

`refunded_amount` counts **settled** refunds only. Showing requested ones as
returned money overstates the refund column on every row still in flight.

| Query | Values |
|---|---|
| `page` / `page_size` | default 20, max 100 |
| `status` | `created` · `success` · `failed` |
| `gateway` | `razorpay` · `stripe` · `paypal` · `upi` |
| `search` | payer email or name, or a gateway id |
| `from` / `to` | `YYYY-MM-DD`, inclusive both ends |

A malformed date is **ignored, not rejected** — a typo in a URL shows the
unfiltered page rather than a `400` where the numbers used to be.

---

## 4. Refunds

### `GET /admin/refunds/`

Paginated. Filters: `status` (`requested` · `processed` · `failed`), `from`,
`to`.

```json
{
  "id": 44, "payment": 501, "order_id": 233,
  "payer_email": "riya@jigyasa.local",
  "amount": "400.00", "reason": "Partial — module not delivered",
  "status": "requested", "gateway_refund_id": "", "is_sent": false,
  "processed_at": null, "created_at": "2026-09-01T10:00:00Z"
}
```

### `POST /admin/refunds/`

```json
{ "payment": 501, "amount": "400.00", "reason": "Module not delivered" }
```

Omit `amount` to refund **everything still refundable** on that payment. Partial
refunds are allowed and can be repeated up to the original amount.

| Rejected | Why |
|---|---|
| A payment that never captured | nothing to send back |
| More than the remaining balance | the message names what's left |
| Zero or negative | — |

> ### `requested` is not a failure
>
> The row is written **before** the gateway is called and survives the call
> failing — a refund we owe is a fact about our books, not about whether
> Razorpay answered. Three states, three meanings:
>
> * **`requested`** — recorded, not yet settled. Either in flight, or the
>   gateway call failed and `manage.py retry_refunds` will pick it up.
> * **`processed`** — money actually returned. Only this counts in the tiles.
> * **`failed`** — the gateway refused; the reason is in the row.
>
> `is_sent` tells you whether the gateway has it (`gateway_refund_id` is set).
> A `requested` refund with `is_sent: false` is the one a human should chase.

---

## 5. `GET /admin/payouts/` — the trainer payout queue

Paginated, read-only. Filters: `status` (`pending` · `paid`), `from`, `to`.

```json
{ "id": 9, "trainer": 4, "trainer_email": "kapoor@jigyasa.local",
  "trainer_name": "Dr. Kapoor", "period_start": "2026-06-01",
  "period_end": "2026-06-30", "gross": "80000.00",
  "platform_fee": "16000.00", "net": "64000.00",
  "status": "pending", "paid_at": null, "created_at": "…" }
```

> ### ⚠️ Nothing generates these rows yet
>
> **`TrainerPayout` is written by no code path in this release.** The
> revenue-share percentage is recorded on trainer profiles, and nothing turns
> settled orders into payout rows. On a real deployment this endpoint returns
> `count: 0` no matter how much revenue the platform has taken.
>
> The endpoint is real and the shape is stable, so build against it — but the
> "Next trainer payout run" line in the mock has no date behind it, and the
> Payouts tile on Reports will read ₹0. Don't present either as a live figure
> until payout generation exists.

**"Configure gateway"** is not here — gateway credentials live in platform
settings (`docs/PLATFORM_SETTINGS_API.md`).

---

## 6. Not in this release

- **Payout generation.** See above.
- **Payout marking.** No endpoint moves a payout to `paid`; the queue is
  read-only.
- **Reconciliation reports.** The page blurb mentions them; nothing computes a
  gateway-versus-ledger comparison.
- **Export.** No CSV endpoint. The paginated JSON is the whole story.
- **Stripe and PayPal.** They are valid `gateway` values on the model and no
  integration exists — only Razorpay settles.
