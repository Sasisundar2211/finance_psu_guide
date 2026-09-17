# REQUIREMENTS.md — Finance PSU Learning Platform

## 1. Source Documents

All requirements below are derived exclusively from these three documents (Google Drive, folder "Finance psu guide"), plus the tech stack and 14-day delivery constraint given directly by the client in this session:

| Document | File ID | Role |
|---|---|---|
| `Finance_PSU_Invoice_Vasu_With_Fee_Breakdown.pdf` | `1LPl2TTTVfYYEaJn5NYc-L79B0e6mAZ-T` | Commercial scope — the contractual deliverable list and fee breakdown |
| `Finance PSU Guide workflow.docx` | `1MDUsMe0QLk8licrACyToA5BSO-C3vv4v` | Functional workflow — page-by-page and step-by-step behavior |
| `technical_stack_specification.pdf` | `13NBxRfx-Llq5t8L2HAzCqFZ37cIw2RGP` | Approved technology stack (client-confirmed; see DECISIONS.md for why the other two stack drafts found in Drive were rejected) |

**Client:** Vasu (billed party) · **Developer:** Sasisundar (sasisundhar2211@gmail.com) · **Invoice:** #2026-001, dated 11 Sep 2026, due on handover · **Total fee:** INR 25,000 (one-time) · **Delivery window:** 14 days (constraint stated by the client in this session; no start date given — see §4 NFR-05).

## 2. Project Summary

Finance PSU is an online learning and mock-test platform for PSU/government accounting exam candidates (AAI, SPMCIL, CONCOR, MECL, PFRDA, CMA, etc.). Students browse and purchase courses, read protected PDF study material in-browser (no download), and take timed MCQ mock tests. Target: 10,000 concurrent users (capacity to be verified by load testing before launch at that scale, per the invoice — not a hard guarantee at handover).

## 2.1 Top-Level Product Principles

Four top-level principles the client has explicitly confirmed govern this build. Each is detailed elsewhere in this document and cross-referenced rather than duplicated here:

- **Capacity** — 10,000 concurrent users is the production target, subject to verification by load testing before launch at that scale, not an unconditional guarantee (NFR-01, REQ-OPS-04).
- **Single active session** — one `User` account has exactly one active authenticated session at a time; a successful login elsewhere invalidates the prior session on its next request, identically whether either session is local-credential or Google-authenticated (REQ-ACC-02, REQ-ACC-03).
- **Protected PDFs** — no normal Download control is ever exposed in the viewer; printing is intentionally permitted (client-approved post-freeze change, DECISIONS.md D15); text-copy deterrence remains (REQ-COURSE-03).
- **Long-term maintainability** — the system favors supported/LTS software, routine upgrades, and a simple, conventional, portable architecture over a 5–10 year operational lifetime, rather than freezing today's exact versions (NFR-07, DECISIONS.md D16).

## 3. Functional Requirements

