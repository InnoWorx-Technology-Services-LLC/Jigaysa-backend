# Course Feedback & Rating API — Student · Trainer · Admin

Two separate systems that are easy to confuse, documented together because the
frontend usually ships them on the same screen.

Base URL `/api/v1/` · `Authorization: Bearer <access>` on every call.

---

## 0. Which one do I want?

| | **Rating / review** | **Feedback form** |
|---|---|---|
| Endpoint | `/reviews/` | `/feedback-forms/`, `/feedback-responses/` |
| Model | `CourseReview` | `FeedbackForm` → `FeedbackQuestion` → `FeedbackResponse` → `FeedbackAnswer` |
| Who authors it | Nobody — fixed shape | The trainer, per course |
| Shape | 1-5 stars + one comment | Any number of typed questions |
| Who reads it | **Public** — on the catalog card | **Private** — trainer + admin only |
| Feeds | `Course.rating_avg` / `rating_count` | Trainer summary endpoint |
| Per student | One per course | One per form |
| Can be anonymous | No | Yes (`is_anonymous`) |

Rule of thumb: the **rating** is marketing copy that sells the course to the
next buyer; the **feedback form** is private course-improvement data for the
trainer. Editing one never touches the other.

---

# PART 1 — RATING / REVIEW SYSTEM

A student who is enrolled may leave exactly one review per course. Every write
recomputes `Course.rating_avg` and `Course.rating_count`, so the catalog card is
always current.

## 1.1 List reviews for a course

### `GET /reviews/?course=3`

Readable by any authenticated user.

```json
{
  "count": 1,
  "results": [
    {
      "id": 12,
      "course": 3,
      "student": { "id": 9, "full_name": "Asha R.", "email": "asha@example.com" },
      "rating": 5,
      "comment": "Clear explanations, good pace.",
      "created_at": "2026-09-20T10:04:00Z"
    }
  ]
}
```

## 1.2 Leave a review

### `POST /reviews/` — role: `student`

```json
{ "course": 3, "rating": 5, "comment": "Clear explanations, good pace." }
```

`201 Created` returns the review. `Course.rating_avg` / `rating_count` are
recomputed in the same request.

**400** when:

| Message | Cause |
|---|---|
| `You must be enrolled to review this course.` | No `Enrollment` row |
| `You have already reviewed this course.` | One review per student per course |
| `Rating must be between 1 and 5.` | `rating` outside 1-5 |

## 1.3 Edit or delete a review

### `PATCH /reviews/{id}/` · `DELETE /reviews/{id}/`

Author or admin only — anyone else gets `403 You can only modify your own
review.` Both recompute the course aggregate.

## 1.4 Where the aggregate shows up

`rating_avg` (string, 2 dp) and `rating_count` appear on every course payload:

```json
{ "id": 3, "title": "React Pro", "rating_avg": "4.67", "rating_count": 3 }
```

Sort the catalog by it with `GET /courses/?ordering=-rating_avg`.

---

# PART 2 — COURSE FEEDBACK FORM

The trainer designs a questionnaire; enrolled students fill it in once; the
trainer reads aggregates. Individual answers are never public.

```
   trainer                          student                    trainer
┌──────────────────┐
│ POST /feedback-  │
│ forms/ with      │──▶ GET /feedback-forms/?course=3 ──▶ POST /feedback-
│ nested questions │        (enrolled students only)       responses/
└──────────────────┘                                            │
         ▲                                                      ▼
         └──────────── GET /feedback-forms/{id}/summary/ ◀───────┘
```

## 2.1 Question types

| `question_type` | Answer field | Valid values | `options` |
|---|---|---|---|
| `rating` | `rating` | 1-5 | ✗ |
| `scale` | `rating` | 1-10 | ✗ |
| `text` | `text` | free text | ✗ |
| `choice` | `text` | must match one option | **required, ≥ 2** |
| `yes_no` | `text` | `"yes"` / `"no"` (case-insensitive) | ✗ |

Sending `options` on a non-`choice` question is a `400`, as is sending a
`rating` on a non-numeric question.

## 2.2 Create the form — `POST /feedback-forms/`

Role: `trainer` (own courses) · `admin`. One form per course.

```json
{
  "course": 3,
  "title": "How did we do?",
  "description": "Two minutes, and it shapes the next cohort.",
  "is_active": true,
  "is_anonymous": false,
  "require_completion": false,
  "questions": [
    { "question_type": "rating", "text": "Rate the course overall", "order": 0 },
    { "question_type": "scale",  "text": "How likely are you to recommend it?", "order": 1 },
    { "question_type": "choice", "text": "How was the pace?", "order": 2,
      "options": ["Too slow", "About right", "Too fast"] },
    { "question_type": "yes_no", "text": "Were the exercises useful?", "order": 3 },
    { "question_type": "text",   "text": "What should we change?",
      "is_required": false, "order": 4 }
  ]
}
```

Form flags:

| Field | Default | Effect |
|---|---|---|
| `is_active` | `true` | `false` closes the form — submissions get a `400` |
| `is_anonymous` | `false` | `true` nulls `student` in every trainer-facing read |
| `require_completion` | `false` | `true` gates the form on `Enrollment.completed_at` |

`201 Created`:

