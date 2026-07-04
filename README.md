# CS Course Planner — KSU CCIS BSc Computer Science

A bilingual (English / العربية) degree-plan advising assistant for the **BSc in Computer
Science** at the **College of Computer and Information Sciences (CCIS), King Saud University
(KSU)**. A student picks a **track** (general / AI / cyber security), enters the courses
they've completed — by **uploading their EduGate transcript (PDF)** or ticking them manually —
and asks what to take next, whether they can take a specific course, or what's left before
graduation.

The **canonical course id is the Arabic code** (e.g. `عال 212`), which matches the
transcript / EduGate record. `code_en` / `title_en` / `title_ar` are **display only**; all
internal eligibility logic keys on the Arabic code.

---

> ## ⚠️ Verify the plan before any real use
> Courses with `verified = 0` in `course_planner.db` are **OCR-ambiguous seed data** whose
> title, code, prerequisites, or placement may not match the official catalogue. They are
> flagged in the UI, and whenever the assistant recommends or discusses one it appends a caution
> (e.g. *"not yet verified against the official plan — confirm with your advisor"*).
>
> **Before using this for real advising, reconcile the catalog against the official current
> KSU CCIS Computer Science study plan.**

---

## The core idea — why this is reliable

**The language model never guesses prerequisites.** A deterministic Python engine in
[`app.py`](app.py) computes eligibility from `course_planner.db` and passes the **verified
result** into the model's system prompt as the authoritative record. The model only *explains
and recommends from that result* — it cannot invent a prerequisite or recommend a blocked course.

> **Code handles truth. The model handles conversation.**

The engine computes, for the student's completed list, completed-credit total, and track:

| Function | Meaning |
|---|---|
| `is_eligible(code, completed, credits)` | not completed, all required courses done, `credits ≥ min_credits`, **and** every co-requisite is either done **or** itself co-registerable this term |
| `prereq_only_ok(code, completed, credits)` | the helper above: `code`'s own prerequisites/credit gate are met (used to decide if a coreq can be taken alongside) |
| `eligible_now(completed, credits)` | every course `is_eligible` right now |
| `remaining_required(completed, track)` | required courses for that track not yet completed |
| `elective_gaps(completed, track)` | per elective-group credits still needed for that track |
| `missing_for(code, …)` | exact missing prerequisite(s), missing (non-co-registerable) coreqs, and credit gap |

**Co-requisites ("مرافق") may be taken in the same term.** A course is eligible if each coreq is
already completed **or** could itself be taken this term (its own prerequisites are met). This is
handled non-recursively by `prereq_only_ok`.

**Completed credits are taken from the transcript**, not assumed to equal the sum of
catalog-matched courses — older transcripts carry prep courses absent from this catalog (the
real demo record is 98 earned credits vs. 86 from catalog matches).

These lists (track, completed credits, eligible-now, blocked-with-reasons, remaining-required,
elective-gaps) are serialized into the system prompt. The assistant treats them as ground truth
and recommends **only** from the eligible-now list.

## Setup

```bash
pip install -r requirements.txt   # flask, google-genai, python-dotenv
# Transcript upload needs a PDF-to-text backend — one of:
sudo apt-get install -y poppler-utils    # provides `pdftotext` (preferred)
pip install pdfplumber                    # pure-Python fallback

echo "GEMINI_API_KEY=..." > .env  # your Google Gemini API key (loaded via python-dotenv)
python app.py                     # serves http://localhost:5000
```

Then open **http://localhost:5000**.

Data lives in **`course_planner.db`** (SQLite), which `app.py` reads at startup via
[`data_layer.py`](data_layer.py). If you don't have a `course_planner.db` yet, build one from the
frozen seed data:

```bash
python migrate_to_sqlite.py       # one-time: courses.json -> course_planner.db
```

- **LLM provider:** Google Gemini via the current **`google-genai`** SDK
  (`from google import genai`). Model constant `MODEL = "gemini-2.5-flash"` in `app.py`
  (swap to `"gemini-2.5-pro"` for the more capable but slower/pricier option). The entire LLM
  call is isolated in one function, `call_llm()`, so the provider can be changed in one place.
