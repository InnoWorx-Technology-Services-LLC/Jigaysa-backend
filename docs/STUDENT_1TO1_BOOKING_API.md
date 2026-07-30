# Student 1:1 Session Booking — Frontend API

Everything the student-facing **Sessions** page and **Book a session** modal need
(PRD §3.6 *Individual Sessions*: 1:1 trainer booking · calendar scheduling · time
slot booking · payment per hour).

Base URL: `/api/v1/` · Auth: `Authorization: Bearer <access token>` on every call.

---

## 1. The flow in one picture

```
Student                          Trainer                        Money
───────────────────────────────────────────────────────────────────────────
POST /individual-bookings/  →  pending ("requested")            —
                               ↓ confirm/
                            awaiting_payment                order created
POST /orders/{id}/checkout/ + verify/  ↓                    student pays
                              confirmed                        paid
                               ↓ complete/
                             completed
```

**Payment comes after the trainer accepts, never before.** A declined request
costs the student nothing, so no refund is needed for the common case.

---

## 2. Step 1 — choose a mentor

### `GET /mentors/`

Approved, active trainers only. Optional `?q=` matches name or expertise.

```json
{
  "count": 3,
  "results": [
    {
      "id": 5,
      "full_name": "Dr. Kapoor",
      "email": "kapoor@example.com",
      "headline": "Senior data scientist",
      "avatar": "",
      "expertise": "React",
      "years_experience": 8,
      "hourly_rate": "1500.00",
      "rating_avg": "4.80",
      "rating_count": 32
    }
  ]
}
```

Render the rate as **₹1,500/hr**. `hourly_rate` of `"0.00"` means the mentor is
free — the booking will skip payment entirely (see §5).

---

## 3. Step 2 — pick a slot

### `GET /trainer-availability/?trainer=<id>&available=true`

`available=true` returns only slots that are **unbooked and in the future**.

```json
{
  "count": 3,
  "results": [
    { "id": 41, "trainer": { "id": 5, "full_name": "Dr. Kapoor", "email": "…" },
      "start": "2026-08-01T10:00:00Z", "end": "2026-08-01T11:00:00Z",
      "slot_minutes": 60, "is_booked": false }
  ]
}
```

> ⚠️ **Each bookable time is its own row.** Group the results by date to build
> the date strip, and list each row's `start` as a time chip. Do **not** split
> one long slot into hourly chips client-side — `start` must match a row
> exactly or the booking is rejected.

---

## 4. Step 3 — submit the request

### `POST /individual-bookings/`

```json
{
  "trainer": 5,
  "start": "2026-08-01T11:00:00Z",
  "duration_minutes": 60,
  "topic": "React hooks deep dive",
  "notes": "Tried useMemo already."
}
```

| Field | Required | Notes |
|---|---|---|
| `trainer` | ✅ | mentor id from §2 |
| `start` | ✅ in practice | must equal a slot's `start` exactly |
| `duration_minutes` | — | defaults to `60`; drives the price |
| `topic` | ✅ | the "What do you need help with?" field |
| `notes` | — | free text for the mentor |

**`201`** → a Booking object (§6). The slot is locked and marked booked, and the
mentor gets a notification.

**`400`** errors — show the message as-is:

| Message | Cause |
|---|---|
| `No open availability for this trainer at that time.` | `start` doesn't match an open slot |
| `You cannot book a session with yourself.` | trainer booking themselves |
| `You already have 3 unconfirmed bookings. Pay for or cancel one before requesting another.` | open-request cap |
| `{"topic": ["This field is required."]}` | missing topic |

---

## 5. Paying

When the mentor accepts, the booking moves to **`awaiting_payment`** and an
order is created at the rate that was live at acceptance time — a later rate
change cannot move the price.

Poll or refresh the bookings list; when you see `status: "awaiting_payment"`,
show a **Pay now** button using `order` and `amount_due`, plus a countdown to
`payment_due_at`.

Payment reuses the **existing Razorpay checkout** — nothing 1:1-specific:

1. `POST /orders/{order}/checkout/` → returns the Razorpay Checkout options
2. open Razorpay Checkout in the browser
3. `POST /orders/{order}/verify/` with the handler payload

Full details in [STUDENT_PAYMENT_FLOW.md](STUDENT_PAYMENT_FLOW.md). Once the
payment settles, the booking flips to `confirmed` on its own — the
`payment.captured` webhook does it even if the browser closed.

**Free mentors** (`hourly_rate` = 0) skip all of this: accepting goes straight to
`confirmed` with `order: null`.

### Payment deadline

