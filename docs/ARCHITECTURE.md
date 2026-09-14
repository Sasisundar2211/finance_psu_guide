# ARCHITECTURE.md — Finance PSU Learning Platform

Source of truth for the stack: `technical_stack_specification.pdf` (client-approved). See DECISIONS.md for why two other stack drafts found in the same Drive were not used.

## 1. System Diagram

```
                              ┌───────────────────────────────┐
                              │   Cloudflare (Free Tier)       │
                              │   DNS proxy (orange cloud),    │
                              │   WAF, edge cache, TLS edge     │
                              └───────────────┬─────────────────┘
                                              │ HTTPS (Full/Strict — DECISIONS.md D5)
                                              ▼
                       ┌───────────────────────────────────────┐
                       │  Oracle Cloud (OCI) "Always Free" VM   │
                       │  Ubuntu, 2 vCPU / 12 GB RAM, 24/7       │
                       │                                          │
                       │  ┌────────────────────────────────────┐ │
                       │  │ Docker Compose                       │ │
                       │  │                                       │ │
                       │  │ ┌──────────┐   ┌───────────────────┐│ │
                       │  │ │  Nginx*  │──▶│  Gunicorn + Django  ││ │
                       │  │ │ (reverse │   │  5.2 LTS (Python    ││ │
                       │  │ │  proxy)  │   │  3.12) — app logic:  ││ │
                       │  │ └──────────┘   │  auth, entitlement,  ││ │
                       │  │                 │  checkout, mock-test ││ │
                       │  │                 │  engine, admin       ││ │
                       │  │                 └──────────┬──────────┘│ │
                       │  │                            │            │ │
                       │  │                            ▼            │ │
                       │  │                 ┌───────────────────┐  │ │
                       │  │                 │ PostgreSQL 18      │  │ │
                       │  │                 │ (Alpine container)  │  │ │
                       │  │                 │ 1GB shared buffer    │  │ │
                       │  │                 └───────────────────┘  │ │
                       │  └────────────────────────────────────┘ │
                       └───────────────────┬───────────────────────┘
                                            │
                         ┌──────────────────┼───────────────────────┐
                         ▼ S3 API           ▼ HTTPS                  ▼ SMTP
              ┌────────────────────┐ ┌──────────────────┐  ┌──────────────────┐
              │ Cloudflare R2       │ │ Razorpay          │  │ Brevo SMTP        │
              │ - Course PDFs       │ │ - UPI/Card/NetB/  │  │ - Verification,   │
              │ - Encrypted DB      │ │   Wallet checkout │  │   password reset  │
              │   backup snapshots  │ │ - Signed webhooks │  │ - Purchase confirm│
              │   (off-host copy,   │ │ - Owns payment/   │  │   (NOT a receipt) │
              │   not live data)    │ │   gateway receipts│  │   (300/day free)  │
              └────────────────────┘ └──────────────────┘  └──────────────────┘
```
**Backup path (not shown as a direct arrow above — it originates *inside* the OCI VM, not from an external service):** nightly at 00:00 UTC, GitHub Actions SSHes into the OCI VM and runs exactly `docker compose -f docker-compose.prod.yml run --rm backup backup`. That one command starts a dedicated `backup` Compose service — attached only to the VM's internal Docker network, publishing no ports of its own — which does the entire job itself: `pg_dump` over the internal network → AES-256/PBKDF2 encrypt → upload to R2 (using R2 credentials the container reads from the host's own `.env`, never from GitHub) → remove plaintext/intermediate files. GitHub Actions holds only SSH credentials; it never sees `DB_*`, `R2_*`, or `BACKUP_AES_PASSPHRASE`. Postgres's `5432` port is never exposed to the VM's public network interface or the internet — neither `db` nor `backup` publish it; the only way either is reached is from another container on the same internal Compose network. See DEPLOYMENT.md §5 for the full design and the restore procedure using the same container.
`*` Nginx in front of Gunicorn is a resolved default, not left open — the approved stack doc doesn't name an origin web server, but the same Cloudflare-Full(Strict)+Nginx topology appears independently in a second architecture exploration for the same 10,000-user target (see DECISIONS.md D5).

