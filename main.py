"""
PDF to Mock Test – FastAPI Backend
Coordinate-aware spatial MCQ parser for JEE/NEET and all real-world PDF formats.

PDF extraction uses page.extract_words(use_text_flow=True, keep_blank_chars=True)
so every word retains its (x0, top, bottom) coordinates.  Words are grouped into
visual lines by their `top` coordinate, preserving per-word x0 for indentation
analysis.  The state-machine parser operates on these spatial lines.

Image extraction saves embedded images to /static/images/ and attaches each
image to the question whose vertical Y-range is closest (±80 px tolerance).

Supported question styles:
  1.  1)  (1)  1:  01.  Q1  Q1.  Q.1  Q 1  Que 1  Question 1
  I.  II.  III.  IV.  V.  VI.  VII.  VIII.  IX.  X.
  OCR-spaced: "2 1 2" → question 212

Supported option styles:
  A)  A.  A:  (A)  [A]  a)  (a)
  (i) (ii) (iii) (iv)
  1)  1.  2)  2.  3)  3.  4)  4.
  • bullet   * star   - dash   – en-dash
"""

import os
import re
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Optional

import pdfplumber
from fastapi import FastAPI, File, UploadFile, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from auth import (
    hash_password, verify_password, create_access_token,
    get_current_user, get_optional_user,
)

# ──────────────────────────────────────────────
# App setup
# ──────────────────────────────────────────────

app = FastAPI(title="PDF to Mock Test")

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
IMAGES_DIR = os.path.join(STATIC_DIR, "images")
DB_PATH    = os.path.join(BASE_DIR, "questions.db")

os.makedirs(IMAGES_DIR, exist_ok=True)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ──────────────────────────────────────────────
# Database helpers
# ──────────────────────────────────────────────

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Create (or recreate) the questions table with image_path support."""
    with get_connection() as conn:
        conn.execute("DROP TABLE IF EXISTS questions")
        conn.execute(
            """
            CREATE TABLE questions (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                question         TEXT    NOT NULL,
                option_a         TEXT,
                option_b         TEXT,
                option_c         TEXT,
                option_d         TEXT,
                option_a_image   TEXT,
                option_b_image   TEXT,
                option_c_image   TEXT,
                option_d_image   TEXT,
                has_diagram      INTEGER DEFAULT 0,
                image_path       TEXT,
                question_image   TEXT,
                correct_option   TEXT    NOT NULL DEFAULT 'a'
                                         CHECK(correct_option IN ('a','b','c','d'))
            )
            """
        )
        conn.commit()


def insert_questions(questions: list[dict]):
    with get_connection() as conn:
        conn.executemany(
            """
            INSERT INTO questions
                (question, option_a, option_b, option_c, option_d,
                 option_a_image, option_b_image, option_c_image, option_d_image,
                 has_diagram, image_path, question_image)
            VALUES
                (:question, :option_a, :option_b, :option_c, :option_d,
                 :option_a_image, :option_b_image, :option_c_image, :option_d_image,
                 :has_diagram, :image_path, :question_image)
            """,
            questions,
        )
        conn.commit()


def init_auth_db():
    """Create users, test_attempts, and user_answers tables if they do not exist.

    On every startup the function also:
      - Ensures user_answers.chosen_key has a CHECK(…IN('a','b','c','d')) constraint.
        If the old table lacks the CHECK it is transparently migrated (data preserved).
      - Creates performance indexes (idempotent: IF NOT EXISTS).
    """
    _USER_ANSWERS_DDL = """
        CREATE TABLE user_answers (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            attempt_id  INTEGER NOT NULL REFERENCES test_attempts(id),
            question_id INTEGER NOT NULL,
            chosen_key  TEXT    NOT NULL CHECK(chosen_key IN ('a','b','c','d')),
            answered_at TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(attempt_id, question_id)
        )
    """

    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                email         TEXT    NOT NULL UNIQUE,
                username      TEXT    NOT NULL,
                password_hash TEXT    NOT NULL,
                created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
            )
            """
        )

        # ── Migrate: add is_admin column if missing (idempotent) ──────────────
        try:
            conn.execute(
                "ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass  # column already exists

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS test_attempts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL REFERENCES users(id),
                pdf_name        TEXT    NOT NULL,
                total_questions INTEGER NOT NULL DEFAULT 0,
                duration        INTEGER NOT NULL DEFAULT 60,
                status          TEXT    NOT NULL DEFAULT 'ongoing',
                score           REAL,
                started_at      TEXT    NOT NULL DEFAULT (datetime('now')),
                completed_at    TEXT
            )
            """
        )

        # ── user_answers: create fresh or migrate to add CHECK constraint ──────
        # Detect whether the CHECK exists by reading the stored DDL directly.
        # The savepoint-probe technique is unreliable when PRAGMA foreign_keys=ON
        # because a FK violation (attempt_id=0 → no parent row) fires before the
        # CHECK and masks the absence of the constraint.
        existing_ddl_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='user_answers'"
        ).fetchone()

        if not existing_ddl_row:
            conn.execute(_USER_ANSWERS_DDL)
        else:
            existing_ddl = existing_ddl_row[0] or ""
            if "CHECK" not in existing_ddl:
                # Migrate: rename old table, create new one with CHECK, copy valid rows
                conn.execute("ALTER TABLE user_answers RENAME TO user_answers_old")
                conn.execute(_USER_ANSWERS_DDL)
                conn.execute(
                    """
                    INSERT INTO user_answers
                        (id, attempt_id, question_id, chosen_key, answered_at)
                    SELECT id, attempt_id, question_id, chosen_key, answered_at
                    FROM   user_answers_old
                    WHERE  chosen_key IN ('a','b','c','d')
                    """
                )
                conn.execute("DROP TABLE user_answers_old")

        # ── Indexes (idempotent) ───────────────────────────────────────────────
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_attempt_user "
            "ON test_attempts(user_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_answer_attempt "
            "ON user_answers(attempt_id)"
        )

        # ── Scoring configuration table ────────────────────────────────────────
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scoring_config (
                id            INTEGER PRIMARY KEY,
                marks_correct INTEGER NOT NULL DEFAULT 4,
                marks_wrong   INTEGER NOT NULL DEFAULT -1
            )
            """
        )
        # Ensure exactly one default row exists
        conn.execute(
            """
            INSERT INTO scoring_config (id, marks_correct, marks_wrong)
            SELECT 1, 4, -1
            WHERE NOT EXISTS (SELECT 1 FROM scoring_config WHERE id = 1)
            """
        )

        # ── Migrate: add summary columns to test_attempts (idempotent) ─────────
        for _col, _default in [
            ("correct",    "0"),
            ("wrong",      "0"),
            ("unanswered", "0"),
            ("total_time", "0"),
        ]:
            try:
                conn.execute(
                    f"ALTER TABLE test_attempts ADD COLUMN {_col} INTEGER NOT NULL DEFAULT {_default}"
                )
            except sqlite3.OperationalError:
                pass  # column already exists

        # ── Per-question time spent ────────────────────────────────────────────
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS question_time_spent (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id  INTEGER NOT NULL REFERENCES test_attempts(id),
                question_id INTEGER NOT NULL,
                seconds     INTEGER NOT NULL DEFAULT 0,
                UNIQUE(attempt_id, question_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_qtime_attempt "
            "ON question_time_spent(attempt_id)"
        )

        conn.commit()


# ── User helpers ──────────────────────────────────────────────────────────────

def db_create_user(email: str, username: str, password_hash: str) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO users (email, username, password_hash) VALUES (?,?,?)",
            (email, username, password_hash),
        )
        conn.commit()
        return cur.lastrowid


def db_get_user_by_email(email: str) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, email, username, password_hash, is_admin FROM users WHERE email = ?",
            (email,),
        ).fetchone()
    return dict(row) if row else None


def db_get_user_by_id(user_id: int) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, email, username, is_admin FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    return dict(row) if row else None


