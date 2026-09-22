"""Phase 9 production/operations artifacts: durable invariants, not line-by-line copies.

REQ-OPS-01 (production stack), REQ-OPS-02 (encrypted nightly backups), NFR-06
(security baseline). Frozen contract: DEPLOYMENT.md §2/§5/§6, SECURITY.md §2/§8,
DECISIONS.md D5/D10/D11.3. These are static checks over the version-controlled
files (no Docker, no network, no YAML parser dependency); the live stack is
verified separately by building and running the images.
"""

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from core.forms import MAX_CHAPTER_PDF_BYTES
from core.r2 import chapter_object_key

ROOT = Path(__file__).resolve().parent.parent

COMPOSE = ROOT / "docker-compose.prod.yml"
WEB_DOCKERFILE = ROOT / "Dockerfile"
WEB_ENTRYPOINT = ROOT / "deploy" / "web" / "entrypoint.sh"
NGINX_CONF = ROOT / "deploy" / "nginx" / "default.conf"
BACKUP_DOCKERFILE = ROOT / "deploy" / "backup" / "Dockerfile"
BACKUP_ENTRYPOINT = ROOT / "deploy" / "backup" / "entrypoint.sh"
BACKUP_R2_HELPER = ROOT / "deploy" / "backup" / "r2_transfer.py"
WORKFLOW = ROOT / ".github" / "workflows" / "nightly-backup.yml"
DOCKERIGNORE = ROOT / ".dockerignore"

BACKUP_COMMAND = "docker compose -f docker-compose.prod.yml run --rm backup backup"


def read_code(path):
    """File text with full-line and trailing `#` comments removed."""
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        lines.append(re.sub(r"\s+#\s.*$", "", line))
    return "\n".join(lines)


def compose_services():
    """{service name: block text}, split on indentation only."""
    services = {}
    current = None
    in_services = False
    for line in read_code(COMPOSE).splitlines():
        if re.match(r"^services:\s*$", line):
            in_services = True
            continue
        if not in_services:
            continue
        if line.strip() and not line.startswith(" "):
            break  # next top-level key (volumes:, networks:, ...)
        match = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if match:
            current = match.group(1)
            services[current] = []
        elif current:
            services[current].append(line)
    return {name: "\n".join(block) for name, block in services.items()}


