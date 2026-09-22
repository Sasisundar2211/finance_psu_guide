# Production web image: Django 5.2 + Gunicorn on Python 3.12 (DEPLOYMENT.md §2).
# Dependencies come only from requirements.txt. psycopg[binary] ships its own
# libpq, so no compiler or system build toolchain is installed.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# Application code stays root-owned and read-only to the runtime user; only the
# collectstatic target is writable (a named volume shared with the proxy).
COPY . .
RUN mkdir -p /app/staticfiles && chown app:app /app/staticfiles

USER app

EXPOSE 8000

# Runs collectstatic, then Gunicorn. It never runs migrations: those are an
# explicit operator step (DEPLOYMENT.md §6).
ENTRYPOINT ["sh", "/app/deploy/web/entrypoint.sh"]