## 2. Component Responsibilities

| Component | Responsibility | Source |
|---|---|---|
| Cloudflare (DNS/WAF/CDN) | TLS at the edge, DDoS/WAF, caches public pages (`s-maxage`), fronts the domain | Tech stack doc |
| Nginx | TLS to origin, request buffering, static file serving, proxy to Gunicorn | Resolved default (DECISIONS.md D5) |
| Gunicorn + Django 5.2 | All server-side logic: auth, entitlement checks, payment verification, mock-test engine, admin CRUD | Tech stack doc |
| django-allauth | Signup, login, email verification, password reset | Tech stack doc |
| PostgreSQL 18 | System of record: users, courses, enrollments, questions, attempts, orders | Tech stack doc |
| Cloudflare R2 | Object storage for course PDFs and encrypted nightly DB backups; served via time-limited presigned URLs, zero egress cost | Tech stack doc |
| Mozilla PDF.js + boto3 | In-browser PDF rendering to `<canvas>` (no native download UI); Django mints a 60-second presigned URL per view | Tech stack doc |
| Razorpay Python SDK | Checkout UI/redirect; Django verifies HMAC-SHA256 webhook signature server-side before granting access | Tech stack doc, REQ-PAY-02 |
| Brevo SMTP | Finance-PSU transactional email only: signup **email verification**, password reset/update, optional plain purchase-confirmation notice. **Not** a payment/tax receipt — see SECURITY.md §7 | Tech stack doc (mechanism: allauth email confirmations); mandatory-verification and receipt-scope specifics are engineering decisions, not stack-doc-mandated wording — see DECISIONS.md D11.6 |
| `backup` (Compose service) | Dedicated container, internal-network-only, no published ports: `pg_dump` over the Compose network → AES-256/PBKDF2 encrypt → upload to R2 → cleanup. Invoked by `docker compose -f docker-compose.prod.yml run --rm backup backup` | Client-frozen backup mechanism, redesigned in D10/D11 |
| GitHub Actions | Nightly cron: SSHes into the OCI VM and runs exactly `docker compose -f docker-compose.prod.yml run --rm backup backup` — holds only SSH credentials, never touches Postgres/R2/secrets directly itself | Tech stack doc (trigger schedule) + client-frozen backup mechanism, redesigned in D10/D11 |

## 3. Key Flows

### 3.1 Public browsing (unauthenticated)
Cloudflare edge cache serves homepage/blog/course-catalog HTML directly where possible; cache miss falls through to Nginx → Gunicorn → Django (reads from Postgres). No session state involved (REQ-WEB-01–05).

### 3.2 Signup/Login + single active session (REQ-ACC-01, REQ-ACC-02, REQ-ACC-03 — REQUIREMENTS.md §7 item 2, client-frozen)
1. `django-allauth` handles credential validation and Django's own DB-backed session framework issues a session.
2. On each successful login, the app writes the new session's key into `UserProfile.active_session_key` (see DATA_MODEL.md), overwriting whatever was there — there is no separate session table to reconcile.
3. A request-level middleware compares the request's session key against `request.user.userprofile.active_session_key`. On a mismatch (i.e. this session has been superseded by a newer login elsewhere), the middleware immediately:
   - calls `request.session.flush()`,
   - calls Django's `logout()`,
   - and returns a redirect to `/login/?error=session_conflict`.
   *No Redis/Valkey is present in the approved stack, so this is implemented directly against PostgreSQL rather than an in-memory session store — this trades a small amount of per-request DB overhead for staying within the approved, zero-cost stack.*

