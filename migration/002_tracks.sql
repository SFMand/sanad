CREATE TABLE tracks (
    code        TEXT PRIMARY KEY,
    name_ar     TEXT NOT NULL,
    position    INTEGER NOT NULL UNIQUE
);