# ── Attempt helpers ───────────────────────────────────────────────────────────

def db_create_attempt(user_id: int, pdf_name: str, total_questions: int, duration: int) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO test_attempts (user_id, pdf_name, total_questions, duration)
               VALUES (?,?,?,?)""",
            (user_id, pdf_name, total_questions, duration),
        )
        conn.commit()
        return cur.lastrowid


def db_get_attempt(attempt_id: int) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM test_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
    return dict(row) if row else None


def db_get_user_attempts(user_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM test_attempts WHERE user_id = ? ORDER BY id DESC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def db_upsert_answer(attempt_id: int, question_id: int, chosen_key: str):
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO user_answers (attempt_id, question_id, chosen_key)
               VALUES (?,?,?)
               ON CONFLICT(attempt_id, question_id) DO UPDATE SET
                   chosen_key  = excluded.chosen_key,
                   answered_at = datetime('now')""",
            (attempt_id, question_id, chosen_key),
        )
        conn.commit()


def db_get_attempt_answers(attempt_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT question_id, chosen_key FROM user_answers WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def db_complete_attempt(
    attempt_id: int,
    score: float,
    correct: int,
    wrong: int,
    unanswered: int,
    total_time: int,
):
    with get_connection() as conn:
        conn.execute(
            """UPDATE test_attempts
               SET status       = 'completed',
                   score        = ?,
                   correct      = ?,
                   wrong        = ?,
                   unanswered   = ?,
                   total_time   = ?,
                   completed_at = datetime('now')
               WHERE id = ?""",
            (score, correct, wrong, unanswered, total_time, attempt_id),
        )
        conn.commit()


def db_save_time_spent(attempt_id: int, time_spent: dict) -> None:
    """Bulk-upsert per-question time (seconds) for an attempt."""
    rows = []
    for qid, secs in time_spent.items():
        try:
            rows.append((attempt_id, int(qid), max(0, int(secs))))
        except (ValueError, TypeError):
            pass  # skip any malformed entries
    if not rows:
        return
    with get_connection() as conn:
        conn.executemany(
            """INSERT INTO question_time_spent (attempt_id, question_id, seconds)
               VALUES (?,?,?)
               ON CONFLICT(attempt_id, question_id) DO UPDATE SET
                   seconds = excluded.seconds""",
            rows,
        )
        conn.commit()


def db_get_time_spent(attempt_id: int) -> dict:
    """Return {str(question_id): seconds} for an attempt."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT question_id, seconds FROM question_time_spent WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchall()
    return {str(r["question_id"]): r["seconds"] for r in rows}


def calculate_score(attempt_id: int) -> float:
    """Compute score server-side from stored answers vs correct_option.

    Marks per outcome are read from the scoring_config table so admins can
    tune them at runtime without a code deployment.

    Scoring:
        marks_correct   correct answer  (default +4)
        marks_wrong     wrong answer    (default -1)
         0              unattempted     (not in user_answers at all)

    Returns the raw float score (can be negative).
    """
    config = db_get_scoring_config()
    marks_correct = float(config["marks_correct"])
    marks_wrong   = float(config["marks_wrong"])

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT ua.chosen_key, q.correct_option
            FROM   user_answers ua
            JOIN   questions    q  ON q.id = ua.question_id
            WHERE  ua.attempt_id = ?
            """,
            (attempt_id,),
        ).fetchall()
    score = 0.0
    for row in rows:
        if row["chosen_key"] == row["correct_option"]:
            score += marks_correct
        else:
            score += marks_wrong
    return score


def calculate_score_detailed(attempt_id: int) -> dict:
    """Server-side scoring with full breakdown.

    Reads answers from user_answers, compares against questions.correct_option.
    Scoring rules (from scoring_config):
        +marks_correct  for a correct answer
        +marks_wrong    for a wrong  answer  (typically negative)
         0              for an unanswered question
    Score is floored at 0 — cannot go below zero.

    Returns:
        {
            "score":      float,   # floored at 0
            "correct":    int,
            "wrong":      int,
            "unanswered": int,
        }
    """
    config        = db_get_scoring_config()
    marks_correct = float(config["marks_correct"])
    marks_wrong   = float(config["marks_wrong"])

    with get_connection() as conn:
        total_questions = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        rows = conn.execute(
            """
            SELECT ua.chosen_key, q.correct_option
            FROM   user_answers ua
            JOIN   questions    q  ON q.id = ua.question_id
            WHERE  ua.attempt_id = ?
            """,
            (attempt_id,),
        ).fetchall()

    correct   = sum(1 for r in rows if r["chosen_key"] == r["correct_option"])
    wrong     = sum(1 for r in rows if r["chosen_key"] != r["correct_option"])
    unanswered = total_questions - len(rows)

    raw_score = correct * marks_correct + wrong * marks_wrong
    score     = max(0.0, raw_score)          # floor at zero

    return {
        "score":      score,
        "correct":    correct,
        "wrong":      wrong,
        "unanswered": unanswered,
    }


# ── Scoring config helpers ────────────────────────────────────────────────────

def db_get_scoring_config() -> dict:
    """Return the single scoring_config row, falling back to defaults."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT marks_correct, marks_wrong FROM scoring_config WHERE id = 1"
        ).fetchone()
    return dict(row) if row else {"marks_correct": 4, "marks_wrong": -1}


def db_update_scoring_config(marks_correct: int, marks_wrong: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE scoring_config SET marks_correct = ?, marks_wrong = ? WHERE id = 1",
            (marks_correct, marks_wrong),
        )
        conn.commit()


def db_set_question_answer(question_id: int, correct_option: str) -> bool:
    """Set correct_option for a question. Returns True if the row existed."""
    with get_connection() as conn:
        updated = conn.execute(
            "UPDATE questions SET correct_option = ? WHERE id = ?",
            (correct_option, question_id),
        ).rowcount
        conn.commit()
    return updated > 0


# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
def on_startup():
    init_auth_db()


# ──────────────────────────────────────────────
# Regex patterns  (applied to .strip()-ed line text)
# ──────────────────────────────────────────────

# ── Numeric question with Q-prefix: Q1 Q1. Q.1 Q 1 Que 1 Question 1
# Always valid \u2014 the prefix is an unambiguous signal.
QUESTION_PREFIXED_RE = re.compile(
    r"""
    ^\s*
    (?:
        (?:Question|Que)\.?\s+
      | Q\.?\s*
    )
    \(?
    (0?\d{1,3})
    \)?
    \s*
    [.):\u2013\-]?
    \s*
    (.*)
    $
    """,
    re.VERBOSE | re.IGNORECASE,
)

# ── Numeric question WITHOUT prefix: 1. 1) (1) 1:
# REQUIRES a separator (. ) :) after the number \u2014 bare "2 should be..."
# must NOT match (it's a math continuation line).
QUESTION_BARE_NUM_RE = re.compile(
    r"""
    ^\s*
    \(?
    (0?\d{1,3})
    \)?
    \s*
    [.):\u2013\-]       # separator is MANDATORY
    \s*
    (.*)
    $
    """,
    re.VERBOSE,
)

# OCR-spaced question number, e.g. "2 1 2" → "212"
QUESTION_OCR_SPACED_RE = re.compile(
    r"""
    ^\s*
    (\d(?:\s+\d){1,3})   # digits separated by spaces, 2-4 digits total
    \s*[.):\-]?\s*
    (.+)                  # must have question text after
    $
    """,
    re.VERBOSE,
)

# Roman numeral question: I. II. III. IV. V. …
QUESTION_ROMAN_RE = re.compile(
    r"""
    ^\s*
    (
        X{0,3}
        (?:IX|IV|V?I{0,3})
    )
    (?!\()
    \s*
    [.:]
    \s*
    (.*)
    $
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Number-only line (question number alone on its own line)
# REQUIRES a Q./Que/Question prefix — bare numbers like "1 2" or "0" from
# math subscripts/superscripts must NOT be treated as question headers.
QNUM_ONLY_RE = re.compile(
    r"""
    ^\s*
    (?:(?:Question|Que)\.?\s+|Q\.\s*)
    \(?
    (0?\d{1,3})
    \)?
    \s*[.):\-]?\s*
    $
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Roman numeral number-only line
QNUM_ROMAN_ONLY_RE = re.compile(
    r"""
    ^\s*
    (X{0,3}(?:IX|IV|V?I{0,3}))
    (?!\()
    \s*[.:]?\s*
    $
    """,
    re.VERBOSE | re.IGNORECASE,
)

# ── Option patterns ──────────────────────────────────────────────────────

# Standard letter options: A) A. A: (A) [A] a) (a)
# Allows empty text after label for diagram-reference options like "(A)" alone
OPTION_LETTER_RE = re.compile(
    r"""
    ^\s*
    [\(\[]?
    ([A-Da-d])
    [\)\].:]
    \s*[-:]?\s*
    (.*)
    $
    """,
    re.VERBOSE,
)

# Roman numeral options: (i) (ii) (iii) (iv)
OPTION_ROMAN_RE = re.compile(
    r"""
    ^\s*
    \(
    (i{1,3}|iv|v?i{0,3})
    \)
    \s*
    (.+)
    $
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Numeric options: 1) 2) 3) 4) or 1. 2. 3. 4. (only when inside a question)
OPTION_NUMERIC_RE = re.compile(
    r"""
    ^\s*
    ([1-4])
    [).]
    \s+
    (.+)
    $
    """,
    re.VERBOSE,
)

