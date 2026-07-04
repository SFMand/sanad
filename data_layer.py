"""Shared SQLite access: the single place that knows the DB path, the
foreign-key pragma, and how to reconstruct the courses.json-shaped dict
that app.py, migrate_to_sqlite.py, and db_admin.py all rely on."""

import datetime
import json
import os
import sqlite3
import uuid
from collections import defaultdict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Overridable so app.py and admin_app.py can point at a shared DB file on a
# mounted volume (e.g. two containers writing the same course_planner.db).
DB_PATH = os.environ.get("COURSE_DB_PATH", os.path.join(BASE_DIR, "course_planner.db"))


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def load_data_from_db(conn=None):
    """Reconstruct the exact nested dict shape that used to come from
    json.load()-ing courses.json: {program, courses, degree_plans,
    elective_groups, students}."""
    own_conn = conn is None
    if own_conn:
        conn = connect()
    try:
        program_row = conn.execute(
            "SELECT name_en, name_ar, college, total_credits_required FROM program WHERE id = 1"
        ).fetchone()
        track_rows = conn.execute(
            "SELECT code, name_ar, name_en, total_credits_required FROM tracks ORDER BY position"
        ).fetchall()
        program = {
            "name_en": program_row["name_en"],
            "name_ar": program_row["name_ar"],
            "college": program_row["college"],
            "total_credits_required": program_row["total_credits_required"],
            "tracks": [t["code"] for t in track_rows],
            "track_names_ar": {t["code"]: t["name_ar"] for t in track_rows},
            # English labels; only present for tracks that set one (falls back to
            # the track code in the UI otherwise).
            "track_names_en": {
                t["code"]: t["name_en"] for t in track_rows if t["name_en"]
            },
            # Per-track graduation total; falls back to the program-global value
            # above for any track that predates this column.
            "track_total_credits": {
                t["code"]: t["total_credits_required"] for t in track_rows
            },
        }

        course_rows = conn.execute(
            "SELECT code, code_en, title_ar, title_en, credits, category, min_credits, verified, "
            "description_en, description_ar FROM courses"
        ).fetchall()
        # Global (track-agnostic) prereqs, plus per-track overrides from
        # course_track_prereqs. A track's scoped set fully replaces the global
        # one for that track (see app.effective_prereqs).
        global_prereqs = defaultdict(list)
        for r in conn.execute("SELECT course_code, prereq_code FROM course_prereqs"):
            global_prereqs[r["course_code"]].append(r["prereq_code"])
        track_prereqs = defaultdict(lambda: defaultdict(list))
        if conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='course_track_prereqs'"
        ).fetchone():
            for r in conn.execute(
                "SELECT course_code, track_code, prereq_code FROM course_track_prereqs"
            ):
                track_prereqs[r["course_code"]][r["track_code"]].append(r["prereq_code"])
        coreqs_by_course = defaultdict(list)
        for r in conn.execute("SELECT course_code, coreq_code FROM course_coreqs"):
            coreqs_by_course[r["course_code"]].append(r["coreq_code"])

        courses = []
        for r in course_rows:
            courses.append({
                "code": r["code"],
                "code_en": r["code_en"],
                "title_ar": r["title_ar"],
                "title_en": r["title_en"],
                "credits": r["credits"],
                "category": r["category"],
                "description_en": r["description_en"],
                "description_ar": r["description_ar"],
                "requirements": {
                    "courses": global_prereqs.get(r["code"], []),
                    "prereqs_by_track": dict(track_prereqs.get(r["code"], {})),
                    "min_credits": r["min_credits"],
                    "coreqs": coreqs_by_course.get(r["code"], []),
                },
                "verified": bool(r["verified"]),
            })

        degree_plans = {}
        for r in conn.execute(
            "SELECT track_code, level_key, course_code FROM degree_plan_entries "
            "ORDER BY track_code, level_key, position"
        ):
            degree_plans.setdefault(r["track_code"], {}).setdefault(r["level_key"], []).append(r["course_code"])

        group_rows = conn.execute(
            "SELECT id, track_code, name_en, name_ar, choose_credits FROM elective_groups "
            "ORDER BY track_code, position"
        ).fetchall()
        options_by_group = defaultdict(list)
        for r in conn.execute(
            "SELECT group_id, course_code FROM elective_group_options ORDER BY group_id, position"
        ):
            options_by_group[r["group_id"]].append(r["course_code"])

        elective_groups = {}
        for r in group_rows:
            elective_groups.setdefault(r["track_code"], []).append({
                "name_en": r["name_en"],
                "name_ar": r["name_ar"],
                "choose_credits": r["choose_credits"],
                "options": options_by_group.get(r["id"], []),
            })

        student_rows = conn.execute(
            "SELECT student_id, name, track_code, completed_credits FROM students"
        ).fetchall()
        completed_by_student = defaultdict(list)
        for r in conn.execute(
            "SELECT student_id, course_code FROM student_completed_courses ORDER BY student_id, position"
        ):
            completed_by_student[r["student_id"]].append(r["course_code"])

        students = {}
        for r in student_rows:
            students[r["student_id"]] = {
                "name": r["name"],
                "track": r["track_code"],
                "completed_credits": r["completed_credits"],
                "completed": completed_by_student.get(r["student_id"], []),
            }

        return {
            "program": program,
            "courses": courses,
            "degree_plans": degree_plans,
            "elective_groups": elective_groups,
            "students": students,
        }
    finally:
        if own_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Saved plan versions (see migration/014_plans.sql).
