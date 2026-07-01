CREATE TABLE course_prereqs (
    course_code     TEXT NOT NULL REFERENCES courses(code) ON DELETE CASCADE,
    prereq_code     TEXT NOT NULL REFERENCES courses(code) ON DELETE RESTRICT,
    PRIMARY KEY (course_code, prereq_code)
);
