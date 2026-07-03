#!/bin/sh
# Seed the shared DB volume from the image's baked-in copy on first boot only,
# so app.py and admin_app.py (separate containers) read/write the same file
# and it survives redeploys instead of resetting to the seed every time.
set -e

if [ -n "$COURSE_DB_PATH" ] && [ ! -f "$COURSE_DB_PATH" ]; then
    mkdir -p "$(dirname "$COURSE_DB_PATH")"
    cp /app/course_planner.db "$COURSE_DB_PATH"
fi

exec "$@"
