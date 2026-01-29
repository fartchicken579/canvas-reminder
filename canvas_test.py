# canvas_test.py
# Compact, optimized version (same behavior, smaller footprint)

import sys
import unittest
import requests
from dateutil.parser import isoparse

# ========= CONFIG =========
# Secrets must come from environment variables (safe for public GitHub repos).
import os

CANVAS_BASE = os.getenv("CANVAS_BASE")
CANVAS_TOKEN = os.getenv("CANVAS_TOKEN")
DEFAULT_DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

if not CANVAS_BASE or not CANVAS_TOKEN:
    raise RuntimeError(
        "Missing configuration. Set CANVAS_BASE and CANVAS_TOKEN as environment variables. "
        "For local use, export them in your shell or use a .env loader."
    )
# ===========================

HEADERS = {"Authorization": f"Bearer {CANVAS_TOKEN}"}


# -----------------------------
# Basic HTTP helpers
# -----------------------------

def request_json(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        return r.json(), r
    except Exception as e:
        raise RuntimeError(f"Request failed: {url} | {e}")


def get_all_pages(url):
    out = []
    while url:
        data, r = request_json(url)
        out.extend(data if isinstance(data, list) else [data])
        url = r.links.get("next", {}).get("url")
    return out


def get_courses():
    return get_all_pages(f"{CANVAS_BASE}/api/v1/users/self/favorites/courses?per_page=100")


# -----------------------------
# Grading period helpers
# -----------------------------

def _is_q3_q4(title):
    t = (title or "").upper()
    return any(k in t for k in ("Q3", "Q4", "QUARTER 3", "QUARTER 4", "3RD QUARTER", "4TH QUARTER"))


def _get_q3_q4_periods(course_id):
    payload, _ = request_json(f"{CANVAS_BASE}/api/v1/courses/{course_id}/grading_periods")
    periods = payload.get("grading_periods", payload) if isinstance(payload, dict) else payload
    return [p for p in periods if _is_q3_q4(p.get("title"))]


def get_q3_q4_period_ids(course_id):
    return [p["id"] for p in _get_q3_q4_periods(course_id) if "id" in p]


def get_q3_q4_date_ranges(course_id):
    out = []
    for p in _get_q3_q4_periods(course_id):
        s, e = p.get("start_date"), p.get("end_date")
        if s and e:
            try:
                out.append((isoparse(s), isoparse(e)))
            except Exception:
                pass
    return out


# -----------------------------
# Core filtering logic
# -----------------------------

def should_include_assignment(a, period_ids, ranges, low_score_threshold_percent=50.0):
    sub = a.get("submission") or {}
    missing = bool(sub.get("missing"))
    score = sub.get("score")
    points = a.get("points_possible")

    # Does it belong to Q3/Q4?
    gp = a.get("grading_period_id")
    belongs = gp in period_ids if gp is not None else False

    if not belongs and ranges:
        due = a.get("due_at")
        if due:
            try:
                d = isoparse(due)
                belongs = any(s <= d <= e for s, e in ranges)
            except Exception:
                pass

    if not belongs:
        return False

    if missing:
        return True

    if score is not None and points:
        try:
            return (score / points) * 100 <= low_score_threshold_percent
        except Exception:
            return False

    return False


def get_assignments(course_id):
    pids = set(get_q3_q4_period_ids(course_id))
    ranges = get_q3_q4_date_ranges(course_id)

    if not pids and not ranges:
        return []

    url = f"{CANVAS_BASE}/api/v1/courses/{course_id}/assignments?include[]=submission&per_page=100"
    return [a for a in get_all_pages(url) if should_include_assignment(a, pids, ranges)]


# -----------------------------
# Structured output (list-based)
# -----------------------------

import json


def _assignment_to_record(course, a):
    sub = a.get("submission") or {}
    name = a.get("name", "Unnamed Assignment")
    score, pts = sub.get("score"), a.get("points_possible")

    percent = None
    if score is not None and pts:
        try:
            percent = round((score / pts) * 100, 2)
        except Exception:
            pass

    if sub.get("missing"):
        status = "missing"
    elif sub.get("late"):
        status = "late"
    else:
        status = "low_score"

    return {
        "course_id": course.get("id"),
        "course_name": course.get("name", "Unnamed Course"),
        "assignment_id": a.get("id"),
        "assignment_name": name,
        "status": status,
        "score": score,
        "points_possible": pts,
        "percent": percent,
        "due_at": a.get("due_at"),
        "url": a.get("html_url"),
    }


def collect_results():
    """
    Returns a list of dictionaries, each representing an assignment record.
    This is the function you would call from a messaging / notification system.
    """
    results = []

    courses = get_courses()

    for c in courses:
        try:
            assignments = get_assignments(c["id"])
        except RuntimeError:
            continue

        for a in assignments:
            results.append(_assignment_to_record(c, a))

    return results


# -----------------------------
# Discord webhook integration
# -----------------------------

def _format_percent(record):
    percent = record.get("percent")
    if percent is None:
        return "N/A"
    return f"{percent:.2f}%"


def _format_assignment_line(record):
    status = record.get("status", "unknown").replace("_", " ")
    course = record.get("course_name", "Unnamed Course")
    name = record.get("assignment_name", "Unnamed Assignment")
    percent = _format_percent(record)
    url = record.get("url")

    line = f"**{course}** — {name} ({status}, {percent})"
    if url:
        line += f" <{url}>"
    return line


def _chunk_lines(lines, max_len=1900):
    chunks = []
    current = []
    current_len = 0

    for line in lines:
        line_len = len(line) + 1
        if current and current_len + line_len > max_len:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += line_len

    if current:
        chunks.append("\n".join(current))

    return chunks


def send_discord_notifications(records, webhook_url=None):
    webhook_url = webhook_url or os.getenv("DISCORD_WEBHOOK_URL") or DEFAULT_DISCORD_WEBHOOK_URL
    if not webhook_url:
        print("Discord webhook not configured; set DISCORD_WEBHOOK_URL to enable notifications.")
        return

    if not records:
        payloads = [{"content": "✅ No missing or below 50% assignments found."}]
    else:
        lines = [_format_assignment_line(r) for r in records]
        payloads = [{"content": chunk} for chunk in _chunk_lines(lines)]

    for payload in payloads:
        try:
            r = requests.post(webhook_url, json=payload, timeout=15)
            r.raise_for_status()
        except Exception as e:
            raise RuntimeError(f"Discord webhook failed: {e}")


# -----------------------------
# CLI (still works, but now prints JSON list)
# -----------------------------

def main():
    try:
        data = collect_results()
    except RuntimeError as e:
        print("ERROR:", e)
        return 1

    # Pretty JSON output for easy piping to other tools
    print(json.dumps(data, indent=2))
    send_discord_notifications(data)
    return 0


# -----------------------------
# Tests (unchanged, plus one extra)
# -----------------------------

class TestFiltering(unittest.TestCase):
    def setUp(self):
        self.period_ids = {1}
        self.ranges = [(
            isoparse("2025-01-01T00:00:00Z"),
            isoparse("2025-03-31T23:59:59Z"),
        )]

    def test_missing_assignment_in_period(self):
        a = {"grading_period_id": 1, "submission": {"missing": True}, "points_possible": 10}
        self.assertTrue(should_include_assignment(a, self.period_ids, self.ranges))

    def test_low_score_in_period(self):
        a = {"grading_period_id": 1, "submission": {"missing": False, "score": 4}, "points_possible": 10}
        self.assertTrue(should_include_assignment(a, self.period_ids, self.ranges))

    def test_good_score_excluded(self):
        a = {"grading_period_id": 1, "submission": {"missing": False, "score": 9}, "points_possible": 10}
        self.assertFalse(should_include_assignment(a, self.period_ids, self.ranges))

    def test_wrong_period_excluded(self):
        a = {"grading_period_id": 2, "submission": {"missing": True}, "points_possible": 10}
        self.assertFalse(should_include_assignment(a, self.period_ids, self.ranges))

    def test_due_date_fallback_in_range_missing(self):
        a = {"grading_period_id": None, "due_at": "2025-02-15T12:00:00Z", "submission": {"missing": True}, "points_possible": 10}
        self.assertTrue(should_include_assignment(a, self.period_ids, self.ranges))

    def test_due_date_fallback_out_of_range_excluded(self):
        a = {"grading_period_id": None, "due_at": "2024-10-15T12:00:00Z", "submission": {"missing": True}, "points_possible": 10}
        self.assertFalse(should_include_assignment(a, self.period_ids, self.ranges))

    def test_ungraded_not_missing_excluded(self):
        a = {"grading_period_id": 1, "submission": {"missing": False, "score": None}, "points_possible": 10}
        self.assertFalse(should_include_assignment(a, self.period_ids, self.ranges))

    def test_points_missing_excluded(self):
        a = {"grading_period_id": 1, "submission": {"missing": False, "score": 0}}
        self.assertFalse(should_include_assignment(a, self.period_ids, self.ranges))

    # New test (edge case): due date in range but not missing and >50% should be excluded
    def test_due_date_in_range_but_good_score_excluded(self):
        a = {
            "grading_period_id": None,
            "due_at": "2025-02-20T12:00:00Z",
            "submission": {"missing": False, "score": 9},
            "points_possible": 10,
        }
        self.assertFalse(should_include_assignment(a, self.period_ids, self.ranges))

    def test_format_assignment_line(self):
        record = {
            "course_name": "Biology",
            "assignment_name": "Lab Report",
            "status": "missing",
            "percent": None,
            "url": "https://example.test/assignment",
        }
        line = _format_assignment_line(record)
        self.assertIn("Biology", line)
        self.assertIn("Lab Report", line)
        self.assertIn("missing", line)
        self.assertIn("N/A", line)
        self.assertIn("https://example.test/assignment", line)


if __name__ == "__main__":
    raise SystemExit(main())
