# DECISIONS.md

1. **Indexes:** We created a unique compound index on `attendance_logs(emp_code, date)` to enforce one daily record per employee, plus a unique `employees(emp_code)` index for business IDs. We also indexed `attendance_logs(date, emp_code)` for pagination and `employees(department)` for lookups and analytics. An index on `status` alone was considered and rejected because status has low cardinality.

2. **Punch-in race:** Concurrent requests may both evaluate business rules, but MongoDB serializes their inserts through the unique compound index. The first succeeds; the second raises `DuplicateKeyError` (E11000), which our handler catches and returns as HTTP 409 Conflict.

3. **Ties:** The leaderboard uses deterministic sorting: `{"total_late_minutes": -1, "emp_code": 1}`. The ascending employee code tie-breaker ensures stable results and prevents pagination drift or test flakiness.

4. **Headcount:** The department summary starts with `employees` and uses `$lookup` into `attendance_logs`. Preserving unmatched employees before grouping ensures people with zero logs remain in department headcount.

5. **100x data:** I would shard `attendance_logs` on a hashed compound key `(emp_code, date)` and move analytics to pre-computed daily rollups updated by MongoDB Change Streams, instead of aggregating raw collections on demand.
