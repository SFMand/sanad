"""
CS Course Planner — degree-plan advising assistant for the BSc in Computer Science
at the College of Computer and Information Sciences (CCIS), King Saud University (KSU).

Design principle (the whole point of this project):
    The language model NEVER guesses prerequisites. A deterministic Python engine
    computes eligibility from course_planner.db and passes the verified result to the model.
    Code handles truth; the model only explains and recommends FROM that result.

Data model (course_planner.db, see migration/*.sql):
    The CANONICAL course id is the Arabic code (e.g. "عال 212") — it matches the
    transcript / EduGate record. code_en / title_en / title_ar are DISPLAY ONLY.
    All internal logic keys on the Arabic "code". courses.json is frozen historical
    seed data (migrated once via migrate_to_sqlite.py); it is no longer read at
    runtime — use db_admin.py to add courses/tracks/plan entries going forward.

Run:
    pip install flask google-genai
    sudo apt-get install -y poppler-utils    # pdftotext, for transcript upload
    export GEMINI_API_KEY=...                 # required only for the /chat endpoint
    python app.py                             # serves http://localhost:5000
    python app.py selftest                    # acceptance checks (no API key needed)
"""

import os
import re
import tempfile

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory

import transcript_parser

# ---------------------------------------------------------------------------
# LLM provider (Google Gemini, via the CURRENT `google-genai` SDK).
# The import is guarded so the deterministic engine and `selftest` run even if
# the SDK isn't installed. The actual call is isolated in call_llm() below so
# the provider can be swapped in exactly one place.
# ---------------------------------------------------------------------------
try:
    from google import genai
    from google.genai import types
    _GENAI_IMPORT_ERROR = None
except Exception as _e:  # pragma: no cover - only hit when SDK missing
    genai = None
    types = None
    _GENAI_IMPORT_ERROR = _e

# Model: gemini-2.5-pro is the most capable.
# Swap to "gemini-2.5-flash" for a faster / cheaper option.
MODEL = "gemini-2.5-flash"

_client = None


def _get_client():
    """Lazily create the Gemini client so the engine works without an API key."""
    global _client
    if _client is None:
        if genai is None:
            raise RuntimeError(
                "google-genai is not installed. Run: pip install google-genai "
                f"(import error: {_GENAI_IMPORT_ERROR})"
            )
        _client = genai.Client()  # reads GEMINI_API_KEY from environment
    return _client


def call_llm(system_prompt, messages):
    """The ONE place that talks to the LLM provider.

    messages: [{"role": "user"|"assistant", "content": "..."}]
    Gemini roles are "user" and "model" (NOT "assistant"); we map accordingly.
    """
    client = _get_client()
    contents = [
        types.Content(
            role="model" if m["role"] == "assistant" else "user",
            parts=[types.Part.from_text(text=m["content"])],
        )
        for m in messages
    ]
    resp = client.models.generate_content(
        model=MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            # Low temperature: this is an advising tool, we want stable answers.
            temperature=0.2,
        ),
    )
    return resp.text


# ---------------------------------------------------------------------------
# Knowledge base
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


from data_layer import load_data_from_db

DB = load_data_from_db()
PROGRAM = DB["program"]
COURSES = {c["code"]: c for c in DB["courses"]}      # Arabic code -> course
STUDENTS = DB.get("students", {})                    # id -> student
TOTAL_REQUIRED = PROGRAM["total_credits_required"]  # program-global fallback
TRACK_TOTALS = PROGRAM.get("track_total_credits", {})
TRACKS = PROGRAM["tracks"]
DEFAULT_TRACK = TRACKS[0]
TRACK_NAMES_AR = PROGRAM.get("track_names_ar", {})


def total_for(track):
    """Graduation credit total for a track (majors differ: CS=128, CE=160)."""
    return TRACK_TOTALS.get(track) or TOTAL_REQUIRED


