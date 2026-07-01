CREATE TABLE course_coreqs (
    course_code     TEXT NOT NULL REFERENCES courses(code) ON DELETE CASCADE,
    coreq_code      TEXT NOT NULL REFERENCES courses(code) ON DELETE RESTRICT,
    PRIMARY KEY (course_code, coreq_code)
);
