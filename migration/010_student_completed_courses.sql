CREATE TABLE student_completed_courses (
    student_id   TEXT NOT NULL REFERENCES students(student_id) ON DELETE CASCADE,
    position     INTEGER NOT NULL,
    course_code  TEXT NOT NULL REFERENCES courses(code) ON DELETE RESTRICT,
    PRIMARY KEY (student_id, course_code)
);
