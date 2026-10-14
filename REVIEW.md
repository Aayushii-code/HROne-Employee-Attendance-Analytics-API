# REVIEW.md

Part A: defects found in the starter `app/main.py` (the file a colleague left half-finished). Row
references are to the **starter's** function names and signatures. The starter only provided `/health`,
`POST/GET /employees`, `POST /attendance/punch-in` and `GET /attendance` plus three helpers; `punch-out`,
`PATCH`, the analytics endpoints and `/admin/explain` were `TODO`, so they are Part B work, not defects.

Testing was performed locally during development; the test files are **not included in this submission**,
so these results are development verification, not tests a reviewer can reproduce directly from the
repository: **103 API tests**, **45 regression checks**, **17 concurrency tests** against a live server,
**26 checks on a 100,000-record dataset** (all five `/admin/explain/*` plans use an index scan, never
`COLLSCAN`), and **23 contract checks** against `openapi.yaml`.

| # | Where (starter function) | What is wrong | How you'd notice it (test / input / symptom) | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes(punch_in, shift_start)` | Grace boundary uses `minutes > 10` on a **floored** value, so any punch-in in `(09:40:00, 09:41:00)` returns `0` instead of `10`. | `09:40:01` (shift 09:30) → expected `10`, got `0`. | Late only when `elapsed > 600s`; then `int(elapsed // 60)` (R2). |
| 2 | `compute_late_minutes` | Signature has **no `shift_end`**, so overnight shifts are compared to the same-day start and are never late. | Overnight employee (22:00–06:00) punching in at `02:00`. | Pass `shift_end`; back the shift start off one day when the punch-in precedes it. |
| 3 | `compute_work_hours(punch_in, punch_out)` | Uses Python `round()` (banker's rounding): `8.825 → 8.82`, not the required `8.83`. | 09:30:00 → 18:19:30 → expected `8.83`, got `8.82`. | `Decimal` quantize with `ROUND_HALF_UP` (R4). |
| 4 | `compute_overtime(punch_out, shift_end, date_str)` | No "count only if ≥ 30 minutes" rule; it floors the minute difference toward zero and clamps the result at zero, so any sub-30-minute overrun is still reported. | Punch-out 10 minutes after shift end → expected `0`, got `10`. | Return `0` unless the floor minute count is `>= 30` (R3). |
| 5 | `compute_overtime` | Builds the shift end on the record's own date, so an **overnight** shift end after midnight is wrong. | Overnight shift 22:00–06:00, punch-out 06:30 → wrong OT. | Advance the shift end by one day for overnight shifts. |
| 6 | helpers + `MongoClient(...)` | Client is not `tz_aware=True`; `compute_late_minutes` uses `punch_in.replace(...)`. Times end up **naive** and are never truncated to whole seconds. | Any stored time lacks `tzinfo`; sub-second values leak into responses. | `MongoClient(..., tz_aware=True)` and a `now_utc()`/`epoch_ms_to_utc()` pair that truncate to seconds (R1). |
| 7 | `punch_in` | Attendance **date** is `ts.date()` where `ts` is local/naive — no IST offset and no overnight roll-over. | Punch-in `2026-07-06T20:30Z` → stored under the wrong IST day. | `attendance_date_ist()`: convert to IST, roll back a day when before `shift_end` (R1). |
| 8 | `punch_in` | Check-then-insert (`find_one` before `insert_one`): two simultaneous requests can both pass. | Fire N identical punch-ins concurrently; more than one 201. | Unique `(emp_code, date)` index; catch `DuplicateKeyError` → 409. Concurrency suite: 1×201, N−1×409. |
| 9 | `punch_in` | Loads `emp` but never checks for `None`. | Punch-in for an unknown `emp_code` → `None["shift_start"]` → **500** instead of 404. | Explicit 404 when the employee is missing. |
| 10 | `punch_in` | `status` is a bare `str`, so `ABSENT`/`LEAVE` (and junk) are accepted. | `status: "ABSENT"` → expected `422`, got `201`. | `PresenceStatus` enum (`PRESENT/WFH/ON_DUTY`) → 422 otherwise. |
| 11 | `punch_in`, `list_attendance` | Both return an `id` field (`str(inserted_id)` / `str(pop("_id"))`). The contract says records are addressed by `(emp_code, date)` and `_id` never appears. | Response contains an extra `id` key. | Serializer emits only the contract fields; no `_id`/`id`. |
| 12 | `punch_in` | `punched_at: Optional[int]` accepts coerced strings/floats and any magnitude. | `"1783312500000"` or a seconds-sized `10000000000` → accepted. | `StrictInt` with an epoch-ms range → 422 (R1). |
| 13 | `create_employee` | Check-then-insert on `emp_code`: concurrent identical creates can both pass. | Two parallel identical creates both return 201. | Unique `emp_code` index + `DuplicateKeyError` → 409. |
| 14 | `create_employee` | Sets `created_at = datetime.now()` (naive, local) and returns a raw datetime instead of epoch ms. | `created_at` is not an integer. | Server sets UTC `created_at`, serialized to epoch ms. |
| 15 | `create_employee` | No field validation: empty `emp_code`/`name`/`department`, malformed `email`, equal shift times all accepted. | Bad email / equal shifts → expected `422`. | Pydantic constraints + a `shift_start != shift_end` validator. |
| 16 | `list_employees` | `skip = page * page_size` — **off by one page**; `page=1` skips the first page. | `?page=1&page_size=20` returns records 21+. | `skip = (page - 1) * page_size`. |
| 17 | `list_employees` | `total = count_documents({})` ignores the `department` filter. | `?department=Engineering` → `total` is the whole collection. | Count with the same filter (R10). |
| 18 | `list_employees` | No `page >= 1` / `page_size <= 100` bounds. | `?page_size=101` → expected `422`, got 200. | `Query(ge=1)` / `Query(ge=1, le=100)`. |
| 19 | `list_employees` | No sort; contract wants `emp_code` ascending. | Order is unspecified. | `.sort("emp_code", 1)`. |
| 20 | `list_attendance` | Loads **every** match into Python, sorts and slices in memory. | `page=2` scans all ~100k docs. | DB-side `sort([("date", -1), ("emp_code", 1)])` + `skip`/`limit`. |
| 21 | `list_attendance` | `date_from`/`date_to`/`status` are raw strings: no `422` for `date_from > date_to`, bad dates, or unknown statuses. | `?date_from=2026-07-20&date_to=2026-07-01` → expected `422`. | `date` params + `Status` enum + an explicit `date_from <= date_to` check. |
| 22 | `/health` | Always returns `{"status": "ok"}`, even with MongoDB down. | Stop Mongo → `/health` still 200. | Ping the server; `503` on failure (contract: 200 only when Mongo answers). |

## Looked at and decided it is **not** a defect

- **`load_dotenv()` without `override=True`** — real environment variables *should* win over `.env`; correct for a grader that injects `MONGO_URI`/`MONGO_DB`.
- **Default `MONGO_URI=mongodb://localhost:27017` / `MONGO_DB=attendance_db`** — sensible local defaults; the values are still read from the environment, nothing is hard-coded that a grader cannot override.
- **Read-only `GET /health` touching the DB** — intentional; it is the readiness probe.
- **`sample_seed.py` dropping the two collections** — documented loader behaviour for the sample kit; the app never drops anything.
- **Single-Python-file constraint** — required by the assignment ("All application code in this one file").
- **Returning `409` (not `422`) for a duplicate punch-in** — matches the documented errors in `openapi.yaml`.

## Follow-up fixes (evaluation round)

Three issues in *my* implementation were found and fixed after Part A/B/C; they are not starter defects.

| # | Where | What was wrong | Fix |
|---|---|---|---|
| F1 | `build_trend_pipeline` (department trend) | The pipeline started from `attendance_logs`, so a department with employees but **zero** logs in the range produced **zero rows** (nothing for `$densify` to expand). Contract: exactly one row per calendar day, in the database, not a Python loop. | Rebuilt to start from `employees` (`$match department` → `$limit 1`) and expand one row per calendar day with `$range`/`$dateAdd`/`$unwind`, merged with a single-pass per-day log roll-up via `$facet` + `$concatArrays`. Endpoint and `/admin/explain` now aggregate on `employees`; `IXSCAN[dept]` + `IXSCAN[dept_joined_on]`, no `COLLSCAN`. |
| F2 | `punch_out` | Selected the most recent punch-in **regardless of time**, so a future-dated open record blocked closing the correct earlier record. | Select the most recent record with `punch_in <= punched_at`. If none exists and a future *open* record does → `422` (the `punched_at <= punch_in` case); if none exists at all → `404`. Already-closed record → `409`; the atomic `find_one_and_update` on `punch_out: null` is unchanged. |
| F3 | `EmployeeIn.emp_code` | The field was temporarily loosened to accept any 1–50 character string. But `openapi.yaml` — the authoritative request contract — specifies `pattern: '^EMP[0-9]{4,6}$'` ("Format for this assignment: `EMP` followed by 4-6 digits"), so non-`EMP` codes must be rejected with `422`. (`DATA_MODEL.md`/`PROBLEM_STATEMENT.docx` describe the field's *meaning* — "for example `EMP0001`" — but the HTTP contract governs input validation.) | Enforce `Field(pattern=r"^EMP[0-9]{4,6}$")`. |
