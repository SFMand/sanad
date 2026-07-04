"""
EduGate transcript parser for the CS Course Planner.

Extracts the courses a student has PASSED (and total earned credits) from an
unofficial EduGate academic-record PDF (the "السجل الأكاديمي" print-to-PDF).
Output codes match the canonical Arabic codes in courses.json (e.g. "عال 212").

Tested against a real KSU CCIS transcript: 32 passed courses, 98 credits,
matching the record's cumulative earned-hours figure.

Requires poppler's `pdftotext` (apt-get install poppler-utils); falls back to
pdfplumber if pdftotext is unavailable.

IMPORTANT
- Reads an UNOFFICIAL record the student uploads themselves. It does NOT connect
  to EduGate. Keep manual entry as a fallback in the UI, and always SHOW the
  parsed list to the student to confirm/edit before it drives any advice.
"""

import re
import shutil
import subprocess
import unicodedata

# KSU letter grades. D and above count as passed (prerequisite-satisfying).
PASS_GRADES = {"أ+", "أ", "ب+", "ب", "ج+", "ج", "د+", "د"}
FAIL_GRADES = {"هـ", "ه"}        # F
IN_PROGRESS = {"ت"}              # grade not yet posted (not pass, not fail)

# Old prep-year codes on older transcripts -> current 4th-edition plan codes.
# VERIFY against your catalog before relying on them; leave empty to disable.
CODE_ALIASES = {
    # "انجل 106": "انجل 100",
    # "انجل 113": "انجل 110",
}

# row layout (pdftotext -layout): grade | points | credits | name | <num><prefix>
_ROW = re.compile(
    r"^\s*(\S+)\s+([\d.]+)\s+(\d+)\s+(.+?)\s+(\d{2,3})\s*([\u0621-\u064A]+)\s*$"
)


def _clean(s: str) -> str:
    """NFKC-normalize and strip bidi/format controls + tatweel."""
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069\u0640]", "", s)


def _raw_text(pdf_path: str) -> str:
    if shutil.which("pdftotext"):
        out = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"], capture_output=True, text=True
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout
    import pdfplumber  # fallback
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text() or "")
    return "\n".join(pages)


def parse_transcript(pdf_path: str) -> dict:
    """
    Returns:
      {
        "completed":         ["عال 111", "عال 113", ...],  # passed, deduped
        "completed_credits": 98,                            # sum of passed credit hours
        "in_progress":       ["عال 340", ...],              # grade "ت" (not yet posted)
        "details":           [{"code","credits","grade","title"}...],
      }
    """
    raw = _raw_text(pdf_path)
    passed, in_progress, details = [], [], []
    seen, credits = set(), 0

    for line in raw.splitlines():
        m = _ROW.match(_clean(line).rstrip())
        if not m:
            continue
        grade, _points, cr, title, num, prefix = m.groups()
        if prefix in ("فصلي", "تراكمي"):   # GPA summary rows, not courses
            continue
        code = CODE_ALIASES.get(f"{prefix} {num}", f"{prefix} {num}")
        cr, title = int(cr), title.strip()
        if grade in PASS_GRADES:
            if code not in seen:           # dedupe retakes; count credits once
                seen.add(code)
                passed.append(code)
                credits += cr
                details.append({"code": code, "credits": cr, "grade": grade, "title": title})
        elif grade in IN_PROGRESS:
            if code not in in_progress:      # dedupe (e.g. failed-then-retaken)
                in_progress.append(code)

    return {
        "completed": passed,
        "completed_credits": credits,
        "in_progress": in_progress,
        "details": details,
    }


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) != 2:
        print("usage: python transcript_parser.py <transcript.pdf>")
        raise SystemExit(1)
    print(json.dumps(parse_transcript(sys.argv[1]), ensure_ascii=False, indent=2))
