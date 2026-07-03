"""One-off additive seed: load the Computer Engineering major into
course_planner.db as a new track alongside the existing CS tracks.

- Applies migration 011 (per-track total_credits_required) if not present and
  backfills existing tracks to the program-global total (128).
- Inserts only CE courses whose Arabic code is not already in the shared
  catalog (39 overlap with prep/IC/CS courses and are reused as-is).
- New courses are inserted in two passes (rows first, then prereqs/coreqs) so
  intra-CE prerequisite references never hit a missing-FK error regardless of
  file order.
- Adds the CE degree-plan entries and the three elective groups + options.
  هال 445 already exists in the catalog (SWE 445, same title) and is reused as
  the CE dept-elective option per the maintainer's decision.

Idempotency: re-running is a no-op-with-errors — the track/course inserts will
raise IntegrityError on the second run. Intended to run exactly once.
"""

import json
import os
import sqlite3

from data_layer import connect

CE_JSON = r"C:\Users\Admin\Downloads\files (1)\computer_engineering.json"
TRACK = "computer_engineering"


def ensure_per_track_total(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(tracks)")]
    if "total_credits_required" not in cols:
        conn.execute(
            "ALTER TABLE tracks ADD COLUMN total_credits_required INTEGER NOT NULL DEFAULT 0"
        )
    # Backfill any pre-existing track still at the 0 default to the CS total.
    conn.execute(
        "UPDATE tracks SET total_credits_required = 128 WHERE total_credits_required = 0"
    )


def scope_cross_listed_prereqs(conn):
    """هال 445 is cross-listed (SWE 445 on the CS/cyber track, CEN 445 on CE) and
    needs a different prerequisite per major. Its global prereq stays عال 329 (the
    CS definition); add a Computer Engineering override of هال 441 (see migration
    012, course_track_prereqs)."""
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
    conn.execute(
        "INSERT OR IGNORE INTO course_track_prereqs (course_code, track_code, prereq_code) "
        "VALUES ('هال 445','computer_engineering','هال 441')"
    )


def main():
    ce = json.load(open(CE_JSON, encoding="utf-8"))
    prog = ce["program"]
    ce_courses = ce["courses"]
    plan = ce["degree_plans"][TRACK]
    egroups = ce["elective_groups"][TRACK]

    conn = connect()
    try:
        conn.execute("BEGIN")
        ensure_per_track_total(conn)

        existing = {r[0] for r in conn.execute("SELECT code FROM courses")}
        new_courses = [c for c in ce_courses if c["code"] not in existing]

        # Track (position = after the existing tracks).
        pos = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM tracks").fetchone()[0]
        conn.execute(
            "INSERT INTO tracks (code, name_ar, position, total_credits_required) VALUES (?, ?, ?, ?)",
            (TRACK, prog["track_names_ar"][TRACK], pos, prog["total_credits_required"]),
        )

        # Pass 1: course rows (no FK refs yet).
        for c in new_courses:
            conn.execute(
                "INSERT INTO courses (code, code_en, title_ar, title_en, credits, category, min_credits, verified) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (c["code"], c["code_en"], c["title_ar"], c["title_en"], c["credits"],
                 c["category"], c["requirements"].get("min_credits", 0),
                 1 if c.get("verified") else 0),
            )
        # Pass 2: prereqs + coreqs (all referenced codes now exist).
        for c in new_courses:
            for p in c["requirements"].get("courses", []):
                conn.execute(
                    "INSERT INTO course_prereqs (course_code, prereq_code) VALUES (?, ?)",
                    (c["code"], p),
                )
            for q in c["requirements"].get("coreqs", []):
                conn.execute(
                    "INSERT INTO course_coreqs (course_code, coreq_code) VALUES (?, ?)",
                    (c["code"], q),
                )

        # Degree-plan entries.
        for level, codes in plan.items():
            for i, code in enumerate(codes):
                conn.execute(
                    "INSERT INTO degree_plan_entries (track_code, level_key, position, course_code) "
                    "VALUES (?, ?, ?, ?)",
                    (TRACK, level, i, code),
                )

        # Elective groups + options.
        for gpos, g in enumerate(egroups):
            cur = conn.execute(
                "INSERT INTO elective_groups (track_code, position, name_en, name_ar, choose_credits) "
                "VALUES (?, ?, ?, ?, ?)",
                (TRACK, gpos, g["name_en"], g["name_ar"], g["choose_credits"]),
            )
            gid = cur.lastrowid
            for opos, code in enumerate(g["options"]):
                conn.execute(
                    "INSERT INTO elective_group_options (group_id, position, course_code) "
                    "VALUES (?, ?, ?)",
                    (gid, opos, code),
                )

        # Cross-listed prereq scoping (needs هال 441 to exist, inserted above).
        scope_cross_listed_prereqs(conn)

        # Fail loudly before commit if any FK is dangling.
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise SystemExit(f"FK violations, aborting: {violations}")

        conn.commit()
        print(f"OK: track '{TRACK}' added; {len(new_courses)} new courses, "
              f"{sum(len(v) for v in plan.values())} plan entries, "
              f"{len(egroups)} elective groups.")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