# Bullet/dash options: • text  * text  - text  – text
OPTION_BULLET_RE = re.compile(
    r"""
    ^\s*
    [•\*\-–]
    \s+
    (.+)
    $
    """,
    re.VERBOSE,
)

# Stop / noise patterns
STOP_PATTERNS = re.compile(
    r"""
    ^\s*
    (?:
        answers?\s*(?:[&]|and)\s*solutions?   # "Answers & Solutions" / "and Solutions"
      | answer\s*key                           # "Answer Key"
      | answer\s*sheet
      | solutions?                             # "Solution" / "Solutions"
      | explanations?                          # "Explanation" / "Explanations"
      | hints?
    )
    \b
    """,
    re.VERBOSE | re.IGNORECASE,
)

NOISE_RE = re.compile(
    r"^\s*(?:page\s*\d+|\d+\s*/\s*\d+|www\.|http|©|copyright)\s*$",
    re.IGNORECASE,
)


# ──────────────────────────────────────────────
# Helper utilities
# ──────────────────────────────────────────────

def _is_valid_question_start(num_str: str) -> bool:
    try:
        return int(num_str.lstrip("0") or "0") <= 200
    except ValueError:
        return True


def _option_letter_to_key(letter: str) -> str:
    return f"option_{letter.lower()}"


def _roman_option_to_key(roman: str) -> str | None:
    mapping = {"i": "a", "ii": "b", "iii": "c", "iv": "d"}
    k = mapping.get(roman.lower())
    return f"option_{k}" if k else None


def _numeric_to_option_key(digit: str) -> str | None:
    mapping = {"1": "a", "2": "b", "3": "c", "4": "d"}
    k = mapping.get(digit)
    return f"option_{k}" if k else None


def _try_option(line: str, in_question: bool) -> tuple[str | None, str | None]:
    """Try to match line as any option format."""
    m = OPTION_LETTER_RE.match(line)
    if m:
        return _option_letter_to_key(m.group(1)), m.group(2).strip()
    if in_question:
        m = OPTION_ROMAN_RE.match(line)
        if m:
            key = _roman_option_to_key(m.group(1))
            if key:
                return key, m.group(2).strip()
        m = OPTION_NUMERIC_RE.match(line)
        if m:
            key = _numeric_to_option_key(m.group(1))
            if key:
                return key, m.group(2).strip()
        m = OPTION_BULLET_RE.match(line)
        if m:
            return "__bullet__", m.group(1).strip()
    return None, None


def _assign_bullet_option(current_q: dict, text: str) -> str | None:
    for key in ("option_a", "option_b", "option_c", "option_d"):
        if not current_q.get(key):
            current_q[key] = text
            return key
    return None


def _count_options(q: dict) -> int:
    return sum(1 for k in ("option_a", "option_b", "option_c", "option_d") if q.get(k))


def _looks_math_fragment(text: str) -> bool:
    t = text.strip()
    if not t or len(t) > 28:
        return False
    return re.fullmatch(r"[A-Za-z0-9\[\]\(\)\+\-−=*/.:\s]+", t) is not None


# ── Math character normalization ──────────────────────────────────────────────
# Some PDFs (especially Indian exam PDFs) use Symbol or custom math fonts where
# glyph codes don't match the expected Unicode codepoints.  This table maps the
# most common problematic encodings to their correct Unicode equivalents.
_MATH_CHAR_MAP: dict[str, str] = {
    "\uf028": "√",   # radical sign in some Symbol-variant fonts
    "\uf0d6": "√",   # alternative radical encoding
    "\uf0b0": "°",   # degree in Symbol font
    "\uf0b2": "²",   # superscript 2 in some fonts
    "\uf0b3": "³",   # superscript 3
    "\uf02d": "−",   # minus in Symbol font
    "\u221a": "√",   # U+221A – already correct, normalise to same char
    "\u2212": "−",   # U+2212 minus – already correct
}


def _normalize_math_chars(text: str) -> str:
    """Map known symbol-font mis-encodings to correct Unicode math characters."""
    return "".join(_MATH_CHAR_MAP.get(c, c) for c in text)


def _append_option_text(existing: str | None, incoming: str) -> str:
    """
    Merge split option fragments while preserving math layout as plain text.

    Handles common OCR/PDF splits like:
      - `t[1+e` + `]` + `1−e`  -> `t[1+e] / 1−e`
      - `mv2` + `0` + `x2` + `0` -> `mv20 / x20`
    """
    new_part = incoming.strip()
    if not new_part:
        return (existing or "").strip()

    current = (existing or "").rstrip()
    if not current:
        return new_part

    if new_part in {"]", ")"}:
        return current + new_part

    compact_current = re.sub(r"\s+", "", current)
    compact_new = re.sub(r"\s+", "", new_part)

    # Subscript/exponent continuation like `mv2` + `0` -> `mv20`
    if re.fullmatch(r"\d+", compact_new) and compact_current and compact_current[-1].isalnum():
        return current + compact_new

    starts_like_denominator = (
        compact_new.lower().startswith("x")
        or re.match(r"^\d*x\d", compact_new, re.IGNORECASE) is not None
        or re.match(r"^\d+[+\-−][A-Za-z0-9]+$", compact_new) is not None
    )
    current_looks_like_numerator = (
        current.endswith("]")
        or "mv" in compact_current.lower()
        or re.search(r"[+\-−]", compact_current) is not None
    )

    if (
        "/" not in current
        and starts_like_denominator
        and current_looks_like_numerator
        and _looks_math_fragment(current)
        and _looks_math_fragment(new_part)
    ):
        return f"{current} / {new_part}"

    return f"{current} {new_part}"


def _normalize_math_option_text(text: str | None) -> str | None:
    if text is None:
        return None
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized or "/" in normalized:
        return normalized

    # Compact OCR fragments from stacked fractions in mechanics PDFs.
    # Examples:
    #   mv20x20      -> mv20 / x20
    #   mv202x20     -> mv20 / 2x20
    #   3 mv202 x20  -> 3 mv20 / 2 x20
    normalized = re.sub(
        r"(?i)\b(mv2\s*0)\s*([23]?\s*x2\s*0)\b",
        r"\1 / \2",
        normalized,
    )
    return normalized


