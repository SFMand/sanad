-- Saved plan versions. A "plan" is a named snapshot of a student's forward
-- schedule (the roadmap engine's output) plus the inputs that produced it, so
-- it can be reopened exactly as saved and regenerated if desired.
--
-- Deliberately FK-free: `handle` is a free-text identity (no login / no users
-- table) and `track` is validated in app code (valid_track), not by the DB. A
-- saved plan is a self-contained JSON artifact that must survive independently
-- of later catalog edits, so it references no other table.
CREATE TABLE IF NOT EXISTS plans (
    plan_id           TEXT PRIMARY KEY,   -- generated opaque id
    handle            TEXT NOT NULL,      -- namespacing identity (lower-cased)
    name              TEXT NOT NULL,      -- user-editable label
    track             TEXT NOT NULL,      -- track code, for the list summary
    completed_credits INTEGER,            -- resolved credit total, for the summary
    payload           TEXT NOT NULL,      -- JSON: inputs + roadmap snapshot
    created_at        TEXT NOT NULL,      -- ISO-8601
    updated_at        TEXT NOT NULL       -- ISO-8601
);

CREATE INDEX IF NOT EXISTS idx_plans_handle ON plans(handle);
