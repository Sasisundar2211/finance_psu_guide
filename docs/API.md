# API.md — Finance PSU Learning Platform

This is primarily a server-rendered Django application (Bootstrap + lightweight JS per REQ-WEB-06), not a JSON SPA backend. The endpoints below are the subset that need a request/response contract because they're called via JS (AJAX) rather than rendered as a full page: payment, autosave, and PDF access. Everything else (course listing, blog, dashboard) is standard Django views/templates and isn't enumerated as "API".

No API surface was specified in any source document — every endpoint below is an **[Assumption]** shaped to satisfy a **[Confirmed]** requirement from REQUIREMENTS.md. Exact URL paths are illustrative, not contractual — **except** django-allauth's own installed route prefix and the Google OAuth callback path (below), which are not illustrative once configured: the Google Cloud Console redirect URI must match the deployed callback path exactly, so that specific path is a real contractual constraint, not a placeholder.

## 1. Auth
Handled by `django-allauth`'s standard views (signup, login, logout, password reset). Satisfies REQ-ACC-01, including **mandatory email verification** (`ACCOUNT_EMAIL_VERIFICATION = "mandatory"` — DATA_MODEL.md `User`): a new signup cannot log in until the Brevo-sent verification link is clicked. No custom API needed — allauth's own views cover signup, login, logout, password reset, *and* email verification/resend.

**URLconf:** all of the above, plus Google login below, are served by mounting allauth's own URLs once: `path("accounts/", include("allauth.urls"))`. This single include is the entire routing surface for auth — no custom auth view or URL pattern is written by this project.

### Google Login (REQ-ACC-04 — client-approved post-freeze scope addition, DECISIONS.md D14) — corrected this pass (Codex MEDIUM findings 1–2)
Additive to the above — local signup/login/password-reset are unchanged and remain fully available alongside this.

- `POST /accounts/google/login/` — allauth's standard `socialaccount` login-initiation view. **Not a `GET`:** `SOCIALACCOUNT_LOGIN_ON_GET = False` (SECURITY.md §3) means a bare `GET` to this URL does not start the OAuth handshake — initiation is a CSRF-protected POST. "Continue with Google" is rendered as a POST form/button (`{% provider_login_url 'google' process='login' %}` inside `{% csrf_token %}`), not a plain `<a href>` link. On success, the view redirects the browser to Google's OAuth2 consent screen — server-rendered redirect only, no client-side Google JS SDK, no custom token-handling code written by this project.
- `GET /accounts/google/login/callback/` — allauth's standard callback view (a real `GET`, since this is Google's own redirect back, not user-initiated). **This exact path must match the Google Cloud Console's configured redirect URI** (DEPLOYMENT.md §4) — production is `https://financepsu.guide/accounts/google/login/callback/`. Google redirects here with an authorization code; allauth exchanges it server-side (PKCE, `OAUTH_PKCE_ENABLED = True`), reads the ID token's email + email-verified flag, then:
  - matches to an existing `User` by verified email — Google-provider-scoped trust only (`SOCIALACCOUNT_PROVIDERS["google"]["EMAIL_AUTHENTICATION"] = True`, not a project-wide setting), never creates a duplicate account for an already-registered email, and does not leave a permanent `SocialAccount` connection on that `User` from this match (`SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = False`); or
  - creates a new stock `User` (+ `UserProfile`, same as a local signup) if no match exists — via stock allauth social-signup, with a `SocialAccount` row recording the Google identity as part of that signup (no `SocialToken` persisted, `SOCIALACCOUNT_STORE_TOKENS = False`); or
  - **for an unverified Google-asserted email, never authenticates or links to an *existing* Finance PSU account** — the account-takeover-relevant guarantee; this does not promise stock allauth creates no row at all for an unmatched, unverified identity, only that it can't reach an existing account via email match (ARCHITECTURE.md §3.2a/§3.6, DECISIONS.md D14).
- No other custom endpoint is introduced — allauth's built-in `socialaccount` views are the complete surface for this feature, same "no custom API needed" posture as local auth above.
- **Scopes requested:** `profile`, `email` only — explicitly excludes Gmail, Drive, Contacts, Calendar, or any other Google API scope. Identity assertion only, nothing else.
- **Session behavior:** identical to local login — the single-active-session middleware (ARCHITECTURE.md §3.2) applies the same way to a session established via Google as to one established via local credentials; a second-device Google login invalidates a first session exactly as REQ-ACC-02/03 already specify, no special-cased logic.
- See ARCHITECTURE.md §3.2a/§3.6 for the full flow and failure/cancel behavior, SECURITY.md §3 for the settings block, DEPLOYMENT.md §3–4 for environment/console setup.

