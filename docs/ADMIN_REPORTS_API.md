# Admin · Reports — Frontend API

Backs `/admin/reports`: four counters, two twelve-month trend charts, the role
breakdown bars, and the per-batch attendance table.

Base URL: `/api/v1/` · Auth: `Authorization: Bearer <access token>`
**Admin only.** Every endpoint here returns `403` for any other role.

Everything is **computed live** from the operational tables — no snapshot job,
nothing to go stale, and nothing to warm up before the page works.

---

## 1. The page is four calls

| Section | Call |
|---|---|
| Four tiles | `GET /admin/reports/summary/` |
| Enrollments + Revenue charts | `GET /admin/reports/trends/` |
| Users by role bars | `GET /admin/reports/users-by-role/` |
| Attendance table | `GET /admin/reports/attendance/` |

Only the last is paginated — it is the only one whose row count grows without
bound.

---

## 2. `GET /admin/reports/summary/` — the four tiles

```json
{ "active_users": 18420, "enrollments": 26310,
  "revenue": "14280000.00", "payouts": "0.00",
  "currency": "INR", "active_window_days": 90 }
```

| Field | Means |
|---|---|
| `active_users` | signed in within `active_window_days`. **Not** total accounts — an account that hasn't appeared in three months is a row in a table, not a user. |
| `enrollments` | all enrollments, all time. |
| `revenue` | captured payments, all time. |
| `payouts` | **paid** trainer payouts — see the warning below. |

`active_window_days` travels with the number on purpose: "active" is a
definition, not a fact. Label the tile from it ("Active in 90 days") rather than
hardcoding a window the backend might change.

> ### ⚠️ `payouts` is always `0`
>
> Nothing generates payout rows in this release (see
> `docs/ADMIN_PAYMENTS_API.md` §5). The tile is wired to a real field reading a
> real table that stays empty. Either hide it or label it clearly — a ₹0 payouts
> figure next to a healthy revenue figure reads as a bug in the money.

---

## 3. `GET /admin/reports/trends/` — the two line charts

```json
{
  "months": 12,
  "enrollments": [ { "month": "2025-10", "value": "412" }, … ],
  "revenue":     [ { "month": "2025-10", "value": "980000.00" }, … ],
  "currency": "INR"
}
```

`?months=` accepts **1–36**, default 12. Out-of-range is clamped and junk falls
back to the default — a bad query parameter should not `400` a dashboard.

### Every month is present, including the empty ones

The database only returns months that had rows, so a quiet December simply isn't
there. Charting that directly draws a line straight from November to January and
**hides the dip**. These series are filled server-side: `months` entries,
oldest first, gaps as explicit zeros.

That also means **index _n_ is the same month in both arrays**, so the two
charts can share one x-axis without any alignment work on your side.

`value` is a string in both series (revenue is money; enrollments is a count
that arrives the same way for consistency).

---

## 4. `GET /admin/reports/users-by-role/` — the bars

```json
{
  "total": 17270,
  "roles": [
    { "role": "admin", "label": "Admin", "count": 24 },
    { "role": "trainer", "label": "Trainer / Lecturer", "count": 1240 },
    { "role": "student", "label": "Student / Learner", "count": 15820 },
    { "role": "institution", "label": "Institution / Corporate Client", "count": 186 }
  ]
}
```

Every role appears, including ones with nobody in them, so the bar list doesn't
change shape as the platform fills up.

`total` is the denominator for the bar widths — use it rather than summing
client-side, so four bars can't round to 101%.

Order follows the platform's role enum, not the mock's visual order. Sort in the
frontend if you want students first.

---

## 5. `GET /admin/reports/attendance/` — the per-batch table

**Paginated** (default 20, max 100). Optional `?course=<slug>`.

```json
{
  "count": 84,
  "next": "…?page=2",
  "previous": null,
  "results": [
    { "batch_id": 7, "batch": "Batch A", "course": "React 19 Pro",
      "course_slug": "react-19-pro", "enrolled": 28, "capacity": 30,
      "attendance_rate": 88.0 }
  ]
}
```

`attendance_rate` is a percentage 0–100, computed as *present records ÷ all
attendance records* across the batch's live sessions.

> ### `null` and `0` mean opposite things
>
> **`null` means no register has been taken for that batch** — not that nobody
> came. Render it as "No data" and leave the bar empty. Colouring a null as 0%
> paints a healthy batch as a total failure, which is the kind of chart that
> starts a meeting.

Newest batches first, by start date.

---

## 6. Notes for whoever builds this

- **Money is a decimal string everywhere.** `revenue`, `payouts`, and every
  trend `value`. Format with a decimal library; never `parseFloat` a total.
- **An empty platform returns zeros and full-length arrays**, never an error and
  never a short series. A fresh deployment sees flat lines, not broken charts.
- **These are live queries.** They are cheap at current scale and will not be
  forever; if they slow down the fix is a scheduled job filling
  `AnalyticsSnapshot`, which is already modelled. The response shapes above are
  designed to survive that swap unchanged, so don't build around their timing.

---

## 7. Not in this release

- **Cohort analysis.** The page blurb promises it; nothing computes retention by
  signup cohort.
- **Revenue breakdowns** by course, trainer or category — revenue is one number
  and one series, not a split.
- **Export.** No CSV or PDF endpoint.
- **Custom date ranges** on the tiles. `summary` is all-time; only `trends` takes
  a window, and only in whole months.
- **Classroom (seat) attendance.** The table reads live-session registers only;
  `classrooms.SeatAttendance` is not included.
