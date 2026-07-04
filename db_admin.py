"""Safe authoring operations for course_planner.db — usable both as a CLI and
as a library (see admin_app.py, the web UI over these same functions).

Mostly-additive (this is for adding a new major/track's data). `update_course`
is the one exception: it supports full-field editing of an existing course
(title, credits, category, min_credits, verified, description) — everything
else (tracks, plan entries, elective groups/options, and a course's
prereqs/coreqs) remains additive only. Every mutating function runs through a
connection with `PRAGMA foreign_keys = ON`, so a typo'd course code in
a prereq/coreq/course/course_code argument fails loudly with an
IntegrityError instead of silently producing a permanently-locked
course the way a hand-edited courses.json used to.

CLI examples:
    python db_admin.py add-course --code "هعم 101" --code-en "CPE 101" \\
        --title-ar "..." --title-en "Digital Logic Design" --credits 3 \\
        --category core --prereq "عال 111"

    python db_admin.py add-track --code computer_engineering --name-ar "..." --position 3

    python db_admin.py add-plan-entry --track computer_engineering \\
        --level "المستوى 1" --course "هعم 101" --position 0

    python db_admin.py add-elective-group --track computer_engineering \\
        --name-en "CE Electives" --name-ar "..." --choose-credits 6

    python db_admin.py add-elective-option --group-id 12 --course "هعم 101" --position 0

    python db_admin.py update-course --code "عال 212" --description-en "..." \\
        --description-ar "..."

    python db_admin.py list-courses [--track computer_engineering]
    python db_admin.py check
"""

import argparse
import sqlite3

from data_layer import connect


