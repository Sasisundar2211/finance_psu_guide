"""R2 transfer helper for the backup service (DEPLOYMENT.md §5, REQ-OPS-02).

Called by entrypoint.sh only:

    python r2_transfer.py upload   <local-file> <object-key>
    python r2_transfer.py download  <object-key> <local-file>

The bucket stays private: nothing here creates a public or presigned URL.
Credentials come from the container environment. Failures print an error class
and code only (never a credential) and exit non-zero, so the shell entrypoint
and the SSH/Actions job fail loudly instead of silently.
"""

import os
import sys

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

# Backups live under their own prefix, apart from chapter PDFs (chapters/<id>.pdf).
BACKUP_PREFIX = "backups/postgres/"

_REQUIRED_ENV = (
    "R2_BUCKET_NAME",
    "R2_ENDPOINT_URL",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
)


def fail(message):
    print(f"r2-transfer: {message}", file=sys.stderr)
    sys.exit(1)


def make_client():
    missing = [name for name in _REQUIRED_ENV if not os.environ.get(name, "").strip()]
    if missing:
        fail("missing environment variables: " + ", ".join(missing))
    client = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT_URL"].strip(),
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"].strip(),
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"].strip(),
        region_name="auto",
        # Same R2-proven client options as core/r2.py, with longer timeouts for
        # a dump-sized object.
        config=Config(
            signature_version="s3v4",
            connect_timeout=10,
            read_timeout=120,
            retries={"max_attempts": 3},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )
    return client, os.environ["R2_BUCKET_NAME"].strip()


def describe(exc):
    if isinstance(exc, ClientError):
        return f"{type(exc).__name__} ({exc.response.get('Error', {}).get('Code', 'unknown')})"
    return type(exc).__name__


def check_key(key):
    if not key.startswith(BACKUP_PREFIX) or ".." in key:
        fail(f"object key must be under {BACKUP_PREFIX}")


def upload(path, key):
    check_key(key)
    client, bucket = make_client()
    size = os.path.getsize(path)
    try:
        client.upload_file(path, bucket, key)
        stored = client.head_object(Bucket=bucket, Key=key)["ContentLength"]
    except (BotoCoreError, ClientError) as exc:
        fail(f"upload failed: {describe(exc)}")
    if stored != size:
        fail(f"upload verification failed: local {size} bytes, R2 {stored} bytes")


def download(key, path):
    check_key(key)
    client, bucket = make_client()
    try:
        client.download_file(bucket, key, path)
    except (BotoCoreError, ClientError) as exc:
        fail(f"download failed: {describe(exc)}")


def main(argv):
    if len(argv) != 4 or argv[1] not in ("upload", "download"):
        fail("usage: r2_transfer.py upload <file> <key> | download <key> <file>")
    if argv[1] == "upload":
        upload(argv[2], argv[3])
    else:
        download(argv[2], argv[3])


if __name__ == "__main__":
    main(sys.argv)