### 3.3 Enrollment & payment (REQ-PAY-01–03) — corrected in D10 and D11.1, see DECISIONS.md D11.1
1. Student selects a course + validity plan → `Enroll Now`.
2. If not logged in → Login/Signup (gated by email verification, REQ-ACC-01) → return to checkout (workflow doc §5).
3. Backend looks up the pricing plan server-side, creates a Razorpay order, and **creates the local `Order` row immediately — before checkout even opens** — with `razorpay_order_id` set, `amount_paise`/`currency` captured from the plan, `status="created"`. This is what the webhook will look up by later (step 5); it cannot look up by `razorpay_payment_id`, since that field is still `NULL` at this point.
4. Razorpay hosted checkout collects UPI/card/net-banking/wallet payment.
5. **Access is granted only from the server-side webhook handler, and only for the exact right event and status, checked in that order.** It verifies the HMAC-SHA256 signature against the *raw* body first (invalid → stop, no parsing); parses the payload and reads **only the `event` field** next — any event other than `payment.captured` is ignored (200, no grant) **without inspecting `status` at all**, since a `payment.authorized` webhook can be delivered while the payment's status already reads `"captured"` and status alone is never a safe signal (DECISIONS.md D11.1); only for a `payment.captured` event does it then read `payment.entity.status` and require `"captured"`, look up the local `Order` by `razorpay_order_id`, lock the row (`select_for_update()` inside `transaction.atomic()`), and validate amount/currency against the `Order`'s own immutable values — never from the client-side "payment success" callback alone (REQ-PAY-02, API.md §2 for the full canonical sequence).
6. On a verified webhook (first delivery only — duplicates and conflicting-payment-id deliveries are handled idempotently, API.md §2), an `Enrollment` or `MockTestEnrollment` row is created — exactly once per `Order`, enforced by a `unique(order)` DB constraint — mapping student → course/mock-test → validity window; it appears immediately in the dashboard (REQ-PAY-03).

### 3.4 Protected PDF viewing (REQ-COURSE-02, REQ-COURSE-03) — loading model corrected this pass, see DECISIONS.md D11.4
1. Student opens a chapter inside a purchased book.
2. Django checks entitlement (does this student have an active `Enrollment` covering this course, unexpired?).
3. If entitled, Django mints a 60-second presigned R2 URL for that PDF object.
4. **Browser immediately performs one complete GET of the PDF directly from R2** while the URL is valid, and hands the resulting bytes to PDF.js in one shot (`pdfjsLib.getDocument({ data })`) — not a series of `Range` requests spread across the reading session (API.md §3 for the full corrected flow, including the one-retry-with-a-fresh-URL behavior on a failed initial fetch).
5. From then on, PDF.js renders pages to `<canvas>` from the **in-memory** document — no further R2 requests, no `<a download>`, no native PDF viewer chrome.
6. **Corrected wording (D11.4):** the 60-second expiry bounds the initial fetch, not "the viewing session" — anyone holding the URL can use it until it expires, same as any presigned URL; the short lifetime limits that exposure window, it doesn't create a notion of a URL that "stops working once viewing starts." Because the document is already fully loaded in memory by then, expiry doesn't interrupt an in-progress read. Re-opening the chapter later repeats the whole entitlement-check-plus-fresh-URL sequence from step 2.
7. **Every successful step 3 also upserts `UserChapterProgress`** (`is_completed=True` on first access, `last_accessed_at=now()` on every access — DATA_MODEL.md). This is what REQ-DASH-01's "completed topic count" and "recently accessed course" are both computed from; no second tracking mechanism was introduced. **"Recently accessed course" is further restricted to courses with a currently active `Enrollment`** (DECISIONS.md D11.5) — an expired course's access history never surfaces as "recently accessed."

