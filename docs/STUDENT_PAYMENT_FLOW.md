# Course Purchase — Frontend Integration Guide

Everything you need to build the buy-a-course flow. Three API calls and a
Razorpay modal.

**Base URL:** `/api/v1` · **Auth:** every call needs `Authorization: Bearer <access_token>`

---

## 1. Setup

Load Razorpay Checkout once (in `index.html`, or lazily before first use):

```html
<script src="https://checkout.razorpay.com/v1/checkout.js"></script>
```

Nothing else to install. No Razorpay keys in your code or `.env` — the key comes
back from the API at checkout time.

---

## 2. The three calls

| # | Call | When |
|---|---|---|
| 1 | `POST /orders/` | Student clicks **Buy** |
| 2 | `POST /orders/{id}/checkout/` | Immediately after, to open the modal |
| 3 | `POST /orders/{id}/verify/` | Inside Razorpay's `handler` callback |

Plus `GET /orders/{id}/` for polling, and `GET /course-prices/?course={id}` to
show the price on the course page.

---

## 3. Show the price

The course object has **no price field** — only `is_free`. Fetch the price
separately:

```
GET /api/v1/course-prices/?course=4
```

```json
{ "count": 1, "results": [
  { "id": 1, "course": 4, "pricing_type": "one_time",
    "amount": "4999.00", "currency": "INR",
    "discount_percent": "0.00", "discount_amount": "0.00" } ] }
```

```js
// Pick the one_time price; if several, the cheapest is what you'll be charged.
const price = res.results
  .filter(p => p.pricing_type === 'one_time')
  .sort((a, b) => Number(a.amount) - Number(b.amount))[0];

// Display price after the course's own discounts:
const net = Number(price.amount)
  - Number(price.discount_amount)
  - (Number(price.amount) - Number(price.discount_amount))
    * Number(price.discount_percent) / 100;
```

> This is the pre-tax price. **18% GST is added at checkout**, so the amount the
> student pays is higher than what you show here. Either label it
> "+ GST at checkout", or call `POST /coupons/validate/` (below) to get the exact
> total before they commit.

**Branch on `is_free`:**

| `course.is_free` | Do this |
|---|---|
| `true` | `POST /courses/{slug}/enroll/` — instant, no payment, no modal |
| `false` | the flow below |

---

## 4. Optional — coupon preview

Lets you show the exact final total (including GST) before the student pays.

```
POST /api/v1/coupons/validate/
{ "code": "LAUNCH20", "items": [ { "item_type": "course", "object_id": 4 } ] }
```

```json
{ "code": "LAUNCH20", "subtotal": "4999.00", "discount": "999.80",
  "tax_gst": "719.86", "total": "4719.06" }
```

Nothing is charged or reserved. Invalid codes return **400** with a message you
can show directly (`This coupon has expired.`, `Invalid or inactive coupon.`,
`Order must be at least 500 to use this coupon.`).

---

## 5. Create the order

```
POST /api/v1/orders/
{ "items": [ { "item_type": "course", "object_id": 4 } ],
  "coupon_code": "LAUNCH20" }
```

Omit `coupon_code` if there isn't one. Don't send amounts — they're ignored and
computed server-side.

**`201`**

```json
{
  "id": 9, "status": "pending",
  "subtotal": "4999.00", "discount": "0.00",
  "tax_gst": "899.82", "total": "5898.82",
  "currency": "INR", "coupon_code": null,
  "items": [ { "title": "React 19 Pro", "amount": "4999.00", "qty": 1 } ],
  "payments": [], "created_at": "…"
}
```

Use `total` for your order-summary UI. Keep `id` — you need it for the next two
calls.

**Buying a subscription plan instead of a course:** same call, with
`{ "item_type": "plan", "object_id": <planId> }`.

---

## 6. Open the Razorpay modal

```
POST /api/v1/orders/9/checkout/
```

No body.

```json
{
  "key": "rzp_test_TJ0Xg4q0cMu4vL",
  "razorpay_order_id": "order_TJ117PbsF3sTdP",
  "amount": 589882,
  "amount_display": "5898.82",
  "currency": "INR",
  "name": "Jigyaasaa",
  "description": "React 19 Pro",
  "image": "",
  "order_id": 9,
  "prefill": { "name": "Riya Sharma", "email": "riya@jigyasa.local", "contact": "" },
  "notes": { "order_id": "9" },
  "callback_url": "https://lms.jigyaasaa.com/student/orders/9",
  "is_test_mode": true
}
```