class ProductionComposeTests(SimpleTestCase):
    def test_defines_exactly_the_four_frozen_services(self):
        self.assertEqual(set(compose_services()), {"web", "db", "proxy", "backup"})

    def test_only_proxy_publishes_host_ports(self):
        services = compose_services()
        for name in ("web", "db", "backup"):
            self.assertNotRegex(services[name], r"(?m)^\s+ports:", f"{name} must not publish ports")
        published = re.findall(r'"(\d+):(\d+)"', services["proxy"])
        self.assertEqual(sorted(published), [("443", "443"), ("80", "80")])

    def test_postgres_and_gunicorn_are_never_published_anywhere(self):
        text = read_code(COMPOSE)
        self.assertNotRegex(text, r"\b5432\s*:\s*\d+")
        self.assertNotRegex(text, r"\b\d+\s*:\s*5432\b")
        self.assertNotRegex(text, r'"?\b8000\s*:\s*\d+')
        self.assertNotRegex(text, r"\b\d+\s*:\s*8000\b")

    def test_db_matches_frozen_design(self):
        db = compose_services()["db"]
        self.assertIn("image: postgres:18-alpine", db)
        self.assertIn("shm_size: 1gb", db)
        self.assertRegex(db, r"(?m)^\s+- psu_data_engine_db\s*$")  # DB_HOST alias
        self.assertRegex(db, r"(?m)psu_prod_db_data:/var/lib/postgresql\s*$", "data must be on a named volume")
        self.assertRegex(read_code(COMPOSE), r"(?m)^volumes:\s*\n(?:\s+\S.*\n)*?\s+psu_prod_db_data:")

    def test_db_takes_frozen_db_variables_and_no_database_url_exists(self):
        db = compose_services()["db"]
        for init_var, frozen_var in (
            ("POSTGRES_DB", "DB_NAME"),
            ("POSTGRES_USER", "DB_USER"),
            ("POSTGRES_PASSWORD", "DB_PASSWORD"),
        ):
            self.assertRegex(db, rf"{init_var}:\s*\$\{{{frozen_var}[:?}}]")
        self.assertNotIn("DATABASE_URL", read_code(COMPOSE))

    def test_backup_is_a_profile_gated_one_shot_service(self):
        services = compose_services()
        backup = services["backup"]
        self.assertRegex(backup, r"(?m)^\s+profiles:\s*\n\s+- backup\s*$")
        self.assertNotIn("restart:", backup)
        for name in ("web", "db", "proxy"):
            self.assertNotIn("profiles:", services[name], f"{name} must start with plain `up`")

    def test_backup_receives_only_its_frozen_variables(self):
        backup = compose_services()["backup"]
        self.assertNotIn("env_file", backup)
        section = backup.split("environment:", 1)[1]
        names = set(re.findall(r"(?m)^\s+- ([A-Z0-9_]+)\s*$", section.split("read_only:")[0]))
        self.assertEqual(
            names,
            {
                "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_HOST", "DB_PORT",
                "R2_BUCKET_NAME", "R2_ENDPOINT_URL", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
                "BACKUP_AES_PASSPHRASE",
            },
        )

    def test_web_reads_host_env_file_and_db_waits_for_health(self):
        web = compose_services()["web"]
        self.assertRegex(web, r"(?m)^\s+env_file: \.env\s*$")
        self.assertIn("condition: service_healthy", web)

    def test_proxy_serves_shared_static_volume_and_host_provisioned_origin_cert(self):
        proxy = compose_services()["proxy"]
        self.assertIn("psu_prod_static:/srv/static:ro", proxy)
        self.assertIn("/opt/financepsu/secrets/cloudflare-origin.pem", proxy)
        self.assertIn("/opt/financepsu/secrets/cloudflare-origin.key", proxy)
        self.assertGreaterEqual(proxy.count("read_only: true"), 2)
        self.assertIn("psu_prod_static:/app/staticfiles", compose_services()["web"])


class WebImageTests(SimpleTestCase):
    def test_static_root_is_configured_for_collectstatic(self):
        self.assertEqual(Path(settings.STATIC_ROOT), ROOT / "staticfiles")

    def test_gunicorn_serves_project_wsgi_and_migrations_stay_manual(self):
        entrypoint = read_code(WEB_ENTRYPOINT)
        dockerfile = read_code(WEB_DOCKERFILE)
        self.assertIn("collectstatic --noinput", entrypoint)
        self.assertRegex(entrypoint, r"exec gunicorn config\.wsgi:application")
        for text in (entrypoint, dockerfile):
            self.assertNotIn("migrate", text)
            self.assertNotIn("runserver", text)

    def test_image_installs_only_requirements_txt_and_runs_non_root(self):
        dockerfile = read_code(WEB_DOCKERFILE)
        self.assertIn("FROM python:3.12", dockerfile)
        self.assertIn("pip install -r requirements.txt", dockerfile)
        self.assertRegex(dockerfile, r"(?m)^USER app\s*$")

    def test_dockerignore_keeps_secrets_out_of_the_build_context(self):
        entries = {line.strip() for line in DOCKERIGNORE.read_text().splitlines()}
        for required in (".env", ".env.*", ".git", ".venv", "__pycache__"):
            self.assertIn(required, entries)


