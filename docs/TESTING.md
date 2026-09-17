# TESTING.md — Finance PSU Learning Platform

No test strategy was specified in any source document. This plan is derived to give each confirmed requirement in REQUIREMENTS.md a verifiable check, sized to fit the 14-day delivery window — depth is prioritized toward money-handling and access-control paths, not exhaustive coverage everywhere.

## 1. Priority Tiers

**Tier 1 — must be automated and passing before handover** (money, access control, data loss risk):
- Payment verification (REQ-PAY-02): webhook signature verified against the **raw body**; `Order` looked up by `razorpay_order_id` (never `razorpay_payment_id`); amount/currency validated against the immutable server-captured `Order` values; no `Enrollment`/`MockTestEnrollment` created on an unverified/tampered/mismatched webhook.
- Payment idempotency (REQ-PAY-02): a duplicate webhook delivery for an already-verified `Order`+matching `razorpay_payment_id` never creates a second grant; a conflicting payment id on an already-verified `Order` grants nothing further.
- Entitlement checks (REQ-COURSE-04, REQ-DASH-02/03, REQ-MOCK-05): a student cannot view another student's course/mock-test/PDF via direct URL; `can_access_mock_test` correctly authorizes both the standalone and course-included paths.
- Single active session (REQ-ACC-02): second login invalidates the first session.
- Mandatory email verification (REQ-ACC-01): an unverified signup cannot log in.
- Google Login (REQ-ACC-04, DECISIONS.md D14): verified-email matching never creates a duplicate `User`; an unverified Google email never authenticates; local login/signup/password-reset remain fully functional after Google login is added; single-active-session enforcement applies identically across both login paths.
- Mock test scoring (REQ-MOCK-01): score computed correctly against `Question.correct_option`.
- Mock-test deadline enforcement (REQ-MOCK-04): no answer can be saved, and no score can include an answer, after the server-computed deadline — regardless of client clock.
- Autosave correctness (REQ-MOCK-04): answers saved mid-attempt survive a simulated disconnect/reload.

**Tier 2 — automated where practical, manual QA acceptable given the timeline:**
- Course/mock-test listing and detail rendering (REQ-WEB-03/04).
- Dashboard figures (REQ-DASH-01) match underlying enrollment/attempt data, including "recently accessed course".
- PDF viewer: page nav, zoom, fullscreen, presence of a working print control, and absence of a download control (REQ-COURSE-02/03).
- Admin PDF upload path (API.md §7): stores only a private object key, never a public URL.

**Tier 3 — manual QA only, given the 14-day scope:**
- Responsive layout across breakpoints (REQ-WEB-06).
- Blog listing/detail (REQ-WEB-05).
- Support/WhatsApp redirect (REQ-WEB-02).

## 2. Automated Tests (Django `TestCase` / `pytest-django`)

### Unit
- `Order`/`Enrollment`/`MockTestEnrollment` state transitions: `created` → `verified` only via a valid, matching webhook; `unique(order)` on both enrollment tables confirmed to prevent a second row.
- `Attempt` scoring logic against a fixed `AttemptAnswer` set with known-correct answers.
- `UserProfile.active_session_key` invalidation logic on new login: second login overwrites the key; the first session's next request hits `session.flush()` + `logout()` + redirect to `/login/?error=session_conflict`.
- Presigned URL generation: confirm entitlement check runs before a URL is minted, and confirm no URL is returned for an unentitled student.
- Practice MCQ answer-check (REQ-COURSE-05): correct/incorrect feedback matches `Question.correct_option`; confirm no `Attempt`/`AttemptAnswer` row is written for a practice check.
- **`Question` DB `CheckConstraint` (REQ-MOCK-03/REQ-COURSE-05, DECISIONS.md D11.8):** attempting to save a `Question` with both `mock_test` and `chapter` set raises a DB-level constraint failure; attempting to save one with neither set also fails; exactly one set saves successfully. This is now a schema-level test (constraint violation), not application-logic validation.
- `UserChapterProgress` upsert (REQ-DASH-01): opening a chapter's signed-URL endpoint creates or flips `is_completed=True` exactly once (no duplicate rows on repeat views), and updates `last_accessed_at` on **every** valid access, not just the first.
- `can_access_mock_test(user, mock_test)` (REQ-MOCK-05): true for a standalone `MockTestEnrollment`; true for an active course `Enrollment` where the test is linked via `CourseMockTest`; false otherwise (including an *expired* course `Enrollment`).
- Signup cannot authenticate before clicking the email verification link; can after (REQ-ACC-01).