def _norm_en(s):
    """Normalize a Latin code so 'csc212' / 'CSC 212' / ' csc  212 ' -> 'CSC 212'."""
    s = str(s).strip().upper()
    m = re.match(r"^([A-Z]+)\s*0*([0-9]+)$", s)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    return re.sub(r"\s+", " ", s)


# English-code lookup so a student can paste either the Arabic or the English code.
BY_CODE_EN = {_norm_en(c["code_en"]): c["code"] for c in DB["courses"]}


def to_canonical(raw):
    """Map any incoming code (Arabic canonical, English display, or loosely
    spaced) to the canonical Arabic code. Unknown shapes are returned as-is."""
    if not isinstance(raw, str):
        return ""
    s = raw.strip()
    if s in COURSES:
        return s
    s2 = re.sub(r"\s+", " ", s)
    if s2 in COURSES:
        return s2
    en = _norm_en(s)
    if en in BY_CODE_EN:
        return BY_CODE_EN[en]
    return s2


def normalize_completed(completed):
    """Canonical Arabic codes that actually exist in the catalog (deduped)."""
    out, seen = [], set()
    for raw in completed or []:
        code = to_canonical(raw)
        if code in COURSES and code not in seen:
            seen.add(code)
            out.append(code)
    return out


def valid_track(track):
    return track if track in TRACKS else DEFAULT_TRACK


# ---------------------------------------------------------------------------
# Deterministic eligibility engine  (THIS is the source of truth)
#
# Co-requisites ("مرافق") may be taken in the SAME term, so a course is eligible
# if each coreq is already completed OR could itself be taken this term. This is
# implemented non-recursively via prereq_only_ok (one level deep is enough,
# because a coreq only needs its own prerequisites to be co-registerable).
#
# `completed` is a collection of canonical Arabic codes.
# `credits` is the student's COMPLETED credit total (from the transcript parser's
# completed_credits, or the sum of manually-entered course credits). Do NOT assume
# completed credits equal the sum of catalog-matched courses — older transcripts
# carry prep courses that are not in this catalog.
# ---------------------------------------------------------------------------
def effective_prereqs(code, track):
    """Prerequisites for `code` as seen by a student in `track`. A course that is
    shared across majors can carry a different prerequisite set per track: if the
    track has its own scoped set it fully OVERRIDES the global one, otherwise the
    global (track-agnostic) set applies. E.g. عال 220 needs عال 111 in Software
    Engineering but ريض 151 on the CS tracks."""
    r = COURSES[code]["requirements"]
    by_track = r.get("prereqs_by_track", {})
    if track in by_track:
        return by_track[track]
    return list(r["courses"])


def prereq_only_ok(code, completed, credits, track):
    if code not in COURSES:
        return False
    r = COURSES[code]["requirements"]
    return all(p in completed for p in effective_prereqs(code, track)) and credits >= r["min_credits"]


def is_eligible(code, completed, credits, track):
    if code not in COURSES or code in completed:
        return False
    r = COURSES[code]["requirements"]
    if not all(p in completed for p in effective_prereqs(code, track)):
        return False
    if credits < r["min_credits"]:
        return False
    for co in r["coreqs"]:                       # coreq: done OR co-registerable this term
        if co in completed or prereq_only_ok(co, completed, credits, track):
            continue
        return False
    return True


def eligible_now(completed, credits, track):
    return [c for c in COURSES if is_eligible(c, completed, credits, track)]


def missing_for(code, completed, credits, track):
    """What blocks `code` right now for a student in `track` (coreqs that are
    themselves co-registerable this term are NOT counted as blockers)."""
    r = COURSES[code]["requirements"]
    return {
        "missing_courses": [p for p in effective_prereqs(code, track) if p not in completed],
        "missing_coreqs": [
            co for co in r["coreqs"]
            if not (co in completed or prereq_only_ok(co, completed, credits, track))
        ],
        "credit_gap": max(0, r["min_credits"] - credits),
    }


