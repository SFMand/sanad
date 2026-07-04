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

import datetime
import os
import re
import tempfile
from collections import defaultdict

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


def _load_data():
    """(Re)load course_planner.db into module globals. Registered as a Flask
    before_request hook so admin-authored edits (courses/tracks/plan entries),
    made by the separate admin_app.py process against the same SQLite file,
    become visible here without a restart — a full reload is cheap at this
    catalog's size (~90 courses)."""
    global DB, PROGRAM, COURSES, STUDENTS, TOTAL_REQUIRED, TRACK_TOTALS
    global TRACKS, DEFAULT_TRACK, TRACK_NAMES_AR, BY_CODE_EN
    DB = load_data_from_db()
    PROGRAM = DB["program"]
    COURSES = {c["code"]: c for c in DB["courses"]}      # Arabic code -> course
    STUDENTS = DB.get("students", {})                    # id -> student
    TOTAL_REQUIRED = PROGRAM["total_credits_required"]  # program-global fallback
    TRACK_TOTALS = PROGRAM.get("track_total_credits", {})
    TRACKS = PROGRAM["tracks"]
    DEFAULT_TRACK = TRACKS[0]
    TRACK_NAMES_AR = PROGRAM.get("track_names_ar", {})
    # English-code lookup so a student can paste either the Arabic or the English code.
    BY_CODE_EN = {_norm_en(c["code_en"]): c["code"] for c in DB["courses"]}


_load_data()


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
        "description_en": c["description_en"],
        "description_ar": c["description_ar"],
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


# ===========================================================================
# Pounce-inspired forward-looking features (all deterministic — no LLM, no API
# key). Georgia State's Pounce turned each student's OWN progress data into
# proactive, personalized guidance; sanad already computes that data, so these
# three features (roadmap / nudges / what-if) just project it forward. Every
# course scheduled below is drawn from `eligible_now`, so a roadmap can NEVER
# place a course before its prerequisites — the engine stays the source of truth.
# ===========================================================================

# KSU CCIS academic calendar — demo constants (no external calendar service).
# KSU runs two main semesters per academic year: the First Semester (الفصل
# الأول, autumn) and the Second Semester (الفصل الثاني, spring), plus an optional
# short Summer term (الفصل الصيفي) that many CCIS students use to accelerate.
# Terms are named by academic year (e.g. "First Semester 2026/27"), matching how
# KSU CCIS students refer to them — not by Western "Fall/Spring".
START_YEAR = 2026                          # academic year in which term 0 begins
# KSU credit-load limits (credit hours / ساعات معتمدة): a regular full-time load
# is 12–18; 19–21 needs excellent standing + advisor approval; the Summer term is
# short (capped lower). Below 12 affects full-time status.
DEFAULT_TERM_CREDITS = 15                  # KSU regular full-time load
MIN_TERM_CREDITS = 12
MAX_TERM_CREDITS = 21
SUMMER_TERM_CREDITS = 7                    # short KSU summer term
REGISTRATION_OPEN_DATE = datetime.date(2026, 8, 10)   # ~First-Semester registration

_SEM_EN = {"first": "First Semester", "second": "Second Semester"}
_SEM_AR = {"first": "الفصل الأول", "second": "الفصل الثاني"}