## 2. Checkout / Payment (REQ-PAY-01–03) — corrected in D10 and D11, see DECISIONS.md D11.1

### `POST /checkout/create-order/`
Creates a Razorpay order for a course or mock test.
- **Auth:** required (redirect to login if anonymous, per workflow doc §5)
- **Request:** `{ "target_type": "course" | "mock_test", "pricing_plan_id": int }` — `pricing_plan_id` refers to a `PricingPlan` when `target_type="course"`, or a `MockTestPricingPlan` when `target_type="mock_test"`.
- **Server behavior:**
  1. Look up the pricing plan server-side; `amount_paise` and `currency` come **only** from that row, never from the client.
  2. Call Razorpay's Orders API to create a Razorpay-side order, receiving a `razorpay_order_id`.
  3. Create the local `Order` row **before** the student reaches checkout: `course_pricing_plan` or `mock_test_pricing_plan` set (exactly one), `razorpay_order_id` set, `amount_paise`/`currency` set, `status="created"`.
- **Response:** `{ "razorpay_order_id": str, "amount_paise": int, "currency": "INR", "razorpay_key_id": str }` — handed to Razorpay's client-side checkout widget.

### `POST /checkout/webhook/` *(Razorpay → server, not browser → server)* — **corrected flow**

The previous version of this document incorrectly had the webhook look up `Order` by `razorpay_payment_id` — but the local `Order` has `razorpay_payment_id = NULL` until *this very webhook* is what's supposed to set it, so that lookup could never succeed on a first delivery. Corrected:

- **Auth:** none (public endpoint) — integrity comes entirely from signature verification, not from Django auth.