# ---------------------------------------------------------------------------
# Per-track "remaining" (required courses + elective-group gaps)
# ---------------------------------------------------------------------------
def remaining_required(completed, track):
    plan = DB["degree_plans"][track]
    return [c for lvl in plan.values() for c in lvl if c not in completed]


def elective_gaps(completed, track):
    gaps = []
    for g in DB["elective_groups"][track]:
        done = sum(COURSES[c]["credits"] for c in g["options"] if c in completed)
        if done < g["choose_credits"]:
            gaps.append({
                "group": g["name_en"],
                "group_ar": g["name_ar"],
                "choose_credits": g["choose_credits"],
                "done_credits": done,
                "remaining_credits": g["choose_credits"] - done,
            })
    return gaps


# ---------------------------------------------------------------------------
# Advising record + system prompt  (the RAG context handed to the model)
# ---------------------------------------------------------------------------
def _view(code):
    c = COURSES[code]
    return {
        "code": c["code"],
        "code_en": c["code_en"],
        "title_en": c["title_en"],
        "title_ar": c["title_ar"],
        "credits": c["credits"],
        "verified": c["verified"],
    }


def build_advising_record(completed, credits, track):
    """Structured, deterministic record the model must treat as ground truth."""
    completed = normalize_completed(completed)
    completed_set = set(completed)
    track = valid_track(track)
    elig = eligible_now(completed_set, credits, track)
    elig_set = set(elig)

    req_remaining = [c for c in remaining_required(completed_set, track)
                     if c in COURSES]

    blocked = []
    for code in req_remaining:
        if code in elig_set:
            continue
        mr = missing_for(code, completed_set, credits, track)
        v = _view(code)
        v.update(mr)
        blocked.append(v)

    tot = total_for(track)
    return {
        "track": track,
        "track_name_ar": TRACK_NAMES_AR.get(track, track),
        "completed_credits": credits,
        "total_required": tot,
        "credits_remaining": max(0, tot - credits),
        "completed": [_view(c) for c in completed],
        "eligible_now": [_view(c) for c in elig],
        "remaining_required": [_view(c) for c in req_remaining],
        "remaining_required_blocked": blocked,
        "elective_gaps": elective_gaps(completed_set, track),
    }


def build_plan_view(completed, credits, track):
    """Year-by-year grid model for the UI, colored by per-course STATE.

    Eligibility/state is computed here (engine = single source of truth); the UI
    only renders it. The 8 plan levels group into 4 years (2 semesters each).
    """
    completed = normalize_completed(completed)
    completed_set = set(completed)
    track = valid_track(track)
    elig_set = set(eligible_now(completed_set, credits, track))

    plan = DB["degree_plans"][track]
    plan_codes = set()

    # level key -> level number, sorted ascending.
    def level_num(key):
        m = re.search(r"\d+", str(key))
        return int(m.group()) if m else 0

    levels = sorted(plan.keys(), key=level_num)
    years = {}
    for key in levels:
        n = level_num(key)
        sem_courses = []
        for code in plan[key]:
            if code not in COURSES:
                continue
            plan_codes.add(code)
            if code in completed_set:
                state = "completed"
            elif code in elig_set:
                state = "eligible"
            else:
                state = "locked"
            v = _view(code)
            v["state"] = state
            v.update(missing_for(code, completed_set, credits, track)
                     if state == "locked" else
                     {"missing_courses": [], "missing_coreqs": [], "credit_gap": 0})
            sem_courses.append(v)
        year = (n + 1) // 2 if n else 0
        years.setdefault(year, []).append({
            "level": key, "level_num": n, "courses": sem_courses,
        })

    other_completed = [c for c in completed if c not in plan_codes]

    tot = total_for(track)
    return {
        "track": track,
        "track_name_ar": TRACK_NAMES_AR.get(track, track),
        "completed_credits": credits,
        "total_required": tot,
        "credits_remaining": max(0, tot - credits),
        "years": [
            {"year": y, "semesters": years[y]} for y in sorted(years)
        ],
        "eligible_codes": sorted(elig_set),
        "other_completed": [_view(c) for c in other_completed],
        "elective_gaps": elective_gaps(completed_set, track),
    }


