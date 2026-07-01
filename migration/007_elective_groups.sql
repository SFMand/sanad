CREATE TABLE elective_groups (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    track_code      TEXT NOT NULL REFERENCES tracks(code) ON DELETE CASCADE,
    position        INTEGER NOT NULL,
    name_en         TEXT NOT NULL,
    name_ar         TEXT NOT NULL,
    choose_credits  INTEGER NOT NULL,
    UNIQUE (track_code, position)
);