#
# These are the only WRITE path through data_layer. A "plan" is a named,
# handle-scoped snapshot of a student's forward schedule: the roadmap engine's
# output plus the inputs that produced it, stored as a JSON `payload`. The
# app.py endpoints recompute the roadmap server-side before saving, so the
# stored snapshot is authoritative (code handles truth), never client-supplied.
# ---------------------------------------------------------------------------

_PLANS_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    plan_id           TEXT PRIMARY KEY,
    handle            TEXT NOT NULL,
    name              TEXT NOT NULL,
    track             TEXT NOT NULL,
    completed_credits INTEGER,
    payload           TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plans_handle ON plans(handle);
"""


def ensure_plans_table(conn=None):
    """Create the plans table if it's missing. Idempotent, so it's safe to call
    at startup against the committed course_planner.db (which predates this
    table) as well as any freshly migrated DB."""
    own_conn = conn is None
    if own_conn:
        conn = connect()
    try:
        conn.executescript(_PLANS_SCHEMA)
        conn.commit()
    finally:
        if own_conn:
            conn.close()


def _norm_handle(handle):
    return (handle or "").strip().lower()


def _now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _plan_summary(row):
    """Lightweight list-item view: the metadata + the snapshot's projected
    graduation, without shipping the whole payload."""
    payload = json.loads(row["payload"])
    rm = payload.get("roadmap") or {}
    return {
        "plan_id": row["plan_id"],
        "name": row["name"],
        "track": row["track"],
        "completed_credits": row["completed_credits"],
        "projected_grad": rm.get("projected_grad"),
        "projected_grad_ar": rm.get("projected_grad_ar"),
        "projected_terms": rm.get("projected_terms"),
        "complete": rm.get("complete"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def save_plan(handle, name, track, completed_credits, payload_dict, plan_id=None):
    """Insert a new plan, or update an existing one when `plan_id` is given.
    Returns the saved plan's summary (with payload), or None if `plan_id` was
    supplied but no matching plan exists for this handle."""
    handle = _norm_handle(handle)
    name = (name or "").strip()
    payload = json.dumps(payload_dict, ensure_ascii=False)
    conn = connect()
    try:
        ensure_plans_table(conn)
        now = _now_iso()
        if plan_id:
            cur = conn.execute(
                "UPDATE plans SET name = ?, track = ?, completed_credits = ?, "
                "payload = ?, updated_at = ? WHERE plan_id = ? AND handle = ?",
                (name, track, completed_credits, payload, now, plan_id, handle),
            )
            if cur.rowcount == 0:
                return None
        else:
            plan_id = uuid.uuid4().hex[:12]
            conn.execute(
                "INSERT INTO plans (plan_id, handle, name, track, completed_credits, "
                "payload, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (plan_id, handle, name, track, completed_credits, payload, now, now),
            )
        conn.commit()
        return get_plan(plan_id, handle, conn)
    finally:
        conn.close()


def list_plans(handle, conn=None):
    """All saved plans for a handle, newest-updated first (summaries only)."""
    handle = _norm_handle(handle)
    own_conn = conn is None
    if own_conn:
        conn = connect()
    try:
        ensure_plans_table(conn)
        rows = conn.execute(
            "SELECT plan_id, name, track, completed_credits, payload, created_at, "
            "updated_at FROM plans WHERE handle = ? ORDER BY updated_at DESC, rowid DESC",
            (handle,),
        ).fetchall()
        return [_plan_summary(r) for r in rows]
    finally:
        if own_conn:
            conn.close()


def get_plan(plan_id, handle=None, conn=None):
    """A single plan's summary plus its full `payload` (inputs + roadmap
    snapshot). Scoped to `handle` when given. None if not found."""
    own_conn = conn is None
    if own_conn:
        conn = connect()
    try:
        ensure_plans_table(conn)
        if handle is not None:
            row = conn.execute(
                "SELECT * FROM plans WHERE plan_id = ? AND handle = ?",
                (plan_id, _norm_handle(handle)),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
        if row is None:
            return None
        summary = _plan_summary(row)
        summary["payload"] = json.loads(row["payload"])
        return summary
    finally:
        if own_conn:
            conn.close()


def rename_plan(plan_id, handle, name):
    """Rename a plan. Returns True if a row was updated."""
    conn = connect()
    try:
        ensure_plans_table(conn)
        cur = conn.execute(
            "UPDATE plans SET name = ?, updated_at = ? WHERE plan_id = ? AND handle = ?",
            ((name or "").strip(), _now_iso(), plan_id, _norm_handle(handle)),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_plan(plan_id, handle):
    """Delete a plan. Returns True if a row was removed."""
    conn = connect()
    try:
        ensure_plans_table(conn)
        cur = conn.execute(
            "DELETE FROM plans WHERE plan_id = ? AND handle = ?",
            (plan_id, _norm_handle(handle)),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