SYSTEM_PROMPT_BASE = """You are the academic advising assistant for the BSc in Computer Science at the College of Computer and Information Sciences (CCIS), King Saud University (KSU). You help students plan which courses to take, for their chosen track (general / AI / cyber security).

HOW YOU MUST WORK (non-negotiable):
- A deterministic engine has already computed this student's record (the "AUTHORITATIVE STUDENT RECORD" below). Treat it as ground truth. It OVERRIDES any belief you have about KSU prerequisites, credits, tracks, or offerings. Never re-derive eligibility yourself.
- Recommend courses ONLY from the "ELIGIBLE NOW" list. Never recommend a course that is not on that list, and never recommend a course with unmet prerequisites.
- If the student asks about a course in the "NOT YET ELIGIBLE" list: clearly say they cannot take it yet, name the EXACT missing prerequisite course(s) and/or the credit threshold shown for it, then give the shortest unlock path (which missing items they can take now, marked "available now").
- Co-requisites ("مرافق") may be taken in the SAME term. When an eligible course has a co-requisite that is not yet done, tell the student it can be taken ALONGSIDE that co-requisite this term. Do not treat a co-registerable coreq as a blocker.
- A course marked "(not yet verified)" is OCR-ambiguous seed data. Whenever you recommend or discuss one, append a short caveat in your reply language, e.g. "(not yet verified against the official plan — confirm with your advisor)" / "(غير مُتحقق منه بعد مقابل الخطة الرسمية — تأكّد مع مرشدك)". Never present an unverified course as authoritative.
- NEVER invent or guess course codes, titles, prerequisites, or credit values. Use only the data below. If something isn't in the data, say you don't have it and refer the student to their academic advisor.
- Always cite courses by their Arabic code AND title, e.g. "عال 311 — تصميم وتحليل الخوارزميات". You may add the English code/title in parentheses.
- Be concise. For a recommendation give a short list (about 3-5 courses), each with one line of reasoning. Do not dump the whole catalog.
- Format every reply in Markdown so it renders cleanly: use **bold** for course codes and titles, a bullet list (one course per line) for any list of courses, and a short **bold sub-header** when it helps scanning. Keep it compact — avoid large headings (#) and tables. This applies in both Arabic and English.
- Write your ENTIRE reply in the language given under "REPLY LANGUAGE" below. This is fixed by the student's UI setting — do NOT switch languages based on the script in the student's message. (Course codes are always written in Arabic per the rule above; that never changes your reply language.)
- You are a planning aid, NOT a replacement for the official academic advisor. When something is uncertain, depends on section availability or exceptions, or falls outside this data, say so and defer to a human advisor and the official study plan.
"""


def _fmt_course(c):
    label = f'{c["code"]} — {c["title_ar"]} ({c["title_en"]}, {c["code_en"]}) [{c["credits"]} cr]'
    if not c.get("verified", True):
        label += " (not yet verified)"
    return label


def build_system_prompt(completed, credits, track, lang="en"):
    """Base instructions + the serialized authoritative record (per track).

    Thin wrapper kept for selftest/back-compat; `/chat` builds the record once and
    calls `format_system_prompt` directly so it can also return eligible_codes.
    """
    rec = build_advising_record(completed, credits, track)
    return format_system_prompt(rec, lang)