Each requirement is tagged **[Confirmed]** (stated explicitly in a source document), **[Resolved implementation baseline]** (a specific implementation choice settled by client freeze or engineering decision where the source document didn't specify one — see DECISIONS.md), **[Assumption]** (reasonable inference needed to make a confirmed requirement buildable, not explicitly stated, not yet elevated to a resolved baseline), or **[Client-approved post-freeze scope addition]** (new scope, or a change to previously-frozen scope, the client explicitly approved *after* the `docs-baseline-v1` freeze — never a silent reinterpretation of a frozen item; currently REQ-ACC-04, the REQ-COURSE-03 print-policy change (DECISIONS.md D15), and NFR-07 (DECISIONS.md D16)). **There are currently no unresolved/open requirements in the frozen baseline** — §7 records the resolution history for what was originally open, not a live open-items list (DECISIONS.md D9–D12); post-freeze additions/changes are tracked separately via this tag rather than reopening §7.

### 3.1 Public Site & Navigation (Invoice: "Website & Student Experience", INR 3,500)

- **REQ-WEB-01** [Confirmed] Homepage with header navigation: Logo (→ home), Courses, Mock Tests, Blog, Support/Chat, Login/Signup.
  *Acceptance:* All six nav items present and route correctly on desktop and mobile.
- **REQ-WEB-02** [Confirmed] Support/Chat link redirects to the client's WhatsApp group/support link.
  *Acceptance:* Clicking Support opens the WhatsApp link in a new tab.
- **REQ-WEB-03** [Resolved implementation baseline — client-retained during planning; provenance fix, DECISIONS.md D10 item 10] Course Listing page: cards showing image/thumbnail, name, short description, price, duration, PDF/chapter count, MCQ count, mock test count, "View Details" and "Enroll Now" buttons.
  *Provenance note:* the workflow doc's own wording is "Each course card **can contain**:" — an illustrative example, not a mandatory list. The build retains the full illustrative list as its actual baseline (nothing here was dropped), but the tag reflects that this is a client-retained implementation choice, not something the source document flatly mandated.
  *Acceptance:* Each published course renders a complete card with all listed fields. Mock test count is derived from `CourseMockTest` (DATA_MODEL.md) — see REQ-MOCK-05.
- **REQ-WEB-04** [Confirmed] Course Details page: name, full description, thumbnail, subjects/topics covered, study-material/PDF/MCQ/mock-test counts, validity options (e.g. 3/6/12 months), pricing per plan, "Enroll Now" CTA.
  *Acceptance:* Selecting a validity plan updates displayed price before enrollment.
- **REQ-WEB-05** [Confirmed] Blog: listing page and detail page for articles/study updates.
  *Acceptance:* Published blog posts are listable and individually viewable via a permalink.
- **REQ-WEB-06** [Confirmed] Responsive, mobile-friendly UI built with Bootstrap and lightweight JavaScript (no heavy SPA framework).
  *Acceptance:* All pages usable at 360px–1920px widths via manual QA across common breakpoints. No source document specifies a numeric performance/usability score target, so none is asserted here.
- **REQ-WEB-07** [Confirmed] Login/Signup pages, reachable from nav and from the enrollment flow.
  *Acceptance:* A logged-out user attempting "Enroll Now" is routed to Login/Signup, then returned to checkout on success (per workflow doc §5). **[Client-approved post-freeze scope addition — REQ-ACC-04, DECISIONS.md D14]** Both the Login and Signup pages present the local username/password form **and** a "Continue with Google" option, side by side — deliberate redundancy for account portability, not a replacement of one by the other; every existing local-auth feature (signup, login, logout, password reset) continues to work exactly as before Google login was added.

### 3.2 Student Dashboard (Invoice: "Website & Student Experience")

- **REQ-DASH-01** [Resolved implementation baseline — client-retained during planning; provenance fix, DECISIONS.md D10 item 10] Dashboard overview: purchased courses, purchased mock tests, recently accessed course, course progress (%), completed topic count, upcoming/available mock tests.
  *Provenance note:* the workflow doc says the dashboard "should display an overview of all purchases" (firm) but then introduces the specific field list with "**It can show:**" — illustrative, not mandatory. The build implements the full illustrative list as its baseline; tag reflects that as a retained choice, not a flat source mandate.
  *Acceptance:* Figures match the student's actual enrollment and attempt records. Course progress (%) and completed topic count are computed from `UserChapterProgress` (DATA_MODEL.md) — `is_completed=True` rows for that course ÷ total chapters in the course. **"Recently accessed course"** is derived from the same table: the `course` belonging to whichever `UserChapterProgress` row has the latest `last_accessed_at`, **restricted to courses the student currently has an active (non-expired) `Enrollment` for** (DATA_MODEL.md, API.md §3) — this was previously unspecified (no field tracked access time), and the active-enrollment restriction is itself a correction from a later pass (DECISIONS.md D11.5): an expired course must never surface as "recently accessed" merely because its access history is newest. If no accessed course currently has active access, the field is empty/null.
- **REQ-DASH-02** [Confirmed] "My Courses" section lists only courses the student has purchased; each links to "Continue Learning".
  *Acceptance:* A course not purchased by the logged-in student never appears or is reachable via direct URL (see REQ-COURSE-04).
- **REQ-DASH-03** [Confirmed] "My Mock Tests" section lists only mock tests the student has purchased.
  *Acceptance:* Same isolation guarantee as REQ-DASH-02.
- **REQ-DASH-04** [Confirmed] Profile section (named explicitly in the invoice: "Student Dashboard with My Courses, My Mock Tests and Profile"; not detailed in the workflow doc).
  *Acceptance:* [Resolved — §7 item 3] Profile displays the account's phone number (`UserProfile.phone_number`, editable) and the account email (from the base `User` model); password changes go through django-allauth's own security forms rather than a custom flow.

### 3.3 Courses & Protected Study Materials (Invoice: INR 4,000)

- **REQ-COURSE-01** [Confirmed] Course → Book/Subject → Chapter/Topic hierarchy ("book-style" library interface).
  *Acceptance:* A purchased course opens as a list of books; each book opens to a list of chapters. [Resolved — DECISIONS.md D10 item 11] `Chapter` is the persisted leaf content/topic unit for this build — the invoice's "chapter/topic" and the workflow doc's interchangeable use of the two terms are both represented by the single `Chapter` model (DATA_MODEL.md); there is no separate `Topic` table, and none is silently implied anywhere else in this doc set.
- **REQ-COURSE-02** [Confirmed] In-site PDF reader per chapter: page navigation, zoom in/out, fullscreen.
  *Acceptance:* A student can open any chapter's PDF, navigate all pages, zoom, and go fullscreen without leaving the site. Chapter PDFs reach R2 via an admin-only upload path (Django Admin, boto3, private bucket — API.md §7); no public PDF URL is ever generated.
- **REQ-COURSE-03** [Confirmed baseline for no-download/text-copy deterrence; **print policy is a client-approved post-freeze change, DECISIONS.md D15, superseding the print-blocking baseline previously set in D11.6**] No normal download control, and no text copy: no download button; normal browser download/save is not offered by the reader UI; the canvas-rendered text layer's copy/select is unbound. **Printing is intentionally permitted** — the reader does not intercept or block the browser/OS print action.
  *Provenance split:* the invoice/workflow doc mandate only "no normal download button" / online-only viewing. The approved technical stack additionally describes stripping text-copy paths, which remains part of this build's scope; don't read this line as "the invoice mandated text-copy blocking" either — it didn't. Printing was originally included in that same stripped-paths description and blocked accordingly (D9/D11.6); the client explicitly reversed that specific piece after the `docs-baseline-v2` freeze (DECISIONS.md D15). The no-download-button requirement is unchanged and remains invoice-mandated.
  *Acceptance:* No download control is present in the viewer; the PDF is not linked as a direct downloadable file; a print control is present and usable in the reader, with no print interception applied (no `beforeprint` blocking, no canvas-blanking before printing); selecting/copying text out of the viewer does not work (PDF.js text layer disabled, or its `user-select` and `copy` event explicitly unbound). **Accepted limitation (DECISIONS.md D15):** because printing is enabled, the browser/OS print dialog's own "Save as PDF" destination cannot be reliably prevented — this is not asserted as blocked anywhere in this doc set. *(Note: this remains deterrence, not DRM — see SECURITY.md §5 for the residual risk this build does and does not cover.)*
- **REQ-COURSE-04** [Confirmed] Course access is scoped strictly to what the student purchased (per validity plan).
  *Acceptance:* Expired-validity or non-purchased courses return an access-denied state, not the content.
- **REQ-COURSE-05** [Confirmed] Topic-wise MCQ practice as course content, distinct from a formal Mock Test: course listing/detail pages show an "MCQ count" separately from a "mock test count", and the example course card in the workflow doc lists "Topic-wise MCQs" and "Mock Tests" as separate bullet points.
  *Acceptance:* [Resolved — §7 item 8] Both draw from the same `Question` table. Practice MCQs are accessed per-chapter, are untimed, and give immediate per-question (correct/incorrect) feedback on option selection — no attempt record, no timer, no autosave. Mock Tests remain the timer/autosave/deferred-results flow (REQ-MOCK-01).

### 3.4 Accounts & Security (Invoice: INR 3,500)

- **REQ-ACC-01** [Confirmed] Login/Signup with credential-based auth, email verification, and account recovery (password reset). The approved stack doc names "email confirmations" as part of django-allauth's role — this is registration email verification, not just password-reset email, and both must be tested explicitly (TESTING.md), not just the latter.
  *Acceptance:* A user can sign up, log in, log out, and reset a forgotten password via email. **[Resolved implementation baseline — production security decision, DECISIONS.md D11.6]** A newly signed-up user **cannot log in** until they click the verification link sent to their email (`ACCOUNT_EMAIL_VERIFICATION = "mandatory"`, DATA_MODEL.md `User`, SECURITY.md §3). *Provenance correction:* the approved stack doc says django-allauth performs "email confirmations" — it does not itself specify *mandatory* (block-login-until-verified) verification; that specific mode is an engineering decision made to satisfy the requirement meaningfully, not a stack-doc mandate. Previously mislabeled `[Confirmed]` on that basis — corrected this pass.
- **REQ-ACC-02** [Confirmed] One independently active login session per account ("single active session" / one-device enforcement) to reduce account sharing.
  *Acceptance:* Logging in on a second device/browser invalidates the first session; the first session's next request is treated as unauthenticated.
- **REQ-ACC-03** [Confirmed — §7 item 2] When a session is invalidated by a new login elsewhere, the displaced session's next request triggers `request.session.flush()` + Django `logout()`, then a redirect to `/login/?error=session_conflict`.
  *Acceptance:* The displaced browser, on its next request after being kicked out, lands on the login page with the `session_conflict` error state — never a silent failure or an unauthenticated 500/403.
- **REQ-ACC-04** [Client-approved post-freeze scope addition — DECISIONS.md D14] "Continue with Google" is added as an additional, optional login/signup method, on top of the existing local username/password flow, which remains fully in place and unchanged.
  *Required behavior:*
  - **Additive only.** Local signup, local login, and password-reset via email (REQ-ACC-01) are not removed, degraded, or hidden — "Continue with Google" appears alongside them, not instead of them.
  - **Google is the only added social provider.** No GitHub, Facebook, Apple, or Microsoft login is introduced by this change.
  - **Stock Django `User` model, unchanged.** No custom `AUTH_USER_MODEL` is introduced to support Google login — DATA_MODEL.md `User` stands as-is; django-allauth's own `socialaccount` framework links a Google identity to a stock `User` row (DATA_MODEL.md, new note this pass).
  - **Finance PSU's own Postgres database remains the system of record** for the account — a Google login authenticates *into* a Finance PSU `User`, it does not move any account data to Google or make Google the source of truth for anything beyond the identity assertion itself (verified email + name) used at sign-in.
  *Acceptance:* A student can sign up or log in equally well via local credentials or via "Continue with Google"; disabling/removing Google login at any future point would not strand any account that also has local credentials set. See SECURITY.md §3, API.md §1, DEPLOYMENT.md §3–4, TESTING.md for the full implementation baseline.

### 3.5 MCQs, Mock Tests & Administration (Invoice: INR 4,000)

- **REQ-MOCK-01** [Confirmed] MCQ practice and mock-test workflows with timing (countdown), autosave of answers during the attempt, scoring, and results.
  *Acceptance:* A student can start a timed mock test, have in-progress answers preserved if the browser is closed and reopened before time expires, submit (manually or on timeout), and see a score/result.
- **REQ-MOCK-02** [Confirmed] Mock Test listing and detail pages, purchasable independently (workflow doc shows a distinct "Mock Tests" nav item and purchase flow, paralleling courses).
  *Acceptance:* A student can browse, purchase, and access mock tests the same way as courses.
- **REQ-MOCK-03** [Confirmed] Admin controls for questions, tests, course content, and student access.
  *Acceptance:* An admin can create/edit/delete questions, assemble them into a mock test, manage course content, and view/adjust a given student's access.
- **REQ-MOCK-04** [Assumption] Auto-save persists to the server periodically during an attempt (not only client-side), so a lost/crashed browser session does not lose answers already auto-saved. *(Source docs say "autosave" without specifying client-only vs server-synced; server-synced is required to make the guarantee meaningful — flagged in DECISIONS.md.)* **Server-authoritative deadline enforcement (this pass, DECISIONS.md D10):** the server's own clock — not the client's — determines when `started_at + time_limit_minutes` has passed; no answer can be saved and no score can include an answer accepted after that point (API.md §5).
  *Acceptance:* An autosave call sent after the deadline is rejected, not saved; the attempt is auto-finalized (scored from whatever was saved before the deadline) on whichever mutating request first arrives after it expires — no background worker is used (no Celery/Redis, consistent with the approved stack). Submitting twice returns the same stored score both times; it never rescores. **Concurrency (corrected this pass, DECISIONS.md D11.2):** every autosave/submit call locks the `Attempt` row (`select_for_update()` inside `transaction.atomic()`) before checking `submitted_at` or the deadline, so two near-simultaneous requests on the same attempt serialize through Postgres rather than racing — this is what makes the "rejected after deadline" and "submit twice = same score" guarantees hold under real concurrency, not just the common single-request case.
- **REQ-MOCK-05** [Confirmed] A purchased **course** also grants access to that course's included formal Mock Tests — the workflow doc lists "Notes / MCQs / Mock Tests" as what a course purchase unlocks, on top of Mock Tests being independently purchasable (REQ-MOCK-02). Represented by `CourseMockTest` (DATA_MODEL.md), a course↔mock-test join distinct from the standalone `MockTestEnrollment` path.
  *Acceptance:* A student may access a given Mock Test if they have a standalone `MockTestEnrollment` for it, **or** an active, non-expired course `Enrollment` where that Mock Test is linked via `CourseMockTest` — evaluated by one centralized `can_access_mock_test(user, mock_test)` helper (API.md §4), never re-implemented per view. [Assumption, not client-frozen] Standalone-purchased tests surface under "My Mock Tests"; course-included tests surface from inside their purchased course — flagged explicitly as an assumption since no source document specifies the UX split (API.md §4).

### 3.6 Payments & Access (Invoice: INR 2,000)

- **REQ-PAY-01** [Confirmed] Razorpay checkout supporting UPI, cards, net banking, and wallets (per workflow doc payment-method list).
  *Acceptance:* All four payment method categories are selectable at checkout via Razorpay's own UI.
- **REQ-PAY-02** [Confirmed] Server-side payment verification before granting course/test access — no access is granted on client-reported success alone.
  *Acceptance (corrected in D10 and D11.1 — see DECISIONS.md D11.1):* Access is only granted after the backend independently verifies the Razorpay webhook signature against the **raw request body**, confirms the webhook `event` is exactly `"payment.captured"` (any other event, including `payment.authorized`, is ignored — a `payment.authorized` webhook can carry a payment entity whose *status* already reads `"captured"`, so status alone is not a safe signal), **and** confirms `payload.payment.entity.status == "captured"` — **both** conditions required, not either alone. Only then does it look up the local `Order` by **`razorpay_order_id`** (set at order-creation time, *before* checkout — never by `razorpay_payment_id`, which doesn't exist to look up by on a first delivery). Processing is wrapped in `transaction.atomic()` with the `Order` row locked (`select_for_update()`). The payment's amount and currency are validated against the immutable, server-set `Order.amount_paise`/`Order.currency` — a mismatch grants nothing. Duplicate webhook deliveries for the same already-verified `Order`+`razorpay_payment_id` are idempotent (return 200, write nothing); a *conflicting* payment id arriving for an already-verified `Order` grants nothing further and is logged as an anomaly rather than silently accepted. Unique DB constraints (DATA_MODEL.md §3: `Order.razorpay_order_id`, `Order.razorpay_payment_id` where set, and `unique(order)` on both `Enrollment` and `MockTestEnrollment`) back this up at the schema level, not just in application logic.
- **REQ-PAY-03** [Confirmed] Automatic access mapping immediately after verified successful payment (course/test appears in the student's dashboard without manual admin action).
  *Acceptance:* Within the same session, a successful payment is followed by the purchased item appearing under My Courses/My Mock Tests.

### 3.7 Cloud Release, QA & Handover (Invoice: INR 5,000)

- **REQ-OPS-01** [Confirmed] Production deployment to the approved stack (see ARCHITECTURE.md).
- **REQ-OPS-02** [Confirmed] Automated backups (nightly, encrypted). *(Corrected in D10/D11 — the backup logic lives entirely inside a dedicated `backup` Compose service, triggered by `docker compose -f docker-compose.prod.yml run --rm backup backup` over SSH — not an ad-hoc host script. See DEPLOYMENT.md §5.)*
- **REQ-OPS-03** [Confirmed] Security checks before go-live (see SECURITY.md).
- **REQ-OPS-04** [Confirmed] Load testing to validate behavior under concurrency before launching at the 10,000-user target; the invoice explicitly frames final capacity as something to be *verified*, not pre-guaranteed.
  *Acceptance:* [Resolved timing — §7 item 6] The load test runs immediately post-handover, as an operational gate before public marketing/registration launch — not required within the 14-day build window. Per the invoice's own language, capacity is *verified* through this simulated load test, not guaranteed by design; no source document specifies numeric pass/fail thresholds, so none is asserted here (an earlier draft of this document proposed specific latency/error-rate numbers on its own initiative — retracted per client instruction; see DECISIONS.md D7).
- **REQ-OPS-05** [Confirmed] Full handover: source code, deployment setup, and administrator access delivered to the client for independent future ownership/operation, using portable, standard (non-proprietary-lock-in) technology.

## 4. Non-Functional Requirements

- **NFR-01 Capacity** [Confirmed, soft target] 10,000 concurrent users is the target; explicitly subject to load-test verification, not a launch-blocking guarantee at handover (invoice wording).
- **NFR-02 Mobile performance** [Confirmed] Bootstrap-based responsive design; lightweight JS (invoice explicitly rules out a heavy JS framework).
- **NFR-03 Portability** [Confirmed] "Standard technologies are used so the application remains portable" (invoice handover commitment) — reinforces the approved stack's own "zero vendor lock-in" framing (Cloudflare R2 is S3-API compatible; Postgres is standard).
- **NFR-04 Cost** [Confirmed] Base infrastructure cost target is INR 0 (Oracle Cloud Always Free + Cloudflare free tier + R2 free tier), per the approved tech stack doc. Razorpay (2% txn fee) and any domain cost are pass-through, billed separately per the invoice's commercial notes.
- **NFR-05 Timeline** [Confirmed — stated directly by the client this session, not in the invoice/workflow docs] 14-day delivery window, covering feature development, integration, and baseline infrastructure setup. Load testing (NFR-01/REQ-OPS-04) is explicitly decoupled — it runs post-handover, not inside this window (§7 item 6). The window's calendar start date still isn't given, but no longer blocks anything as a result. The Django 5.2 LTS upgrade-planning note from the approved stack doc (DEPLOYMENT.md, new "Post-Handover Maintenance" section) is likewise explicitly outside this window — a maintenance note for 2028, not a handover blocker.
- **NFR-06 Security baseline** See SECURITY.md — HTTPS/HSTS, secure cookies, CSRF protection, encrypted backups, no secrets in source, R2 CORS restricted to production origins, and authenticated/personalized routes never edge-cached by Cloudflare (SECURITY.md §9, new this pass).
- **NFR-07 Long-term maintainability** [Client-approved post-freeze scope addition — DECISIONS.md D16] Finance PSU is intended to remain operable and upgradable for a 5–10 year lifetime. This does not freeze today's exact dependency versions — supported/LTS software and routine framework/security upgrades are expected over that period, on the same simple, conventional Django/PostgreSQL/Docker architecture already approved, with reproducible deployment, migration-backed schema changes, automated regression tests for critical behavior, and restore-tested encrypted backups. No new infrastructure is introduced to satisfy this principle.

## 5. Out of Scope

Explicitly excluded by the invoice's own commercial notes and by absence from all three source documents:

- Any feature or major scope change not listed in the invoice — requires separate discussion/approval before work begins (invoice, Commercial Notes).
- Software license fees for open-source components (none charged, per invoice).
- Domain registration, cloud hosting overage, payment-gateway fees, and email-provider charges beyond free tiers — billed separately by the relevant provider, not part of the INR 25,000 fee.
- The two alternative technology stacks found alongside the approved one (Fastify/Node+Valkey+Hetzner; Next.js/Vercel+Neon) — see DECISIONS.md.
- Forensic/dynamic watermarking of PDFs — present in one *rejected* stack draft, not in the approved stack spec or the invoice. **Provenance correction (DECISIONS.md D11.6), print policy since updated (DECISIONS.md D15):** PDF protection scope isn't all invoice-mandated — the invoice/workflow doc specify "no normal download button" / online-only viewing; the approved technical stack additionally describes stripping text-copy paths, which the current implementation baseline (REQ-COURSE-03) retains as the build's chosen scope, not because the invoice itself mandated it. Print stripping was originally part of that same scope but was reversed by explicit client decision after the v2 freeze — printing is now intentionally permitted.
- Native mobile apps — invoice specifies a responsive *website*, not iOS/Android apps.
- Multi-language/i18n — not mentioned anywhere in source docs.
- Content authoring tooling beyond basic admin CRUD — no CMS or bulk-import tooling is specified.
- Custom PDF tax receipt / GST ledger generation — payments rely on Razorpay's own automated email receipts (§7 item 7).
- Bulk/CSV/Excel/JSON question-bank import tooling — admin enters MCQs manually via Django Admin (§7 item 5).

## 6. Resolved Since First Draft

Two items originally listed here as open were closed using material already read this session but outside the approved project folder (the two rejected stack drafts elsewhere in the client's Drive) — disclosed in full in DECISIONS.md so it's clear these are engineering-pattern cross-references, not additional requirements sources:

- **Origin web/app server & TLS termination** — resolved to Nginx + Gunicorn behind Cloudflare Full (Strict). See DECISIONS.md D5.
- **Autosave persistence** — resolved to server-synced (REQ-MOCK-04 is now a firm default, not contingent on confirmation). See DECISIONS.md D4.
- **Load-test pass criteria** — an earlier draft proposed specific numeric latency/error-rate targets here on its own initiative; that proposal was retracted per explicit client instruction (DECISIONS.md D7, superseded). Standard is now just the invoice's own language: capacity is verified via simulated load testing before scale launch, with no numeric target prescribed by any source document.

## 7. Resolution of Initial Project Questions [Frozen Source of Truth]

All items from the original open-questions list (formerly §7 items 1–9) are frozen by direct client decision with the following technical architectures and commercial definitions. These supersede any assistant-proposed default stated earlier in this document or elsewhere in `/docs` — the other files have been updated to match and are cross-referenced below.

### Architecture & Operational Defaults

#### 1. Deployment Mechanism (Replaces Item 1)
- **Resolution:** The deployment process is locked down as a clean, manual Git-and-Docker workflow on the host.
- **Implementation:** Deployment requires SSH access to the Oracle VPS, pulling updates via `git pull origin main`, running `docker compose -f docker-compose.prod.yml up --build -d`, and applying migrations through the container shell. Push-to-deploy via GitHub Actions is officially out of scope for this phase.
- **Applied in:** DEPLOYMENT.md §6.

#### 2. Single-Session Conflict User Experience (Replaces Item 2 / REQ-ACC-03)
- **Resolution:** The user session conflict handler is explicitly defined.
- **Implementation:** The moment a concurrent session mismatch is detected by the middleware, the system immediately runs `request.session.flush()`, executes a clean Django `logout()`, and terminates the request by returning a strict redirect to `/login/?error=session_conflict`.
- **Applied in:** ARCHITECTURE.md §3.2, REQ-ACC-03 below.

#### 3. Student Profile Fields Framework (Replaces Item 5 / REQ-DASH-04)
- **Resolution:** The database persistence model constraints are bound strictly to baseline entities.
- **Implementation:** The `UserProfile` model tracks only `user` (1:1 link), `active_session_key` (tracking hash), and `phone_number` (CharField, max_length=15, null=True, for transactional alignment). Password changes utilize the built-in, secure `django-allauth` security forms.
- **Applied in:** DATA_MODEL.md (`UserProfile`, replacing the earlier standalone `ActiveSession` model), REQ-DASH-04 below.

#### 4. Platform Administration & Content Management (Replaces Item 6)
- **Resolution:** The backend content control engine is locked to native toolsets.
- **Implementation:** The platform relies completely on the out-of-the-box **Django Admin Dashboard** (`django.contrib.admin`) for all CRUD data entry tasks (creating courses, uploading books, parsing chapters, attaching PDF keys, managing questions, blog posts, and viewing logs). No custom management panel will be built.
- **Applied in:** API.md §7.

#### 5. MCQ Question Bank Data Entry Operations (Replaces Item 8)
- **Resolution:** Question bank populating methods are restricted to native structures.
- **Implementation:** Admins will enter multiple-choice questions manually using standard creation grids inside the Django Admin panel. Bulk file importers (CSV, Excel, or JSON parsing engines) are explicitly excluded from this build phase.
- **Applied in:** DATA_MODEL.md `Question` entity note.

### Commercial & Business Context Definitions

#### 6. Load-Testing Schedule & Delivery Window (Replaces Items 3 & 4)
- **Resolution:** System launch timing gates are decoupled into specific milestone phases.
- **Definition:** The 14-day development lifecycle covers full feature development, integration execution, and baseline infrastructure setup. The high-volume concurrent user load-testing suite will be executed immediately post-handover, as an initial operational check gate before public marketing/registration goes live — not required to complete inside the 14-day window itself.
- **Note:** This settles *when* the load test runs. On *how* it's judged: no numeric pass/fail target is prescribed by any source document — an earlier draft proposed one on its own initiative and that has been retracted per explicit client instruction (DECISIONS.md D7). The standard is the invoice's own language: capacity is verified through simulated load testing before scale launch. The calendar start date of the 14-day window itself also still isn't given, but no longer blocks anything since the load test is decoupled from it.
- **Applied in:** REQ-OPS-04 below, TESTING.md §3.

#### 7. Billing Documentation & Transaction Invoicing (Replaces Item 7)
- **Resolution:** Transaction invoicing processes rely natively on payment gateway structures.
- **Definition:** The application relies on **Razorpay's automated email receipt dispatch system** to deliver transaction summaries and payment records to students. A custom, internal PDF tax receipt generation or GST ledger computation microservice is outside the scope of this project fee.
- **Applied in:** API.md §8 (removed from "Not Yet Specified" — now a confirmed exclusion), REQUIREMENTS.md §5 (Out of Scope).

#### 8. Practice MCQs vs. Formal Mock Exams (Replaces Item 9 / REQ-COURSE-05)
- **Resolution:** Study verification modules are bound to a single data engine layer.
- **Definition:** Both features draw from the same underlying `Question` table. However, **Mock Tests** are formal, timed examinations that use countdown timers, background autosaves, and deferred results. **Practice MCQs** are rendered as untimed, relaxed chapter companion modules that provide immediate green/red validation feedback directly upon option selection.
- **Applied in:** DATA_MODEL.md `Question` entity, API.md (new Practice MCQ endpoint), REQ-COURSE-05 below.

Nothing remains genuinely unresolved from the original nine items. Numeric load-test pass/fail thresholds are deliberately *not* prescribed (item 6 above, sub-note) — the client explicitly rejected an earlier assistant-proposed default; the invoice's own "verify via simulated load testing" language is the standard, not a gap.
