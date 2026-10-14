# DECISIONS.md

1. **Indexes.** `employees`: `uniq_emp_code` (unique, race-free creates), `dept`, `joined_on`, `dept_joined_on` (R9 headcount). `attendance_logs`: `uniq_emp_code_date` (unique — one record/day, race-safe punch-in) and `date_desc_emp_code` (the `date`-range path). A standalone `status` index was rejected: low selectivity (five values) only adds write cost. Every `explain` shows `IXSCAN`, never `COLLSCAN`.

2. **Punch-in race.** Both requests build the same document and call `insert_one`. The unique `(emp_code, date)` index lets only one succeed; the loser raises `DuplicateKeyError` → 409. No check-then-insert window: concurrent duplicates give one 201 and the rest 409.

3. **Ties.** `$setWindowFields` with `$rank` gives competition ranking (1, 2, 2, 4), ties ordered by `emp_code`. `limit` applies *after* ranking, so a tie at the cutoff returns **all** tied employees (`limit=1` can return both tied rows).

4. **Headcount.** Stages: `$match` → `$lookup` → `$project` → `$group` — match employees joined by month end, look up their month logs, project per-employee roll-ups, group by department (`headcount = $sum 1`). Zero-log employees still count, so a department appears whenever it has a member.

5. **100x the data.** Stop aggregating raw logs per request: keep per-day pre-aggregated rolls (per employee present/late/overtime; per-day per-department headcount), synced by change-stream listeners. The trend already builds its calendar server-side (`$range`/`$dateAdd`).