def format_system_prompt(rec, lang="en"):
    """Serialize a prebuilt advising record into the model's system prompt."""
    elig_codes = {c["code"] for c in rec["eligible_now"]}

    lines = [SYSTEM_PROMPT_BASE, ""]
    lines.append("===== AUTHORITATIVE STUDENT RECORD (computed by the engine — ground truth) =====")
    lines.append(f'Program: {PROGRAM["name_en"]} ({PROGRAM["name_ar"]}) — {PROGRAM["college"]}')
    lines.append(f'Track: {rec["track"]} — {rec["track_name_ar"]}')
    lines.append(f'Credits completed: {rec["completed_credits"]} / {rec["total_required"]} '
                 f'(remaining toward the {rec["total_required"]}-credit requirement: {rec["credits_remaining"]})')
    ui_lang = "Arabic" if lang == "ar" else "English"
    lines.append(f"REPLY LANGUAGE: Write your entire reply in {ui_lang}. This is set by the "
                 f"student's UI and is non-negotiable — reply in {ui_lang} even if the student's "
                 f"message contains Arabic course codes or is written in the other language.")
    lines.append("")

    lines.append(f'COMPLETED COURSES ({len(rec["completed"])}):')
    lines += [f"  - {_fmt_course(c)}" for c in rec["completed"]] or ["  (none yet)"]
    lines.append("")

    lines.append(f'ELIGIBLE NOW — recommend ONLY from this list ({len(rec["eligible_now"])}):')
    lines += [f"  - {_fmt_course(c)}" for c in rec["eligible_now"]] or ["  (none)"]
    lines.append("")

    lines.append('NOT YET ELIGIBLE (required for this track) — do NOT recommend; if asked, '
                 f'explain the blocker ({len(rec["remaining_required_blocked"])}):')
    if rec["remaining_required_blocked"]:
        for b in rec["remaining_required_blocked"]:
            reasons = []
            if b["missing_courses"]:
                parts = []
                for mc in b["missing_courses"]:
                    tag = " (available now)" if mc in elig_codes else ""
                    parts.append(f'{mc} — {COURSES[mc]["title_ar"]}{tag}')
                reasons.append("missing prerequisite(s): " + "; ".join(parts))
            if b["missing_coreqs"]:
                parts = [f'{co} — {COURSES[co]["title_ar"]}' for co in b["missing_coreqs"]]
                reasons.append("missing co-requisite(s) (not yet co-registerable): " + "; ".join(parts))
            if b["credit_gap"] > 0:
                reasons.append(f'needs {b["credit_gap"]} more credit(s) (has {rec["completed_credits"]})')
            label = f'{b["code"]} — {b["title_ar"]} ({b["title_en"]})'
            if not b["verified"]:
                label += " (not yet verified)"
            lines.append(f"  - {label}: " + " | ".join(reasons or ["(no blocker recorded)"]))
    else:
        lines.append("  (none — every remaining required course for this track is currently eligible)")
    lines.append("")

    lines.append(f'REMAINING REQUIRED FOR THIS TRACK ({len(rec["remaining_required"])} courses):')
    lines += [f"  - {_fmt_course(c)}" for c in rec["remaining_required"]] or ["  (none — all required courses complete)"]
    lines.append("")

    lines.append("TRACK ELECTIVE GAPS (credits still needed per elective group):")
    if rec["elective_gaps"]:
        for g in rec["elective_gaps"]:
            lines.append(f'  - {g["group"]} ({g["group_ar"]}): '
                         f'{g["done_credits"]} of {g["choose_credits"]} credits done '
                         f'({g["remaining_credits"]} more needed)')
    else:
        lines.append("  (none — all track elective requirements satisfied)")
    lines.append("")
    lines.append("===== END RECORD =====")
    return "\n".join(lines)


def resolve_credits(completed, completed_credits):
    """Completed-credit total to feed the engine: prefer the explicit value from
    the transcript parser; fall back to summing catalog-matched course credits."""
    if isinstance(completed_credits, (int, float)) and completed_credits >= 0:
        return int(completed_credits)
    return sum(COURSES[c]["credits"] for c in normalize_completed(completed))


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/courses")
def courses():
    """Catalog + per-track degree plans + elective groups + demo students."""
    return jsonify({
        "program": PROGRAM,
        "courses": DB["courses"],
        "degree_plans": DB["degree_plans"],
        "elective_groups": DB["elective_groups"],
        "students": STUDENTS,
    })