```json
{
  "id": 7, "course": 3, "title": "How did we do?",
  "is_active": true, "is_anonymous": false, "require_completion": false,
  "questions": [
    { "id": 21, "question_type": "rating", "text": "Rate the course overall",
      "help_text": "", "is_required": true, "order": 0, "options": [] }
  ],
  "response_count": 0,
  "created_at": "2026-09-27T09:00:00Z", "updated_at": "2026-09-27T09:00:00Z"
}
```

`403 You can only add a form to your own course.` if the course is not yours.

## 2.3 Edit the form — `PATCH /feedback-forms/{id}/`

Sending `questions` **replaces the whole set** — the builder always submits the
full list. Question ids change, and answers to removed questions are deleted
with them. Omit `questions` to change only the flags:

```json
{ "is_active": false }
```

## 2.4 Fetch the form to fill in

### `GET /feedback-forms/?course=3`

Students see a course's form only when they are enrolled **and** it is
`is_active`. Trainers see their own courses' forms regardless; admins see all.
Same payload as §2.2.

## 2.5 Submit feedback — `POST /feedback-responses/`

Role: `student`. Answers are nested; `student` and `submitted_at` come from the
server.

```json
{
  "form": 7,
  "answers": [
    { "question": 21, "rating": 5 },
    { "question": 22, "rating": 9 },
    { "question": 23, "text": "About right" },
    { "question": 24, "text": "yes" },
    { "question": 25, "text": "More exercises on hooks." }
  ]
}
```

`201 Created`:

```json
{
  "id": 40, "form": 7,
  "student": { "id": 9, "full_name": "Asha R.", "email": "asha@example.com" },
  "answers": [ { "id": 88, "question": 21, "rating": 5, "text": "" } ],
  "submitted_at": "2026-09-27T09:30:00Z",
  "created_at": "2026-09-27T09:30:00Z"
}
```

On an anonymous form `student` is `null` here and everywhere else.

**400** when:

| Message | Cause |
|---|---|
| `This feedback form is closed.` | `is_active: false` |
| `You must be enrolled to submit feedback.` | No `Enrollment` row |
| `Finish the course before giving feedback.` | `require_completion` and `completed_at` is null |
| `You have already submitted this feedback.` | One response per student per form |
| `Required questions unanswered: [21].` | A required question has no answer |
| `Question 21 must be between 1 and 5.` | Rating outside the type's range |
| `Question 23: 'Fast' is not one of the options.` | `choice` answer off-list |
| `Question 21 does not take a rating.` | `rating` on a text/choice/yes-no question |

Responses are **immutable** — there is no `PATCH` or `DELETE`. A trainer acting
on results should not have the ground shift under them.

## 2.6 Read responses — `GET /feedback-responses/?form=7`

| Role | Sees |
|---|---|
| Student | Their own submissions only |
| Trainer | Every response on their own courses' forms |
| Admin | Everything |

## 2.7 Trainer summary — `GET /feedback-forms/{id}/summary/`

Aggregates only, never a name against an answer — safe to render even for an
anonymous form. Course trainer or admin; anyone else gets `403`.

```json
{
  "form": 7,
  "course": 3,
  "response_count": 24,
  "enrolled_count": 61,
  "questions": [
    { "id": 21, "text": "Rate the course overall", "question_type": "rating",
      "answered": 24, "average": 4.42,
      "distribution": { "3": 2, "4": 10, "5": 12 } },
    { "id": 23, "text": "How was the pace?", "question_type": "choice",
      "answered": 24,
      "distribution": { "About right": 18, "Too fast": 4, "Too slow": 2 } },
    { "id": 25, "text": "What should we change?", "question_type": "text",
      "answered": 11,
      "responses": ["More exercises on hooks.", "Longer Q&A."] }
  ]
}
```

Per-question keys by type:

- `rating` / `scale` → `average` + `distribution` keyed by score
- `choice` / `yes_no` → `distribution` keyed by answer, most common first
- `text` → `responses`, a flat list capped at 200 entries

---

## 3. Data model

```
Course ──1:1──▶ FeedbackForm ──1:N──▶ FeedbackQuestion
                     │                       │
                    1:N                     1:N
                     ▼                       ▼
              FeedbackResponse ──1:N──▶ FeedbackAnswer
                     │
                  student (FK, kept even when is_anonymous)

Course ──1:N──▶ CourseReview ──▶ recomputes Course.rating_avg / rating_count
```

Uniqueness: one form per course · one response per `(form, student)` · one
answer per `(response, question)` · one review per `(course, student)`.

`FeedbackAnswer` stores numeric answers in `rating` and everything else in
`text`, so the summary aggregates in the database rather than in Python.
`FeedbackQuestion.options` is a plain JSON list — options carry no answer key
(unlike `assessments.Choice`) and are always rewritten as a set.

---

## 4. Endpoint index

| Method | Path | Role |
|---|---|---|
| `GET` | `/reviews/?course=` | any authenticated |
| `POST` | `/reviews/` | student |
| `PATCH` `DELETE` | `/reviews/{id}/` | author, admin |
| `GET` | `/feedback-forms/?course=` | trainer (own), enrolled student, admin |
| `POST` | `/feedback-forms/` | trainer (own course), admin |
| `PATCH` `DELETE` | `/feedback-forms/{id}/` | course trainer, admin |
| `GET` | `/feedback-forms/{id}/summary/` | course trainer, admin |
| `GET` | `/feedback-responses/?form=` | own (student), course trainer, admin |
| `POST` | `/feedback-responses/` | enrolled student |
