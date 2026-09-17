# DEPLOYMENT.md — Finance PSU Learning Platform

Sequence to stand up the approved stack (`technical_stack_specification.pdf`) and hand it over per REQ-OPS-05. Steps marked **[Resolved default]** are engineering implementation choices adopted during planning and recorded in DECISIONS.md; steps marked **[Resolved — client-frozen]** are direct client decisions recorded in REQUIREMENTS.md §7; everything marked **[Confirmed]** comes directly from the approved source documents. No unresolved architectural assumptions remain in the frozen baseline.

## 1. Provisioning

1. **[Confirmed]** Create an Oracle Cloud (OCI) "Always Free" compute instance: 2 vCPUs / 12 GB RAM, Ubuntu, running 24/7 (no scale-to-zero).
2. **[Confirmed]** Install Docker Engine + Docker Compose v2 on the host. **No other packages are installed at the OS level** — Nginx in particular is not `apt install`-ed; it's defined purely as the `proxy` service inside `docker-compose.prod.yml` (§2 below). The host stays minimal: Docker/Compose plus whatever OS-level firewall rules are needed (§2 — port 5432 stays closed).
3. **[Confirmed]** Point DNS for `financepsu.guide` / `www.financepsu.guide` through Cloudflare (proxied/orange-cloud) to the OCI instance's public IP.
4. **[Resolved default — DECISIONS.md D5]** Issue a Cloudflare Origin CA certificate for the VM and configure Cloudflare SSL mode to Full (Strict), so traffic between Cloudflare and the origin is also encrypted, not just edge-to-visitor.
5. **[Confirmed this pass, DECISIONS.md D10]** Configure Cloudflare cache rules per SECURITY.md §9: edge-cache the public/non-personalized routes only; bypass cache entirely for every authenticated/personalized route (dashboard, library, checkout, admin, mock-test attempts, etc.).

## 2. Application Containers

Docker Compose services on the host (`docker-compose.prod.yml`):
- `web` — Gunicorn serving Django 5.2 (Python 3.12), built from the project's Dockerfile.
- `db` — PostgreSQL 18 (Alpine), configured with a 1 GB `shm_size` per the stack doc, data on a named volume (this volume is the live application data — see ARCHITECTURE.md §4). **Carries no `ports:` mapping** — port 5432 is never published to the host's network interface or the internet; reachable only from other containers on the Compose network (`web`, and the `backup` service below). **Compose-network alias `psu_data_engine_db`** (corrected this pass, DECISIONS.md D11.3) — matches `DB_HOST=psu_data_engine_db` in `.env` (§3) exactly, so `web` and `backup` both resolve `DB_HOST` correctly; the Compose service key stays `db`, but no command anywhere hard-codes that as a hostname.
- `proxy` **[Resolved — DECISIONS.md D5, refined by direct client instruction]** — Nginx, running strictly as this container service, never installed on the host OS; terminates the origin side of TLS and proxies to `web`.
- `backup` **[Resolved — client instruction, redesigned in D10/D11]** — a dedicated, one-shot utility container (see §5 below); attached only to the Compose-internal network, **no published ports**, not started by `up`, only run on demand (`docker compose -f docker-compose.prod.yml run --rm backup backup`).

`docker compose -f docker-compose.prod.yml up -d` brings the always-on services (`web`, `db`, `proxy`) up; `docker compose -f docker-compose.prod.yml exec web python manage.py migrate` applies schema; `docker compose -f docker-compose.prod.yml exec web python manage.py createsuperuser` provisions the first admin account — a Django superuser (`is_staff=True`, `is_superuser=True`), since admin authorization is native Django, not a custom field (REQ-MOCK-03, DATA_MODEL.md `User`).

## 3. Environment Configuration