- The `GEMINI_API_KEY` is required only for the `/chat` endpoint. The eligibility engine and the
  self-test below run **without** an API key.

### Run the eligibility self-test (no API key needed)

```bash
python app.py selftest
```

This runs the four acceptance checks and prints PASS/FAIL for each. To also exercise the real
transcript-upload check, point it at a PDF:

```bash
TRANSCRIPT_PDF=/path/to/edugate-record.pdf python app.py selftest
```

The checks: (1) `demo_real` (general, 98 cr) — non-empty eligible list, ~10 remaining required,
`عال 496` eligible (90-credit gate met, coreq `عال 343` done); (2) `demo_early` (ai, 59 cr) —
`عال 462`/`عال 311` correctly blocked with named missing prerequisites, `عال 212` eligible;
(3) concurrent coreq — a student with `عال 227` but not `عال 329` is eligible for `عال 429`
(its coreq `عال 329` is co-registerable this term); (4) uploading the real transcript returns
**98 credits / 32 courses**.

## Privacy / scope

- **No live EduGate integration and no scraping.** Completed courses come only from the
  student's own uploaded transcript PDF or manual entry. The uploaded PDF is parsed to a temp
  file that is deleted immediately, and the parsed list is shown for the student to confirm/edit
  before it drives any advice.
- Scope is **BSc Computer Science only.**

## What to demo

The UI has three tabbed, full-width views — **📅 Plan**, **🎓 Roadmap**, and **💬 Advisor** —
plus a **🔔 notifications** tray in the header.