`payment_due_at` = **24 hours after acceptance**, or **2 hours before the session
starts**, whichever is sooner. Miss it and the booking is cancelled and the slot
released — the student is notified. Treat `payment_due_at` as a hard countdown in
the UI.

---

## 6. The Booking object

Returned by every endpoint below.

```json
{
  "id": 77,
  "trainer": 5,
  "trainer_name": "Dr. Kapoor",
  "student": 12,
  "student_name": "Asha Rao",
  "topic": "React hooks deep dive",
  "notes": "Tried useMemo already.",
  "start": "2026-08-01T11:00:00Z",
  "duration_minutes": 60,
  "status": "awaiting_payment",
  "order": 301,
  "amount_due": "1770.00",
  "is_paid": false,
  "payment_due_at": "2026-08-01T09:00:00Z",
  "meeting_url": "",
  "created_at": "2026-07-30T06:20:00Z"
}
```

| Field | Meaning |
|---|---|
| `status` | see §7 |
| `order` | order id to pay, or `null` when nothing is owed |
| `amount_due` | total **incl. 18% GST**, or `null` once paid / not payable |
| `is_paid` | the order settled |
| `payment_due_at` | payment deadline; `null` unless `awaiting_payment` |
| `meeting_url` | set by the mentor at acceptance; may be empty |

---

## 7. Status → UI label

| API `status` | Show as | Student can |
|---|---|---|
| `pending` | **Requested** | Cancel request |
| `awaiting_payment` | **Accepted — pay now** | Pay · Cancel |
| `confirmed` | **Confirmed** | Join (if `meeting_url`) · Cancel |
| `completed` | **Completed** → *Past sessions* | — |
| `cancelled` | **Cancelled** | — |

Your existing "requested" chip maps to `pending`. `awaiting_payment` is the one
new state the UI needs.

### Page counters

Derive client-side from the bookings list — there is no summary endpoint:

- **Upcoming** — count of `pending` + `awaiting_payment` + `confirmed`
- **Completed** — count of `completed`
- **Total hours** — sum `duration_minutes / 60` over `completed`

---

## 8. Endpoints

### `GET /individual-bookings/`

Bookings you made (trainers see ones they received). Paginated.

### `GET /individual-bookings/{id}/`

One booking.

### `POST /individual-bookings/{id}/cancel/`

Student **or** trainer, while `pending` / `awaiting_payment` / `confirmed`.
Frees the slot and notifies the other party.

```json
{ "...booking fields...": "…", "refund_requested": true, "paid": true }
```

- `refund_requested: true` — the **trainer** cancelled a paid session, so the fee
  is recorded as owed back. Tell the student a refund is being processed.
- If the **student** cancels a paid session, `refund_requested` is `false` — no
  automatic refund. Point them at support.

> Refunds are **recorded, not executed**: an admin settles them manually. There
> is no student-facing refund endpoint (PRD §3.13 refunds is not built).

**`400`** `This booking is already cancelled.` on a repeat call.

### Trainer-only actions

These are for the mentor UI, not the student app:

| Endpoint | Effect |
|---|---|
| `POST /individual-bookings/{id}/confirm/` | `pending` → `awaiting_payment` (or `confirmed` if free). Optional body `{ "meeting_url": "https://…" }` |
| `POST /individual-bookings/{id}/decline/` | `pending` → `cancelled`, frees the slot |
| `POST /individual-bookings/{id}/complete/` | `confirmed` → `completed` |

All return the updated booking; all `400` with
`This booking is already <status>.` on an invalid transition. A trainer who
doesn't own the booking gets **`404`**.

---

## 9. Notifications

Delivered through the existing bell (`GET /notifications/`), category
`live_class`:

| Event | Who | Title |
|---|---|---|
| Request created | mentor | New 1:1 booking request |
| Accepted, payment owed | student | 1:1 request accepted — payment needed |
| Accepted, free | student | 1:1 session confirmed 🎉 |
| Declined | student | 1:1 request declined |
| Cancelled | other party | 1:1 booking cancelled |
| Payment window lapsed | student | 1:1 booking expired |

---

## 10. Not built — don't build UI for it

- **Rescheduling.** A mentor with an emergency can only cancel, which for a paid
  session records a refund. There is no "propose a new time" flow.
- **Student-facing refunds.** No request endpoint, no status to poll.
- **Recurring / package bookings**, group 1:1s, mentor-side availability
  suggestions.
- **Automatic expiry without ops setup** — slot release runs from
  `python manage.py expire_unpaid_bookings`, which must be scheduled (every 15
  min is plenty). Until it runs, an unpaid booking still shows as
  `awaiting_payment`.
