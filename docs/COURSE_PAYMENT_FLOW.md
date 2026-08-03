# Course Payment Flow — Student

Buying a **course** end to end, with exact payloads and responses (PRD §3.3 paid
market training, §3.13 payments).

For plan/subscription billing see
[STUDENT_BILLING_PLANS_API.md](STUDENT_BILLING_PLANS_API.md); for 1:1 session
payment see [STUDENT_1TO1_BOOKING_API.md](STUDENT_1TO1_BOOKING_API.md).

Base URL `/api/v1/` · `Authorization: Bearer <access>` on every call ·
All amounts are **rupees** as strings; Razorpay works in **paise**.

---

## 0. Which path applies

| Course | What to do |
|---|---|
| `is_free: true` | `POST /courses/{slug}/enroll/` — no payment at all |
| `is_free: false`, student has a plan with `all_paid_courses` | `POST /courses/{slug}/enroll/` — no payment |
| `is_free: false`, no plan | **This document** |

Check `GET /billing/summary/` → `entitlements.all_paid_courses` before showing a
Buy button; a subscriber should see "Start learning", not a price.

---

## 1. Read the price

### `GET /course-prices/?course=<id>`

```json
{
  "count": 1,
  "results": [
    {
      "id": 7, "course": 3, "pricing_type": "one_time",
      "amount": "999.00", "currency": "INR",
      "discount_percent": "10.00", "discount_amount": "0.00",
      "valid_from": null, "valid_to": "2026-09-30T00:00:00Z"
    }
  ]
}
```

The server re-derives the payable amount at checkout from this row —
`amount − discount_amount`, then `− discount_percent%`. **Never send a price
from the client**; it is ignored.

---

## 2. (Optional) Preview a coupon

### `POST /coupons/validate/`

```json
{
  "code": "LAUNCH20",
  "items": [{ "item_type": "course", "object_id": 3 }]
}
```

**`200`**
```json
{ "code": "LAUNCH20", "subtotal": "899.10", "discount": "179.82",
  "tax_gst": "129.47", "total": "848.75" }
```

**`400`** — show the message verbatim: `Invalid or inactive coupon.` ·
`This coupon has expired.` · `This coupon has been fully redeemed.` ·
`Order must be at least 500 to use this coupon.` ·
`This coupon only applies to course purchases.`

---

## 3. Create the order

### `POST /orders/`

```json
{
  "items": [{ "item_type": "course", "object_id": 3 }],
  "coupon_code": "LAUNCH20"
}
```

`item_type` is `course` here. (`plan` = subscription, `session` = 1:1 booking.)
`coupon_code` is optional.

**`201`**
```json
{
  "id": 301,
  "status": "pending",
  "subtotal": "899.10",
  "discount": "179.82",
  "tax_gst": "129.47",
  "total": "848.75",
  "currency": "INR",
  "coupon_code": "LAUNCH20",
  "items": [
    { "id": 410, "item_type": "course", "object_id": 3,
      "title": "React Pro", "amount": "899.10", "qty": 1 }
  ],
  "payments": [],
  "created_at": "2026-08-03T06:20:00Z"
}
```

GST is **18%**, applied to `subtotal − discount`. `total` is what the student pays.

**`400`** cases:

| Message | Meaning |
|---|---|
| `Course 3 not found.` | bad `object_id` |
| `'React Pro' is free — just enroll.` | call `enroll/` instead |
| `'React Pro' is not purchasable yet.` | no `CoursePrice` row — trainer hasn't set pricing |
| `An order needs at least one item.` | empty `items` |

---

## 4. Open Razorpay Checkout

### `POST /orders/301/checkout/`

No body.

**`200`**
```json
{
  "key": "rzp_test_XXXXXXXX",
  "razorpay_order_id": "order_QxYz123",
  "amount": 84875,
  "amount_display": "848.75",
  "currency": "INR",
  "name": "Jigyaasaa",
  "description": "React Pro",
  "image": "",
  "order_id": 301,
  "prefill": { "name": "Asha Rao", "email": "asha@…", "contact": "+91…" },
  "notes": { "order_id": "301" },
  "callback_url": "https://lms.jigyaasaa.com/student/orders/301",
  "is_test_mode": true
}
```