1. In **Plan**, pick a **track** (General / AI / Cyber). Click **"Upload transcript (PDF)"** and
   choose an EduGate academic-record PDF → the parsed courses appear for review; confirm/edit and
   they populate your record (with the transcript's earned-credit total). Or **"Load demo
   student"**.
2. The **year-by-year grid** colors every course by engine-computed state — **green = completed,
   blue = eligible now, gray = locked** (locked cells show their exact blockers). **Click any cell
   to toggle it completed**; eligible courses unlock their dependents on the next repaint.
   Track-elective progress bars sit below the grid.
3. In **Advisor**, **"What should I take next semester?"** → the assistant recommends **only
   eligible** courses, and the cited eligible courses also render as **recommendation cards**
   under the reply (tap a card to mark it done). Unverified courses carry a caveat.
4. Ask **"Can I take ... now?"** for a blocked course → it **refuses**, names the exact missing
   prerequisite(s)/credit gate, and gives the shortest unlock path (and it is **not** carded,
   since it isn't eligible). A course whose only gap is a co-registerable co-requisite is offered
   to be taken **together** with it.
5. Toggle **ع** (top-right): views, the grid (RTL year/semester flow), and the cards flip to
   **RTL** and Arabic. (The assistant mirrors whichever language you type in.)

### 🎓 Roadmap, 🔔 nudges, and the what-if simulator (forward-looking features)

Inspired by Georgia State's **Pounce** advising bot — which cut summer melt ~22% by turning each
student's own progress data into proactive, personalized guidance — these three features project
sanad's verified record *forward*. All are **deterministic** (pure engine, no API key), so they
work offline and are safe to demo live.

6. Open **🎓 Roadmap** → a greedy term-by-term schedule to graduation with a **projected graduation
   date**. Every course is drawn from `eligible_now` for that simulated term, so the plan can never
   place a course before its prerequisites. Gateway courses carry a **"unlocks N"** badge (how many
   later required courses depend on them).
7. In the **what-if simulator** (under the roadmap), drag **credits per term** or tap a course under
   **"delay a course"** → the **graduation date shifts live** (e.g. overloading to 18–21 credits can
   pull graduation in a term). Green = sooner, red = later.
8. Click the header **🔔 bell** → a **"For you"** tray of ranked, personalized nudge cards built
   from the record: registration countdown, "prioritize this gateway", credits-to-graduation,
   elective gaps, a blocked-course unlock path, and a **"Talk to a human advisor"** hand-off with a
   copy-ready summary. Each card deep-links into the relevant view. All text is bilingual.

## Endpoints

| Method & path | Purpose |
|---|---|
| `GET /` | Serves `index.html` |
| `GET /courses` | `program` + `courses[]` + `degree_plans` + `elective_groups` + demo `students` |
| `POST /upload_transcript` | Multipart `file` (PDF) → `{ completed, completed_credits, in_progress, details }` (parsed via `transcript_parser`, each detail annotated with catalog status) |
| `POST /plan` | Body `{ completed, completed_credits, track }` → engine-computed grid state: `{ years:[{year,semesters:[{level,level_num,courses:[{…,state,missing_courses,missing_coreqs,credit_gap}]}]}], eligible_codes, other_completed, elective_gaps, … }`. **Pure engine — no API key**, so the Plan grid works offline. |
| `POST /chat` | Body `{ messages, completed:["عال 212", …], completed_credits, track, lang:"ar"\|"en" }` → builds the system prompt from the same VERIFIED record and returns `{ reply, eligible_codes }` (the `eligible_codes` drive the recommendation cards). |
| `POST /roadmap` | Body `{ completed, completed_credits, track }` → `{ roadmap:{ terms:[{term_label,term_credits,courses[]}], projected_terms, projected_grad, complete, … }, bottlenecks:[{code,unlock_score,eligible_now}], unlock_scores:{code:N} }`. **Pure engine — no API key.** |
| `POST /nudges` | Body `{ completed, completed_credits, track, lang }` → `{ nudges:[{type,icon,priority,title,body,action}] }`, ranked, bilingual. **Pure engine.** |
| `POST /whatif` | Body `{ completed, completed_credits, track, defer:[codes], max_credits_per_term }` → re-simulates the roadmap and returns `{ base, new, delta_terms, base_grad, new_grad }`. **Pure engine.** |

## Adding courses, tracks, and plan entries

`courses.json` is **frozen historical seed data** — migrated once into `course_planner.db` via
`migrate_to_sqlite.py` and no longer read at runtime. Going forward, use **`db_admin.py`** (CLI)
or **`admin_app.py`** (web UI) to add new majors/tracks/courses. Both are *additive only* — they
don't edit or delete existing rows — and both run every mutation through a connection with
`PRAGMA foreign_keys = ON`, so a typo'd course code in a prereq/coreq/course-code argument fails
loudly with an `IntegrityError` (and rolls back) instead of silently producing a broken row.

**CLI** (see `python db_admin.py --help` for every subcommand):

```bash
python db_admin.py add-course --code "هعم 101" --code-en "CPE 101" \
    --title-ar "..." --title-en "Digital Logic Design" --credits 3 \
    --category core --prereq "عال 111"
python db_admin.py add-track --code computer_engineering --name-ar "..." --position 3
python db_admin.py add-plan-entry --track computer_engineering --level "المستوى 1" \
    --course "هعم 101" --position 0
python db_admin.py list-courses [--track computer_engineering]
python db_admin.py check       # PRAGMA foreign_key_check across the whole DB
```

**Web UI** — a small admin page over the same `db_admin.py` functions, for adding data without
hand-crafting CLI flags:

```bash
python admin_app.py            # serves http://localhost:5050
```

Tabs for browsing the catalog and adding courses/tracks/plan-entries/elective-groups/options, plus
an integrity-check tab and a live activity log. Styled to match `index.html`'s theme; both entry
points call the exact same `db_admin.py` core functions, so there's one code path for every
mutation regardless of which UI you use.

## Files

- [`app.py`](app.py) — Flask server, deterministic eligibility engine, system-prompt builder,
  `/plan` (grid state) and `/upload_transcript`, isolated `call_llm()`, and the `selftest` command.
- [`data_layer.py`](data_layer.py) — the single place that knows the SQLite path and the
  `PRAGMA foreign_keys = ON` connection; `connect()` and `load_data_from_db()` (reconstructs the
  courses.json-shaped in-memory dict that `app.py`'s engine operates on).
- [`db_admin.py`](db_admin.py) — additive authoring functions (add-course/track/plan-entry/
  elective-group/elective-option, list-courses, check) usable both as a CLI and as a library;
  `admin_app.py` calls the same functions.
- [`admin_app.py`](admin_app.py) / [`admin.html`](admin.html) — small Flask web UI over
  `db_admin.py`'s functions (see above).
- [`transcript_parser.py`](transcript_parser.py) — parses passed courses + earned credits from
  an EduGate academic-record PDF (via `pdftotext`, falling back to `pdfplumber`). Output codes
  are the canonical Arabic codes.
- [`course_planner.db`](course_planner.db) — SQLite database; the runtime data source (see schema
  below). Built once from `courses.json` via `migrate_to_sqlite.py`; use `db_admin.py`/
  `admin_app.py` to add data from here on.
- [`courses.json`](courses.json) — **frozen seed data**, no longer read at runtime. Kept as the
  historical source `migrate_to_sqlite.py` migrated from.
- [`migrate_to_sqlite.py`](migrate_to_sqlite.py) — one-time `courses.json` → `course_planner.db`
  migration (`python migrate_to_sqlite.py [--force]`); `migration/*.sql` holds the schema, applied
  in numbered order.
- [`index.html`](index.html) — single-page vanilla HTML/CSS/JS UI (no build, no deps): progress
  header, EN/ع toggle with RTL, and two tabbed views — a **Plan** view (track selector, transcript
  upload + review/confirm, the **year-by-year grid** of clickable state-colored cells, and
  track-elective progress bars) and an **Advisor** chat (bilingual starter chips, Markdown replies,
  and **recommendation cards** for cited eligible courses). The grid's colors come from `/plan`
  (the engine), never re-computed in JS. Accessible: visible keyboard focus,
  `prefers-reduced-motion`, ARIA labels.
- [`README.md`](README.md) — this file.

## Data model (`course_planner.db`)

SQLite, schema in `migration/*.sql`. Core tables:

| Table | Purpose |
|---|---|
| `program` | Single row: name, college, `total_credits_required` |
| `tracks` | `code` (PK), `name_ar`, `position` |
| `courses` | `code` (PK, canonical Arabic id), `code_en`, `title_ar`, `title_en`, `credits`, `category`, `min_credits`, `verified` |
| `course_prereqs` / `course_coreqs` | `(course_code, prereq_code)` / `(course_code, coreq_code)` pairs, FK'd to `courses.code` |
| `degree_plan_entries` | `(track_code, level_key, position) → course_code` — places a course in a track's plan at a level |
| `elective_groups` / `elective_group_options` | per-track elective groups (`name_en/ar`, `choose_credits`) and their course options |
| `students` / `student_completed_courses` | demo students used by the UI's "Load demo student" |

`data_layer.load_data_from_db()` reconstructs this into the same nested dict shape the engine in
`app.py` operates on:

```json
{
  "code": "عال 212",              // canonical id (matches the transcript/EduGate)
  "code_en": "CSC 212",            // display only
  "title_ar": "هياكل البيانات",
  "title_en": "Data Structures",
  "credits": 3,
  "category": "core",
  "requirements": { "courses": ["عال 113"], "min_credits": 0, "coreqs": [] },
  "verified": true
}
```

- All requirement codes are **Arabic** canonical codes.
- `requirements.courses` — all must be completed.
- `requirements.min_credits` — completed credits must be ≥ this (e.g. the Graduation Project gate).
- `requirements.coreqs` — may be taken in the **same term**: satisfied if completed **or**
  co-registerable this term (its own prerequisites met).
- `verified: false` — OCR-ambiguous; flagged in the UI and caveated by the assistant.

The reconstructed dict also has `program.tracks` / `program.track_names_ar`, per-track
`degree_plans` (`{ level → [codes] }`), per-track `elective_groups` (`[{ name_en, name_ar,
choose_credits, options }]`), and `students` (`{ id → { name, track, completed_credits,
completed[] } }`).
