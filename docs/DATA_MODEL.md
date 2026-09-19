# DATA_MODEL.md — Finance PSU Learning Platform

PostgreSQL 18, accessed via Django's ORM. Every entity below traces to a requirement in REQUIREMENTS.md. Fields marked **[Assumption]** are needed to satisfy a confirmed requirement but weren't explicitly enumerated in a source document. This is a simple Django/Postgres monolith by design — no Redis, no Celery, no microservices, no event bus, no speculative abstractions (consistent with the approved stack and DECISIONS.md D9/D10/D11).

## 1. Entities

### User
**Django's built-in `django.contrib.auth.models.User`, used unmodified — no custom `AUTH_USER_MODEL`.**
- Standard fields only: `id`, `username`, `email`, `password` (hashed), `date_joined`, plus Django's native `is_staff`/`is_superuser` and the standard permissions/groups system. `username` is the active login credential (SECURITY.md §3, `ACCOUNT_LOGIN_METHODS = {"username"}`) — `email` is collected and verified but is not itself the login identifier.
- **Admin authorization uses `is_staff`/`is_superuser`/Django permissions natively — there is no custom `User.is_admin` field.** An earlier draft of this document listed a custom `is_admin` boolean alongside "Django's built-in user model," which was self-contradictory (DECISIONS.md D10). Corrected: REQ-MOCK-03's "admin" is simply a `User` with `is_staff=True` (and `is_superuser=True` for full access), managed the same way any Django Admin operator account is.
- **Email verification (REQ-ACC-01) — [Resolved implementation baseline, not stack-doc-mandated]:** the approved stack specifies django-allauth email confirmations. Finance PSU's implementation baseline (DECISIONS.md D11.6) configures `ACCOUNT_EMAIL_VERIFICATION = "mandatory"`, so an unverified signup cannot log in until they click the verification link Brevo sends (REQ-ACC-01, SECURITY.md §3, TESTING.md).
- **Google Login (REQ-ACC-04 — client-approved post-freeze scope addition, DECISIONS.md D14): no custom `GoogleAccount`/social-identity table is added to this schema.** `allauth.socialaccount` owns its own framework tables (`SocialAccount`, `SocialApp`, `SocialToken`) that link a Google identity to a stock `User` row via a plain FK — these are allauth's tables, not a Finance-PSU-designed model, and are not enumerated further here since they aren't part of this project's own schema design. `SocialToken` rows are not populated for this integration (`SOCIALACCOUNT_STORE_TOKENS = False`, SECURITY.md §3) — Finance PSU never persists a Google access/refresh token. The stock `User` row created or matched via Google login is identical in shape to one created via local signup — same table, same fields, no branch in `User` itself for "how this account authenticates."