⚠️ **`amount` is in paise** (84875 = ₹848.75) because that is what Checkout
expects. Use `amount_display` for anything you render.

Pass the object straight into `new Razorpay(options)`. **Safe to call again** —
an unpaid order reuses its existing gateway order instead of creating a
duplicate.

| Status | Meaning |
|---|---|
| **`503`** | `Payment gateway is not configured on this server.` |
| **`502`** | `Could not reach the payment gateway: …` — let them retry |

---

## 5. Verify the payment

Razorpay's `handler` gives you three values. Post them back:

### `POST /orders/301/verify/`

```json
{
  "razorpay_order_id": "order_QxYz123",
  "razorpay_payment_id": "pay_QxYz456",
  "razorpay_signature": "9ef4dffbfd84f1318f6739a3ce19f9d85851857ae648f114332d8401e0949a3d"
}
```

**`200`** → the same Order object, now `"status": "paid"`, with the payment
recorded. **The enrollment already exists at this point** — go straight to the
player.

| Status | Meaning | What to show |
|---|---|---|
| **`400`** | `Payment signature verification failed.` | "We couldn't verify that payment" — do **not** retry automatically |
| **`503`** | gateway not configured | contact support |
| **`502`** | gateway unreachable | retry |

The signature is verified server-side, the payment is **re-fetched from
Razorpay**, and the amount is checked against the order total before anything is
granted — a tampered client payload cannot buy a course.

---

## 6. Don't rely on step 5

The `payment.captured` **webhook settles the order independently**. If the
student closes the tab mid-payment, they still get the course.

Both paths are idempotent and converge on the same code, so whichever lands
first wins and the other is a no-op. Practical consequence: **if `verify/`
fails or never runs, poll `GET /orders/301/` — it may already be `paid`.**

---

## 7. What a paid order grants

On settlement the server, in one transaction:

1. marks the order `paid`
2. issues a **GST invoice** (`JIG-2026-0042`)
3. creates the **enrollment** with `source: "purchase"`

```json
GET /enrollments/
{
  "id": 88, "student": 12,
  "course": { "id": 3, "slug": "react-pro", "title": "React Pro", "...": "…" },
  "batch": null, "status": "active", "source": "purchase", "order": 301,
  "progress_pct": 0,
  "enrolled_at": "2026-08-03T06:22:00Z", "completed_at": null
}
```

`source: "purchase"` matters: purchased access is **owned outright** and is
never revoked, unlike `source: "subscription"` which lapses with the plan.

---

## 8. Invoices

`GET /invoices/` — the Billing page list.

```json
{ "id": 55, "number": "JIG-2026-0042", "order": 301,
  "description": "React Pro", "amount": "719.28", "gst_amount": "129.47",
  "status": "paid", "issued_date": "2026-08-03", "pdf_url": "" }
```

`amount` here is **pre-GST** (`subtotal − discount`); the student paid
`amount + gst_amount`. `pdf_url` is empty — no PDF is generated yet.

---

## 9. Dev / test only

### `POST /orders/{id}/pay/`

Marks the order paid with no money involved. Returns **`409`** whenever Razorpay
keys are configured, so it cannot fire in production.

---

## 10. Not built

- **Refunds for course purchases.** A `Refund` model exists and the 1:1 booking
  flow uses it, but there is **no student-facing refund request** for a course
  and nothing revokes an enrollment on refund.
- **EMI / installments, corporate & group pricing** — `pricing_type` accepts
  these values but checkout rejects anything that isn't `one_time`.
- **Invoice PDFs** — `pdf_url` is always empty.
- **Saved cards** — `GET /payment-methods/` exists, but Razorpay Checkout handles
  instrument selection in its own modal, so you don't need it.
- **Trainer revenue share** — no payout is attributed to the trainer on a sale.