# ---------------------------------------------------------------------------
# Core functions: explicit kwargs in, IntegrityError propagates to the caller
# (the CLI wrappers below convert it to SystemExit; admin_app.py converts it
# to a JSON error response). This is the one place each mutation is defined.
# ---------------------------------------------------------------------------
def add_course(code, code_en, title_ar, title_en, credits, category,
                min_credits=0, prereqs=None, coreqs=None, unverified=False):
    conn = connect()
    try:
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO courses (code, code_en, title_ar, title_en, credits, category, min_credits, verified) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (code, code_en, title_ar, title_en, credits, category,
             min_credits, 0 if unverified else 1),
        )
        for prereq in prereqs or []:
            conn.execute(
                "INSERT INTO course_prereqs (course_code, prereq_code) VALUES (?, ?)",
                (code, prereq),
            )
        for coreq in coreqs or []:
            conn.execute(
                "INSERT INTO course_coreqs (course_code, coreq_code) VALUES (?, ?)",
                (code, coreq),
            )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_course(code, code_en=None, title_ar=None, title_en=None, credits=None,
                   category=None, min_credits=None, verified=None,
                   description_en=None, description_ar=None):
    """Update editable fields on an existing course. Only fields explicitly
    passed (non-None) are written, so a partial call can't clobber the rest
    of the row with NULLs. Raises KeyError if `code` doesn't exist (checked
    explicitly, since an UPDATE ... WHERE that matches zero rows otherwise
    succeeds silently)."""
    fields = {k: v for k, v in {
        "code_en": code_en, "title_ar": title_ar, "title_en": title_en,
        "credits": credits, "category": category, "min_credits": min_credits,
        "verified": verified, "description_en": description_en,
        "description_ar": description_ar,
    }.items() if v is not None}
    if not fields:
        return
    conn = connect()
    try:
        conn.execute("BEGIN")
        if conn.execute("SELECT 1 FROM courses WHERE code = ?", (code,)).fetchone() is None:
            conn.rollback()
            raise KeyError(f"no course with code {code!r}")
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(
            f"UPDATE courses SET {set_clause} WHERE code = ?",
            (*fields.values(), code),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise
    finally:
        conn.close()


def add_track(code, name_ar, position, total_credits_required=128, name_en=None):
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO tracks (code, name_ar, position, total_credits_required, name_en) "
            "VALUES (?, ?, ?, ?, ?)",
            (code, name_ar, position, total_credits_required, name_en),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise
    finally:
        conn.close()


def add_plan_entry(track, level, course, position):
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO degree_plan_entries (track_code, level_key, position, course_code) VALUES (?, ?, ?, ?)",
            (track, level, position, course),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise
    finally:
        conn.close()


def add_elective_group(track, position, name_en, name_ar, choose_credits):
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO elective_groups (track_code, position, name_en, name_ar, choose_credits) "
            "VALUES (?, ?, ?, ?, ?)",
            (track, position, name_en, name_ar, choose_credits),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        conn.rollback()
        raise
    finally:
        conn.close()


def add_elective_option(group_id, course, position):
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO elective_group_options (group_id, position, course_code) VALUES (?, ?, ?)",
            (group_id, position, course),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_courses(track=None):
    """Rows for `track`'s degree plan, or the whole catalog if `track` is None."""
    conn = connect()
    try:
        if track:
            rows = conn.execute(
                "SELECT DISTINCT c.code, c.code_en, c.title_en, c.credits FROM courses c "
                "JOIN degree_plan_entries d ON d.course_code = c.code "
                "WHERE d.track_code = ? ORDER BY c.code",
                (track,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT code, code_en, title_en, credits FROM courses ORDER BY code"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def check_violations():
    """PRAGMA foreign_key_check results across the whole DB, as dicts."""
    conn = connect()
    try:
        rows = conn.execute("PRAGMA foreign_key_check").fetchall()
        return [
            {"table": r[0], "rowid": r[1], "references_table": r[2], "fk_id": r[3]}
            for r in rows
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI wrappers: argparse.Namespace in, prints + SystemExit on rejection
# ---------------------------------------------------------------------------
def _cli_add_course(args):
    try:
        add_course(args.code, args.code_en, args.title_ar, args.title_en, args.credits,
                    args.category, args.min_credits, args.prereq, args.coreq, args.unverified)
    except sqlite3.IntegrityError as e:
        raise SystemExit(f"Rejected: {e}") from e
    print(f"Added course {args.code}.")


def _cli_update_course(args):
    try:
        update_course(
            args.code, code_en=args.code_en, title_ar=args.title_ar, title_en=args.title_en,
            credits=args.credits, category=args.category, min_credits=args.min_credits,
            verified=args.verified, description_en=args.description_en,
            description_ar=args.description_ar,
        )
    except KeyError as e:
        raise SystemExit(f"Rejected: {e}") from e
    except sqlite3.IntegrityError as e:
        raise SystemExit(f"Rejected: {e}") from e
    print(f"Updated course {args.code}.")


def _cli_add_track(args):
    try:
        add_track(args.code, args.name_ar, args.position, args.total_credits, args.name_en)
    except sqlite3.IntegrityError as e:
        raise SystemExit(f"Rejected: {e}") from e
    print(f"Added track {args.code}.")


def _cli_add_plan_entry(args):
    try:
        add_plan_entry(args.track, args.level, args.course, args.position)
    except sqlite3.IntegrityError as e:
        raise SystemExit(f"Rejected: {e}") from e
    print(f"Added {args.course} to {args.track}/{args.level} at position {args.position}.")


def _cli_add_elective_group(args):
    try:
        group_id = add_elective_group(args.track, args.position, args.name_en, args.name_ar,
                                       args.choose_credits)
    except sqlite3.IntegrityError as e:
        raise SystemExit(f"Rejected: {e}") from e
    print(f"Added elective group {group_id} ({args.name_en!r}) to {args.track}.")


def _cli_add_elective_option(args):
    try:
        add_elective_option(args.group_id, args.course, args.position)
    except sqlite3.IntegrityError as e:
        raise SystemExit(f"Rejected: {e}") from e
    print(f"Added {args.course} to elective group {args.group_id} at position {args.position}.")


def _cli_list_courses(args):
    rows = list_courses(args.track)
    for r in rows:
        print(f"{r['code']:12s} {r['code_en']:10s} {r['credits']} cr  {r['title_en']}")
    print(f"\n{len(rows)} course(s).")


def _cli_check(args):
    violations = check_violations()
    if violations:
        for v in violations:
            print(f"VIOLATION: table={v['table']} rowid={v['rowid']} "
                  f"references table={v['references_table']} fk_id={v['fk_id']}")
        raise SystemExit(f"{len(violations)} foreign key violation(s) found.")
    print("No foreign key violations found.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("add-course", help="add a new course to the shared catalog")
    p.add_argument("--code", required=True, help="canonical Arabic code")
    p.add_argument("--code-en", required=True)
    p.add_argument("--title-ar", required=True)
    p.add_argument("--title-en", required=True)
    p.add_argument("--credits", type=int, required=True)
    p.add_argument("--category", required=True)
    p.add_argument("--min-credits", type=int, default=0)
    p.add_argument("--prereq", action="append", metavar="CODE", help="repeatable")
    p.add_argument("--coreq", action="append", metavar="CODE", help="repeatable")
    p.add_argument("--unverified", action="store_true")
    p.set_defaults(func=_cli_add_course)

    p = sub.add_parser("update-course", help="edit fields on an existing course")
    p.add_argument("--code", required=True, help="canonical Arabic code of the course to edit")
    p.add_argument("--code-en")
    p.add_argument("--title-ar")
    p.add_argument("--title-en")
    p.add_argument("--credits", type=int)
    p.add_argument("--category")
    p.add_argument("--min-credits", type=int)
    p.add_argument("--verified", type=int, choices=[0, 1], help="omit to leave unchanged")
    p.add_argument("--description-en")
    p.add_argument("--description-ar")
    p.set_defaults(func=_cli_update_course)

    p = sub.add_parser("add-track", help="add a new track/major")
    p.add_argument("--code", required=True)
    p.add_argument("--name-ar", required=True)
    p.add_argument("--position", type=int, required=True)
    p.add_argument("--total-credits", type=int, default=128,
                   help="total credits required to graduate in this track")
    p.add_argument("--name-en", help="English display name for the track")
    p.set_defaults(func=_cli_add_track)

    p = sub.add_parser("add-plan-entry", help="place a course in a track's degree plan")
    p.add_argument("--track", required=True)
    p.add_argument("--level", required=True, help='e.g. "المستوى 1"')
    p.add_argument("--course", required=True)
    p.add_argument("--position", type=int, required=True)
    p.set_defaults(func=_cli_add_plan_entry)

    p = sub.add_parser("add-elective-group", help="add an elective group to a track")
    p.add_argument("--track", required=True)
    p.add_argument("--position", type=int, required=True)
    p.add_argument("--name-en", required=True)
    p.add_argument("--name-ar", required=True)
    p.add_argument("--choose-credits", type=int, required=True)
    p.set_defaults(func=_cli_add_elective_group)

    p = sub.add_parser("add-elective-option", help="add a course option to an elective group")
    p.add_argument("--group-id", type=int, required=True)
    p.add_argument("--course", required=True)
    p.add_argument("--position", type=int, required=True)
    p.set_defaults(func=_cli_add_elective_option)

    p = sub.add_parser("list-courses", help="list courses (optionally filtered by track)")
    p.add_argument("--track")
    p.set_defaults(func=_cli_list_courses)

    p = sub.add_parser("check", help="run PRAGMA foreign_key_check across the whole DB")
    p.set_defaults(func=_cli_check)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