**Canonical sequence (DECISIONS.md D12 — order of operations made internally consistent across docs):**
1. Read the raw request body bytes.
2. Verify the Razorpay HMAC-SHA256 signature against those raw bytes, using `RAZORPAY_WEBHOOK_SECRET`, compared against `X-Razorpay-Signature`.
3. Invalid signature → reject with 400, stop. No JSON parsing, no DB access.
4. Parse the JSON payload (only after signature verification succeeds).
5. Read **only** the top-level `event` field first.
6. **If `event != "payment.captured"`:** return HTTP 200 (ignored, not an error — this tells Razorpay not to retry an event we intentionally don't process), grant nothing, and **do not inspect or rely on `payload.payment.entity.status` for entitlement logic at all** — a `payment.authorized` webhook can be delivered while the payment entity's status already reads `"captured"`, so status is never a safe signal on its own (DECISIONS.md D11.1). `order.paid` and any other event are likewise ignored unless a separate path is explicitly designed later — none exists today.
7. **Only for `event == "payment.captured"`**, continue:
   - Read `payload.payment.entity` and require `entity.status == "captured"` — both the event *and* the status must hold; this is the specific check this sequencing exists to make unambiguous.
   - Look up the local `Order` by `payload.payment.entity.order_id` (`razorpay_order_id` — never by `razorpay_payment_id`, which doesn't exist to look up by until this handler sets it). No matching `Order` → reject with 400 (the "webhook referencing unknown Razorpay order" case; nothing to process).
   - Process inside `transaction.atomic()`, row-locked: `Order.objects.select_for_update().get(razorpay_order_id=...)`, so two near-simultaneous deliveries for the same order can't race each other.
   - Validate `payload.payment.entity.amount == order.amount_paise` and `.currency == order.currency`, both checked against the **immutable, server-set values captured at order-creation time** (`create-order/` above) — never against anything newly read from this webhook call. Mismatch on either → reject with 400, no state change, nothing granted.
   - **Idempotency, branch on `order.status`:**
     - Already `verified`, same `razorpay_payment_id` as this payload → duplicate delivery (Razorpay retries on timeout/non-200 — expected, routine). Return 200 immediately, write nothing.
     - Already `verified`, but this payload's payment id is *different* → a conflicting payment attached to an already-settled order. Grant nothing further, change nothing about the existing (already-correct) grant; log as an anomaly for manual review. Return 200 without any write.
     - Not yet `verified` (`created` or `failed`) → set `razorpay_payment_id`, `status="verified"`, `verified_at=now()`, and — in the same atomic block — create exactly one `Enrollment` (if `course_pricing_plan` is set) or `MockTestEnrollment` (if `mock_test_pricing_plan` is set). The `unique(order)` constraint on both (DATA_MODEL.md §3) means this can never happen twice for the same `Order` even under a bug or a race the row lock didn't fully prevent.
- On invalid signature (step 2–3), reject with 400 and grant nothing (REQ-PAY-02). On any step-7 validation failure (status, order lookup, amount/currency), same: 400, nothing granted, nothing written.
- **Webhook subscription:** Razorpay's dashboard webhook configuration for this endpoint subscribes to `payment.captured` only — that's the one entitlement-granting event this handler acts on (step 6); nothing else needs to reach this endpoint at all, though the `event != "payment.captured"` check (step 6) is kept regardless as defense-in-depth.

## 3. Protected PDF Access & Progress Tracking (REQ-COURSE-02, REQ-COURSE-03, REQ-DASH-01) — loading model corrected this pass, DECISIONS.md D11.4

### `GET /library/chapter/<chapter_id>/signed-url/`
- **Auth:** required; server checks the requesting student has a non-expired `Enrollment` covering `chapter.book.course` (REQ-COURSE-04).
- **Response:** `{ "url": str, "expires_in": 60 }` — a presigned R2 URL, valid 60 seconds. **Wording correction (D11.4):** the short expiry limits how long a leaked URL is usable, not what a "session" is — anyone who obtains the URL can use it until it expires; it doesn't stop working "once viewing starts," it simply times out 60 seconds after issuance either way.
- **Failure:** 403 if not entitled; no PDF object key or URL is ever returned in that case.
- **Server behavior:** on a successful (entitled) call, upserts `UserChapterProgress(user, chapter)`: creates the row if missing, sets `is_completed=True` (first access only — never flips back), and **always** sets `last_accessed_at = now()` (every valid access, not just the first). See DATA_MODEL.md §1 (`UserChapterProgress`) — this single upsert is what both "completed topic count" and "recently accessed course" (REQ-DASH-01, now filtered to active enrollments only — DECISIONS.md D11.5) are computed from.

### Client-side loading behavior (corrected this pass — D11.4, replaces an earlier Range-request design)
The approved stack's 60-second presigned URL is short enough that a design relying on it for the PDF viewer's *entire* reading session — repeated `Range` requests as the student pages through a long document — could break mid-read once it expires. Simpler baseline, staying inside the approved stack (no Cloudflare Workers, no Django byte-streaming, no complex signed-URL rotation):
1. Student opens a chapter → the signed-URL endpoint above runs (entitlement check + `UserChapterProgress` upsert).
2. Browser immediately performs **one complete GET** of the PDF directly from R2, while the URL is still valid.
3. The response bytes (`ArrayBuffer`/`Uint8Array`) are handed to PDF.js in one shot: `pdfjsLib.getDocument({ data })`.
4. All further page navigation/rendering/zoom works off those **in-memory** bytes — PDF.js makes **no further R2 requests** for this document, so the signed URL can expire mid-read without interrupting anything already loaded.
5. If the initial full GET fails *before* the document finishes loading (network blip, or the 60s window lapsed before the fetch completed), the client requests **one** fresh signed URL and retries the initial fetch once — it does not attempt to parse an expired R2 403 response body or retry indefinitely.
6. Reopening the same chapter later (new page load, or after closing the viewer) repeats the whole entitlement-check-plus-fresh-URL sequence from step 1 — no signed URL is ever reused across separate opens.

**Consequences:** R2 stays private, Django never proxies/streams PDF bytes itself, no permanent URL is created anywhere, and the approved 60-second expiry is unchanged — it now bounds only the initial fetch, not the reading session. The known DRM residual-risk note (SECURITY.md §5) still applies unchanged: this deters casual download/print/copy, not a determined screenshot/devtools capture.

## 4. Mock Test Access Authorization — new this pass (DECISIONS.md D10 item 3, REQ-MOCK-05)

The workflow doc says a purchased course gives access to "Notes / MCQs / Mock Tests" — courses can *include* formal Mock Tests, on top of Mock Tests being independently purchasable (REQ-MOCK-02). One centralized helper is the single source of truth for whether a student may access a given `MockTest`:

```
can_access_mock_test(user, mock_test) -> bool:
    return (
        MockTestEnrollment.objects.filter(student=user, mock_test=mock_test).exists()
        or Enrollment.objects.filter(
               student=user,
               expires_at__gt=now(),
               course__coursemocktest__mock_test=mock_test,
           ).exists()
    )
```
**Every** mock-test start/autosave/submit/result route (§5 below) calls this same helper — entitlement logic is never re-implemented or re-approximated in an individual view.

**UX placement [Assumption, not client-frozen — flagged per instruction rather than silently merged]:** standalone-purchased mock tests appear under "My Mock Tests"; course-included mock tests are reachable from inside their purchased course (parallel to how books/chapters live inside the course, not duplicated into "My Mock Tests"). If the client wants course-included tests to *also* show under "My Mock Tests", that's a UI decision to confirm explicitly — nothing in the source documents states it either way, so it isn't assumed here.

## 5. Mock Test Attempts (REQ-MOCK-01, REQ-MOCK-04) — deadline enforcement + row-locking corrected this pass (DECISIONS.md D10, D11.2)

Every route below first calls `can_access_mock_test(user, mock_test)` (§4) — 403 if it returns `False`.

**Server-authoritative deadline:** `deadline = attempt.started_at + attempt.time_limit_minutes`, using the server's own clock at request time. The client never supplies or influences this value — a manipulated browser clock has no effect, since nothing about the deadline check reads from the request.

**Every mutating `Attempt` operation below — autosave, submit, and deadline auto-finalization — shares one locking discipline (corrected this pass, DECISIONS.md D11.2):**
```python
with transaction.atomic():
    attempt = Attempt.objects.select_for_update().get(id=attempt_id, student=request.user)
    # ... ownership check, then submitted_at check, then deadline check,
    # then the write (answer upsert, or finalize+score) — all while still holding the lock
```
The row lock is acquired **before** anything else — before checking `submitted_at`, before computing/checking the deadline, before an answer is accepted, before auto-finalizing, before manual scoring. This is what makes the deadline and idempotency guarantees below actually hold under concurrent requests, not just in the common single-request case: two near-simultaneous autosave/submit calls for the same attempt serialize through Postgres's row lock rather than racing each other. No Celery/Redis/background worker — the lock is held only for the duration of one short request.

### `POST /mocktest/<mock_test_id>/start/`
- **Auth:** required; `can_access_mock_test` check (§4).
- **Response:** `{ "attempt_id": int, "time_limit_minutes": int, "questions": [...] }` (correct answers withheld from the payload).

### `POST /mocktest/attempt/<attempt_id>/autosave/`
Inside the lock (above), in order:
1. Lock `Attempt`, verify it belongs to the requesting student (403 otherwise).
2. Compute the server's `now()` and `deadline` from the locked row's `started_at`/`time_limit_minutes`.
3. If `submitted_at` is already set (finalized, by a prior submit or a prior deadline auto-finalize) → reject (409), return the stored final state, no write.
4. If `now() >= deadline` (and not yet submitted) → **auto-finalize right here**: set `submitted_at = deadline`, compute `score` from whatever `AttemptAnswer` rows already exist as of this moment inside the lock — the incoming answer this request was trying to save is **rejected, not saved**, since it arrived after the deadline. Respond 409 with the now-final score. This is the one documented path by which an attempt becomes "expired": lazy, triggered by whichever mutating request first arrives past the deadline **and obtains the lock** — if an autosave was already waiting on the lock when the deadline passed, it is still evaluated *after* acquiring the lock, so it correctly lands in this branch and is rejected, not accepted.
5. Otherwise (before deadline, unsubmitted): validate `question.mock_test_id == attempt.mock_test_id`, then upsert one `AttemptAnswer` row (unique on `attempt`+`question`, DATA_MODEL.md §3).
6. Commit.
- **Request:** `{ "question_id": int, "selected_option": "A"|"B"|"C"|"D"|null }`. **Response:** `{ "saved_at": iso8601 }` (success) or 409 with final state (steps 3–4).

### `POST /mocktest/attempt/<attempt_id>/submit/`
Inside the same lock discipline, in order:
1. Lock `Attempt`, verify ownership (403 otherwise).
2. If `submitted_at` is already set (from a prior manual submit *or* a prior deadline-triggered auto-finalize via autosave, above) → **return the already-stored `{score, total_questions}` immediately — no recomputation, no further write.** This is what makes two simultaneous `submit` calls produce exactly one final score: only the call that wins the row lock race actually scores; the other blocks until the lock is released, then finds `submitted_at` already set and returns the same stored result.
3. Otherwise: compute `deadline`, set `submitted_at = min(now(), deadline)`, compute `score` from the `AttemptAnswer` rows already persisted (the lock held since step 1 means no concurrent autosave could have mutated them mid-computation), save `score`/`total_questions`.
4. Commit.
- **Response:** `{ "score": int, "total_questions": int }`

## 6. Practice MCQs (REQ-COURSE-05 — REQUIREMENTS.md §7 item 8, client-frozen)

Stateless, untimed, per-chapter — distinct from the Mock Test attempt flow in §5. No `Attempt`/`AttemptAnswer` row is written.

### `GET /course/chapter/<chapter_id>/practice-mcqs/`
- **Auth:** required; same entitlement check as the PDF signed-URL endpoint (REQ-COURSE-04) — a student must have an active `Enrollment` covering this chapter's course.
- **Response:** `{ "questions": [{ "id": int, "text": str, "option_a": str, ... }] }` (correct answers withheld).

### `POST /course/chapter/<chapter_id>/practice-mcqs/<question_id>/check/`
- **Auth:** required, same entitlement check.
- **Request:** `{ "selected_option": "A"|"B"|"C"|"D" }`
- **Response:** `{ "correct": bool, "correct_option": str, "explanation": str|null }` — returned immediately, no attempt record created.

## 7. Admin (REQ-MOCK-03 — REQUIREMENTS.md §7 item 4, client-frozen)

Django's built-in admin site (`django.contrib.admin`) is the confirmed, final interface for all CRUD: Courses, Books, Chapters (including PDF upload — below), Questions (both Mock Test and Practice MCQ, entered manually — no bulk import, per REQUIREMENTS.md §7 item 5), MockTests, `CourseMockTest` links, BlogPosts, and viewing/adjusting a student's `Enrollment` rows. No custom admin API or custom-branded admin UI will be built — this is a closed decision, not an open option.

### Chapter PDF upload (DECISIONS.md D10 item 8 — admin PDF-upload workflow)
- The Chapter admin change form carries a **non-model file upload field** (a plain `forms.FileField` on the `ModelAdmin`'s form — *not* a Django model `FileField`/`ImageField` on the `Chapter` model itself, since `Chapter.pdf_object_key` is a plain `CharField`, not a storage-backed field; DATA_MODEL.md `Chapter`).
- On save, the admin's `save_model` (or the form's `save()`) validates the upload server-side (content-type/extension must be PDF; a sane size ceiling is enforced — the exact number isn't specified by any source document, so it's an implementation-time detail rather than a frozen requirement), then uploads it via `boto3` directly to the private R2 bucket at a deterministic key (e.g. `chapters/<chapter_id>.pdf`), and stores only that key string into `Chapter.pdf_object_key`.
- **Replacing a chapter's PDF** = uploading a new file through the same field; because the key is deterministic per chapter, this simply overwrites the existing R2 object — no orphaned-object cleanup needed, no second object key to reconcile.
- **No public URL is ever generated, stored, or exposed** — the only way to read the PDF's bytes is the 60-second presigned URL from §3, gated by the same entitlement check as everything else.

## 8. Receipts — terminology (REQUIREMENTS.md §7 item 7, client-frozen; reconciled this pass, see DECISIONS.md D10)

- **Razorpay** sends its own payment/gateway receipt automatically — nothing built by this project.
- **Brevo** (the project's transactional mailer) sends Finance-PSU-specific emails only: signup email verification (§1 above), password reset/update, and optionally a plain purchase-confirmation / "access granted" notice. **None of these is a tax receipt, GST invoice, or statutory document** — they're plain confirmation emails, never labeled or treated as one.
- No custom GST computation, tax ledger, or PDF tax-invoice generation exists anywhere in this system (REQUIREMENTS.md §5 Out of Scope).

## 9. Explicitly Out of Scope

- GST/tax receipt generation (REQUIREMENTS.md §7 item 7) — payments rely on Razorpay's own automated receipts, and Brevo's purchase-confirmation email is not a substitute for one; no custom invoice/receipt endpoint exists.
- Bulk question import (CSV/Excel/JSON) (REQUIREMENTS.md §7 item 5) — no import endpoint; Django Admin manual entry only.
