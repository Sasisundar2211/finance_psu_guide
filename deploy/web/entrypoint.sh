#!/bin/sh
# Web container entrypoint (DEPLOYMENT.md §2, §6).
#
# collectstatic runs on every start so the shared static volume always matches
# the running image. Migrations are deliberately NOT run here: they stay an
# explicit operator action (`docker compose ... exec web python manage.py migrate`).
set -eu

python manage.py collectstatic --noinput

# Worker settings are IMPLEMENTATION DETAILS, not frozen values. They are a
# conservative fit for the 2-vCPU OCI baseline and do not prove capacity;
# capacity is measured in the Phase 10 load test.
#   --workers 4       sync workers; the VM also runs Postgres and Nginx.
#   --timeout 120     leaves room for a ~50 MiB admin PDF upload (Nginx buffers
#                     the body first) plus the server-side R2 upload.
#   --worker-tmp-dir  keeps Gunicorn's heartbeat file off the container disk.
#   --no-control-socket  the runtime control interface is unused here, and its
#                     default socket path (/app/.gunicorn) is not writable by
#                     the non-root runtime user.
exec gunicorn config.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers 4 \
    --timeout 120 \
    --graceful-timeout 30 \
    --worker-tmp-dir /dev/shm \
    --no-control-socket \
    --access-logfile - \
    --error-logfile -