def _term_slots(include_summer, n):
    """First `n` KSU term slots as (kind, academic_year_start). Two main semesters
    per academic year (first, second) plus an optional short summer term."""
    pattern = ["first", "second", "summer"] if include_summer else ["first", "second"]
    return [(pattern[i % len(pattern)], START_YEAR + i // len(pattern)) for i in range(n)]


_KIND_ORDER = {"first": 0, "second": 1, "summer": 2}


def _term_rank(kind, ay):
    """A monotonic calendar position for a term, so graduations in different
    scenarios (with/without summer) can be compared chronologically."""
    return ay * 3 + _KIND_ORDER[kind]


def _slot_label(kind, ay, lang="en"):
    """KSU-style label, e.g. 'First Semester 2026/27' / 'الفصل الأول 2026/27',
    'Summer 2027' / 'الفصل الصيفي 2027' (summer of AY ay falls in calendar ay+1)."""
    if kind == "summer":
        return f"الفصل الصيفي {ay + 1}" if lang == "ar" else f"Summer {ay + 1}"
    name = (_SEM_AR if lang == "ar" else _SEM_EN)[kind]
    return f"{name} {ay}/{(ay + 1) % 100:02d}"


def _term_cap(kind, max_credits_per_term):
    """The short summer term carries a lower credit cap than a main semester."""
    return min(SUMMER_TERM_CREDITS, max_credits_per_term) if kind == "summer" else max_credits_per_term


def term_label(idx, lang="en", include_summer=False):
    """Label for the idx-th planned term (0 = next term). Thin wrapper used by the
    nudges; defaults to the two-semester (no-summer) KSU sequence."""
    kind, ay = _term_slots(include_summer, idx + 1)[idx]
    return _slot_label(kind, ay, lang)


def days_until_registration():
    """Countdown to the demo registration date (>=0), for the urgency nudge."""
    return max(0, (REGISTRATION_OPEN_DATE - datetime.date.today()).days)


def build_unlock_scores(completed, track):
    """For each still-needed required course, how many OTHER still-needed required
    courses transitively depend on it (a "gateway"/bottleneck score). Built from
    `effective_prereqs` over the track's remaining required set."""
    completed_set = set(normalize_completed(completed))
    track = valid_track(track)
    remaining = [c for c in remaining_required(completed_set, track) if c in COURSES]
    remaining_set = set(remaining)

    dependents = defaultdict(set)          # prereq -> {courses that need it}
    for c in remaining:
        for p in effective_prereqs(c, track):
            if p in remaining_set:
                dependents[p].add(c)

    scores = {}
    for c in remaining:
        seen, stack = set(), list(dependents.get(c, ()))
        while stack:
            d = stack.pop()
            if d in seen:
                continue
            seen.add(d)
            stack.extend(dependents.get(d, ()))
        scores[c] = len(seen)
    return scores


def build_bottlenecks(completed, credits, track, top=6):
    """Ranked gateway courses (highest unlock score first) with whether each is
    takeable now — powers the 'unlocks N' badges and the priority nudge."""
    completed_set = set(normalize_completed(completed))
    track = valid_track(track)
    scores = build_unlock_scores(completed_set, track)
    elig_set = set(eligible_now(completed_set, credits, track))
    ranked = sorted(
        (c for c, n in scores.items() if n > 0),
        key=lambda c: (-scores[c], COURSES[c]["code"]),
    )
    out = []
    for c in ranked[:top]:
        v = _view(c)
        v["unlock_score"] = scores[c]
        v["eligible_now"] = c in elig_set
        out.append(v)
    return out


def _gap_option_codes(completed_set, track):
    """Elective-option courses that still count toward an unmet elective group."""
    codes = set()
    for g in DB["elective_groups"][track]:
        done = sum(COURSES[c]["credits"] for c in g["options"] if c in completed_set)
        if done < g["choose_credits"]:
            codes.update(o for o in g["options"] if o in COURSES and o not in completed_set)
    return codes


def build_roadmap(completed, credits, track,
                  max_credits_per_term=DEFAULT_TERM_CREDITS, defer=None,
                  include_summer=False):
    """Greedy term-by-term schedule to graduation over the KSU CCIS calendar. Each
    term only picks from `eligible_now`, prioritising remaining-required gateways,
    then unmet elective options, packing up to the term's credit cap (co-requisites
    travel together; the short Summer term uses a lower cap). `defer` pushes the
    given courses out of the FIRST term only (used by the what-if simulator).
    `include_summer` interleaves a Summer term after each Second Semester. Returns
    the term list + projected graduation (labelled in KSU academic-year style)."""
    completed = normalize_completed(completed)
    completed_set = set(completed)
    track = valid_track(track)
    credits = int(credits)
    total = total_for(track)
    defer_set = set(normalize_completed(defer or []))
    start_credits = credits

    MAX_SLOTS, MAX_TERMS = 24, 16
    slots = _term_slots(include_summer, MAX_SLOTS)
    terms, stalled, slot_idx = [], False, 0
    while True:
        rem = [c for c in remaining_required(completed_set, track) if c in COURSES]
        gaps = elective_gaps(completed_set, track)
        if (not rem and not gaps and credits >= total):
            break
        if len(terms) >= MAX_TERMS or slot_idx >= MAX_SLOTS:
            break

        kind, ay = slots[slot_idx]
        slot_idx += 1
        cap = _term_cap(kind, max_credits_per_term)

        elig = eligible_now(completed_set, credits, track)
        scores = build_unlock_scores(completed_set, track)
        rem_set = set(rem)
        gap_codes = _gap_option_codes(completed_set, track)
        needed = rem_set | gap_codes            # only what's still required to graduate

        # How many credits each still-open elective group still needs — capped
        # per-term too, so a term doesn't schedule every remaining option in a
        # group (e.g. all 9 one-credit options) once a couple of credits close it.
        group_left = {g["group"]: g["remaining_credits"] for g in gaps}
        group_of = defaultdict(list)
        for g in DB["elective_groups"][track]:
            if g["name_en"] in group_left:
                for o in g["options"]:
                    group_of[o].append(g["name_en"])

        def rank_key(c):
            tier = 0 if c in rem_set else 1     # gap_codes (elective options)
            return (tier, -scores.get(c, 0), -COURSES[c]["credits"], COURSES[c]["code"])

        ranked = sorted((c for c in elig if c in needed), key=rank_key)
        if terms == [] and defer_set:                 # defer applies to term 1 only
            ranked = [c for c in ranked if c not in defer_set]

        chosen, chosen_set, term_credits = [], set(), 0
        for c in ranked:
            if c in chosen_set:
                continue
            is_elective = c in gap_codes and c not in rem_set
            if is_elective and all(group_left.get(g, 0) <= 0 for g in group_of.get(c, [])):
                continue                               # this group's gap is already closed
            bundle = [c] + [co for co in COURSES[c]["requirements"]["coreqs"]
                            if co in COURSES and co not in completed_set and co not in chosen_set]
            bcr = sum(COURSES[x]["credits"] for x in bundle)
            if term_credits + bcr > cap and chosen:
                continue                               # full — leave the rest for later
            for x in bundle:
                if x not in chosen_set:
                    chosen.append(x)
                    chosen_set.add(x)
            term_credits += bcr
            if is_elective:
                for g in group_of.get(c, []):
                    group_left[g] = group_left.get(g, 0) - COURSES[c]["credits"]

        if not chosen:
            if kind == "summer":
                continue                               # nothing fits the short summer — skip it
            stalled = True                             # a main term with no progress = data gap
            break

        completed_set.update(chosen_set)
        credits += term_credits
        terms.append({
            "term_label": _slot_label(kind, ay, "en"),
            "term_label_ar": _slot_label(kind, ay, "ar"),
            "term_kind": kind,
            "term_ay": ay,
            "term_credits": term_credits,
            "courses": [{**_view(c), "unlock_score": scores.get(c, 0)} for c in chosen],
        })

    rem = [c for c in remaining_required(completed_set, track) if c in COURSES]
    gaps = elective_gaps(completed_set, track)
    complete = not rem and not gaps and credits >= total
    return {
        "track": track,
        "track_name_ar": TRACK_NAMES_AR.get(track, track),
        "start_credits": start_credits,
        "total_required": total,
        "terms": terms,
        "projected_terms": len(terms),
        "projected_grad": terms[-1]["term_label"] if terms else None,
        "projected_grad_ar": terms[-1]["term_label_ar"] if terms else None,
        "grad_rank": _term_rank(terms[-1]["term_kind"], terms[-1]["term_ay"]) if terms else None,
        "credits_remaining_after": max(0, total - credits),
        "complete": complete,
        "stalled": stalled,
        "max_credits_per_term": max_credits_per_term,
        "include_summer": include_summer,
    }


def _t(lang, en, ar):
    return ar if lang == "ar" else en


def build_nudges(completed, credits, track, lang="en"):
    """Ranked, personalized 'smart nudge' cards — Pounce's signature mechanic,
    delivered in-app. Every card is derived from the deterministic record; none
    are invented by the model."""
    rec = build_advising_record(completed, credits, track)
    completed_set = set(normalize_completed(completed))
    track = valid_track(track)
    roadmap = build_roadmap(completed, credits, track)
    bottlenecks = build_bottlenecks(completed, credits, track)
    elig = rec["eligible_now"]
    nudges = []

    def caveat(view):
        return "" if view.get("verified", True) else _t(
            lang, " (not yet verified — confirm with your advisor)",
            " (غير مُتحقق منه بعد — تأكّد مع مرشدك)")

    # 1) Registration urgency (the melt-prevention nudge).
    days = days_until_registration()
    next_term = term_label(0, lang)
    nudges.append({
        "type": "registration", "icon": "🗓️", "priority": 10,
        "title": _t(lang, f"Registration opens in {days} days",
                    f"يبدأ التسجيل خلال {days} يومًا"),
        "body": _t(lang,
                   f"You have {len(elig)} course(s) ready to take for {next_term}. "
                   "Open your roadmap to lock in the term.",
                   f"لديك {len(elig)} مقرر(ات) جاهزة لفصل {next_term}. "
                   "افتح خطتك لتثبيت مقررات الفصل."),
        "action": {"view": "roadmap",
                   "label": _t(lang, "Show my roadmap", "اعرض خطتي")},
    })

    # 2) Bottleneck priority — take the biggest gateway you can take now.
    top = next((b for b in bottlenecks if b["eligible_now"]), None)
    if top:
        nudges.append({
            "type": "bottleneck", "icon": "🔑", "priority": 9,
            "title": _t(lang, f"Prioritize {top['code']} this term",
                        f"قدِّم {top['code']} هذا الفصل"),
            "body": _t(lang,
                       f"{top['code']} — {top['title_en']} unlocks "
                       f"{top['unlock_score']} later required course(s). "
                       "Taking it now keeps you on the fastest path." + caveat(top),
                       f"{top['code']} — {top['title_ar']} يفتح "
                       f"{top['unlock_score']} مقرر(ات) لاحقة مطلوبة. "
                       "أخذه الآن يبقيك على أسرع مسار." + caveat(top)),
            "action": {"view": "advisor",
                       "label": _t(lang, "Ask the advisor", "اسأل المرشد"),
                       "payload": {"code": top["code"]}},
        })

    # 3) Almost there — the progress/encouragement nudge.
    nudges.append({
        "type": "progress", "icon": "🎓", "priority": 7,
        "title": _t(lang,
                    f"{rec['credits_remaining']} credits to graduation",
                    f"{rec['credits_remaining']} ساعة حتى التخرج"),
        "body": _t(lang,
                   f"About {roadmap['projected_terms']} term(s) left — "
                   f"projected graduation {roadmap['projected_grad']}."
                   if roadmap["projected_grad"] else
                   "You've completed all requirements — congratulations!",
                   f"يتبقى نحو {roadmap['projected_terms']} فصل — "
                   f"التخرج المتوقع {roadmap['projected_grad_ar']}."
                   if roadmap["projected_grad"] else
                   "أكملت جميع المتطلبات — مبارك!"),
        "action": {"view": "roadmap",
                   "label": _t(lang, "See the plan", "شاهد الخطة")},
    })

    # 4) Elective gap — largest unmet elective group.
    if rec["elective_gaps"]:
        g = max(rec["elective_gaps"], key=lambda x: x["remaining_credits"])
        nudges.append({
            "type": "elective", "icon": "🧩", "priority": 6,
            "title": _t(lang, f"{g['remaining_credits']} elective credits needed",
                        f"تحتاج {g['remaining_credits']} ساعة اختيارية"),
            "body": _t(lang,
                       f"Group “{g['group']}”: {g['done_credits']} of "
                       f"{g['choose_credits']} credits done.",
                       f"مجموعة «{g['group_ar']}»: أنجزت {g['done_credits']} من "
                       f"{g['choose_credits']} ساعة."),
            "action": {"view": "advisor",
                       "label": _t(lang, "Find electives", "اقترح اختيارية")},
        })

    # 5) Blocked-course unlock path — a locked course whose missing prereq is takeable now.
    elig_codes = {c["code"] for c in elig}
    for b in rec["remaining_required_blocked"]:
        avail = [mc for mc in b["missing_courses"] if mc in elig_codes]
        if avail:
            mc = avail[0]
            nudges.append({
                "type": "unlock_path", "icon": "🚀", "priority": 5,
                "title": _t(lang, f"Unlock {b['code']} sooner",
                            f"افتح {b['code']} مبكرًا"),
                "body": _t(lang,
                           f"{b['code']} — {b['title_en']} is blocked by "
                           f"{mc} — {COURSES[mc]['title_en']}, which you can take now.",
                           f"{b['code']} — {b['title_ar']} محجوب بـ "
                           f"{mc} — {COURSES[mc]['title_ar']}، ويمكنك أخذه الآن."),
                "action": {"view": "advisor",
                           "label": _t(lang, "Ask the advisor", "اسأل المرشد"),
                           "payload": {"code": mc}},
            })
            break

    # 6) Human escalation (Pounce always offered a real person) — prefilled summary.
    top_elig = ", ".join(c["code"] for c in elig[:5]) or _t(lang, "none yet", "لا يوجد بعد")
    summary = _t(lang,
                 f"Track: {rec['track']} • Completed: {rec['completed_credits']}/"
                 f"{rec['total_required']} cr • Eligible now: {top_elig}",
                 f"المسار: {rec['track']} • المُنجز: {rec['completed_credits']}/"
                 f"{rec['total_required']} ساعة • المتاح الآن: {top_elig}")
    nudges.append({
        "type": "escalation", "icon": "🧑‍🏫", "priority": 1,
        "title": _t(lang, "Talk to a human advisor", "تواصل مع مرشد بشري"),
        "body": _t(lang,
                   "Copy this summary into your advising request:\n" + summary,
                   "انسخ هذا الملخص في طلب الإرشاد:\n" + summary),
        "action": {"view": "copy", "label": _t(lang, "Copy summary", "انسخ الملخص"),
                   "payload": {"summary": summary}},
    })

    nudges.sort(key=lambda n: -n["priority"])
    return nudges


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
app.before_request(_load_data)


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


@app.post("/roadmap")
def roadmap():
    """Term-by-term path to graduation + gateway bottlenecks + unlock scores for
    badging. Pure engine — no API key."""
    data = request.get_json(force=True, silent=True) or {}
    completed = data.get("completed", [])
    track = valid_track(data.get("track", DEFAULT_TRACK))
    credits = resolve_credits(completed, data.get("completed_credits"))
    return jsonify({
        "roadmap": build_roadmap(completed, credits, track),
        "bottlenecks": build_bottlenecks(completed, credits, track),
        "unlock_scores": build_unlock_scores(completed, track),
    })


@app.post("/nudges")
def nudges():
    """Ranked, personalized in-app nudge cards (Pounce's signature). Pure engine."""
    data = request.get_json(force=True, silent=True) or {}
    completed = data.get("completed", [])
    track = valid_track(data.get("track", DEFAULT_TRACK))
    lang = data.get("lang", "en")
    credits = resolve_credits(completed, data.get("completed_credits"))
    return jsonify({"nudges": build_nudges(completed, credits, track, lang)})


@app.post("/whatif")
def whatif():
    """Re-simulate the roadmap under a modification (defer courses and/or change
    the per-term credit cap) and return the graduation delta. Pure engine."""
    data = request.get_json(force=True, silent=True) or {}
    completed = data.get("completed", [])
    track = valid_track(data.get("track", DEFAULT_TRACK))
    credits = resolve_credits(completed, data.get("completed_credits"))
    defer = data.get("defer", [])
    include_summer = bool(data.get("include_summer", False))
    cap = data.get("max_credits_per_term", DEFAULT_TERM_CREDITS)
    try:
        cap = max(MIN_TERM_CREDITS, min(MAX_TERM_CREDITS, int(cap)))
    except (TypeError, ValueError):
        cap = DEFAULT_TERM_CREDITS

    base = build_roadmap(completed, credits, track)
    new = build_roadmap(completed, credits, track, max_credits_per_term=cap,
                        defer=defer, include_summer=include_summer)

    # Compare graduations CHRONOLOGICALLY (a summer term can move the date earlier
    # without changing the term count), not just by number of terms.
    br, nr = base.get("grad_rank"), new.get("grad_rank")
    if br is None or nr is None or base["projected_grad"] == new["projected_grad"]:
        direction = "same"
    elif nr < br:
        direction = "earlier"
    else:
        direction = "later"

    return jsonify({
        "base": base,
        "new": new,
        "direction": direction,
        "delta_terms": new["projected_terms"] - base["projected_terms"],
        "base_grad": base["projected_grad"],
        "new_grad": new["projected_grad"],
        "base_grad_ar": base["projected_grad_ar"],
        "new_grad_ar": new["projected_grad_ar"],
    })


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

    # ---- Test 5: roadmap / bottlenecks / what-if (Pounce-inspired features) ----
    print("Test 5 — roadmap & what-if (demo_early, ai, 59 cr):")
    s = STUDENTS["demo_early"]
    comp = list(normalize_completed(s["completed"]))
    cr = s["completed_credits"]
    rm = build_roadmap(comp, cr, "ai")
    check("roadmap terminates and reaches graduation (credits_remaining_after == 0)",
          rm["complete"] and rm["credits_remaining_after"] == 0 and not rm["stalled"],
          f"terms={rm['projected_terms']}, grad={rm['projected_grad']}, "
          f"remaining_after={rm['credits_remaining_after']}, stalled={rm['stalled']}")

    # Every course is prereq-valid IN ITS TERM: replay the schedule and assert
    # each course was eligible at the moment it was placed. This is the core
    # "never schedule a course before its prerequisites" guarantee.
    replay, rcred, valid_schedule = set(comp), cr, True
    for t in rm["terms"]:
        codes = [c["code"] for c in t["courses"]]
        for code in codes:
            if not is_eligible(code, replay, rcred, "ai"):
                valid_schedule = False
        replay.update(codes)
        rcred += t["term_credits"]
    check("every roadmap course was eligible in its own term", valid_schedule)

    check("no term exceeds the credit cap (15)",
          all(t["term_credits"] <= DEFAULT_TERM_CREDITS for t in rm["terms"]),
          f"term credits: {[t['term_credits'] for t in rm['terms']]}")

    check("terms use KSU academic-year naming (First/Second Semester)",
          rm["terms"][0]["term_label"].startswith("First Semester"),
          f"first term label: {rm['terms'][0]['term_label']}")

    rm_summer = build_roadmap(comp, cr, "ai", include_summer=True)
    check("summer roadmap adds a Summer term within its lower cap",
          any(t["term_kind"] == "summer" for t in rm_summer["terms"]) and
          all(t["term_credits"] <= (SUMMER_TERM_CREDITS if t["term_kind"] == "summer"
                                    else DEFAULT_TERM_CREDITS) for t in rm_summer["terms"]),
          f"kinds/credits: {[(t['term_kind'], t['term_credits']) for t in rm_summer['terms']]}")

    bn = build_bottlenecks(comp, cr, "ai")
    check("bottlenecks are ranked by unlock score (desc)",
          all(bn[i]["unlock_score"] >= bn[i + 1]["unlock_score"] for i in range(len(bn) - 1)),
          f"top bottleneck: {bn[0]['code']} unlocks {bn[0]['unlock_score']}" if bn else "none")

    # Deferring the top eligible gateway must not graduate the student SOONER.
    top_gate = next((b["code"] for b in bn if b["eligible_now"]), None)
    if top_gate:
        deferred = build_roadmap(comp, cr, "ai", defer=[top_gate])
        check(f"deferring gateway {top_gate} does not shorten the plan",
              deferred["projected_terms"] >= rm["projected_terms"],
              f"base={rm['projected_terms']} terms, deferred={deferred['projected_terms']} terms")

    nd = build_nudges(comp, cr, "ai", "en")
    check("nudges are produced and ranked by priority (desc)",
          len(nd) >= 3 and all(nd[i]["priority"] >= nd[i + 1]["priority"]
                               for i in range(len(nd) - 1)),
          f"{len(nd)} nudges: {', '.join(n['type'] for n in nd)}")
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
