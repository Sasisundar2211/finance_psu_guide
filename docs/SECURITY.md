# SECURITY.md — Finance PSU Learning Platform

Maps REQ-OPS-03 ("security checks before go-live") to concrete controls. Settings quoted directly below are from `technical_stack_specification.pdf` (approved) and are **[Confirmed]**; anything else is derived to satisfy a confirmed requirement and marked **[Assumption]**.

## 1. Transport & Cookies [Confirmed — verbatim from approved stack doc]

```python
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SAMESITE = 'Lax'

SECURE_HSTS_SECONDS = 31536000  # 1 year
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
```
`DEBUG = False` in production; `ALLOWED_HOSTS` restricted to `financepsu.guide,www.financepsu.guide`.

## 2. Secrets Management [Confirmed]

All credentials come from environment variables, never hardcoded — the approved stack doc is explicit: *"Your development tools (Claude Code/Codex) must extract configuration properties exclusively from environment parameters. Hardcoded keys inside the repository source code are strictly prohibited."*

Required variables (`.env`, never committed): `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `ALLOWED_HOSTS`, `DB_NAME`/`DB_USER`/`DB_PASSWORD`/`DB_HOST`/`DB_PORT`, `R2_BUCKET_NAME`/`R2_ENDPOINT_URL`/`R2_ACCESS_KEY_ID`/`R2_SECRET_ACCESS_KEY`, `RAZORPAY_KEY_ID`/`RAZORPAY_KEY_SECRET`/`RAZORPAY_WEBHOOK_SECRET`, `EMAIL_HOST`/`EMAIL_PORT`/`EMAIL_HOST_USER`/`EMAIL_HOST_PASSWORD`, `BACKUP_AES_PASSPHRASE`.

A `.env.example` (placeholders only) ships in the repo; the real `.env` is excluded via `.gitignore` and provisioned only on the OCI host, read by the app (`web` service) and the backup service (`backup`, DEPLOYMENT.md §5) directly through Docker Compose's `env_file`. **GitHub Actions secrets carry only SSH credentials** (`OCI_SSH_HOST`/`OCI_SSH_USER`/`OCI_SSH_PRIVATE_KEY`) — GitHub never receives `DB_*`, `R2_*`, `RAZORPAY_*`, `EMAIL_*`, or `BACKUP_AES_PASSPHRASE` at all; the nightly workflow's entire job is `ssh ... 'docker compose -f docker-compose.prod.yml run --rm backup backup'` (DEPLOYMENT.md §5) — it never touches Postgres, R2, or any secret value directly itself.

## 3. Authentication & Session Integrity

- **[Confirmed]** `django-allauth` handles credential hashing, password reset, and email verification ("email confirmations" per the approved stack doc). **[Resolved implementation baseline — production security decision, not stack-doc-mandated, DECISIONS.md D11.6]** Verification is configured as *mandatory* (`ACCOUNT_EMAIL_VERIFICATION = "mandatory"`) — a new signup cannot log in until the link (sent via Brevo) is clicked. The stack doc says allauth performs email confirmations; it does not itself specify the mandatory/blocking mode — that's an engineering choice made so the requirement is testable and meaningful (TESTING.md), corrected this pass from an earlier draft that over-attributed it to the stack doc.
- **[Confirmed → designed]** Single active session per account (REQ-ACC-02): implemented as described in ARCHITECTURE.md §3.2 — a new login invalidates the previous session server-side. This directly reduces account-sharing, which is the stated purpose in the invoice.
- **[Confirmed, corrected this pass]** Admin authorization uses Django's **native** `is_staff`/`is_superuser`/permissions system — there is no custom `User.is_admin` field, and no custom `AUTH_USER_MODEL`. An earlier draft contradicted itself by naming both "Django's built-in User" and a custom `is_admin` field; resolved in favor of the built-in model, unmodified (DATA_MODEL.md `User`, DECISIONS.md D10).
- **[Assumption]** Password policy (minimum length/complexity) is not specified anywhere — Django/allauth defaults are used unless the client specifies otherwise.

## 4. Payment Integrity (REQ-PAY-02) — corrected in D10 and D11.1, see DECISIONS.md D11.1

The webhook handler's design was previously inconsistent (it claimed to look up `Order` by `razorpay_payment_id`, which is `NULL` on the very webhook that's supposed to set it, and granted access on *either* a captured status *or* the `payment.captured` event — unsafe, since Razorpay documents that a `payment.authorized` webhook can be delivered while the payment entity's status already reads `"captured"`). Corrected flow, in order:

1. **Verify the Razorpay webhook signature (HMAC-SHA256, `RAZORPAY_WEBHOOK_SECRET`) against the raw request body bytes**, before parsing or trusting anything in the payload. Invalid signature → 400, no DB access, no JSON parsing at all.
2. **Parse the JSON payload, then read only the top-level `event` field.** If `event != "payment.captured"`, grant nothing and return 200 (a valid-but-irrelevant event, so Razorpay doesn't retry it — this is not an error case) — **`payload.payment.entity.status` is not inspected at all in this branch.**
3. **Only for a `payment.captured` event**, read `payload.payment.entity` and require `.status == "captured"` too — **both** the event and the status, not either. This is the specific bug this pass fixes: an earlier design accepted status-or-event, which a `payment.authorized` delivery with an already-captured-looking status could satisfy incorrectly.
4. **Look up the local `Order` by `razorpay_order_id`** (set server-side at order-creation time, before the student ever reaches Razorpay checkout) — never by `razorpay_payment_id`. An unknown `razorpay_order_id` → 400, nothing to process.
5. **Process inside `transaction.atomic()` with the `Order` row locked** (`select_for_update()`), so two near-simultaneous webhook deliveries for the same order can't race each other into a double-write.
6. **Validate amount and currency against the immutable, server-captured `Order.amount_paise`/`Order.currency`** (set from the pricing plan at order-creation time) — never against anything read from this webhook call itself. Mismatch → 400, nothing granted, nothing written. Amounts are stored in paise (integer), never rupees/decimal, precisely so this comparison is never ambiguous about units (DATA_MODEL.md §2).
7. **Idempotent by design:** a duplicate delivery for an already-verified `Order`+matching `razorpay_payment_id` returns 200 and writes nothing. A *conflicting* payment id on an already-verified `Order` grants nothing further and is logged as an anomaly, never silently accepted or allowed to overwrite the existing (already-correct) grant.
8. Only on a genuinely new, valid, matching payment does the handler set `razorpay_payment_id`, mark the `Order` verified, and create the `Enrollment`/`MockTestEnrollment` — all in the same atomic block. Client-reported payment success is never trusted on its own, independent of all of the above.
9. Amounts are always looked up server-side from `PricingPlan`/`MockTestPricingPlan` at order-creation time (API.md §2) — never accepted from the client, preventing price tampering.
10. DB-level backstops, not just application logic: `Order.razorpay_order_id` unique, `Order.razorpay_payment_id` unique where set, and `unique(order)` on both `Enrollment` and `MockTestEnrollment` (DATA_MODEL.md §3) — so even a bug that got past steps 5–7 could not physically write a duplicate grant.

## 5. Content Protection (REQ-COURSE-03) — and its explicit limits

- PDFs are never served as static, permanently-linked files. Access requires: (a) an authenticated session, (b) a valid, non-expired `Enrollment`, (c) a freshly minted 60-second presigned R2 URL, consumed by a PDF.js viewer that renders to `<canvas>` with no native download UI.
- **Print blocked:** a `beforeprint` event listener intercepts the browser's print action (Ctrl+P, menu, or `window.print()`) and blocks it (or blanks the canvas before any print dialog can render page content) — the viewer never exposes a print-friendly view of the document.
- **Text copy unbound:** PDF.js's normally-selectable text layer (present for accessibility/search) is disabled, or its `user-select: none` plus a bound `copy` event handler (`preventDefault()`) stops selected text from reaching the clipboard.
- **Admin upload path (this pass, API.md §7):** the only way a PDF reaches R2 is a server-side `boto3` upload triggered by an admin (`is_staff`) action in Django Admin — never a client-side/browser-direct-to-R2 upload, and no public URL is ever generated or stored. `Chapter.pdf_object_key` is a plain private object key string, not a public-URL field.
- **Known residual risk, explicitly out of scope per REQUIREMENTS.md §5:** the above deters casual downloading, printing, and copy-paste, but does not prevent a determined user from screenshotting or using browser devtools to intercept the rendered content. The invoice's own wording — "no normal download button" — and the approved stack doc frame this as the intended bar, not full DRM. Forensic watermarking (present in a *rejected* stack draft) is not in scope unless the client asks for it.

## 6. Standard Web Vulnerability Coverage (OWASP-relevant)

| Risk | Control | Status |
|---|---|---|
| CSRF | Django's built-in CSRF middleware + `CSRF_COOKIE_SECURE`/`SAMESITE` | Confirmed (framework + stack doc) |
| XSS | Django template auto-escaping (default, not to be disabled without cause) | Assumption — standard Django behavior, not explicitly stated but not to be overridden |
| SQL injection | Django ORM (parameterized queries) throughout; no raw SQL string interpolation | Assumption — standard practice, enforced during implementation/review |
| Broken access control | Server-side entitlement checks on every protected view (course content, mock tests via `can_access_mock_test`, admin) — never client-side-only gating | Confirmed requirement (REQ-COURSE-04, REQ-DASH-02/03, REQ-MOCK-05) |
| Insecure direct object reference | PDF/chapter/attempt endpoints always re-check ownership/entitlement server-side, not just via obscure IDs | Assumption, standard practice |
| DDoS / bot abuse | Cloudflare WAF + edge caching absorbs and filters traffic before it reaches the origin | Confirmed (stack doc) |
| Backup confidentiality | `pg_dump` output AES-256 (PBKDF2) encrypted, inside the `backup` container, before upload to R2 | Confirmed (stack doc + DECISIONS.md D10 redesign) |

## 7. Receipts — terminology (reconciled this pass, see DECISIONS.md D10)

The approved stack doc names Brevo for "transactional email... payment receipts," while the client-frozen commercial decision (REQUIREMENTS.md §7 item 7) says Razorpay's own receipts are used and no custom GST/PDF invoice system is built. These don't actually conflict once the responsibilities are separated cleanly:
- **Razorpay** generates and sends its own payment/gateway receipt — nothing built by this project.
- **Brevo** sends signup email verification, password reset/update, and optionally a plain purchase-confirmation/"access granted" notice — **never** labeled or treated as a tax receipt, GST invoice, or statutory document.
- No custom GST computation, tax ledger, or PDF tax-invoice generation exists anywhere in this system.

## 8. Backup Security (REQ-OPS-02) — redesigned in D10, made concretely executable in D11.3

- The `backup` service is a dedicated Docker Compose container, attached **only** to the internal Compose network — it publishes no ports, and neither does `db` (Postgres `5432` is never reachable from outside the OCI VM, under any design in this document). `db` carries the network alias `psu_data_engine_db`, matching `DB_HOST` in `.env` exactly — neither `backup` nor `web` ever hard-codes a different hostname (DEPLOYMENT.md §2).
- GitHub Actions holds only SSH credentials and runs exactly one host-local command over SSH (`docker compose -f docker-compose.prod.yml run --rm backup backup`) — it never receives `DB_*`, `R2_*`, `RAZORPAY_*`, `BREVO`/`EMAIL_*`, or `BACKUP_AES_PASSPHRASE` secrets; those are read by the `backup` container directly from the host's `.env` via Compose.
- **Credentials scoped to the ephemeral container (D11.3):** `PGPASSWORD` is exported inside the `backup`/`restore` entrypoint only, from `DB_PASSWORD` (libpq does not read `DB_PASSWORD` itself) — never written outside that short-lived container, never exposed to `web` or `proxy`.
- Inside the container, on `backup`: `pg_dump --format=custom --no-owner --no-acl` over the private Compose network → AES-256 (PBKDF2) encryption via OpenSSL → upload to the R2 backup prefix → plaintext dump *and* local encrypted artifact removed → non-zero exit on any stage failure (surfaces as a failed SSH/Actions job, not a silent gap). A matching `restore` subcommand on the same image reverses this with `pg_restore --no-owner --no-acl`, into a fresh/empty target by default. One versioned entrypoint, two subcommands — not an ad-hoc or unspecified script. See DEPLOYMENT.md §5 for the full command reference and restore procedure.

## 9. R2 CORS & Cloudflare Cache Boundaries — introduced in D10, CORS simplified in D11.4

**R2 bucket:**
- Stays fully private — no public read, no public listing, no wildcard public write access anywhere in the bucket policy.
- Access is presigned-URL-only (60-second expiry, API.md §3) for reads; writes happen only server-side via the admin upload path (§5 above), never directly from a browser.
- **CORS (simplified this pass, D11.4):** scoped to production site origins only (`https://financepsu.guide`, `https://www.financepsu.guide` — no wildcard `*`), `AllowedMethods: [GET]` (PDF.js only ever reads), headers sufficient for a normal cross-origin GET. The PDF viewer now performs one complete GET into browser memory rather than a series of `Range` requests spread across the reading session (API.md §3, ARCHITECTURE.md §3.4) — so `Range`/`Content-Range`/`Accept-Ranges` handling is no longer a functional dependency of the reader; CORS doesn't need to be provisioned for it.