Populate `.env` on the host (never committed — see SECURITY.md §2) from `.env.example`:
```
DJANGO_SECRET_KEY=...           # 64+ char random value
DJANGO_DEBUG=False
ALLOWED_HOSTS=financepsu.guide,www.financepsu.guide

DB_NAME=finance_psu_prod_metadata
DB_USER=psu_secure_admin_worker
DB_PASSWORD=...
DB_HOST=psu_data_engine_db       # Compose-network alias on the `db` service, not the service key itself — §2
DB_PORT=5432

R2_BUCKET_NAME=finance-psu-vault-drive
R2_ENDPOINT_URL=https://<account_id>.r2.cloudflarestorage.com
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...

RAZORPAY_KEY_ID=rzp_live_...
RAZORPAY_KEY_SECRET=...
RAZORPAY_WEBHOOK_SECRET=...

EMAIL_HOST=smtp-relay.brevo.com
EMAIL_PORT=587
EMAIL_HOST_USER=...
EMAIL_HOST_PASSWORD=...
DEFAULT_FROM_EMAIL="Finance PSU Guide"

BACKUP_AES_PASSPHRASE=...

# Google Login (REQ-ACC-04 — client-approved post-freeze scope addition, DECISIONS.md D14)
GOOGLE_OAUTH_CLIENT_ID=...
GOOGLE_OAUTH_CLIENT_SECRET=...
```
Database connection (built directly from the five discrete `DB_*` variables above — **no `DATABASE_URL` / `dj_database_url` indirection**; per client instruction, avoids connection-slot starvation under load):
```python
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ['DB_NAME'],
        'USER': os.environ['DB_USER'],
        'PASSWORD': os.environ['DB_PASSWORD'],
        'HOST': os.environ['DB_HOST'],
        'PORT': os.environ['DB_PORT'],
        'CONN_MAX_AGE': 0,
        'OPTIONS': {
            'connect_timeout': 15,
        },
    }
}
```
Also set django-allauth's account settings — stock `User` model, no custom `AUTH_USER_MODEL` (REQ-ACC-01, DATA_MODEL.md `User`, SECURITY.md §3 for the full rationale and version note):
```python
ACCOUNT_LOGIN_METHODS = {"username"}
ACCOUNT_SIGNUP_FIELDS = ["username*", "email*", "password1*", "password2*"]
ACCOUNT_EMAIL_VERIFICATION = "mandatory"
```
Also set the Google Login provider config (REQ-ACC-04, client-approved post-freeze scope addition, DECISIONS.md D14) — additive to the settings above, local login is unaffected. Full settings block and rationale in SECURITY.md §3; the env-sourced piece is:
```python
SOCIALACCOUNT_PROVIDERS = {
    "google": {
        "SCOPE": ["profile", "email"],
        "AUTH_PARAMS": {"access_type": "online"},
        "OAUTH_PKCE_ENABLED": True,
        "APP": {
            "client_id": os.environ["GOOGLE_OAUTH_CLIENT_ID"],
            "secret": os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
            "key": "",
        },
    }
}
```

## 4. Third-Party Account Setup

- **Cloudflare**: DNS, WAF, CDN, R2 bucket — all free tier. R2 bucket CORS scoped to the production site origins only (SECURITY.md §9) — no wildcard.
- **Razorpay**: business account, live API keys, webhook URL registered pointing at `/checkout/webhook/`, webhook secret generated.
- **Brevo**: SMTP account, sender domain verified, within the 300 emails/day free tier. Used only for signup email verification, password reset/update, and an optional purchase-confirmation notice — never a payment/tax receipt (SECURITY.md §7).
- **GitHub**: repo hosting the source + the Actions workflow for nightly backups. Needs only SSH secrets — `OCI_SSH_HOST`, `OCI_SSH_USER`, `OCI_SSH_PRIVATE_KEY`. **R2 and backup-encryption credentials are never given to GitHub at all** — the workflow's entire job is one SSH command running the `backup` container's `backup` subcommand, which reads the host's own `.env` through Compose (§5).
- **Google Cloud Console** (REQ-ACC-04 — client-approved post-freeze scope addition, DECISIONS.md D14): an OAuth 2.0 Client ID (Web application type) is created in a Google Cloud project, scoped to identity-only use (`profile`/`email`). This is a one-time operational setup step performed in Google's own console — not a source-controlled artifact, and no actual client ID/secret value is invented or recorded anywhere in this doc set.
  - **Authorized redirect URI (production):** `https://financepsu.guide/accounts/google/login/callback/` — must match allauth's callback path (API.md §1) exactly, HTTPS required (Google rejects a plain-HTTP redirect URI for a production OAuth client).
  - **Authorized JavaScript origins:** `https://financepsu.guide` (and `https://www.financepsu.guide` if the www host is kept reachable) — no other origins.
  - The resulting Client ID/Secret are placed into the host's `.env` as `GOOGLE_OAUTH_CLIENT_ID`/`GOOGLE_OAUTH_CLIENT_SECRET` (§3 above) — same handling as every other secret (SECURITY.md §2): never committed, never logged, never placed in `.env.example` as a real value.
  - OAuth consent screen configured for the identity-only scopes only (`profile`, `email`) — no additional Google API scope is requested or enabled for this project.

