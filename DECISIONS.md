# DECISIONS.md

1. **Indexes:** `attendance_logs(emp_code, date)` is unique, enforcing one attendance record per employee per calendar date. `employees(emp_code)` is unique, enforcing stable business identifiers. The performance index `attendance_logs(date, emp_code)` supports date ordering and employee tie-breaking; `employees(department)` supports department filters and analytics. An index on `status` was considered but rejected because status has low cardinality and poor selectivity.

2. **Punch-in race:** Concurrent requests may both validate the employee, but the unique compound index serializes conflicting inserts. One insert succeeds; the other raises `DuplicateKeyError` (`E11000`). The endpoint catches that database error and returns HTTP 409, making the uniqueness guarantee atomic rather than dependent on a check-then-insert sequence.

3. **Ties:** The leaderboard sorts by `total_late_minutes: -1` and then `emp_code: 1`. This deterministic secondary key makes tied results stable, preventing test flakiness and pagination duplicates.

4. **Headcount:** The department pipeline starts from `employees` and uses `$lookup` into `attendance_logs`. Preserving unmatched employees before grouping ensures employees with zero logs remain included in department totals.

5. **100x data:** I would shard `attendance_logs` on compound key `(emp_code, date)` and move heavy analytics to pre-aggregated daily rollups updated through MongoDB Change Streams.