# ──────────────────────────────────────────────
# Text extraction: extract_text() + chars for Y-coordinates
# ──────────────────────────────────────────────
# Many JEE/NEET PDFs encode individual characters as separate glyphs with
# wide inter-character spacing.  pdfplumber's extract_words() treats each
# char as a separate "word" regardless of x_tolerance.
#
# However, extract_text() uses pdfplumber's internal layout engine which
# correctly reconstructs words with proper spacing.  So we:
#   1. Use extract_text() to get properly-spaced text lines.
#   2. Use page.chars to build a Y-coordinate lookup for each line.
#   3. Combine them into the same visual-line dicts the parser expects.
# ──────────────────────────────────────────────

# Indentation tolerance: if a continuation line's x0 is at least this many
# points to the right of the option label's x0, treat it as continuation text.
INDENT_TOL = 10.0

# Tolerance for matching chars to a text line's Y-coordinate.
LINE_Y_TOL = 5.0


def _extract_text_lines(page) -> list[dict]:
    """
    Build visual lines directly from raw character data.

    Groups chars by Y-position (LINE_Y_TOL tolerance), reconstructs text
    by inserting spaces where there is a horizontal gap between chars, and
    merges subscript/superscript rows (avg font-size < 80% of dominant)
    into the preceding line.

    This approach avoids the index-alignment bug that occurred when pairing
    extract_text() lines with y_rows derived from chars — pdfplumber's
    extract_text() folds subscripts inline while the char data keeps them on
    separate Y-rows, causing a one-off mismatch for every subscript line.

    Returns list of dicts sorted by vertical position:
        [{"text": str, "top": float, "bottom": float, "x0": float}, …]
    """
    chars = page.chars or []
    if not chars:
        return []

    # Sort chars by top Y then x0
    sorted_chars = sorted(chars, key=lambda c: (float(c["top"]), float(c.get("x0", 0))))

    # Group into Y-rows by top tolerance
    rows: list[list] = []
    current_row = [sorted_chars[0]]
    current_top = float(sorted_chars[0]["top"])
    for c in sorted_chars[1:]:
        c_top = float(c["top"])
        if abs(c_top - current_top) <= LINE_Y_TOL:
            current_row.append(c)
        else:
            rows.append(current_row)
            current_row = [c]
            current_top = c_top
    if current_row:
        rows.append(current_row)

    # Dominant font size (median across all chars)
    all_sizes = sorted(
        float(c.get("size", 0))
        for row in rows
        for c in row
        if float(c.get("size", 0)) > 0
    )
    dominant_size = all_sizes[len(all_sizes) // 2] if all_sizes else 12.0

    result: list[dict] = []
    for row in rows:
        row_sorted = sorted(row, key=lambda c: float(c.get("x0", 0)))

        # Reconstruct text, inserting a space wherever char gap > 25% of font size
        text_parts: list[str] = []
        prev_x1: float | None = None
        for c in row_sorted:
            ch = c.get("text", "")
            if not ch:
                continue
            x0 = float(c.get("x0", 0))
            sz = float(c.get("size", dominant_size)) or dominant_size
            x1 = float(c.get("x1", x0 + sz * 0.5))
            if prev_x1 is not None and x0 - prev_x1 > sz * 0.25:
                text_parts.append(" ")
            text_parts.append(ch)
            prev_x1 = max(prev_x1 or 0.0, x1)

        text = "".join(text_parts).strip()
        if not text:
            continue

        # Normalise symbol-font mis-encodings (e.g. \uf028 → √)
        text = _normalize_math_chars(text)

        avg_top  = sum(float(c["top"]) for c in row) / len(row)
        avg_bot  = sum(float(c.get("bottom", c["top"] + 12)) for c in row) / len(row)
        min_x0   = min(float(c.get("x0", 0)) for c in row)
        sizes_row = [float(c.get("size", 0)) for c in row if float(c.get("size", 0)) > 0]
        avg_size  = sum(sizes_row) / len(sizes_row) if sizes_row else 0.0

        is_sub = avg_size > 0 and avg_size < dominant_size * 0.80

        if is_sub and result:
            prev = result[-1]
            prev_center = (prev["top"] + prev["bottom"]) / 2.0
            row_center  = avg_top + (avg_bot - avg_top) / 2.0
            # Row center above previous line center → superscript; else subscript
            if row_center < prev_center:
                sup_map = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")
                text = text.translate(sup_map)
            else:
                sub_map = str.maketrans("0123456789", "₀₁₂₃₄₅₆₇₈₉")
                text = text.translate(sub_map)
            result[-1]["text"] += text
            result[-1]["bottom"] = max(result[-1]["bottom"], avg_bot)
        else:
            result.append({
                "text":   text,
                "top":    avg_top,
                "bottom": avg_bot,
                "x0":     min_x0,
            })

    return result


# ──────────────────────────────────────────────
# State-machine parser (operates on visual lines)
# ──────────────────────────────────────────────

def parse_questions_from_lines(visual_lines: list[dict]) -> list[dict]:
    """
    Coordinate-aware state-machine MCQ parser that works on spatially-grouped
    visual lines.

    Each entry in `visual_lines` is:
        {"text": str, "top": float, "bottom": float, "x0": float}

    States: IDLE → IN_QUESTION → IN_OPTIONS

    Returns list of dicts:
        {question, option_a…option_d, has_diagram, image_path,
         _num, _y_start, _y_end}

    _y_start / _y_end are the vertical Y-range of the question block
    (used for image attachment, stripped before DB insert).

    CRITICAL: When a new question header appears, the previous question is
    finalized UNCONDITIONALLY — even if options are incomplete.  Two questions
    are NEVER merged.
    """
    questions: list[dict] = []
    current_q: dict | None = None
    state = "IDLE"
    stopped = False
    last_option_key: str | None = None
    last_option_x0: float = 0.0    # x0 of the most recently matched option label

    def _make_question(num_str: str, text: str, y_top: float) -> dict:
        return {
            "question":      text,
            "option_a":      None,
            "option_b":      None,
            "option_c":      None,
            "option_d":      None,
            "option_a_image": None,
            "option_b_image": None,
            "option_c_image": None,
            "option_d_image": None,
            "has_diagram":   0,
            "image_path":    None,
            "_num":          num_str,
            "_y_start":      y_top,
            "_y_end":        y_top,
            "_opt_y":        {},   # {letter: y_top} recorded when each option first appears
        }

    def _finish_question():
        """Finalize current question — always emitted, never gated on option count."""
        nonlocal current_q, last_option_key, last_option_x0
        if current_q is None:
            return
        for key in ("option_a", "option_b", "option_c", "option_d"):
            current_q[key] = _normalize_math_option_text(current_q.get(key))
        # Always emit the question (even with 0 options)
        questions.append(current_q)
        current_q = None
        last_option_key = None
        last_option_x0 = 0.0

    for vl in visual_lines:
        line = vl["text"].strip()
        y_top = vl["top"]
        y_bot = vl["bottom"]
        line_x0 = vl.get("x0", 0.0)

        if not line:
            continue

        # ── Hard stop ─────────────────────────────────────────────────────────
        if STOP_PATTERNS.match(line):
            _finish_question()
            stopped = True
        # Also stop when any line *contains* "solution:" (answer-section marker)
        if not stopped and re.search(r"\bsolution\s*:", line, re.IGNORECASE):
            _finish_question()
            stopped = True
        if stopped:
            continue

        # ── Noise ─────────────────────────────────────────────────────────────
        if NOISE_RE.match(line):
            continue

        # Track Y-extent of current question block
        if current_q is not None:
            current_q["_y_end"] = y_bot

        in_question_ctx = (state in ("IN_QUESTION", "IN_OPTIONS")) and current_q is not None

        # ── 1. Roman numeral question (highest priority, any state) ───────────
        q_rom_match  = QUESTION_ROMAN_RE.match(line)
        qrom_only    = QNUM_ROMAN_ONLY_RE.match(line)

        if q_rom_match and q_rom_match.group(1):
            _finish_question()
            current_q = _make_question(
                q_rom_match.group(1).upper(),
                q_rom_match.group(2).strip(),
                y_top
            )
            state = "IN_QUESTION"
            last_option_key = None
            last_option_x0 = 0.0
            continue

        if qrom_only and qrom_only.group(1):
            _finish_question()
            current_q = _make_question(qrom_only.group(1).upper(), "", y_top)
            state = "IN_QUESTION"
            last_option_key = None
            last_option_x0 = 0.0
            continue

        # ── 2. Try option ──────────────────────────────────────────────────────
        opt_key, opt_text = _try_option(line, in_question=in_question_ctx)

        # ── 3. Numeric question-number-only line ──────────────────────────────
        qnum_only = QNUM_ONLY_RE.match(line)

        # ── 4. Full numeric question line ──────────────────────────────────────
        # Try prefixed pattern first (Q.1, Que 1, Question 1), then bare (1. 1) (1))
        q_num_match = QUESTION_PREFIXED_RE.match(line)
        q_num_has_prefix = bool(q_num_match)

        if not q_num_match:
            q_num_match = QUESTION_BARE_NUM_RE.match(line)

        if q_num_match:
            num_str = q_num_match.group(1)
            rest = q_num_match.group(2).strip()

            if not _is_valid_question_start(num_str):
                q_num_match = None

            # If in options state and no body text, probably a numeric option
            if q_num_match and state == "IN_OPTIONS" and current_q is not None:
                if not rest:
                    q_num_match = None
            # If line also matches as an option, prefer the option interpretation
            if q_num_match and opt_key:
                q_num_match = None

            # Suppress if the body text is an answer/solution reference
            # e.g. "Q.1 Answer: (B)" or "Q1 Solution: ..." must NOT become questions
            if q_num_match and re.search(r"\b(?:answer|solution)\b", rest, re.IGNORECASE):
                q_num_match = None

        # ── 5. OCR-spaced question number: "2 1 2" ────────────────────────────
        # ONLY match in IDLE state — inside a question, spaced digits are almost
        # always math subscripts / superscripts (e.g. m₁ m₂ rendering as "1 2").
        q_ocr_match = None
        if state == "IDLE" and not q_num_match and not opt_key and not qnum_only:
            q_ocr_match = QUESTION_OCR_SPACED_RE.match(line)
            if q_ocr_match:
                collapsed = q_ocr_match.group(1).replace(" ", "")
                if not _is_valid_question_start(collapsed):
                    q_ocr_match = None

        # ── Transitions ───────────────────────────────────────────────────────

        if qnum_only and not opt_key:
            # Question number on its own line
            _finish_question()
            current_q = _make_question(qnum_only.group(1), "", y_top)
            state = "IN_QUESTION"
            last_option_key = None
            last_option_x0 = 0.0

        elif q_num_match and q_num_match.group(2).strip():
            # Full numeric question line with body text
            _finish_question()
            current_q = _make_question(
                q_num_match.group(1),
                q_num_match.group(2).strip(),
                y_top
            )
            state = "IN_QUESTION"
            last_option_key = None
            last_option_x0 = 0.0

        elif q_ocr_match:
            # OCR-spaced question number with body text
            collapsed = q_ocr_match.group(1).replace(" ", "")
            _finish_question()
            current_q = _make_question(
                collapsed,
                q_ocr_match.group(2).strip(),
                y_top
            )
            state = "IN_QUESTION"
            last_option_key = None
            last_option_x0 = 0.0

        elif opt_key and current_q is not None:
            # Option line
            if opt_key == "__bullet__":
                assigned = _assign_bullet_option(current_q, opt_text)
                if assigned:
                    last_option_key = assigned
                    last_option_x0 = line_x0
                    letter = assigned[-1]  # 'a', 'b', 'c', 'd'
                    if letter not in current_q["_opt_y"]:
                        current_q["_opt_y"][letter] = y_top
            else:
                if not current_q.get(opt_key):
                    current_q[opt_key] = opt_text
                    last_option_key = opt_key
                    last_option_x0 = line_x0
                    letter = opt_key[-1]  # 'a', 'b', 'c', 'd'
                    if letter not in current_q["_opt_y"]:
                        current_q["_opt_y"][letter] = y_top
            state = "IN_OPTIONS"

        elif current_q is not None:
            # Continuation line (not a new question, not an option)
            if state == "IN_QUESTION":
                # Multi-line question body
                sep = " " if current_q["question"] else ""
                current_q["question"] += sep + line
            elif state == "IN_OPTIONS":
                # Multi-line option continuation:
                # If current line is indented further than the option label,
                # append to the last option; otherwise append to question text.
                if (last_option_key
                        and current_q.get(last_option_key) is not None
                        and line_x0 >= last_option_x0 + INDENT_TOL):
                    current_q[last_option_key] = _append_option_text(current_q[last_option_key], line)
                elif last_option_key and current_q.get(last_option_key) is not None:
                    # Same or less indentation — still append to last option
                    # (common for wrapped option text at same indent level)
                    current_q[last_option_key] = _append_option_text(current_q[last_option_key], line)

    _finish_question()
    return questions


# Legacy wrapper: parse from raw text (for unit tests / backward compat)
def parse_questions_from_text(full_text: str) -> list[dict]:
    """Parse MCQs from plain text (no spatial info). Lines get synthetic Y coords."""
    fake_lines = []
    for i, raw in enumerate(full_text.splitlines()):
        fake_lines.append({"text": raw, "top": float(i), "bottom": float(i + 1), "x0": 0.0})
    questions = parse_questions_from_lines(fake_lines)
    for q in questions:
        q.pop("_num", None)
        q.pop("_y_start", None)
        q.pop("_y_end", None)
    return questions


# ──────────────────────────────────────────────
# Image extraction helpers
# ──────────────────────────────────────────────

def _save_page_images(page, page_num: int) -> list[dict]:
    """
    Extract diagrams from a pdfplumber page.

    Two sources are tried:
      1. page.images  – embedded raster images (JPEG/PNG streams inside the PDF).
      2. page.figures – bounding boxes of vector-graphic regions (lines, curves,
                        fills drawn with PDF path operators).  This captures
                        diagrams that were drawn rather than embedded, including
                        diagrams that span two pages (each half is captured
                        separately and both halves attach to the same question
                        via the y_offset coordinate system in the caller).

    Saves each region as PNG under static/images/.
    Returns list of dicts: [{"path": web_path, "top": y_top, "bottom": y_bot}, …]
    """
    saved: list[dict] = []
    MIN_DIM = 40.0   # PDF points (~56 px at 96 dpi); ignore tiny decorative elements

    try:
        from PIL import Image as _PILImage  # type: ignore  # noqa: PLC0415, F841
    except ImportError:
        print("[WARN] Pillow not installed – image extraction skipped.")
        return saved

    # ── 1. Embedded raster images ────────────────────────────────────────────
    for idx, img_meta in enumerate(page.images or []):
        try:
            x0 = float(img_meta.get("x0", 0))
            y0 = float(img_meta.get("top", img_meta.get("y0", 0)))
            x1 = float(img_meta.get("x1", page.width))
            y1 = float(img_meta.get("bottom", img_meta.get("y1", page.height)))

            # pdfplumber's page.images uses top-origin coords (top < bottom).
            top    = min(y0, y1)
            bottom = max(y0, y1)
            if top >= bottom or x0 >= x1:
                continue

            # Skip tiny decorative images (logos, favicons, footer strips)
            if (x1 - x0) < MIN_DIM or (bottom - top) < MIN_DIM:
                continue

            cropped = page.crop((x0, top, x1, bottom))
            pil_img = cropped.to_image(resolution=150).original

            fname    = f"page{page_num}_img{idx}_{uuid.uuid4().hex[:6]}.png"
            out_path = os.path.join(IMAGES_DIR, fname)
            pil_img.save(out_path, "PNG")
            saved.append({"path": f"/static/images/{fname}", "top": top, "bottom": bottom})
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] Could not extract image on page {page_num} idx {idx}: {exc}")

    # ── 2. Vector/drawn figures via page.figures ─────────────────────────────
    # page.figures groups all path-based graphical objects (rects, lines, curves)
    # into bounding-box regions.  This captures diagrams that are drawn rather
    # than embedded, including those that span two pages.
    for idx2, fig in enumerate(getattr(page, "figures", None) or []):
        try:
            fx0     = float(fig.get("x0", 0))
            ftop    = float(fig.get("top", 0))
            fx1     = float(fig.get("x1", page.width))
            fbottom = float(fig.get("bottom", page.height))

            if (fx1 - fx0) < MIN_DIM or (fbottom - ftop) < MIN_DIM:
                continue

            # Skip if a raster image already covers approximately the same region
            fig_center_y = (ftop + fbottom) / 2.0
            already_covered = any(
                abs(((r["top"] + r["bottom"]) / 2.0) - fig_center_y) < 30
                for r in saved
            )
            if already_covered:
                continue

            cropped = page.crop((fx0, ftop, fx1, fbottom))
            pil_img = cropped.to_image(resolution=150).original

            fname    = f"page{page_num}_fig{idx2}_{uuid.uuid4().hex[:6]}.png"
            out_path = os.path.join(IMAGES_DIR, fname)
            pil_img.save(out_path, "PNG")
            saved.append({"path": f"/static/images/{fname}", "top": ftop, "bottom": fbottom})
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] Could not render figure on page {page_num} idx2 {idx2}: {exc}")

    return saved


