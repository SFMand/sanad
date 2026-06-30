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
> Courses with `"verified": false` in [`courses.json`](courses.json) are **OCR-ambiguous seed
> data** whose title, code, prerequisites, or placement may not match the official catalogue.
> They are flagged in the UI, and whenever the assistant recommends or discusses one it appends
> a caution (e.g. *"not yet verified against the official plan — confirm with your advisor"*).
>
> **Before using this for real advising, reconcile `courses.json` against the official current
> KSU CCIS Computer Science study plan.**

---

## The core idea — why this is reliable

**The language model never guesses prerequisites.** A deterministic Python engine in
[`app.py`](app.py) computes eligibility from `courses.json` and passes the **verified result**
into the model's system prompt as the authoritative record. The model only *explains and
recommends from that result* — it cannot invent a prerequisite or recommend a blocked course.

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
pip install flask google-genai
# Transcript upload needs a PDF-to-text backend — one of:
sudo apt-get install -y poppler-utils    # provides `pdftotext` (preferred)
pip install pdfplumber                    # pure-Python fallback

export GEMINI_API_KEY=...        # your Google Gemini API key
python app.py                    # serves http://localhost:5000
```

Then open **http://localhost:5000**.

- **LLM provider:** Google Gemini via the current **`google-genai`** SDK
  (`from google import genai`). Model constant `MODEL = "gemini-2.5-pro"` in `app.py`
  (swap to `"gemini-2.5-flash"` for a faster/cheaper option). The entire LLM call is isolated
  in one function, `call_llm()`, so the provider can be changed in one place.
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

The UI has two tabbed, full-width views: **📅 Plan** and **💬 Advisor**.

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

## Endpoints

| Method & path | Purpose |
|---|---|
| `GET /` | Serves `index.html` |
| `GET /courses` | `program` + `courses[]` + `degree_plans` + `elective_groups` + demo `students` |
| `POST /upload_transcript` | Multipart `file` (PDF) → `{ completed, completed_credits, in_progress, details }` (parsed via `transcript_parser`, each detail annotated with catalog status) |
| `POST /plan` | Body `{ completed, completed_credits, track }` → engine-computed grid state: `{ years:[{year,semesters:[{level,level_num,courses:[{…,state,missing_courses,missing_coreqs,credit_gap}]}]}], eligible_codes, other_completed, elective_gaps, … }`. **Pure engine — no API key**, so the Plan grid works offline. |
| `POST /chat` | Body `{ messages, completed:["عال 212", …], completed_credits, track, lang:"ar"\|"en" }` → builds the system prompt from the same VERIFIED record and returns `{ reply, eligible_codes }` (the `eligible_codes` drive the recommendation cards). |

## Files

- [`app.py`](app.py) — Flask server, deterministic eligibility engine, system-prompt builder,
  `/plan` (grid state) and `/upload_transcript`, isolated `call_llm()`, and the `selftest` command.
- [`transcript_parser.py`](transcript_parser.py) — parses passed courses + earned credits from
  an EduGate academic-record PDF (via `pdftotext`, falling back to `pdfplumber`). Output codes
  are the canonical Arabic codes.
- [`courses.json`](courses.json) — knowledge base: `program` (with `tracks` + `track_names_ar`),
  `courses[]`, per-track `degree_plans`, per-track `elective_groups`, and demo `students`.
  **No database.**
- [`index.html`](index.html) — single-page vanilla HTML/CSS/JS UI (no build, no deps): progress
  header, EN/ع toggle with RTL, and two tabbed views — a **Plan** view (track selector, transcript
  upload + review/confirm, the **year-by-year grid** of clickable state-colored cells, and
  track-elective progress bars) and an **Advisor** chat (bilingual starter chips, Markdown replies,
  and **recommendation cards** for cited eligible courses). The grid's colors come from `/plan`
  (the engine), never re-computed in JS. Accessible: visible keyboard focus,
  `prefers-reduced-motion`, ARIA labels.
- [`README.md`](README.md) — this file.

## Data model (`courses.json`)

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

Top-level also has `program.tracks` / `program.track_names_ar`, per-track `degree_plans`
(`{ level → [codes] }`), per-track `elective_groups` (`[{ name_en, name_ar, choose_credits,
options }]`), and `students` (`{ id → { name, track, completed_credits, completed[] } }`).
