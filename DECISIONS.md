# DECISIONS.md

1. **Indexes:**
We created a unique compound index on `attendance_logs(emp_code, date)` to enforce one daily record per employee, and a unique index on `employees(emp_code)` to guarantee unique business IDs. We also indexed `attendance_logs(date, emp_code)` and `employees(department)` for fast pagination and aggregation lookups. An index on `status` alone was considered and rejected due to low cardinality.

2. **Punch-in race:**
When two identical punch-in requests arrive simultaneously, both evaluate business rules, but MongoDB's atomic index engine serializes the writes. The first insert succeeds; the second violates the unique compound index `(emp_code, date)` and throws a `DuplicateKeyError` (E11000), which our handler catches to return a clean HTTP 409 Conflict.

3. **Ties:**
When employees tie on late minutes at the limit threshold, our pipeline uses deterministic secondary sorting: `{"total_late_minutes": -1, "emp_code": 1}`. Sorting by `emp_code` ascending ensures consistent, deterministic results and prevents pagination drift.

4. **Headcount:**
The department summary pipeline initiates from the `employees` collection and performs a `$lookup` into `attendance_logs`. Grouping by department after preserving unmatched employees ensures employees with zero logs are accurately counted in total headcount.

5. **One thing you would change for 100x data:**
Shard `attendance_logs` on a hashed compound key `(emp_code, date)` and offload analytics queries to pre-computed daily rollups updated via MongoDB Change Streams rather than running on-demand aggregation pipelines over raw collections.