## 5. Backups (REQ-OPS-02) — redesigned this pass; DB host/password/restore made concrete and consistent, see DECISIONS.md D11.3

The host is intentionally minimal (Docker/Compose only — §1 step 2) and shouldn't depend on a hand-maintained shell script living outside the versioned container images. The backup logic instead lives **inside a dedicated `backup` service's image**, defined in `docker-compose.prod.yml` alongside `web`/`db`/`proxy`, with **one versioned entrypoint carrying exactly two subcommands** — `backup` and `restore` — not an unspecified script name:

- **Network:** attached only to the Compose-internal network — same as `db`. **Publishes no ports.**
- **Network alias (corrected this pass — D11.3):** the `db` service is given the Compose-network alias `psu_data_engine_db`, matching `DB_HOST=psu_data_engine_db` in `.env` (§3) exactly. Both `web` and `backup` resolve `DB_HOST` to the same name the approved `.env` already specifies — the Compose service key can stay `db` internally, but nothing in either container ever hard-codes the literal string `db` as a hostname; every command below reads `$DB_HOST`.
- **Image contents:** PostgreSQL client tools (`pg_dump`/`pg_restore`, version-matched to Postgres 18), OpenSSL, Python 3.12 + `boto3` (already in the approved stack — no new dependency introduced for this).
- **Configuration:** reads `DB_*`, `R2_*`, and `BACKUP_AES_PASSPHRASE` from the host's `.env` via Compose's `env_file:` — the same `.env` `web` already uses, nothing duplicated or re-entered elsewhere.
- **Password handling (corrected this pass — D11.3):** libpq does **not** read `DB_PASSWORD` automatically — `pg_dump`/`pg_restore` need `PGPASSWORD` (or a generated `.pgpass`). The entrypoint does `export PGPASSWORD="$DB_PASSWORD"` internally, scoped to that ephemeral container's own process — never written to a file outside it, never exposed to `web`, `proxy`, or GitHub.
- **Dump format (corrected this pass — D11.3):** `pg_dump --format=custom --no-owner --no-acl` consistently, matched by `pg_restore --no-owner --no-acl` on the way back — not a plain-SQL dump piped through `openssl` as an earlier draft described.

**`backup` subcommand** — what the container does, each run (whether triggered by GitHub Actions nightly, or manually):
```bash
export PGPASSWORD="$DB_PASSWORD"
pg_dump -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" \
  --format=custom --no-owner --no-acl \
  -f /tmp/dump.custom
openssl enc -aes-256-cbc -pbkdf2 -pass pass:"$BACKUP_AES_PASSPHRASE" \
  -in /tmp/dump.custom -out /tmp/dump.custom.enc
# upload /tmp/dump.custom.enc to the R2 backup prefix via boto3
rm -f /tmp/dump.custom /tmp/dump.custom.enc
```
1. Dump, custom format, over the private Compose network (`-h "$DB_HOST"`, never a hard-coded `-h db`) — container-to-container, never touching the host's public interface.
2. Encrypt with OpenSSL, AES-256/PBKDF2.
3. Upload the encrypted artifact to the R2 backup prefix via `boto3`.
4. Remove the plaintext dump *and* the local encrypted artifact once the upload confirms.
5. **`set -e` — exits non-zero if any stage fails** — a failed dump, encryption, or upload all surface as a failed job, not a silent gap.

Invoked as:
```bash
docker compose -f docker-compose.prod.yml run --rm backup backup
```

**`restore` subcommand** — same image, same tooling, not a second architecture:
```bash
# download <r2-object-key> from the R2 backup prefix via boto3 -> /tmp/dump.custom.enc
openssl enc -d -aes-256-cbc -pbkdf2 -pass pass:"$BACKUP_AES_PASSPHRASE" \
  -in /tmp/dump.custom.enc -out /tmp/dump.custom
export PGPASSWORD="$DB_PASSWORD"
pg_restore -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" \
  --no-owner --no-acl /tmp/dump.custom
rm -f /tmp/dump.custom /tmp/dump.custom.enc
```
1. Restores **only into a fresh/empty target database** unless an operator deliberately chooses otherwise (restoring on top of live data is a manual, deliberate override, not the default path).
2. Download the named encrypted artifact from R2.
3. Decrypt with the same OpenSSL parameters used to create it.
4. `pg_restore` into `$DB_HOST`/`$DB_NAME` with `PGPASSWORD` exported the same way as backup.
5. Clean up temp files.
6. Non-zero exit on any failed step.