# ──────────────────────────────────────────────
# Question screenshot helper
# ──────────────────────────────────────────────

def _crop_question_screenshot(
    page_meta: list[dict],
    y_start_global: float,
    y_end_global: float,
    q_idx: int,
) -> str | None:
    """
    Crop the full vertical span of a question, stitching across page boundaries.

    For every page whose visible area overlaps [y_start_global - PAD_TOP, y_end_global]:
      - Convert the overlap to page-local coordinates.
      - Crop full page width for that slice and render at 150 dpi.

    Slices are stitched vertically (top → bottom) into one PIL image and saved.

    Only a small top padding (PAD_TOP) is applied; no padding is added below so
    the next question never bleeds into the screenshot.

    Returns web path /static/images/… or None on failure.
    """
    try:
        from PIL import Image as _PILImage  # type: ignore  # noqa: PLC0415
    except ImportError:
        return None

    PAD_TOP = 6.0  # PDF points of padding above the first line

    slices: list = []  # PIL images ordered top → bottom

    for pm in page_meta:
        page_global_start = pm["y_offset"]
        page_global_end   = pm["y_offset"] + pm["height"]

        # Overlap of the question's global range with this page's global range
        overlap_start = max(y_start_global - PAD_TOP, page_global_start)
        overlap_end   = min(y_end_global,              page_global_end)

        if overlap_end <= overlap_start:
            continue  # this page doesn't contribute

        # Convert overlap to page-local coordinates
        local_start = max(0.0, overlap_start - page_global_start)
        local_end   = min(float(pm["height"]), overlap_end - page_global_start)

        if local_end <= local_start:
            continue

        try:
            cropped = pm["page"].crop((0, local_start, pm["page"].width, local_end))
            slices.append(cropped.to_image(resolution=150).original)
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] Crop failed Q{q_idx} page {pm['page_num']}: {exc}")

    if not slices:
        return None

    # Stitch slices vertically
    if len(slices) == 1:
        final_img = slices[0]
    else:
        from PIL import Image as _PILImage  # type: ignore  # noqa: PLC0415
        total_h = sum(img.height for img in slices)
        max_w   = max(img.width  for img in slices)
        final_img = _PILImage.new("RGB", (max_w, total_h), color=(255, 255, 255))
        y_cur = 0
        for img in slices:
            final_img.paste(img, (0, y_cur))
            y_cur += img.height

    try:
        fname    = f"qshot{q_idx}_{uuid.uuid4().hex[:6]}.png"
        out_path = os.path.join(IMAGES_DIR, fname)
        final_img.save(out_path, "PNG")
        return f"/static/images/{fname}"
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] Save failed Q{q_idx}: {exc}")
        return None


