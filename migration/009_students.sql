CREATE TABLE students (
    student_id          TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    track_code          TEXT NOT NULL REFERENCES tracks(code),
    completed_credits   INTEGER NOT NULL
);