### UserProfile *(REQ-ACC-02 single active session + REQ-DASH-04 Profile — REQUIREMENTS.md §7 items 2 & 3, client-frozen)*
- `id`, `user` (FK → User, one-to-one), `active_session_key` (tracking hash — the current session's key; a mismatch on any request triggers `session.flush()` + `logout()` + redirect to `/accounts/login/?error=session_conflict`, per REQ-ACC-03), `phone_number` (CharField, max_length=15, null=True).
- **No other fields.** These three (plus `user`) are exactly what the client froze — no timestamp or other field has been added here; "recently accessed course" (§ below) is tracked on `UserChapterProgress`, not here, so there was no other corrected requirement that needed a new `UserProfile` field.
- Password changes go through django-allauth's own forms, not a custom field here.

### Course
- `id`, `title`, `short_description`, `full_description`, `thumbnail`, `subjects_covered` (text/JSON list), `is_published`.

### PricingPlan *(REQ-WEB-04 — 3/6/12 month validity options)*
- `id`, `course` (FK → Course), `duration_months`, `price_paise` (positive integer — see Order below for why money is stored in paise, not rupees).

### Book *(REQ-COURSE-01 — course → book hierarchy)*
- `id`, `course` (FK → Course), `title`, `order`.

### Chapter
- `id`, `book` (FK → Book), `title`, `order`, `pdf_object_key` (CharField — private R2 object path, **not** a Django `FileField`/`ImageField` and never a public URL; see API.md §7 "Chapter PDF upload" for how it gets populated).
- [Resolved provenance — DECISIONS.md D10 item 11] **`Chapter` is the persisted leaf content/topic unit for this build.** The invoice says "course/book/chapter/topic structure" and the workflow doc uses "chapter/topic" interchangeably; there is no separate `Topic` table. Where source documents say "topic," read "Chapter." This was previously true in practice but never stated explicitly — stated now so no one later assumes a `Topic` model exists.

### UserChapterProgress *(REQ-DASH-01 — "course progress %", "completed topic count", AND "recently accessed course")*
- `id`, `user` (FK → User), `chapter` (FK → Chapter), `is_completed` (bool, default `False`), `last_accessed_at` (DateTimeField, auto-updated).
- **Constraint:** unique together on (`user`, `chapter`).
- **Upsert behavior**, triggered by every successful, *entitled* chapter PDF signed-URL request (API.md §3):
  - Row is created if it doesn't exist.
  - `is_completed` flips to `True` the first time the student opens that chapter's PDF viewer [Assumption, unchanged from the original design] — it never flips back to `False`.
  - `last_accessed_at` is set to `now()` on **every** valid access, first or repeat — this is what's new in this pass.
- **Course progress %** = `is_completed=True` rows for that course's chapters ÷ total chapters in the course, for the given student (unchanged).
- **"Recently accessed course" (REQ-DASH-01) — corrected this pass, DECISIONS.md D11.5:** the `course` of whichever `UserChapterProgress` row has the maximum `last_accessed_at`, **restricted to courses the student currently has an active (non-expired) `Enrollment` for.** An expired course's access history must not surface as "recently accessed" even if it's the most recent row on file — expired access isn't accessible access. If no accessed course currently has an active `Enrollment`, "recently accessed course" is empty/null, not a stale expired one. No second tracking table was added — this one field, filtered by `Enrollment`, answers both "completed topic count" and "recently accessed course." Query shape:
  ```python
  active_course_ids = Enrollment.objects.filter(
      student=student, expires_at__gt=now(),
  ).values_list("course_id", flat=True)
  recent = UserChapterProgress.objects.filter(
      user=student, chapter__book__course_id__in=active_course_ids,
  ).order_by("-last_accessed_at").first()
  ```

### MockTest *(REQ-MOCK-02 — purchasable independently of courses)*
- `id`, `title`, `description`, `duration_minutes`, `is_published`.

### MockTestPricingPlan
- `id`, `mock_test` (FK → MockTest), `price_paise` (positive integer). [Assumption — workflow doc shows mock tests are purchased but doesn't detail their own plan/validity structure the way courses have 3/6/12-month plans; modeled as a simple one-time purchase unless told otherwise.]

### CourseMockTest *(DECISIONS.md D10 item 3 — course-included mock tests, REQ-MOCK-05)*
The workflow doc says a purchased course gives access to "Notes / MCQs / Mock Tests" — i.e. a course can *include* formal Mock Tests, on top of Mock Tests also being independently purchasable (REQ-MOCK-02). The original model only supported the standalone path; this join table adds the missing one.
- `id`, `course` (FK → Course), `mock_test` (FK → MockTest), `order` (integer — display ordering within the course).
- **Constraint:** unique together on (`course`, `mock_test`).
- A course's "mock test count" (REQ-WEB-03/04 card field) is derived from `CourseMockTest.objects.filter(course=course).count()`, not a free-standing number.
- See API.md §4 for the resulting `can_access_mock_test(user, mock_test)` authorization rule this table feeds.

### Question *(REQ-MOCK-03 — admin-managed question bank; REQ-COURSE-05 — REQUIREMENTS.md §7 item 8, client-frozen)*
- `id`, `mock_test` (FK → MockTest, nullable), `chapter` (FK → Chapter, nullable), `text`, `option_a`, `option_b`, `option_c`, `option_d`, `correct_option`, `explanation` [Assumption — common for MCQ platforms, not explicitly requested].
- **Constraint (corrected this pass, DECISIONS.md D11.8): exactly one of `mock_test` / `chapter` is set, enforced as a real DB `CheckConstraint`** — both FK columns live on the `Question` row itself, so this doesn't need to span tables the way the `AttemptAnswer`↔`Attempt` invariant below does; Postgres enforces it directly:
  ```python
  CheckConstraint(
      check=(
          Q(mock_test__isnull=False, chapter__isnull=True)
          | Q(mock_test__isnull=True, chapter__isnull=False)
      ),
      name="question_exactly_one_target",
  )
  ```
  A single shared table serves both formal Mock Tests (`mock_test` set, `chapter` null — timed, autosaved, deferred results) and untimed per-chapter Practice MCQs (`chapter` set, `mock_test` null — immediate correct/incorrect feedback on selection, no `Attempt`/`AttemptAnswer` row created). Entered manually via Django Admin only — no bulk/CSV/Excel/JSON importer is in scope.

### MockTestEnrollment
- `id`, `student` (FK → User), `mock_test` (FK → MockTest), `purchased_at`, `order` (FK → Order, **unique** — see §3 Constraints).

### Attempt *(REQ-MOCK-01)*
- `id`, `student` (FK → User), `mock_test` (FK → MockTest), `started_at`, `submitted_at` (null while in progress), `time_limit_minutes` (copied from `mock_test.duration_minutes` at start, so later edits to the test don't retroactively change an in-progress attempt), `score`, `total_questions`.
- **Server-authoritative deadline** = `started_at + time_limit_minutes`. Computed at request time from these two stored fields — no extra column needed. See API.md §5 for the enforcement path (autosave/submit both check the server's own clock against this deadline; client-supplied timestamps are never trusted).
- **Row-locking discipline (corrected this pass, DECISIONS.md D11.2):** every mutating operation on an `Attempt` — autosave, manual submit, and deadline auto-finalization — acquires `Attempt.objects.select_for_update()` inside `transaction.atomic()` **before** reading `submitted_at`, computing the deadline check, or writing anything. This serializes concurrent autosave/submit calls on the same attempt through Postgres's row lock, so two near-simultaneous requests can't both observe "not yet submitted" and both try to finalize/score it. No Celery/Redis — the lock is a plain Postgres row lock held for the duration of one short request.

### AttemptAnswer *(REQ-MOCK-01 autosave, REQ-MOCK-04 server-synced)*
- `id`, `attempt` (FK → Attempt), `question` (FK → Question), `selected_option` (nullable — unanswered), `saved_at`.
- **Constraint:** unique together on (`attempt`, `question`) — autosave is an upsert of this row per (attempt, question), never a second row for the same question.
- **Invariant:** `question` must belong to the same `MockTest` as `attempt` (i.e. `question.mock_test_id == attempt.mock_test_id`). This spans two FK'd tables, so it's enforced in application logic at write time (the autosave/submit view), not as a raw SQL `CHECK` — a cross-table DB-level constraint here would need a trigger, which is more machinery than this invariant is worth (CLAUDE.md "Simplicity First").
- Final submit is a status flip on `Attempt` (`submitted_at`, `score`), not a new write path; submitting twice is idempotent (API.md §5) — the second call returns the already-stored score rather than rescoring.

### Order *(REQ-PAY-01–03 — redesigned this pass; see DECISIONS.md D10)*
- `id`, `student` (FK → User).
- `course_pricing_plan` (FK → PricingPlan, **nullable**), `mock_test_pricing_plan` (FK → MockTestPricingPlan, **nullable**). **Exactly one of these two is set** — enforced by a DB `CHECK` constraint (`(course_pricing_plan_id IS NOT NULL) != (mock_test_pricing_plan_id IS NOT NULL)`), not just application logic. This replaces an earlier `item_type`/`item_id` generic-polymorphic design that had no DB-level foreign-key integrity — see DECISIONS.md D10 for why the FK+CHECK approach was chosen instead and why the polymorphic design is no longer used anywhere in this doc set.
- `razorpay_order_id` (CharField, **unique, not null** — created and set *before* the student ever reaches Razorpay's checkout; this is what the webhook looks up by, see API.md §2).
- `razorpay_payment_id` (CharField, **nullable, unique when set** — only populated once a payment is verified).
- `amount_paise` (positive integer — the smallest INR unit; captured server-side from the pricing plan at order-creation time, **never** trusted from the client or from Razorpay's webhook payload for anything other than cross-checking).
- `currency` (CharField, default `"INR"`).
- `status` (`created` | `verified` | `failed`).
- `verified_at` (nullable).
- `Enrollment`/`MockTestEnrollment` rows are only created after `status = verified`, inside the same atomic transaction as the webhook's row-locked update (API.md §2) — never on `created` alone, and never twice for the same `Order` (see §3 Constraints).

### Enrollment *(REQ-DASH-02, REQ-PAY-03 — purchase → access mapping)*
- `id`, `student` (FK → User), `course` (FK → Course), `pricing_plan` (FK → PricingPlan), `purchased_at`, `expires_at` (derived from `pricing_plan.duration_months`), `order` (FK → Order, **unique** — see §3 Constraints).
- Access check for REQ-COURSE-04: `Enrollment` exists for this student+course and `expires_at > now()`.

### BlogPost *(REQ-WEB-05)*
- `id`, `title`, `slug`, `body`, `published_at`, `author` (FK → User, admin).

## 2. Notes

- No Redis/Valkey/queue table exists in the approved stack, so `UserProfile.active_session_key` and autosave both go straight to Postgres rather than an in-memory store — consistent with ARCHITECTURE.md §3.2.
- `PricingPlan`/`MockTestPricingPlan` are separate from `Order`/`Enrollment` so a plan's price can change without mutating historical orders (`Order.amount_paise` is captured at purchase time).
- Practice MCQ answer-checks (`chapter`-linked `Question`s) are stateless — no `Attempt`/`AttemptAnswer` row is written, since there's no attempt to track for an untimed, immediate-feedback practice question.
- **Money is stored in paise (integer), never rupees/decimal, anywhere a Razorpay amount is involved** (`Order.amount_paise`, `PricingPlan.price_paise`, `MockTestPricingPlan.price_paise`). Razorpay's own API operates in paise natively, so this avoids a rupee↔paise conversion step (and its rounding-error risk) at the one place — payment verification — where a unit mistake would be a money bug, not a display bug.

## 3. Database Constraints (Invariants)

The complete list of DB-level constraints this design relies on — deliberately minimal, nothing speculative:

| Table | Constraint |
|---|---|
| `CourseMockTest` | unique(`course`, `mock_test`) |
| `UserChapterProgress` | unique(`user`, `chapter`) |
| `AttemptAnswer` | unique(`attempt`, `question`) |
| `Order` | unique(`razorpay_order_id`); unique(`razorpay_payment_id`) where not null; `CHECK` exactly one of `course_pricing_plan`/`mock_test_pricing_plan` set |
| `Question` | `CHECK` exactly one of `mock_test`/`chapter` set — **corrected this pass, DECISIONS.md D11.8: a real DB `CheckConstraint`**, not application logic (both FKs live on the same row, so Postgres enforces it directly; see `Question` entity note above) |
| `Enrollment` | unique(`order`) — one verified `Order` can never produce two `Enrollment` rows |
| `MockTestEnrollment` | unique(`order`) — same guarantee for the standalone mock-test purchase path |

No Redis, no Celery, no microservices, no event bus, no speculative abstractions were added to satisfy any of the above — every one of these is a plain Postgres `UNIQUE`/`CHECK` constraint or, where a constraint would need to span two FK'd tables, an application-layer check at the one or two write paths that touch it.
