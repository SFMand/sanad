"""Web UI over db_admin.py's additive authoring operations.

A small, separate admin tool (not the student-facing app.py) for adding
courses/tracks/plan-entries/elective-groups/elective-options without
hand-crafting db_admin.py CLI invocations. Every mutation here calls the
exact same core function the CLI uses, so there is one code path — this
file only adapts sqlite3.IntegrityError into a JSON error response instead
of a CLI SystemExit.

Run:
    python admin_app.py    # serves http://localhost:5050
"""

import os
import sqlite3

from flask import Flask, jsonify, request, send_from_directory

import db_admin
from data_layer import connect

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "admin.html")


@app.get("/api/meta")
def meta():
    """Bootstrap payload for every form's dropdowns/datalists."""
    conn = connect()
    try:
        tracks = [dict(r) for r in conn.execute(
            "SELECT code, name_ar FROM tracks ORDER BY position"
        )]
        categories = [r[0] for r in conn.execute(
            "SELECT DISTINCT category FROM courses ORDER BY category"
        )]
        level_keys = [r[0] for r in conn.execute(
            "SELECT DISTINCT level_key FROM degree_plan_entries ORDER BY level_key"
        )]
        courses = [dict(r) for r in conn.execute(
            "SELECT code, code_en, title_en, credits FROM courses ORDER BY code"
        )]
        elective_groups = [dict(r) for r in conn.execute(
            "SELECT id, track_code, name_en, name_ar, choose_credits FROM elective_groups "
            "ORDER BY track_code, position"
        )]
    finally:
        conn.close()
    return jsonify({
        "tracks": tracks,
        "categories": categories,
        "level_keys": level_keys,
        "courses": courses,
        "elective_groups": elective_groups,
    })


@app.get("/api/courses")
def api_list_courses():
    track = request.args.get("track") or None
    return jsonify(db_admin.list_courses(track))


@app.get("/api/check")
def api_check():
    return jsonify(db_admin.check_violations())


@app.post("/api/courses")
def api_add_course():
    data = request.get_json(force=True, silent=True) or {}
    required = ["code", "code_en", "title_ar", "title_en", "credits", "category"]
    missing = [k for k in required if not data.get(k) and data.get(k) != 0]
    if missing:
        return jsonify({"error": f"missing required field(s): {', '.join(missing)}"}), 400
    try:
        db_admin.add_course(
            code=data["code"], code_en=data["code_en"], title_ar=data["title_ar"],
            title_en=data["title_en"], credits=data["credits"], category=data["category"],
            min_credits=data.get("min_credits") or 0,
            prereqs=data.get("prereqs") or [], coreqs=data.get("coreqs") or [],
            unverified=bool(data.get("unverified")),
        )
    except sqlite3.IntegrityError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "code": data["code"]})


@app.post("/api/tracks")
def api_add_track():
    data = request.get_json(force=True, silent=True) or {}
    required = ["code", "name_ar", "position"]
    missing = [k for k in required if data.get(k) is None or data.get(k) == ""]
    if missing:
        return jsonify({"error": f"missing required field(s): {', '.join(missing)}"}), 400
    try:
        db_admin.add_track(data["code"], data["name_ar"], data["position"])
    except sqlite3.IntegrityError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "code": data["code"]})


@app.post("/api/plan-entries")
def api_add_plan_entry():
    data = request.get_json(force=True, silent=True) or {}
    required = ["track", "level", "course", "position"]
    missing = [k for k in required if data.get(k) is None or data.get(k) == ""]
    if missing:
        return jsonify({"error": f"missing required field(s): {', '.join(missing)}"}), 400
    try:
        db_admin.add_plan_entry(data["track"], data["level"], data["course"], data["position"])
    except sqlite3.IntegrityError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.post("/api/elective-groups")
def api_add_elective_group():
    data = request.get_json(force=True, silent=True) or {}
    required = ["track", "position", "name_en", "name_ar", "choose_credits"]
    missing = [k for k in required if data.get(k) is None or data.get(k) == ""]
    if missing:
        return jsonify({"error": f"missing required field(s): {', '.join(missing)}"}), 400
    try:
        group_id = db_admin.add_elective_group(
            data["track"], data["position"], data["name_en"], data["name_ar"],
            data["choose_credits"],
        )
    except sqlite3.IntegrityError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "id": group_id})


@app.post("/api/elective-options")
def api_add_elective_option():
    data = request.get_json(force=True, silent=True) or {}
    required = ["group_id", "course", "position"]
    missing = [k for k in required if data.get(k) is None or data.get(k) == ""]
    if missing:
        return jsonify({"error": f"missing required field(s): {', '.join(missing)}"}), 400
    try:
        db_admin.add_elective_option(data["group_id"], data["course"], data["position"])
    except sqlite3.IntegrityError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=True)