# ──────────────────────────────────────────────
# PDF extraction: spatial word-based pipeline
# ──────────────────────────────────────────────

# Y-axis tolerance (in PDF points) for attaching an image to a question.
IMAGE_Y_TOLERANCE = 150.0


def parse_with_diagram_info(pdf_path: str) -> list[dict]:
    """
    Full spatial pipeline:
      1. For each page, extract text lines (with Y-coords) and embedded images.
      2. Collect a page_meta list so page objects stay accessible while PDF is open.
      3. Parse MCQs from the accumulated visual lines.
      4. Attach embedded images to questions by Y-position.
      5. Promote per-option images.
      6. Crop each question's vertical span as a PNG (question_image).
      7. Clean up internal metadata fields.

    All processing is done inside the pdfplumber.open() 'with' block so that
    page objects remain valid when _crop_question_screenshot() needs them.
    """
    all_visual_lines: list[dict] = []
    all_images: list[dict] = []
    page_meta: list[dict] = []   # {page, y_offset, height, page_num}
    y_offset = 0.0

    with pdfplumber.open(pdf_path) as pdf:
        # ── Pass 1: collect text lines, images, and page metadata ───────────
        for page_num, page in enumerate(pdf.pages, start=1):
            page_y_start = y_offset

            try:
                page_lines = _extract_text_lines(page)
                if page_lines:
                    for pl in page_lines:
                        pl["top"]    += y_offset
                        pl["bottom"] += y_offset
                    all_visual_lines.extend(page_lines)
            except Exception as exc:  # noqa: BLE001
                print(f"[WARN] Skipping text on page {page_num}: {exc}")

            try:
                page_imgs = _save_page_images(page, page_num)
                for img in page_imgs:
                    img["top"]    += y_offset
                    img["bottom"] += y_offset
                all_images.extend(page_imgs)
            except Exception as exc:  # noqa: BLE001
                print(f"[WARN] Image extraction failed on page {page_num}: {exc}")

            page_meta.append({
                "page":     page,
                "y_offset": page_y_start,
                "height":   float(page.height),
                "page_num": page_num,
            })
            y_offset += float(page.height) + 20.0

        # ── Parse questions (inside 'with' so page objects remain live) ──────
        questions = parse_questions_from_lines(all_visual_lines)

        # Build a coord-lookup so the second pass can find image positions by path
        path_to_coords: dict[str, tuple[float, float]] = {
            img["path"]: (img["top"], img["bottom"]) for img in all_images
        }

        # ── First pass: attach embedded images to questions by Y-position ────
        for img in all_images:
            img_center_y = (img["top"] + img["bottom"]) / 2.0
            best_q = None
            best_dist = float("inf")

            for q in questions:
                y_start = q.get("_y_start", 0)
                y_end   = q.get("_y_end", 0)
                range_top    = y_start - IMAGE_Y_TOLERANCE
                range_bottom = y_end   + IMAGE_Y_TOLERANCE
                if range_top <= img_center_y <= range_bottom:
                    if y_start <= img_center_y <= y_end:
                        dist = 0.0
                    else:
                        dist = min(abs(img_center_y - y_start), abs(img_center_y - y_end))
                    if dist < best_dist:
                        best_dist = dist
                        best_q = q

            if best_q is None:
                for q in questions:
                    y_start = q.get("_y_start", 0)
                    y_end   = q.get("_y_end", 0)
                    dist = min(abs(img_center_y - y_start), abs(img_center_y - y_end))
                    if dist < best_dist:
                        best_dist = dist
                        best_q = q

            if best_q is not None:
                best_q["has_diagram"] = 1
                if best_q["image_path"] is None:
                    best_q["image_path"] = img["path"]
                else:
                    best_q["image_path"] += "," + img["path"]

        # ── Second pass: promote question-level images to per-option images ──
        for q in questions:
            opt_y: dict[str, float] = q.get("_opt_y", {})
            if not opt_y or not q.get("image_path"):
                continue

            letters_sorted = sorted(opt_y.keys())
            opt_ranges: dict[str, tuple[float, float]] = {}
            for i, letter in enumerate(letters_sorted):
                y_s = opt_y[letter]
                if i + 1 < len(letters_sorted):
                    y_e = opt_y[letters_sorted[i + 1]]
                else:
                    y_e = q.get("_y_end", y_s) + IMAGE_Y_TOLERANCE
                opt_ranges[letter] = (y_s, y_e)

            paths = q["image_path"].split(",")
            remaining: list[str] = []
            for path in paths:
                coords = path_to_coords.get(path)
                if coords is None:
                    remaining.append(path)
                    continue
                cy = (coords[0] + coords[1]) / 2.0
                matched_letter: str | None = None
                for letter, (y_s, y_e) in opt_ranges.items():
                    if y_s - 20 <= cy <= y_e:
                        matched_letter = letter
                        break
                if matched_letter:
                    opt_img_key = f"option_{matched_letter}_image"
                    if not q.get(opt_img_key):
                        q[opt_img_key] = path
                        continue
                remaining.append(path)

            q["image_path"] = ",".join(remaining) if remaining else None

        # ── Screenshot pass: crop each question's region as a PNG ───────────
        for idx, q in enumerate(questions):
            q_y_start = q.get("_y_start", 0.0)
            q_y_end   = q.get("_y_end",   0.0)

            # Clamp bottom edge to next question's start so it never appears
            # in this question's screenshot.
            if idx + 1 < len(questions):
                next_y_start = questions[idx + 1].get("_y_start", q_y_end)
                q_y_end = min(q_y_end, next_y_start)

            q["question_image"] = _crop_question_screenshot(
                page_meta, q_y_start, q_y_end, idx + 1
            )

        # ── Clean up internal metadata ────────────────────────────────────────
        for q in questions:
            q.pop("_num", None)
            q.pop("_y_start", None)
            q.pop("_y_end", None)
            q.pop("_opt_y", None)

    return questions


