"""One-time migration: courses.json -> course_planner.db (SQLite).

Run once: `python migrate_to_sqlite.py [--force]`. Re-running without
--force refuses to touch an existing DB file; --force deletes and
rebuilds it from courses.json.

Insert order matters and is deliberately: program -> tracks -> courses
(no course references any other table yet) -> everything that
references courses (prereqs/coreqs/degree_plan_entries/elective
groups+options/students). Since nothing upstream ever references a
downstream table, no deferred foreign keys are needed.
"""

import argparse
import glob
import json
import os
import sys

from data_layer import DB_PATH, connect, load_data_from_db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MIGRATION_DIR = os.path.join(BASE_DIR, "migration")
COURSES_JSON_PATH = os.path.join(BASE_DIR, "courses.json")


def load_source():
    with open(COURSES_JSON_PATH, encoding="utf-8") as f:
        return json.load(f)


def apply_schema(conn):
    """Run each migration/NNN_*.sql file in filename order (NNN encodes the
    table creation order needed to satisfy foreign-key dependencies)."""
    for path in sorted(glob.glob(os.path.join(MIGRATION_DIR, "*.sql"))):
        conn.executescript(open(path, encoding="utf-8").read())


def migrate(data, conn):
    apply_schema(conn)

    program = data["program"]
    conn.execute(
        "INSERT INTO program (id, name_en, name_ar, college, total_credits_required) VALUES (1, ?, ?, ?, ?)",
        (program["name_en"], program["name_ar"], program["college"], program["total_credits_required"]),
    )

    track_names_ar = program.get("track_names_ar", {})
    # Legacy courses.json has no per-track total; every track in it belongs to
    # the single program, so seed each with the program-global total.
    for position, track in enumerate(program["tracks"]):
        conn.execute(
            "INSERT INTO tracks (code, name_ar, position, total_credits_required) VALUES (?, ?, ?, ?)",
            (track, track_names_ar.get(track, track), position,
             program["total_credits_required"]),
        )

    for c in data["courses"]:
        conn.execute(
            "INSERT INTO courses (code, code_en, title_ar, title_en, credits, category, min_credits, verified) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                c["code"], c["code_en"], c["title_ar"], c["title_en"], c["credits"], c["category"],
                c["requirements"].get("min_credits", 0), 1 if c.get("verified", True) else 0,
            ),
        )

    for c in data["courses"]:
        for prereq in c["requirements"].get("courses", []):
            conn.execute(
                "INSERT INTO course_prereqs (course_code, prereq_code) VALUES (?, ?)",
                (c["code"], prereq),
            )
        for coreq in c["requirements"].get("coreqs", []):
            conn.execute(
                "INSERT INTO course_coreqs (course_code, coreq_code) VALUES (?, ?)",
                (c["code"], coreq),
            )

    for track, levels in data["degree_plans"].items():
        for level_key, codes in levels.items():
            for position, code in enumerate(codes):
                conn.execute(
                    "INSERT INTO degree_plan_entries (track_code, level_key, position, course_code) "
                    "VALUES (?, ?, ?, ?)",
                    (track, level_key, position, code),
                )

    for track, groups in data["elective_groups"].items():
        for group_position, group in enumerate(groups):
            cur = conn.execute(
                "INSERT INTO elective_groups (track_code, position, name_en, name_ar, choose_credits) "
                "VALUES (?, ?, ?, ?, ?)",
                (track, group_position, group["name_en"], group["name_ar"], group["choose_credits"]),
            )
            group_id = cur.lastrowid
            for option_position, code in enumerate(group["options"]):
                conn.execute(
                    "INSERT INTO elective_group_options (group_id, position, course_code) VALUES (?, ?, ?)",
                    (group_id, option_position, code),
                )

    for student_id, student in data.get("students", {}).items():
        conn.execute(
            "INSERT INTO students (student_id, name, track_code, completed_credits) VALUES (?, ?, ?, ?)",
            (student_id, student["name"], student["track"], student["completed_credits"]),
        )
        for position, code in enumerate(student.get("completed", [])):
            conn.execute(
                "INSERT INTO student_completed_courses (student_id, position, course_code) VALUES (?, ?, ?)",
                (student_id, position, code),
            )


def normalize_for_compare(data):
    """Order-insensitive view of the parts where SQL row order isn't
    guaranteed to match JSON array order (top-level courses list, and
    each course's prereq/coreq lists — the engine only ever does
    membership checks on those, never indexes into them)."""
    out = json.loads(json.dumps(data))  # deep copy
    # Derived, DB-only fields with no counterpart in the frozen JSON.
    for k in ("track_total_credits", "track_names_en"):
        out.get("program", {}).pop(k, None)
    out["courses"] = {c["code"]: c for c in out["courses"]}
    for c in out["courses"].values():
        c["requirements"]["courses"] = sorted(c["requirements"]["courses"])
        c["requirements"]["coreqs"] = sorted(c["requirements"]["coreqs"])
        # Derived, DB-only field with no counterpart in the frozen JSON.
        c["requirements"].pop("prereqs_by_track", None)
    return out


SCHEMA_KEYS = ("program", "courses", "degree_plans", "elective_groups", "students")


def self_verify(original, conn):
    rebuilt = load_data_from_db(conn)
    a = normalize_for_compare(original)
    b = normalize_for_compare(rebuilt)
    if any(a[key] != b[key] for key in SCHEMA_KEYS):
        for key in SCHEMA_KEYS:
            if a[key] != b[key]:
                print(f"MISMATCH in top-level key: {key!r}", file=sys.stderr)
        raise SystemExit("Self-verification FAILED: rebuilt data does not match courses.json")
    print("Self-verification passed: DB reconstructs courses.json exactly (order-insensitive where expected).")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="overwrite an existing course_planner.db")
    args = parser.parse_args()

    if os.path.exists(DB_PATH):
        if not args.force:
            raise SystemExit(f"{DB_PATH} already exists. Re-run with --force to overwrite it.")
        os.remove(DB_PATH)

    data = load_source()
    conn = connect()
    try:
        conn.execute("BEGIN")
        migrate(data, conn)
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
        raise

    try:
        self_verify(data, conn)

        counts = {}
        for table in (
            "program", "tracks", "courses", "course_prereqs", "course_coreqs",
            "degree_plan_entries", "elective_groups", "elective_group_options",
            "students", "student_completed_courses",
        ):
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print("Row counts:", counts)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
