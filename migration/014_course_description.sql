-- Student-facing course description, bilingual to match every other
-- user-facing text field in this schema (title_en/ar, name_en/ar, ...).
-- DB-level placeholder default so every existing course gets non-null
-- text immediately on migration, with no frontend fallback branching
-- needed for the "no description yet" case.
ALTER TABLE courses ADD COLUMN description_en TEXT NOT NULL DEFAULT 'Description coming soon.';
ALTER TABLE courses ADD COLUMN description_ar TEXT NOT NULL DEFAULT 'الوصف قادم قريبًا.';
