"""Additive import of another KSU CCIS major/track into course_planner.db.

Generalises seed_computer_engineering.py to any of the study-plan JSON files
(software_engineering.json, is_general.json, is_data_science.json, …). For each
file it:

  * ensures the per-track columns exist (total_credits_required, name_en on
    tracks; track_code on course_prereqs);
  * inserts the track(s), remapping the file's track code when it would collide
    with an existing one (IS ships a track literally called "general");
  * inserts only courses whose Arabic code is not already in the shared catalog
    (two passes: rows first, then prereqs/coreqs, so intra-file references never
    hit a missing FK). New-course prereqs are stored GLOBAL (track-agnostic);
  * for a course that already exists but whose prerequisites differ in this
    major, writes TRACK-SCOPED override rows — the engine treats a track's
    scoped prereq set as a full replacement for the global one
    (see app.effective_prereqs). This is how عال 220 can need عال 111 in SWE but
    ريض 151 on the CS tracks.

Idempotency: intended to run once per file; re-running raises IntegrityError on
the duplicate track insert.
"""

import json
import sqlite3
import sys

from data_layer import connect


def ensure_columns(conn):
    tcols = [r[1] for r in conn.execute("PRAGMA table_info(tracks)")]
    if "total_credits_required" not in tcols:
        conn.execute("ALTER TABLE tracks ADD COLUMN total_credits_required INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE tracks SET total_credits_required = 128 WHERE total_credits_required = 0")
    if "name_en" not in tcols:
        conn.execute("ALTER TABLE tracks ADD COLUMN name_en TEXT")
    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='course_track_prereqs'"
    ).fetchone():
        conn.execute(
            "CREATE TABLE course_track_prereqs ("
            "course_code TEXT NOT NULL REFERENCES courses(code) ON DELETE CASCADE, "
            "track_code  TEXT NOT NULL REFERENCES tracks(code) ON DELETE CASCADE, "
            "prereq_code TEXT NOT NULL REFERENCES courses(code) ON DELETE RESTRICT, "
            "PRIMARY KEY (course_code, track_code, prereq_code))"
        )


