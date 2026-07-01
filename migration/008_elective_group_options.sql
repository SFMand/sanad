CREATE TABLE elective_group_options (
    group_id     INTEGER NOT NULL REFERENCES elective_groups(id) ON DELETE CASCADE,
    position     INTEGER NOT NULL,
    course_code  TEXT NOT NULL REFERENCES courses(code) ON DELETE RESTRICT,
    PRIMARY KEY (group_id, position),
    UNIQUE (group_id, course_code)
);