### 3.5 Mock test access & attempt (REQ-MOCK-01, REQ-MOCK-04, REQ-MOCK-05) — access rule, deadline enforcement, and row-locking corrected this pass, see DECISIONS.md D10, D11.2
1. Every mock-test route (start/autosave/submit) first calls the single `can_access_mock_test(user, mock_test)` helper (API.md §4) — true via a standalone `MockTestEnrollment`, **or** via an active course `Enrollment` where the test is linked through `CourseMockTest` (REQ-MOCK-05 — a purchased course can include formal Mock Tests, not just standalone-purchasable ones). No view re-implements this check independently.
2. Student starts an accessible mock test → server creates an `Attempt` row (`started_at`, `time_limit_minutes`), from which the server computes `deadline = started_at + time_limit_minutes` at request time — never stored as its own column, never supplied by the client.
3. **Every mutating call — autosave or submit — first locks the `Attempt` row** (`select_for_update()` inside `transaction.atomic()`, API.md §5) **before** checking `submitted_at`, computing the deadline, or writing anything. This is what makes the deadline/idempotency guarantees hold under two near-simultaneous requests, not just the common single-request case — corrected this pass, since the design previously described these checks without a shared lock. Before the deadline (and while holding the lock): the answer is saved normally. At or after the deadline: the answer is rejected and the attempt is auto-finalized right there (lazy, on whichever request first *acquires the lock* past the deadline — no background worker). A manipulated client clock has no effect, since the request never supplies a timestamp the deadline check relies on.
4. On manual submit (or the lazy auto-finalize above), server scores the attempt — still holding the same row lock — against the question bank's correct answers, using only answers already persisted before the deadline, and stores the result. Submitting twice is idempotent: the second call, once it acquires the lock, finds `submitted_at` already set and returns the identical stored score rather than rescoring — this holds even for two genuinely simultaneous submit calls, since only one can hold the lock at a time.

## 4. Deployment Topology & Data Persistence

Single OCI VM running Docker Compose with three application-layer services — `proxy` (Nginx), `web` (Django/Gunicorn), and `db` (Postgres).

**Live application data — every Postgres table (users, courses, enrollments, orders, questions, attempts) — is stored on a persistent Docker named volume attached to the OCI VM itself.** That volume, not R2, is the authoritative, hot copy of the data. Course PDFs live in Cloudflare R2 as object storage (never transactional data). Cloudflare R2 additionally holds the **off-host, encrypted, daily backup snapshots** of the Postgres volume (DEPLOYMENT.md §5) — those are disaster-recovery copies, not the live data source.

This means the VM is not freely disposable in the way a stateless web server would be: redeploying application *code* is safe and routine (DEPLOYMENT.md §6, doesn't touch the volume), but replacing the VM itself requires restoring the Postgres volume from the latest R2 backup snapshot (DEPLOYMENT.md §5 restore runbook) — losing the volume without a recent snapshot loses data back to the last nightly backup. See DEPLOYMENT.md for the full provisioning and backup sequence.

## 5. Operational Verification & Go-Live Checks

The architecture is defined and frozen (DECISIONS.md D1–D12) — nothing below is an open design question. These are operational verification tasks that remain to be *executed*, not decisions that remain to be *made*.

- **A. Capacity** — the architecture (single 2 vCPU/12 GB free-tier VM running Django/Gunicorn, with Cloudflare absorbing cached catalog/browsing traffic at the edge) is defined; actual capacity at the 10,000-user target remains to be *measured* by the simulated load test REQ-OPS-04 requires (post-handover, per REQUIREMENTS.md §7 item 6) before scale launch. No numeric capacity target is asserted here — per the invoice, capacity is something the load test *verifies*, not something guaranteed up front (see DECISIONS.md D7 for a retracted earlier attempt at inventing specific numbers).
- **B. Backup restore** — the procedure is fully defined and documented (`backup`/`restore` subcommands on the same container, DEPLOYMENT.md §5); what remains is a pre-handover *rehearsal* — actually running it once and confirming it produces a working database — per SECURITY.md §10 checklist. Documenting the steps isn't the same as having proven they work.