@app.post("/upload_transcript")
def upload_transcript():
    """Accept an EduGate academic-record PDF, parse the passed courses, and return
    the parsed result for the student to confirm/edit before it drives any advice."""
    file = request.files.get("file")
    if file is None or not file.filename:
        return jsonify({"error": "no file uploaded (form field 'file')"}), 400

    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    try:
        file.save(tmp.name)
        tmp.close()
        parsed = transcript_parser.parse_transcript(tmp.name)
    except Exception as e:
        return jsonify({"error": f"could not parse transcript: {e}"}), 500
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    # Annotate each parsed course with catalog status so the UI can flag
    # unverified / unknown codes during the confirm step.
    annotated = []
    for d in parsed["details"]:
        c = COURSES.get(d["code"])
        annotated.append({
            **d,
            "in_catalog": c is not None,
            "verified": (c or {}).get("verified", None),
            "title_en": (c or {}).get("title_en"),
            "code_en": (c or {}).get("code_en"),
        })

    return jsonify({
        "completed": parsed["completed"],
        "completed_credits": parsed["completed_credits"],
        "in_progress": parsed["in_progress"],
        "details": annotated,
    })


@app.post("/plan")
def plan():
    """Per-course state for the year-by-year grid (pure engine — no API key)."""
    data = request.get_json(force=True, silent=True) or {}
    completed = data.get("completed", [])
    track = valid_track(data.get("track", DEFAULT_TRACK))
    credits = resolve_credits(completed, data.get("completed_credits"))
    return jsonify(build_plan_view(completed, credits, track))


@app.post("/chat")
def chat():
    data = request.get_json(force=True, silent=True) or {}
    messages = data.get("messages", [])
    completed = data.get("completed", [])
    completed_credits = data.get("completed_credits")
    track = valid_track(data.get("track", DEFAULT_TRACK))
    lang = data.get("lang", "en")

    if not isinstance(messages, list) or not messages:
        return jsonify({"error": "messages must be a non-empty list"}), 400

    credits = resolve_credits(completed, completed_credits)
    # Build the authoritative record ONCE; the prompt and the eligible-code list
    # the UI uses for recommendation cards come from the same VERIFIED record.
    rec = build_advising_record(completed, credits, track)
    system_prompt = format_system_prompt(rec, lang)

    try:
        reply = call_llm(system_prompt, messages)
    except Exception as e:  # surface a clean message to the UI (e.g. missing key)
        return jsonify({"error": f"LLM call failed: {e}"}), 500

    return jsonify({
        "reply": reply,
        "eligible_codes": [c["code"] for c in rec["eligible_now"]],
    })