| Field | Use it for |
|---|---|
| `key`, `razorpay_order_id`, `amount`, `currency` | pass straight to Razorpay |
| `amount` | **paise** (589882 = ₹5898.82) — Razorpay's unit, never divide it |
| `amount_display` | rupees, for your own UI only |
| `prefill` | pre-fills the student's name/email/phone in the modal |
| `is_test_mode` | `true` → show a "Test mode" badge so nobody thinks it's real |

**Safe to call again.** If the student closes the modal and comes back, calling
`checkout/` returns the same `razorpay_order_id` — it won't create a duplicate
charge.

---

## 7. Wire it up

```js
async function buyCourse(orderId) {
  const cfg = await api.post(`/orders/${orderId}/checkout/`).then(r => r.data);

  const rzp = new window.Razorpay({
    key:         cfg.key,
    order_id:    cfg.razorpay_order_id,
    amount:      cfg.amount,
    currency:    cfg.currency,
    name:        cfg.name,
    description: cfg.description,
    image:       cfg.image || undefined,
    prefill:     cfg.prefill,
    notes:       cfg.notes,
    theme:       { color: '#4f46e5' },

    handler: async (res) => {
      // res = { razorpay_order_id, razorpay_payment_id, razorpay_signature }
      setStatus('confirming');
      try {
        const order = await api.post(`/orders/${orderId}/verify/`, res)
                               .then(r => r.data);
        if (order.status === 'paid') return onSuccess(order);
        await pollOrder(orderId);          // unexpected — fall back to polling
      } catch (err) {
        // ⚠️ Do NOT show "payment failed" here. See §8.
        await pollOrder(orderId);
      }
    },

    modal: {
      ondismiss: () => setStatus('idle'),  // closed without paying
    },
  });

  rzp.on('payment.failed', (res) => {
    setStatus('idle');
    showError(res.error.description);      // declined card, UPI timeout, etc.
  });

  rzp.open();
}
```

### Verify

```
POST /api/v1/orders/9/verify/
{ "razorpay_order_id":   "order_TJ117PbsF3sTdP",
  "razorpay_payment_id": "pay_TJ0nQ2xY8kLmNo",
  "razorpay_signature":  "9f8c…" }
```

Pass Razorpay's `handler` argument through **unchanged** — don't rename, reorder
or re-sign anything. **`200`** returns the order with `status: "paid"`; the
student is now enrolled.

---

## 8. The one rule that matters

**A failed `verify/` call does not mean the payment failed.**

The server is also notified of the payment directly by Razorpay, independently of
the browser. So if `verify/` times out, 500s, or the student's connection drops —
the payment can still complete a second or two later.

If you show "Payment failed" on a `verify/` error, students will retry and pay
twice.

Instead: show **"Confirming your payment…"** and poll.

```js
async function pollOrder(orderId, tries = 10, delayMs = 2000) {
  for (let i = 0; i < tries; i++) {
    const o = await api.get(`/orders/${orderId}/`).then(r => r.data);
    if (o.status === 'paid') return onSuccess(o);
    await new Promise(r => setTimeout(r, delayMs));
  }
  // ~20s and still pending:
  showMessage("We're still confirming your payment. You'll get an email shortly.");
}
```

The only place it's safe to show a hard failure is Razorpay's own
`payment.failed` event — that one is definitive.

---

## 9. Complete React example

```jsx
import { useState } from 'react';

const STATES = {
  idle:       { label: 'Buy now',              disabled: false },
  creating:   { label: 'Preparing…',           disabled: true  },
  paying:     { label: 'Waiting for payment…', disabled: true  },
  confirming: { label: 'Confirming payment…',  disabled: true  },
  paid:       { label: 'Enrolled ✓',           disabled: true  },
};

export function BuyCourseButton({ course, onEnrolled }) {
  const [state, setState] = useState('idle');
  const [error, setError] = useState(null);

  async function handleBuy() {
    setError(null);
    setState('creating');
    try {
      // 1. create the order
      const order = await api.post('/orders/', {
        items: [{ item_type: 'course', object_id: course.id }],
      }).then(r => r.data);

      // 2. get checkout params
      const cfg = await api.post(`/orders/${order.id}/checkout/`)
                           .then(r => r.data);

      // 3. open Razorpay
      setState('paying');
      const rzp = new window.Razorpay({
        key: cfg.key,
        order_id: cfg.razorpay_order_id,
        amount: cfg.amount,
        currency: cfg.currency,
        name: cfg.name,
        description: cfg.description,
        image: cfg.image || undefined,
        prefill: cfg.prefill,
        notes: cfg.notes,
        theme: { color: '#4f46e5' },

        handler: async (res) => {
          setState('confirming');
          try {
            const paid = await api.post(`/orders/${order.id}/verify/`, res)
                                  .then(r => r.data);
            if (paid.status === 'paid') {
              setState('paid');
              return onEnrolled(paid);
            }
          } catch { /* fall through to polling */ }
          await poll(order.id);
        },

        modal: { ondismiss: () => setState('idle') },
      });

      rzp.on('payment.failed', (res) => {
        setState('idle');
        setError(res.error?.description ?? 'Payment failed. Please try again.');
      });

      rzp.open();
    } catch (err) {
      setState('idle');
      setError(err.response?.data?.detail ?? 'Something went wrong.');
    }
  }

  async function poll(orderId, tries = 10) {
    for (let i = 0; i < tries; i++) {
      const o = await api.get(`/orders/${orderId}/`).then(r => r.data);
      if (o.status === 'paid') { setState('paid'); return onEnrolled(o); }
      await new Promise(r => setTimeout(r, 2000));
    }
    setError("We're still confirming your payment — you'll get an email shortly.");
    setState('idle');
  }

  const ui = STATES[state];
  return (
    <div>
      <button onClick={handleBuy} disabled={ui.disabled}>{ui.label}</button>
      {error && <p role="alert">{error}</p>}
    </div>
  );
}
```

