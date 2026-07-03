-- English display name per track. Previously the three CS tracks had their
-- English labels hard-coded in the frontend; with multiple majors (CE, SWE, IS)
-- the label must come from the data so every track renders correctly in English.
ALTER TABLE tracks ADD COLUMN name_en TEXT;
