"""Private Cloudflare R2 access for chapter PDFs (API.md §3 and §7, DECISIONS.md D10 item 8, D11.4).

boto3 talks to R2 through its S3-compatible API. Nothing here is public: the only
read path is a short-lived presigned GET URL, and the only write path is a
server-side upload from Django Admin. Any missing configuration or R2 failure
raises `R2Error`, which callers treat as "fail closed" — no fake or public URL is
ever produced, and no boto3/botocore detail is passed on to a student.
"""

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings

# API.md §3: the presigned GET URL is valid for 60 seconds. It bounds the
# initial fetch only, not the reading session (D11.4).
PRESIGNED_URL_EXPIRY_SECONDS = 60

# R2 is region-less; "auto" is the region value its S3 API expects. This is a
# protocol constant, not configuration.
_R2_REGION = "auto"


class R2Error(Exception):
    """R2 is unconfigured or an R2 operation failed. The message is safe to log."""


def chapter_object_key(chapter_id):
    """Deterministic private key, so replacing a chapter's PDF overwrites in place."""
    return f"chapters/{chapter_id}.pdf"


def _client_and_bucket():
    bucket = settings.R2_BUCKET_NAME
    endpoint = settings.R2_ENDPOINT_URL
    access_key = settings.R2_ACCESS_KEY_ID
    secret_key = settings.R2_SECRET_ACCESS_KEY
    if not (bucket and endpoint and access_key and secret_key):
        raise R2Error("R2 is not configured")
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=_R2_REGION,
        config=Config(
            signature_version="s3v4",
            connect_timeout=5,
            read_timeout=60,
            retries={"max_attempts": 2},
            # boto3 >= 1.36 adds checksum trailers by default; only send them
            # when an operation requires one, which keeps R2 uploads simple.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )
    return client, bucket


def presign_pdf_get(object_key):
    """A presigned GET URL for one private object, valid for 60 seconds."""
    client, bucket = _client_and_bucket()
    try:
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": object_key},
            ExpiresIn=PRESIGNED_URL_EXPIRY_SECONDS,
        )
    except (BotoCoreError, ClientError) as exc:
        raise R2Error(f"presign failed: {type(exc).__name__}") from None


def upload_chapter_pdf(chapter_id, fileobj):
    """Upload a chapter PDF to its deterministic private key and return that key."""
    client, bucket = _client_and_bucket()
    key = chapter_object_key(chapter_id)
    try:
        client.upload_fileobj(
            fileobj, bucket, key, ExtraArgs={"ContentType": "application/pdf"}
        )
    except (BotoCoreError, ClientError) as exc:
        raise R2Error(f"upload failed: {type(exc).__name__}") from None
    return key