### Integration — Payment (REQ-PAY-02, DECISIONS.md D10 + D11.1)
- **Valid first webhook:** order created → checkout → signature-valid `payment.captured` webhook, `payment.entity.status == "captured"`, referencing the correct `razorpay_order_id` → `Order` transitions to `verified`, `Enrollment`/`MockTestEnrollment` created, dashboard reflects it.
- **`payment.authorized` with a captured-looking payment entity → NO entitlement (D11.1):** a validly-signed webhook whose `event == "payment.authorized"` but whose `payload.payment.entity.status == "captured"` must **not** create any `Enrollment`/`MockTestEnrollment` and must not mark the `Order` verified — the handler must reject on `event` alone, before ever inspecting `status`. This is the exact bug this pass fixes (an earlier draft granted access on status-or-event).
- **Duplicate webhook:** the same valid webhook delivered twice (simulating Razorpay's at-least-once retry) → only one `Enrollment` exists; second delivery returns 200 and writes nothing further.
- **Invalid signature:** tampered body/signature → 400, `Order` stays `created`, nothing written.
- **Amount mismatch:** valid signature, but `payload.payment.entity.amount != Order.amount_paise` → 400, nothing granted, `Order` stays unverified.
- **Currency mismatch:** valid signature, `currency != Order.currency` (e.g. not `"INR"`) → 400, nothing granted.
- **Webhook referencing unknown Razorpay order:** valid signature, but `razorpay_order_id` in the payload matches no local `Order` → 400, nothing to process, nothing written.
- **Conflicting payment ID on an already-verified Order:** a second, *different* `razorpay_payment_id` delivered for an `Order` already `verified` with a different one → no further grant, existing `Enrollment` unchanged, anomaly logged (not silently accepted).

### Integration — Mock Test Deadline & Concurrency (REQ-MOCK-04, DECISIONS.md D10 + D11.2)
- Answer submitted **before** `deadline` → accepted, `AttemptAnswer` row written/updated.
- Answer submitted **after** `deadline` → rejected; attempt auto-finalized on this same request (`submitted_at` set, score computed from answers saved before the deadline only).
- **Client clock manipulation does not extend the test:** a request carrying a manipulated/absent client timestamp has no effect — the deadline check uses only the server's own clock and the stored `started_at`/`time_limit_minutes`.
- **Duplicate answer autosave updates one row, never creates two:** repeated autosave calls for the same (attempt, question) upsert the same `AttemptAnswer` row (`unique(attempt, question)`, DATA_MODEL.md §3).
- **Submit twice does not rescore differently:** a second `submit` call on an already-finalized attempt returns the identical stored `{score, total_questions}`, performs no recomputation, and makes no further writes.
- **Concurrent autosave + submit race cannot produce a post-finalization write (D11.2):** fire an autosave and a submit for the same attempt at effectively the same time (e.g. two threads/transactions both attempting `select_for_update()` on the same `Attempt` row) — whichever acquires the lock first determines the outcome, and the loser, once it proceeds, must observe the now-finalized state and write nothing further (no `AttemptAnswer` written after finalization).
- **Two simultaneous `submit` calls produce exactly one final score (D11.2):** both calls target the same unsubmitted attempt at once; only one computes and stores the score, the other (after acquiring the lock second) finds `submitted_at` already set and returns the identical stored result — never two different scores, never two writes.
- **An autosave that was waiting on the lock and only obtains it after the deadline is rejected (D11.2):** simulate an autosave blocked behind another transaction's lock on the same `Attempt`, where the deadline passes while it waits — once it finally acquires the lock, it must still be evaluated against the (now-passed) deadline and rejected, not accepted just because it was "in flight" before the deadline.

### Integration — Google Login (REQ-ACC-04 — client-approved post-freeze scope addition, DECISIONS.md D14) — corrected this pass (Codex MEDIUM/LOW findings)
All fifteen tests below use **mocked Google/OAuth responses** (stubbed authorization-code exchange and ID-token payload) — no real network call to Google's servers is made in a normal test run, consistent with the rest of this suite's approach to third-party integrations (Razorpay tests likewise mock the webhook payload, not a live Razorpay call). Fixture note: assertions below distinguish two cases explicitly — **Case A** (existing local `User`, Google-verified-email match) and **Case B** (brand-new student, first-ever Google signup) — since they have different expected `SocialAccount` row outcomes; do not conflate them into one generic "Google login" fixture.

1. **[Case A] Verified-email match authenticates the existing local `User`, no permanent `SocialAccount` connection added:** a student who already has a local account signs in via Google using the same, Google-verified email → authenticated into the same existing `User` row; confirm no second `User` is created, and confirm no `SocialAccount` row persists connecting that `User` to the Google identity afterward (`SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = False` — the match authenticates but does not auto-connect).
2. **[Case B] No existing match creates a new stock `User` plus a `SocialAccount` row:** a Google-verified email with no matching local `User` → a new `User` (+ `UserProfile`) is created via stock allauth social-signup, same shape as a local signup would produce, **and** a `SocialAccount` row recording the Google identity is created as part of that signup — confirm this row exists (it is the expected outcome for a new Google signup, distinct from Case A).
3. **Unverified Google email never authenticates or links to an *existing* account (narrowed scope — Codex MEDIUM finding 3):** the mocked ID token asserts `email_verified: false`, and a local `User` already exists with that same email → the existing `User` is **not** authenticated and **no** `SocialAccount` link is made to it; the student is returned to a safe login state. This test asserts the existing-account guarantee specifically — it does not assert that stock allauth can create zero rows for an unmatched, unverified identity in general (no such blanket claim is made; `SOCIALACCOUNT_AUTO_SIGNUP` keeps its default and no custom adapter was added to change that).
4. **Local login remains fully functional after Google login is added:** a plain username/password login succeeds exactly as before — this is a regression check that REQ-ACC-04 is additive, not a replacement.
5. **Local signup remains fully functional:** a new student can still sign up locally (username/email/password) and complete mandatory email verification (REQ-ACC-01), unaffected by the Google login code path existing.
6. **Local password reset remains fully functional:** the existing allauth password-reset flow is unaffected by the Google provider being installed.
7. **Second Google login on another device invalidates the first session:** a student logs in via Google on device A, then via Google again on device B → device A's next request lands on `/login/?error=session_conflict`, identical to REQ-ACC-02/03's existing behavior for local logins.
8. **Cross-path session invalidation — Google login invalidates a local-login session, and vice versa:** a student logs in locally on device A, then via Google on device B (same underlying `User`) → device A's session is invalidated the same way; and the reverse (Google first, then local) also invalidates correctly. Proves single-active-session enforcement is per-`User`, not per-login-method.
9. **Cancel at Google's consent screen returns safely, no local authentication:** the mocked flow simulates a user-cancelled consent (no authorization code ever returned to the callback) → no local authentication occurs for the initiating browser, and no `User`/`UserProfile`/`SocialAccount` row exists from this attempt; student lands back on the login page.
10. **Google provider error does not authenticate and does not break local login:** the mocked token exchange returns a provider-side error (5xx / malformed response) → no local authentication occurs; a subsequent local username/password login in the same test still succeeds, proving the two paths are independent.
11. **Invalid/tampered OAuth `state` or callback parameters are rejected:** a mocked callback request with a mismatched/invalid `state` value → rejected by allauth's own CSRF/state validation, no authentication, no account mutation.
12. **No Google token is ever persisted, distinct from the `SocialAccount` relationship:** after a successful mocked Google login (either Case A or Case B), query allauth's `SocialToken` table → confirm no row exists for this login (`SOCIALACCOUNT_STORE_TOKENS = False`), proving no access/refresh token is stored anywhere. This is a separate assertion from the `SocialAccount` row checks in tests 1–2 — token non-persistence and the identity-relationship row are not the same thing and must not be conflated in the fixture.
13. **Missing/invalid Google OAuth credentials fail closed, not open:** with `GOOGLE_OAUTH_CLIENT_ID`/`GOOGLE_OAUTH_CLIENT_SECRET` unset or invalid in the test settings → the Google login path errors as a configuration failure (and is logged as such) — it never falls back to authenticating the request some other way.
14. **Google login initiation requires POST, not GET:** a plain `GET /accounts/google/login/` (no form submission) does not redirect to Google's consent screen and does not start the OAuth handshake, consistent with `SOCIALACCOUNT_LOGIN_ON_GET = False`; a CSRF-protected `POST` to the same URL does.
15. **No duplicate `User` across repeated Google logins:** the same Google identity logs in via Google three times in a row (simulating a returning student) → the first login is Case B (new `User` + `SocialAccount` created); the second and third logins recognize the existing `SocialAccount` and authenticate through it directly, creating no additional `User` or `SocialAccount` row — exactly one of each exists throughout, never a duplicate.

### Integration — Other
- Cross-student access attempt: Student A requests Student B's chapter PDF signed-URL endpoint or mock-test attempt → assert 403/redirect, not content.
- **Recently accessed course, basic case** (REQ-DASH-01): a student with `UserChapterProgress` rows in two different active courses, where the most recent `last_accessed_at` belongs to a chapter in course B, sees course B (not course A) as "recently accessed" on the dashboard — proves the derivation actually uses the *latest* access, not just any access.
- **Recently accessed course, expired-enrollment exclusion (D11.5):** the newest `UserChapterProgress` row belongs to a course whose `Enrollment.expires_at` has passed; an older row belongs to a course with an active `Enrollment` → the **active** course is shown as recently accessed, not the newer-but-expired one.
- **Recently accessed course, all expired (D11.5):** every course the student has ever accessed now has an expired `Enrollment` → "recently accessed course" is empty/null, not a stale expired course.
- **PDF loads fully before viewer initialization, survives URL expiry (replaces the earlier Range-request test — D11.4):**
  - A valid presigned URL, fetched with a plain GET from the production origin, returns the complete PDF successfully.
  - The complete response is loaded into PDF.js (`getDocument({ data })`) before the viewer renders its first page — not a partial/streamed load.
  - After the bytes are loaded, simulate the signed URL's 60-second expiry (e.g. mock the URL as now-invalid) and confirm page navigation within the already-loaded document continues to work — no further request to R2 is made or needed.
  - A failed initial fetch (before the document finishes loading) triggers exactly **one** fresh signed-URL request and one retry of the full GET — not a retry loop, and not an attempt to parse an expired R2 403 response body.
- **Cache bypass:** an authenticated/personalized route (e.g. dashboard) is requested twice through Cloudflare and confirmed **not** served from edge cache the second time (`Cf-Cache-Status` header, or equivalent) — while a public route (e.g. course listing) is confirmed cacheable.

## 3. Load Testing (REQ-OPS-04)

The invoice frames 10,000-concurrent-user capacity as something to be **verified** through simulated load testing before scale launch, not guaranteed by design. No source document prescribes numeric pass/fail thresholds (an earlier draft of this document proposed some on its own initiative — retracted per explicit client instruction; see DECISIONS.md D7), so none is asserted here. Plan:
1. Tool: any standard HTTP load tool (e.g. Locust or k6) driving a realistic traffic mix — mostly cached catalog/homepage reads, a smaller slice of authenticated dashboard/PDF/mock-test traffic, matching the platform's actual described usage pattern.
2. Output: a report of measured concurrent-user capacity, latency, and error rate at increasing load, plus identified bottlenecks (DB connections, Gunicorn worker count, VM CPU/RAM ceiling). This report — not a pre-set numeric bar — is what determines whether the platform is ready for the 10,000-user public launch.
3. **Timing (resolved — REQUIREMENTS.md §7 item 6):** this load test runs immediately post-handover, as an operational gate before public marketing/registration launch — not required inside the 14-day build window.

## 4. Manual QA Checklist (traces to workflow doc, section by section)

- [ ] Homepage nav → each of Logo/Courses/Mock Tests/Blog/Support/Login routes correctly.
- [ ] Course listing → details → plan selection → price updates → Enroll Now.
- [ ] Enroll Now while logged out → redirected to login/signup → returned to checkout after auth.
- [ ] Signup requires clicking an emailed verification link before login succeeds.
- [ ] Login page shows both the local credential form and a "Continue with Google" option; both paths work independently (REQ-ACC-04).
- [ ] Google login with an account whose email matches an existing local account signs into that same account, not a new one.
- [ ] Cancelling at Google's consent screen returns to the login page cleanly, with local login still usable afterward.
- [ ] Successful payment (test mode) → course appears in My Courses immediately.
- [ ] Dashboard shows correct purchased courses, purchased mock tests, progress, and recently accessed course.
- [ ] Course opens as book list → book opens as chapter list → chapter opens PDF viewer.
- [ ] PDF viewer: page navigation, zoom in/out, fullscreen all functional; no download button present; right-click save is not offered; Ctrl+P / the browser's print control opens normal print behavior (client-approved, D15) — no test asserts that the browser/OS print dialog's own "Save as PDF" output can be reliably blocked; selecting/copying viewer text does not work. Open a chapter, read for over 60 seconds (past presigned-URL expiry) — confirm page navigation is unaffected, since the document loaded fully into memory up front (D11.4).
- [ ] Mock test: timer visible and counting down; answers autosave; submit produces a result; timeout auto-submits; answers can't be changed after the timer visually reaches zero.
- [ ] A course-included mock test (via `CourseMockTest`) is reachable from inside its purchased course, without a separate mock-test purchase.
- [ ] Second login on another browser/device: first session's next request lands on `/login/?error=session_conflict`, not a silent failure.
- [ ] Practice MCQ (per chapter): selecting an option gives immediate correct/incorrect feedback, no timer or submit step.
- [ ] Admin can create a course, a book, a chapter (uploading a PDF through the admin form — confirm only a private object key is stored, no public URL), a mock test, a `CourseMockTest` link, and questions (all via Django Admin — no bulk import present, by design).
- [ ] Admin authorization confirmed via native `is_staff`/`is_superuser` — no custom "is_admin" field exists to check.

## 5. Security Regression Checks

Covered in SECURITY.md §10 — treated as part of the pre-launch gate, not a separate test suite.