def import_file(path, track_config, conn):
    """track_config: {file_track_code: {"code": new_code, "name_en": str,
    "name_ar": str|None}}. name_ar defaults to the file's track_names_ar."""
    data = json.load(open(path, encoding="utf-8"))
    prog = data["program"]
    total = prog["total_credits_required"]
    file_courses = {c["code"]: c for c in data["courses"]}

    existing = {r[0] for r in conn.execute("SELECT code FROM courses")}
    # Global (track-agnostic) prereqs already recorded, for override comparison.
    global_prereqs = {}
    for r in conn.execute("SELECT course_code, prereq_code FROM course_prereqs"):
        global_prereqs.setdefault(r[0], set()).add(r[1])

    # Reference integrity: every code used in plans/electives/prereqs is known.
    known = existing | set(file_courses)
    unresolved = []
    for c in data["courses"]:
        for p in c["requirements"].get("courses", []):
            if p not in known:
                unresolved.append(f"{c['code']} prereq {p}")
    for tcode in prog["tracks"]:
        for lvl, codes in data["degree_plans"][tcode].items():
            unresolved += [f"plan {lvl} {cd}" for cd in codes if cd not in known]
        for g in data["elective_groups"][tcode]:
            unresolved += [f"elective {cd}" for cd in g["options"] if cd not in known]
    if unresolved:
        raise SystemExit(f"Unresolved references in {path}: {unresolved[:20]}")

    # New courses: rows first, then global prereqs/coreqs.
    new_courses = [c for c in data["courses"] if c["code"] not in existing]
    for c in new_courses:
        conn.execute(
            "INSERT INTO courses (code, code_en, title_ar, title_en, credits, category, min_credits, verified) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (c["code"], c["code_en"], c["title_ar"], c["title_en"], c["credits"],
             c["category"], c["requirements"].get("min_credits", 0),
             1 if c.get("verified") else 0),
        )
    for c in new_courses:
        for p in c["requirements"].get("courses", []):
            conn.execute("INSERT INTO course_prereqs (course_code, prereq_code) VALUES (?, ?)",
                         (c["code"], p))
        for q in c["requirements"].get("coreqs", []):
            conn.execute("INSERT INTO course_coreqs (course_code, coreq_code) VALUES (?, ?)",
                         (c["code"], q))

    summary = {"new_courses": len(new_courses), "tracks": [], "overrides": 0}

    for file_tcode in prog["tracks"]:
        cfg = track_config[file_tcode]
        new_code = cfg["code"]
        name_ar = cfg.get("name_ar") or prog["track_names_ar"][file_tcode]
        pos = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM tracks").fetchone()[0]
        conn.execute(
            "INSERT INTO tracks (code, name_ar, position, total_credits_required, name_en) "
            "VALUES (?, ?, ?, ?, ?)",
            (new_code, name_ar, pos, total, cfg.get("name_en")),
        )

        for level, codes in data["degree_plans"][file_tcode].items():
            for i, code in enumerate(codes):
                conn.execute(
                    "INSERT INTO degree_plan_entries (track_code, level_key, position, course_code) "
                    "VALUES (?, ?, ?, ?)",
                    (new_code, level, i, code),
                )

        for gpos, g in enumerate(data["elective_groups"][file_tcode]):
            cur = conn.execute(
                "INSERT INTO elective_groups (track_code, position, name_en, name_ar, choose_credits) "
                "VALUES (?, ?, ?, ?, ?)",
                (new_code, gpos, g["name_en"], g["name_ar"], g["choose_credits"]),
            )
            gid = cur.lastrowid
            for opos, code in enumerate(g["options"]):
                conn.execute(
                    "INSERT INTO elective_group_options (group_id, position, course_code) VALUES (?, ?, ?)",
                    (gid, opos, code),
                )

        # Per-track prereq overrides for shared courses whose requirements differ
        # from the global set. Only for courses that already existed before this
        # file (new courses' prereqs are global and already correct for this track).
        overrides = 0
        for code, c in file_courses.items():
            if code not in existing:
                continue  # newly inserted -> global already equals this file's set
            want = c["requirements"].get("courses", [])
            if set(want) != global_prereqs.get(code, set()):
                for p in want:
                    conn.execute(
                        "INSERT INTO course_track_prereqs (course_code, track_code, prereq_code) VALUES (?, ?, ?)",
                        (code, new_code, p),
                    )
                overrides += 1
        summary["overrides"] += overrides
        summary["tracks"].append({"code": new_code, "name_en": cfg.get("name_en"),
                                  "total": total, "plan_entries": sum(len(v) for v in data["degree_plans"][file_tcode].values()),
                                  "overrides": overrides})
    return summary


# Backfill English names for the tracks that predate the name_en column.
BACKFILL_NAME_EN = {
    "general": "General",
    "ai": "Artificial Intelligence",
    "cyber": "Cyber Security",
    "computer_engineering": "Computer Engineering",
}

FILES = [
    ("software_engineering.json", {
        "software_engineering": {"code": "software_engineering",
                                 "name_en": "Software Engineering"},
    }),
    ("is_general.json", {
        "general": {"code": "is_general", "name_en": "Information Systems — General",
                    "name_ar": "نظم المعلومات — المسار العام"},
    }),
    ("is_data_science.json", {
        "data_science": {"code": "is_data_science", "name_en": "Information Systems — Data Science",
                         "name_ar": "نظم المعلومات — علم وإدارة البيانات"},
    }),
]

BASE = r"C:\Users\Admin\Downloads\files (1)"


def main():
    conn = connect()
    try:
        conn.execute("BEGIN")
        ensure_columns(conn)
        for code, name in BACKFILL_NAME_EN.items():
            conn.execute("UPDATE tracks SET name_en = ? WHERE code = ? AND name_en IS NULL",
                         (name, code))
        for fname, cfg in FILES:
            s = import_file(f"{BASE}\\{fname}", cfg, conn)
            print(f"{fname}: +{s['new_courses']} courses, "
                  f"tracks={[t['code'] for t in s['tracks']]}, overrides={s['overrides']}")
        viol = conn.execute("PRAGMA foreign_key_check").fetchall()
        if viol:
            raise SystemExit(f"FK violations, aborting: {viol}")
        conn.commit()
        print("OK: all files imported.")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
