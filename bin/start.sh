#!/usr/bin/env bash
# Production entrypoint for the free single-container deployment: the web
# server and the Celery worker (with beat embedded) share one container.
# If either process exits, this script exits too, so the platform restarts
# the whole container rather than leaving a web server with no worker.
set -euo pipefail

# Free hosting has no "pre-deploy" step or shell, so one-off setup runs here.
python manage.py migrate --noinput
if [ -n "${DJANGO_SUPERUSER_PASSWORD:-}" ]; then
  # Creates it from DJANGO_SUPERUSER_USERNAME / _EMAIL / _PASSWORD only if
  # missing; never changes an existing user's password.
  python manage.py ensure_superuser
fi
if [ -n "${DEMO_OWNER_PASSWORD:-}" ]; then
  python manage.py seed_demo
fi

# One worker process each, to fit in 512 MB. Beat is embedded (--beat): safe
# with exactly one worker; a paid setup should run beat as its own service.
celery -A config worker --beat --concurrency=1 --loglevel=info \
  --without-heartbeat --without-gossip --without-mingle \
  --schedule /tmp/celerybeat-schedule &

gunicorn config.wsgi:application \
  --bind "0.0.0.0:${PORT:-8000}" --workers 1 --threads 4 --timeout 60 \
  --access-logfile - &

# Pass the platform's stop signal on to both, then wait for the first to exit.
trap 'kill -TERM $(jobs -p) 2>/dev/null' TERM INT
wait -n
status=$?
kill -TERM $(jobs -p) 2>/dev/null || true
wait
exit "$status"