---

## 10. UI states

| State | Trigger | Show |
|---|---|---|
| `idle` | default, or modal dismissed, or card declined | **Buy now**, enabled |
| `creating` | `POST /orders/` + `checkout/` in flight | spinner, disabled |
| `paying` | Razorpay modal open | disabled behind the modal |
| `confirming` | `verify/` in flight, or polling | **"Confirming your payment…"** — never a failure message |
| `paid` | order `status: "paid"` | success → route to the course player |

---

## 11. Errors → what to show

### `POST /orders/`

| Detail from API | Show |
|---|---|
| `'{title}' is free — just enroll.` | (bug in your branching — use `enroll/`) |
| `'{title}' is not purchasable yet.` | "This course isn't available for purchase yet." |
| `Course {id} not found.` | "Course unavailable." |
| coupon messages | show verbatim — they're written for students |

### `POST /orders/{id}/checkout/`

| Code | Show |
|---|---|
| 400 `This order is already paid.` | route to the course — they own it |
| 404 | "Order not found." |
| 502 | "Payment service is busy. Please try again." — retryable |
| 503 | "Payments are temporarily unavailable." — don't retry automatically |

### `POST /orders/{id}/verify/`

**Show nothing.** Every failure here goes to polling (§8). Log them for
debugging, but the student sees "Confirming your payment…" either way.

---

## 12. Test mode

`is_test_mode: true` means no real money moves. Use these:

| Method | Value |
|---|---|
| Card | `4111 1111 1111 1111` · any future expiry · any CVV · OTP `1234` |
| UPI — success | `success@razorpay` |
| UPI — failure | `failure@razorpay` |
| Netbanking | any bank → click **Success** on the simulated page |

Test the failure path too — `failure@razorpay` should leave the button back at
`idle` with an error, and the order still purchasable.

---

## 13. After a successful purchase

```
GET /api/v1/enrollments/   → the course appears, source: "purchase"
GET /api/v1/invoices/      → the GST invoice (number, amount, gst_amount)
```

Route the student to the course player: `/student/courses/{course.slug}`.

---

## 14. Gotchas

- **`amount` is paise.** 589882 means ₹5898.82. Pass it to Razorpay untouched;
  use `amount_display` for your own text.
- **Never hardcode the Razorpay key.** It comes from `checkout/` and differs
  between test and live.
- **Don't reuse an order id across attempts** — one `POST /orders/` per purchase
  attempt. Re-opening the *same* order via `checkout/` is fine and encouraged.
- **Don't call `POST /orders/{id}/pay/`.** It's a dev-only stub and returns
  **409** in every deployed environment.
- **Don't treat `verify/` errors as failure** (§8). This is the one that causes
  double payments.
- **Hide the buy button when already enrolled** — check `GET /enrollments/` first.
  A second purchase would charge them without granting anything new.
- **Total ≠ course price.** 18% GST is added at checkout; show the order `total`
  on the confirmation step, not the course price.

---

## 15. Not available yet

Don't build UI for these — the endpoints don't exist:

- **Refunds** — no student-facing refund request.
- **EMI / installments, corporate & group pricing** — these price types are
  readable but checkout rejects them.
- **Refund execution** — a cancelled paid booking records a `Refund` row in
  `requested` state, but nothing calls Razorpay's refund API. An admin settles
  it manually, and there is no student-facing refund endpoint to poll.
- **Subscription auto-renew** — plans activate for one period, then expire.
- **Saved cards at checkout** — `GET /payment-methods/` exists, but Razorpay
  handles instrument selection inside its own modal.
