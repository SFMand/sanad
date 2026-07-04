#!/bin/sh
# Seed the shared DB volume from the image's baked-in copy on first boot only,
# so app.py and admin_app.py (separate containers) read/write the same file
# and it survives redeploys instead of resetting to the seed every time.
set -e

if [ -n "$COURSE_DB_PATH" ] && [ ! -f "$COURSE_DB_PATH" ]; then
    mkdir -p "$(dirname "$COURSE_DB_PATH")"
    cp /app/course_planner.db "$COURSE_DB_PATH"
fi

# Bring an existing volume's DB schema up to date with any migrations shipped
# in this image since it was last deployed. Idempotent (skips whatever's
# already applied) and safe to run on every boot, on both containers sharing
# the volume — without this, a new migration's column/table is only ever
# present in the image's own baked-in copy, never on an already-seeded volume,
# and the app crashes at import with "no such column".
python migrate_to_sqlite.py --apply-pending

exec "$@"
