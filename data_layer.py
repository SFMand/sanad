"""Shared SQLite access: the single place that knows the DB path, the
foreign-key pragma, and how to reconstruct the courses.json-shaped dict
that app.py, migrate_to_sqlite.py, and db_admin.py all rely on."""

import os
import sqlite3
from collections import defaultdict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "course_planner.db")


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
        track_rows = conn.execute("SELECT code, name_ar FROM tracks ORDER BY position").fetchall()
        program = {
            "name_en": program_row["name_en"],
            "name_ar": program_row["name_ar"],
            "college": program_row["college"],
            "total_credits_required": program_row["total_credits_required"],
            "tracks": [t["code"] for t in track_rows],
            "track_names_ar": {t["code"]: t["name_ar"] for t in track_rows},
        }

        course_rows = conn.execute(
            "SELECT code, code_en, title_ar, title_en, credits, category, min_credits, verified FROM courses"
        ).fetchall()
        prereqs_by_course = defaultdict(list)
        for r in conn.execute("SELECT course_code, prereq_code FROM course_prereqs"):
            prereqs_by_course[r["course_code"]].append(r["prereq_code"])
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
                "requirements": {
                    "courses": prereqs_by_course.get(r["code"], []),
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
