CREATE TABLE program (
    id                      INTEGER PRIMARY KEY CHECK (id = 1),
    name_en                 TEXT NOT NULL,
    name_ar                 TEXT NOT NULL,
    college                 TEXT NOT NULL,
    total_credits_required  INTEGER NOT NULL
);