# ──────────────────────────────────────────────
# Pydantic request models
# ──────────────────────────────────────────────

class SignupRequest(BaseModel):
    username: str
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


class StartAttemptRequest(BaseModel):
    pdf_name: str
    total_questions: int
    duration: int = 60


class SaveAnswerRequest(BaseModel):
    question_id: int
    chosen_key: str


class SetAnswerRequest(BaseModel):
    correct_option: str


class ScoringConfigRequest(BaseModel):
    marks_correct: int
    marks_wrong: int


class SubmitAttemptRequest(BaseModel):
    """Payload sent by the frontend when the user submits a test.

    answers    – final answer state: {str(question_id): 'a'|'b'|'c'|'d'}
    time_spent – seconds spent per question: {str(question_id): int}

    The server ignores any client-side score and recalculates from scratch.
    """
    answers:    dict = {}   # {str(qid): key}
    time_spent: dict = {}   # {str(qid): seconds}


# ──────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────

# ── Static HTML pages ─────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/login", response_class=HTMLResponse)
async def serve_login():
    with open(os.path.join(STATIC_DIR, "login.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/signup", response_class=HTMLResponse)
async def serve_signup():
    with open(os.path.join(STATIC_DIR, "signup.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/test", response_class=HTMLResponse)
async def serve_test():
    with open(os.path.join(STATIC_DIR, "test.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/dashboard", response_class=HTMLResponse)
async def serve_dashboard():
    with open(os.path.join(STATIC_DIR, "dashboard.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/result", response_class=HTMLResponse)
async def serve_result():
    with open(os.path.join(STATIC_DIR, "result.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/exams", response_class=HTMLResponse)
async def serve_exams():
    with open(os.path.join(STATIC_DIR, "exams.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/reports", response_class=HTMLResponse)
async def serve_reports():
    with open(os.path.join(STATIC_DIR, "reports.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/settings", response_class=HTMLResponse)
async def serve_settings():
    with open(os.path.join(STATIC_DIR, "settings.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/privacy", response_class=HTMLResponse)
async def serve_privacy():
    with open(os.path.join(STATIC_DIR, "privacy.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/support", response_class=HTMLResponse)
async def serve_support():
    with open(os.path.join(STATIC_DIR, "support.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/terms", response_class=HTMLResponse)
async def serve_terms():
    with open(os.path.join(STATIC_DIR, "terms.html"), encoding="utf-8") as f:
        return f.read()


# ── Auth API ──────────────────────────────────

@app.post("/api/auth/signup")
async def api_signup(body: SignupRequest):
    if len(body.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")
    if db_get_user_by_email(body.email):
        raise HTTPException(status_code=409, detail="An account with this email already exists.")
    hashed = hash_password(body.password)
    user_id = db_create_user(body.email.lower().strip(), body.username.strip(), hashed)
    token = create_access_token(user_id, body.email.lower().strip())
    return {"token": token, "user_id": user_id, "username": body.username}


@app.post("/api/auth/login")
async def api_login(body: LoginRequest):
    user = db_get_user_by_email(body.email.lower().strip())
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    token = create_access_token(user["id"], user["email"])
    return {"token": token, "user_id": user["id"], "username": user["username"]}


@app.get("/api/auth/me")
async def api_me(current_user: dict = Depends(get_current_user)):
    user = db_get_user_by_id(current_user["user_id"])
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    return user


# ── Test attempt API ──────────────────────────

@app.post("/api/attempt/start")
async def api_start_attempt(
    body: StartAttemptRequest,
    current_user: dict = Depends(get_current_user),
):
    # Reuse an existing ongoing attempt for the same user + pdf to prevent
    # duplicates when the /test page is refreshed or revisited.
    with get_connection() as conn:
        existing = conn.execute(
            """SELECT id FROM test_attempts
               WHERE user_id = ? AND pdf_name = ? AND status = 'ongoing'
               ORDER BY id DESC LIMIT 1""",
            (current_user["user_id"], body.pdf_name),
        ).fetchone()
    if existing:
        return {"attempt_id": existing["id"]}

    attempt_id = db_create_attempt(
        user_id=current_user["user_id"],
        pdf_name=body.pdf_name,
        total_questions=body.total_questions,
        duration=body.duration,
    )
    return {"attempt_id": attempt_id}


@app.post("/api/attempt/{attempt_id}/answer")
async def api_save_answer(
    attempt_id: int,
    body: SaveAnswerRequest,
    current_user: dict = Depends(get_current_user),
):
    attempt = db_get_attempt(attempt_id)
    if not attempt:
        raise HTTPException(status_code=404, detail="Attempt not found.")
    if attempt["user_id"] != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="Forbidden.")
    if attempt["status"] != "ongoing":
        raise HTTPException(status_code=400, detail="Attempt is already completed.")
    if body.chosen_key not in ("a", "b", "c", "d"):
        raise HTTPException(status_code=400, detail="chosen_key must be a, b, c, or d.")
    db_upsert_answer(attempt_id, body.question_id, body.chosen_key)
    return {"ok": True}


@app.post("/api/attempt/{attempt_id}/submit")
async def api_submit_attempt(
    attempt_id: int,
    body: SubmitAttemptRequest,
    current_user: dict = Depends(get_current_user),
):
    """Submit a test attempt and return a structured scoring summary.

    Flow:
      1. Validate ownership and that attempt is still ongoing.
      2. Bulk-upsert all answers supplied by the frontend (overwrites interim saves).
      3. Calculate score server-side — client-supplied score is never trusted.
      4. Save per-question time spent.
      5. Lock the attempt (status → 'completed').
      6. Return structured JSON: score / correct / wrong / unanswered / times.

    If the attempt is already completed, return the stored summary immediately
    without recalculating.
    """
    attempt = db_get_attempt(attempt_id)
    if not attempt:
        raise HTTPException(status_code=404, detail="Attempt not found.")
    if attempt["user_id"] != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="Forbidden.")

    if attempt["status"] == "completed":
        # Return previously calculated and locked results.
        return {
            "score":             attempt["score"],
            "correct":           attempt.get("correct",    0),
            "wrong":             attempt.get("wrong",      0),
            "unanswered":        attempt.get("unanswered", 0),
            "total_time":        attempt.get("total_time", 0),
            "per_question_time": db_get_time_spent(attempt_id),
        }

    # ── 1. Persist final answer state ────────────────────────────────────────
    # Overwrite any interim answers with the definitive frontend state.
    valid_keys = {"a", "b", "c", "d"}
    for qid_str, key in body.answers.items():
        if key not in valid_keys:
            continue
        try:
            db_upsert_answer(attempt_id, int(qid_str), key)
        except (ValueError, Exception):
            pass  # skip malformed question_ids

    # ── 2. Score server-side (never trust client score) ──────────────────────
    result = calculate_score_detailed(attempt_id)

    # ── 3. Time accounting ───────────────────────────────────────────────────
    total_time = int(sum(
        v for v in body.time_spent.values()
        if isinstance(v, (int, float)) and v >= 0
    ))
    if body.time_spent:
        db_save_time_spent(attempt_id, body.time_spent)

    # ── 4. Lock attempt with full summary ────────────────────────────────────
    db_complete_attempt(
        attempt_id,
        score      = result["score"],
        correct    = result["correct"],
        wrong      = result["wrong"],
        unanswered = result["unanswered"],
        total_time = total_time,
    )

    return {
        "score":             result["score"],
        "correct":           result["correct"],
        "wrong":             result["wrong"],
        "unanswered":        result["unanswered"],
        "total_time":        total_time,
        "per_question_time": db_get_time_spent(attempt_id),
    }


@app.get("/api/attempt/{attempt_id}")
async def api_get_attempt(
    attempt_id: int,
    current_user: dict = Depends(get_current_user),
):
    attempt = db_get_attempt(attempt_id)
    if not attempt:
        raise HTTPException(status_code=404, detail="Attempt not found.")
    if attempt["user_id"] != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="Forbidden.")

    time_spent = db_get_time_spent(attempt_id)

    with get_connection() as conn:
        # User answers with correct options for each answered question
        answer_rows = conn.execute(
            """SELECT ua.question_id, ua.chosen_key, q.correct_option
               FROM   user_answers ua
               JOIN   questions    q  ON q.id = ua.question_id
               WHERE  ua.attempt_id = ?""",
            (attempt_id,),
        ).fetchall()

        # All questions in display order (id == display order after init_db)
        question_rows = conn.execute(
            "SELECT id, correct_option FROM questions ORDER BY id"
        ).fetchall()

    # Build O(1) lookup: question_id → answered row
    answer_map = {r["question_id"]: r for r in answer_rows}

    # One breakdown entry per question
    breakdown = []
    for num, q in enumerate(question_rows, start=1):
        qid    = q["id"]
        ans    = answer_map.get(qid)
        chosen  = ans["chosen_key"]    if ans else None
        correct = q["correct_option"]

        if chosen is None:
            status = "unanswered"
        elif chosen == correct:
            status = "correct"
        else:
            status = "wrong"

        breakdown.append({
            "question_num":   num,
            "question_id":    qid,
            "chosen_key":     chosen,
            "correct_option": correct,
            "time_seconds":   time_spent.get(str(qid), 0),
            "status":         status,
        })

    return {
        **dict(attempt),
        "per_question_time": time_spent,
        "breakdown":         breakdown,
    }


@app.delete("/api/attempt/{attempt_id}")
async def api_delete_attempt(
    attempt_id: int,
    current_user: dict = Depends(get_current_user),
):
    """Delete a test attempt and all associated answer/time data."""
    attempt = db_get_attempt(attempt_id)
    if not attempt:
        raise HTTPException(status_code=404, detail="Attempt not found.")
    if attempt["user_id"] != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="Forbidden.")
    with get_connection() as conn:
        conn.execute("DELETE FROM question_time_spent WHERE attempt_id = ?", (attempt_id,))
        conn.execute("DELETE FROM user_answers WHERE attempt_id = ?", (attempt_id,))
        conn.execute("DELETE FROM test_attempts WHERE id = ?", (attempt_id,))
        conn.commit()
    return {"ok": True}


@app.delete("/api/attempts/all")
async def api_delete_all_attempts(
    current_user: dict = Depends(get_current_user),
):
    """Delete ALL test attempts for the current user."""
    with get_connection() as conn:
        attempt_ids = [
            r["id"] for r in conn.execute(
                "SELECT id FROM test_attempts WHERE user_id = ?",
                (current_user["user_id"],),
            ).fetchall()
        ]
        for aid in attempt_ids:
            conn.execute("DELETE FROM question_time_spent WHERE attempt_id = ?", (aid,))
            conn.execute("DELETE FROM user_answers WHERE attempt_id = ?", (aid,))
        conn.execute(
            "DELETE FROM test_attempts WHERE user_id = ?",
            (current_user["user_id"],),
        )
        conn.commit()
    return {"ok": True, "deleted": len(attempt_ids)}


@app.get("/api/attempts")
async def api_get_attempts(current_user: dict = Depends(get_current_user)):
    attempts = db_get_user_attempts(current_user["user_id"])
    return attempts


# ── PDF upload ────────────────────────────────

@app.post("/upload")
async def upload_pdf(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted.")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        questions = parse_with_diagram_info(tmp_path)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"PDF read error: {exc}") from exc
    finally:
        os.unlink(tmp_path)

    if not questions:
        raise HTTPException(
            status_code=422,
            detail=(
                "No MCQ questions detected. "
                "Ensure the PDF contains standard question numbering "
                "(1. / Q1 / Q.1 / Question 1 / Que 1 / I. II. III. …) "
                "and option labels (A) B) C) D) or (A) a. • etc)."
            ),
        )

    init_db()
    insert_questions(questions)

    return JSONResponse({"count": len(questions), "redirect": "/test"})


@app.get("/api/questions")
async def get_all_questions():
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, question, option_a, option_b, option_c, option_d, "
            "option_a_image, option_b_image, option_c_image, option_d_image, "
            "has_diagram, image_path, question_image FROM questions ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


# ──────────────────────────────────────────────
# Admin helpers and routes
# ──────────────────────────────────────────────

def require_admin(current_user: dict = Depends(get_current_user)) -> dict:
    """FastAPI dependency: raises HTTP 403 unless the user has is_admin = 1."""
    user = db_get_user_by_id(current_user["user_id"])
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required.")
    return user


@app.post("/api/admin/question/{question_id}/answer")
async def api_set_question_answer(
    question_id: int,
    body: SetAnswerRequest,
    admin: dict = Depends(require_admin),
):
    """Admin only: set the correct answer for a question."""
    if body.correct_option not in ("a", "b", "c", "d"):
        raise HTTPException(
            status_code=400, detail="correct_option must be one of: a, b, c, d."
        )
    found = db_set_question_answer(question_id, body.correct_option)
    if not found:
        raise HTTPException(status_code=404, detail="Question not found.")
    return {"ok": True, "question_id": question_id, "correct_option": body.correct_option}


@app.get("/api/admin/config")
async def api_get_scoring_config(admin: dict = Depends(require_admin)):
    """Admin only: retrieve current scoring configuration."""
    return db_get_scoring_config()


@app.post("/api/admin/config")
async def api_update_scoring_config(
    body: ScoringConfigRequest,
    admin: dict = Depends(require_admin),
):
    """Admin only: update marks_correct and marks_wrong."""
    db_update_scoring_config(body.marks_correct, body.marks_wrong)
    return {"ok": True, **db_get_scoring_config()}


# ──────────────────────────────────────────────
# Local dev entry-point
# ──────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
