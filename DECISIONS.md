# DECISIONS.md

1. **Indexes.** The unique `(emp_code, date)` index guarantees one attendance record per employee per day. The unique
`(emp_code)` index guarantees employee identity. `(date, emp_code)` supports attendance date ordering and tie-breaking,
while `(department)` supports employee filtering and department analytics. I considered an index on `(status)`, but
rejected it because status has low cardinality and would provide little selectivity.

2. **Punch-in race.** Concurrent requests may both verify that the employee exists, but neither relies on that check to
prevent duplicates. They both attempt the insert; MongoDB serializes the unique compound key. One succeeds, while the
other receives `DuplicateKeyError` (`E11000`), which the endpoint converts to HTTP 409.

3. **Ties.** The leaderboard sorts by total late minutes descending, then `emp_code` ascending. This deterministic
secondary key makes tied results stable, preventing test flakiness and pagination duplicates.

4. **Headcount.** The department aggregation starts with employees and `$lookup`s attendance logs. Employees with zero
logs are preserved, so they still count in department headcount.

5. **100x data.** I would shard `attendance_logs` on `(emp_code, date)` and pre-aggregate daily metrics using background
workers or change streams.
