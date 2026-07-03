-- Per-track total credit requirement.
-- Previously the required-credits total lived only on the single `program`
-- row (128 for BSc Computer Science). A second major with a different total
-- (Computer Engineering = 160) cannot share one global value, so each track
-- now carries its own total. Backfill of existing rows is done by the loader
-- (migrate_to_sqlite.py) / the additive seed, not here, to keep this file
-- schema-only like the others.
ALTER TABLE tracks ADD COLUMN total_credits_required INTEGER NOT NULL DEFAULT 0;
