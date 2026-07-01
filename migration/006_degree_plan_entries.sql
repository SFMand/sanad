CREATE TABLE degree_plan_entries (
    track_code   TEXT NOT NULL REFERENCES tracks(code) ON DELETE CASCADE,
    level_key    TEXT NOT NULL,
    position     INTEGER NOT NULL,
    course_code  TEXT NOT NULL REFERENCES courses(code) ON DELETE RESTRICT,
    PRIMARY KEY (track_code, level_key, position),
    UNIQUE (track_code, level_key, course_code)
);