**Cloudflare / Django cache boundaries:**
- **Cacheable at the edge** (public, non-personalized): homepage, public course listing/details, public mock-test listing/details (no student-specific data), blog listing/details, static assets.
- **Never edge-cached** — Cloudflare must bypass cache for these, and Django must send `Cache-Control: private, no-store` (or equivalent) on them: login/signup/account (allauth) routes, dashboard, My Courses/My Mock Tests, library/PDF signed-URL issuance, practice MCQ checks, mock-test attempts/autosave/submit/results, checkout/webhook endpoints, Django Admin, and any other authenticated/personalized response.
- This is a hard boundary, not a performance tweak: caching a signed URL, a webhook response, or a dashboard page at Cloudflare's edge would leak one student's data/session context to whoever hits the cached copy next.

## 10. Pre-Launch Security Checklist (REQ-OPS-03)

- [ ] `DEBUG = False`, `ALLOWED_HOSTS` locked to production domains.
- [ ] All secrets sourced from environment, `.env` not in version control, verified via repo scan.
- [ ] HSTS/secure-cookie settings confirmed active in production response headers.
- [ ] Razorpay webhook signature verified against the **raw body**, tested with both valid and tampered payloads.
- [ ] A validly-signed `payment.authorized` webhook whose payment entity shows `status: "captured"` confirmed to grant **nothing** — only an actual `payment.captured` event with `status == "captured"` grants access (D11.1).
- [ ] Webhook amount/currency mismatch confirmed rejected (no grant, no write).
- [ ] Duplicate Razorpay webhook delivery (same `razorpay_order_id`+`razorpay_payment_id`) confirmed not to create a second `Enrollment`/`MockTestEnrollment`; a conflicting payment id on an already-verified `Order` confirmed to grant nothing further.
- [ ] Signup cannot log in before email verification; verification link tested end-to-end.
- [ ] Direct-URL access to another student's course/mock-test content confirmed blocked (REQ-DASH-02/03, REQ-COURSE-04, REQ-MOCK-05's `can_access_mock_test`).
- [ ] Session invalidation on second-device login verified end-to-end.
- [ ] R2 bucket confirmed not publicly listable/readable without a presigned URL; CORS confirmed scoped to production origins only; a real PDF's presigned URL confirmed to return the full file via a normal GET from the production origin.
- [ ] Postgres port 5432 confirmed not published/reachable from outside the OCI VM (neither `db` nor `backup` publish ports); `DB_HOST` resolves correctly for both `web` and `backup` via the `psu_data_engine_db` alias.
- [ ] PDF viewer: print action blocked (Ctrl+P and menu), text-layer copy confirmed non-functional, and a chapter read for over 60 seconds confirmed unaffected by presigned-URL expiry (D11.4).
- [ ] Mock-test answers confirmed rejected after the server-computed deadline; client clock manipulation confirmed to have no effect; double-submit confirmed to not rescore; two simultaneous submit/autosave calls on the same attempt confirmed to serialize through the `Attempt` row lock rather than racing (D11.2).
- [ ] "Recently accessed course" confirmed to exclude expired-`Enrollment` courses even when their access history is newest (D11.5).
- [ ] Authenticated/personalized routes (dashboard, library, checkout, admin, etc. — §9 above) confirmed never served from Cloudflare's edge cache (check `Cf-Cache-Status` / response headers).
- [ ] Admin PDF upload confirmed to store only a private object key — no public URL created anywhere.
- [ ] `Question` DB constraint confirmed to reject both-set and neither-set rows (D11.8).
- [ ] Backup restore procedure rehearsed at least once, using only the documented `backup`/`restore` subcommands and container contents — no undocumented host tooling or manual steps (DEPLOYMENT.md §5).
