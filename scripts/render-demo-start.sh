#!/bin/sh
# Free Render plans have no background workers, so the notifier runs next to the API.
# The loop restarts it if it exits; uvicorn stays PID 1 so Render tracks the API.
(while true; do python -m app.jobs.notifier; sleep 5; done) &
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers --forwarded-allow-ips '*'