class NginxConfigTests(SimpleTestCase):
    def setUp(self):
        self.conf = read_code(NGINX_CONF)

    def test_upload_limit_covers_the_phase_6_pdf_ceiling_without_being_unlimited(self):
        match = re.search(r"client_max_body_size\s+(\d+)([kmg]?)\s*;", self.conf, re.I)
        self.assertIsNotNone(match, "client_max_body_size must be set explicitly")
        size = int(match.group(1)) * {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[match.group(2).lower()]
        self.assertGreater(size, MAX_CHAPTER_PDF_BYTES)
        self.assertGreater(size, 0, "0 would disable the limit")

    def test_terminates_origin_tls_with_the_mounted_origin_certificate(self):
        self.assertRegex(self.conf, r"listen 443 ssl")
        self.assertIn("ssl_certificate     /etc/nginx/certs/origin.pem;", self.conf)
        self.assertIn("ssl_certificate_key /etc/nginx/certs/origin.key;", self.conf)
        self.assertRegex(self.conf, r"return 301 https://")

    def test_serves_static_directly_from_the_shared_volume(self):
        self.assertRegex(self.conf, r"location /static/ \{\s*alias /srv/static/;")

    def test_forwards_the_headers_django_expects(self):
        for header in (
            r"Host\s+\$host",
            r"X-Real-IP\s+\$remote_addr",
            r"X-Forwarded-For\s+\$proxy_add_x_forwarded_for",
            r"X-Forwarded-Proto\s+https",
        ):
            self.assertRegex(self.conf, rf"proxy_set_header {header};")
        # Consistent with settings.SECURE_PROXY_SSL_HEADER.
        self.assertEqual(settings.SECURE_PROXY_SSL_HEADER, ("HTTP_X_FORWARDED_PROTO", "https"))

    def test_adds_no_response_caching_and_leaves_hsts_to_django(self):
        self.assertNotIn("proxy_cache", self.conf)
        self.assertNotIn("Strict-Transport-Security", self.conf)


class BackupServiceTests(SimpleTestCase):
    def setUp(self):
        self.script = read_code(BACKUP_ENTRYPOINT)
        self.helper = read_code(BACKUP_R2_HELPER)

    def test_entrypoint_has_exactly_the_backup_and_restore_subcommands(self):
        cases = re.findall(r"(?m)^\s+(\w+)\) do_\w+", self.script)
        self.assertEqual(cases, ["backup", "restore"])

    def test_dump_uses_frozen_flags_and_the_env_host_not_a_hardcoded_one(self):
        self.assertIn("--format=custom", self.script)
        self.assertIn("--no-owner --no-acl", self.script)
        self.assertRegex(self.script, r'pg_dump -h "\$DB_HOST"')
        self.assertNotRegex(self.script, r"-h\s+db\b")
        self.assertIn('export PGPASSWORD="$DB_PASSWORD"', self.script)

    def test_encryption_is_aes256_pbkdf2_with_passphrase_from_the_environment(self):
        self.assertEqual(self.script.count("aes-256-cbc -pbkdf2"), 2)  # encrypt and decrypt
        self.assertNotRegex(self.script, r"-pass\s+pass:", "passphrase must not be on a command line")
        self.assertEqual(self.script.count("-pass env:BACKUP_AES_PASSPHRASE"), 2)

    def test_secrets_are_never_traced_or_printed(self):
        self.assertNotRegex(self.script, r"set\s+-\w*x")
        self.assertNotRegex(self.script, r"xtrace")
        for line in self.script.splitlines():
            if re.match(r"\s*(echo|printf)\b", line):
                self.assertNotRegex(line, r"PASSWORD|PASSPHRASE|SECRET|PGPASSWORD", line)

    def test_only_the_encrypted_artifact_is_uploaded_and_cleanup_always_runs(self):
        self.assertIn('upload "$ENCRYPTED" "$key"', self.script)
        self.assertNotRegex(self.script, r'upload "\$DUMP"')
        self.assertRegex(self.script, r"(?m)^set -eu\s*$")
        self.assertRegex(self.script, r"(?m)^trap cleanup EXIT\s*$")
        self.assertIn('rm -rf "$WORKDIR"', self.script)

    def test_every_stage_fails_loudly(self):
        for stage in ("pg_dump failed", "encryption failed", "upload failed", "download failed",
                      "decryption failed", "pg_restore failed"):
            self.assertIn(stage, self.script)

    def test_restore_takes_one_validated_key_and_refuses_a_non_empty_target(self):
        restore = self.script.split("do_restore() {", 1)[1].split("\ncase ", 1)[0]
        self.assertIn("exactly one argument", restore)
        self.assertIn("validate_key", restore)
        self.assertLess(restore.index("assert_target_empty"), restore.index("download"))
        self.assertIn("is not empty", self.script)
        # No destructive path: never drops or cleans existing data.
        for flag in ("--clean", "--create", "DROP "):
            self.assertNotIn(flag, self.script)
        self.assertIn("--no-owner --no-acl --single-transaction", self.script)

    def test_backup_keys_live_under_their_own_prefix_apart_from_chapter_pdfs(self):
        self.assertIn('PREFIX="backups/postgres/"', self.script)
        self.assertFalse(chapter_object_key(1).startswith("backups/postgres/"))
        self.assertIn('BACKUP_PREFIX = "backups/postgres/"', self.helper)

    def test_r2_client_is_private_and_uses_the_frozen_connection_settings(self):
        for fragment in ('endpoint_url=', 'aws_access_key_id=', 'aws_secret_access_key=', 'region_name="auto"'):
            self.assertIn(fragment, self.helper)
        self.assertNotIn("generate_presigned_url", self.helper)
        self.assertNotIn("ACL", self.helper)

    def test_image_has_pg18_client_openssl_and_boto3_at_the_application_pin(self):
        dockerfile = read_code(BACKUP_DOCKERFILE)
        self.assertIn("FROM python:3.12", dockerfile)
        self.assertIn("postgresql18-client", dockerfile)
        self.assertIn("openssl", dockerfile)
        app_pin = re.search(r"(?m)^boto3==(\S+)$", (ROOT / "requirements.txt").read_text()).group(1)
        self.assertIn(f"boto3=={app_pin}", dockerfile)


class NightlyWorkflowTests(SimpleTestCase):
    def setUp(self):
        self.text = read_code(WORKFLOW)

    def test_runs_at_midnight_utc_and_on_manual_dispatch_only(self):
        self.assertIn('- cron: "0 0 * * *"', self.text)
        self.assertRegex(self.text, r"(?m)^\s+workflow_dispatch:")
        # Backup only: no push/PR trigger, so nothing here can act as push-to-deploy.
        self.assertNotRegex(self.text, r"(?m)^\s+(push|pull_request|pull_request_target|release):")

    def test_references_only_the_three_ssh_secrets(self):
        self.assertEqual(
            set(re.findall(r"secrets\.([A-Za-z0-9_]+)", self.text)),
            {"OCI_SSH_HOST", "OCI_SSH_USER", "OCI_SSH_PRIVATE_KEY"},
        )
        for forbidden in ("DB_", "R2_", "RAZORPAY_", "EMAIL_", "GOOGLE_", "BACKUP_AES"):
            self.assertNotIn(forbidden, self.text)

    def test_only_job_is_the_backup_subcommand_over_ssh(self):
        self.assertIn(f"cd /opt/financepsuguide && {BACKUP_COMMAND}", self.text)
        for other in ("pg_dump", "pg_restore", "openssl", "boto3", "up --build", "git pull", " migrate"):
            self.assertNotIn(other, self.text)

    def test_key_is_ephemeral_locked_down_unechoed_and_hostkey_checking_stays_on(self):
        self.assertIn('chmod 600 "$key_file"', self.text)
        self.assertRegex(self.text, r"trap 'rm -f \"\$key_file\"[^']*' EXIT")
        self.assertIn("StrictHostKeyChecking=yes", self.text)
        self.assertNotIn("StrictHostKeyChecking=no", self.text)
        for line in self.text.splitlines():
            if "OCI_SSH_PRIVATE_KEY" in line and re.search(r"\b(echo|printf)\b", line):
                # The only permitted use writes the key to the ephemeral key file.
                self.assertRegex(line, r'>\s*"\$key_file"\s*$')
        self.assertNotRegex(self.text, r"set\s+-\w*x")


class NoKeyMaterialTests(SimpleTestCase):
    def test_new_ops_files_contain_no_credentials_or_private_keys(self):
        files = [
            COMPOSE, WEB_DOCKERFILE, WEB_ENTRYPOINT, NGINX_CONF, BACKUP_DOCKERFILE,
            BACKUP_ENTRYPOINT, BACKUP_R2_HELPER, WORKFLOW, DOCKERIGNORE,
        ]
        pattern = re.compile(r"BEGIN [A-Z ]*PRIVATE KEY|rzp_(live|test)_[A-Za-z0-9]{6,}|AKIA[0-9A-Z]{12,}")
        for path in files:
            self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")), path.name)