Invoked as:
```bash
docker compose -f docker-compose.prod.yml run --rm backup restore <r2-object-key>
```

**GitHub Actions' entire nightly job** (00:00 UTC): SSH into the OCI host using the `OCI_SSH_*` secrets, then run exactly the `backup` subcommand above. That's the complete host-local command — GitHub Actions never sees `DB_*`, `R2_*`, `RAZORPAY_*`, `EMAIL_*`, or `BACKUP_AES_PASSPHRASE`; the container reads all of that itself, from the host's `.env`, via Compose.

**Why Postgres stays unreachable from outside the VM either way:** `db` carries no `ports:` mapping (§2); `pg_dump`/`pg_restore` run from `backup`, another container on the *same internal Compose network*, addressing `db` by its `psu_data_engine_db` alias — never over a TCP connection that crosses the VM's public interface. This holds regardless of which container initiates the query.

**Restore rehearsal is required before handover** (SECURITY.md §10 checklist) — documenting the steps isn't the same as having proven `docker compose -f docker-compose.prod.yml run --rm backup restore <artifact>` actually returns a working database, using only what's documented above (no undocumented host tooling, no manual steps outside the two subcommands). Required for REQ-OPS-05 (handover must let the client actually operate the system, including recovering from a bad state) and to restore the live-data volume described in ARCHITECTURE.md §4 if the VM is ever replaced.

## 6. Deploy Process **[Resolved — REQUIREMENTS.md §7 item 1, client-frozen]**

A clean, manual Git-and-Docker workflow on the host. Push-to-deploy via GitHub Actions is explicitly out of scope for this phase.
1. SSH into the OCI host.
2. `git pull origin main`.
3. `docker compose -f docker-compose.prod.yml up --build -d`.
4. Apply migrations through the container shell: `docker compose -f docker-compose.prod.yml exec web python manage.py migrate`.

## 7. Handover Sequence (within the 14-day window)

1. Complete SECURITY.md §10 checklist.
2. Run the Tier 1 automated test suite (TESTING.md §2) against production-like config.
3. Switch Razorpay from test to live keys.
4. Final handover per REQ-OPS-05: transfer source code (repo access), OCI/Cloudflare/R2/Razorpay/Brevo/GitHub credentials, this `/docs` set, and confirm the client (or their nominee) can independently redeploy and restore a backup.

## 8. Post-Handover Gate (before public marketing/registration launch — REQUIREMENTS.md §7 item 6, client-frozen)

Decoupled from the 14-day build window by client decision:
1. Execute the simulated load test (TESTING.md §3) and review the resulting report of measured capacity, latency, and error rate — per the invoice, no numeric pass/fail target is prescribed by any source document; the test's job is to *verify* actual capacity, not clear a pre-set bar.
2. Only after this review does the platform go live for public marketing/registration at the 10,000-user target — the invoice frames that capacity as *verified*, not guaranteed at handover.

## 9. Post-Handover Maintenance (NOT part of the 14-day scope, NOT a handover blocker)

From the approved stack doc's own closing note: **Django 5.2 LTS receives security patches until April 2028.** Management should schedule a version-upgrade window to the next LTS baseline in early 2028, to keep the platform inside its security-patch support window past that date.

This is recorded here purely as a future maintenance planning note — it does not add any obligation, contract, or deliverable to this engagement, and it is explicitly **not** a condition of handover under REQ-OPS-05. No other maintenance commitments beyond this single, source-stated note are implied or invented.

**Client-approved 5–10 year maintainability objective (DECISIONS.md D16):** the Django 5.2 LTS window above is the first of what the client expects to be routine, supported dependency/framework upgrades over the platform's intended 5–10 year operational lifetime — today's exact versions are not expected to remain frozen for that whole period. This is an expectation for future maintenance work, not a new deliverable inside this engagement, and does not introduce any new infrastructure now.
