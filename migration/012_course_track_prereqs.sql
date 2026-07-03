-- Per-track prerequisite overrides.
-- The base course_prereqs table holds a course's GLOBAL (track-agnostic)
-- prerequisites. A course that is shared across majors can require a different
-- set in a particular track — e.g. عال 220 needs عال 111 in Software Engineering
-- but ريض 151 on the CS tracks. Rows here override the global set for one track:
-- when a student's track has any row for a course, that scoped set fully
-- replaces the global one (see app.effective_prereqs). Kept in a separate table
-- so a scoped prereq can reuse a code that also appears globally without
-- colliding with course_prereqs' (course_code, prereq_code) primary key.
CREATE TABLE course_track_prereqs (
    course_code TEXT NOT NULL REFERENCES courses(code) ON DELETE CASCADE,
    track_code  TEXT NOT NULL REFERENCES tracks(code) ON DELETE CASCADE,
    prereq_code TEXT NOT NULL REFERENCES courses(code) ON DELETE RESTRICT,
    PRIMARY KEY (course_code, track_code, prereq_code)
);