# ---------------------------------------------------------------------------
# Self-test: the four acceptance checks (no API key needed)
# ---------------------------------------------------------------------------
def run_selftest():
    print("=" * 72)
    print("CS Course Planner — eligibility engine acceptance tests")
    print("=" * 72)
    catalog_credits = sum(c["credits"] for c in DB["courses"])
    print(f"Loaded course_planner.db: {len(COURSES)} courses, {catalog_credits} catalog credits; "
          f"program requires {TOTAL_REQUIRED}. Tracks: {', '.join(TRACKS)}.\n")

    results = []

    def check(name, condition, detail=""):
        results.append(bool(condition))
        print(f"  [{'PASS' if condition else 'FAIL'}] {name}")
        if detail:
            print(f"         {detail}")

    # ---- Test 1: demo_real (general, 98 cr) ----
    print("Test 1 — demo_real (general, 98 cr):")
    s = STUDENTS["demo_real"]
    comp = set(normalize_completed(s["completed"]))
    cr = s["completed_credits"]
    elig = eligible_now(comp, cr, "general")
    rem = remaining_required(comp, "general")
    check("eligible_now is non-empty", len(elig) > 0,
          f"{len(elig)} eligible: {', '.join(elig)}")
    check("remaining_required lists ~10 courses", 8 <= len(rem) <= 12,
          f"{len(rem)} remaining: {', '.join(rem)}")
    check("is_eligible('عال 496') is True (min_credits 90 met, coreq عال 343 completed)",
          is_eligible("عال 496", comp, cr, "general"),
          f"min_credits={COURSES['عال 496']['requirements']['min_credits']}, "
          f"coreq عال 343 completed={'عال 343' in comp}")
    print()

    # ---- Test 2: demo_early (ai, 59 cr) ----
    print("Test 2 — demo_early (ai, 59 cr):")
    s = STUDENTS["demo_early"]
    comp = set(normalize_completed(s["completed"]))
    cr = s["completed_credits"]
    m462 = missing_for("عال 462", comp, cr, "ai")
    check("is_eligible('عال 462') is False, missing prereq عال 361",
          not is_eligible("عال 462", comp, cr, "ai") and "عال 361" in m462["missing_courses"],
          f"missing_courses={m462['missing_courses']}")
    m311 = missing_for("عال 311", comp, cr, "ai")
    check("is_eligible('عال 311') is False, missing عال 212",
          not is_eligible("عال 311", comp, cr, "ai") and "عال 212" in m311["missing_courses"],
          f"missing_courses={m311['missing_courses']}")
    check("is_eligible('عال 212') is True (prereq عال 113 done)",
          is_eligible("عال 212", comp, cr, "ai"),
          f"عال 113 completed={'عال 113' in comp}")
    print()

    # ---- Test 3: concurrent co-requisite ----
    print("Test 3 — concurrent co-requisite (completed عال 227, not عال 329):")
    comp = {"عال 113", "عال 212", "عال 227"}   # has 227, NOT 329
    cr = sum(COURSES[c]["credits"] for c in comp)
    check("is_eligible('عال 429') is True (coreq عال 329 is co-registerable this term)",
          is_eligible("عال 429", comp, cr, "general"),
          f"عال 429 coreqs={COURSES['عال 429']['requirements']['coreqs']}; "
          f"عال 329 co-registerable={prereq_only_ok('عال 329', comp, cr, 'general')} "
          f"(عال 329 coreq عال 227 completed={'عال 227' in comp})")
    print()

    # ---- Test 4: real transcript upload ----
    print("Test 4 — real transcript upload (/upload_transcript):")
    pdf = os.environ.get("TRANSCRIPT_PDF")
    if pdf and os.path.exists(pdf):
        with app.test_client() as client:
            with open(pdf, "rb") as fh:
                resp = client.post(
                    "/upload_transcript",
                    data={"file": (fh, "transcript.pdf")},
                    content_type="multipart/form-data",
                )
            body = resp.get_json()
            check("returns completed_credits == 98",
                  body.get("completed_credits") == 98,
                  f"completed_credits={body.get('completed_credits')}")
            check("returns 32 completed courses",
                  len(body.get("completed", [])) == 32,
                  f"completed count={len(body.get('completed', []))}")
    else:
        print("  [SKIP] set TRANSCRIPT_PDF=/path/to/record.pdf to run this check.")
    print()

    passed = sum(1 for r in results if r)
    total = len(results)
    print("-" * 72)
    print(f"RESULT: {passed}/{total} checks passed.")
    print("-" * 72)
    return 0 if passed == total else 1


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        raise SystemExit(run_selftest())
    # Bind to 0.0.0.0 so it also works inside containers/VMs; open localhost:5000.
    load_dotenv()    
    app.run(host="0.0.0.0", port=5000, debug=True)
