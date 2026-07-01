CREATE TABLE courses (
    code        TEXT PRIMARY KEY,
    code_en     TEXT NOT NULL,
    title_ar    TEXT NOT NULL,
    title_en    TEXT NOT NULL,
    credits     INTEGER NOT NULL,
    category    TEXT NOT NULL,
    min_credits INTEGER NOT NULL DEFAULT 0,
    verified    INTEGER NOT NULL DEFAULT 1 CHECK (verified IN (0, 1))
);
CREATE INDEX idx_courses_code_en ON courses(code_en);
