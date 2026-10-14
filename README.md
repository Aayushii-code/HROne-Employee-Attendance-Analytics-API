# Employee Attendance & Analytics API (HROne SDT candidate submission)

All application code lives in `app/main.py` (single-file requirement). It implements the assignment's API
contract and business rules **R1–R10**: IST-aware punch-in/out with overnight shifts, late/OT/half-day
derivations (half-up), an audit-trailed regularization `PATCH`, employee/monthly analytics, department
summary and late-leaderboard (competition ranking), a gap-filled daily attendance rate trend with a
7-day moving average, and an `/admin/explain` endpoint that explains each endpoint's exact query.

## Run

```bash
python -m venv .venv && .venv\Scripts\activate     # (Unices: source .venv/bin/activate)
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

**MongoDB 6.0+ is required** (the assignment's stated stack; local testing used MongoDB 7.0.14). The app
reads `MONGO_URI` (default `mongodb://localhost:27017`) and `MONGO_DB` (default `attendance_db`) from the
environment or a local `.env` file. Real environment variables win. No `.env`, secrets, Dockerfile, or venv
are shipped.

## Files

- `app/main.py` — all application code (models, helpers, endpoints, aggregations, indexes, `explain`).
- `app/__init__.py` — empty package marker.
- `requirements.txt` — runtime dependencies (`pip install -r requirements.txt`).
- `README.md` — this file.
- `REVIEW.md` — every defect found in the starter and how it was fixed.
- `DECISIONS.md` — answers to the five required decision questions.
- `.gitignore` — excludes caches, virtual environments, secrets and local development files.

## Verification summary

These checks were run locally during development and are **not included in the submitted repository**, so a
reviewer cannot reproduce them from the submission alone.

- 103 API tests (status codes, boundaries, IST/overnight, derivations, pagination, PATCH history, `explain`).
- 45 regression tests for the follow-up fixes: a department with employees but zero logs still yields one row per calendar day, punch-out closes the most recent record with `punch_in <= punched_at`, and `emp_code` is validated against the contract pattern `^EMP[0-9]{4,6}$`.
- 17 concurrency tests on a live server: punch-in/punch-out/PATCH races yield exactly one winner, including a punch-out race that must close the earlier eligible record.
- 100k-record dataset: every `/admin/explain/*` plan uses an index scan (`IXSCAN`/`ixseek`) with no `COLLSCAN`.
- Contract spot-check: all paths/methods in the assignment contract exist and request-body required fields match.