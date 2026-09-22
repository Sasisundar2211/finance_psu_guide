# Dedicated one-shot backup/restore image (DEPLOYMENT.md §5, DECISIONS.md D11.3).
#
# Contents: Python 3.12, PostgreSQL 18 client tools (pg_dump / pg_restore / psql,
# matching the postgres:18-alpine server), OpenSSL, and boto3. boto3 is pinned to
# the same version as the application's requirements.txt (a test keeps the two
# pins identical), so no new dependency is introduced.
FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

RUN apk add --no-cache postgresql18-client openssl \
    && pip install boto3==1.43.93 \
    && addgroup -S -g 10001 backup \
    && adduser -S -u 10001 -G backup -h /home/backup backup

COPY entrypoint.sh r2_transfer.py /opt/backup/

USER backup

# One versioned entrypoint, exactly two subcommands: `backup` and `restore <r2-object-key>`.
ENTRYPOINT ["sh", "/opt/backup/entrypoint.sh"]
