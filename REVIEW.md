# REVIEW.md

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `list_employees` | Offset pagination skipped the first page (`skip = page * page_size`), and `total` counted all docs unconditionally. | Requesting `page=1` returned records starting from index 20. Total count ignored department filters. | Changed pagination offset to `(page - 1) * page_size` and updated count to `db.employees.count_documents(q)`. |
| 2 | `punch_in` | Lacked an employee existence check and suffered from a concurrent insert race condition. | Punching in with a non-existent `emp_code` raised an unhandled 500 error. Concurrent requests caused duplicate records. | Added a 404 validation check for missing employees, created a unique compound index on `(emp_code, date)`, and handled `DuplicateKeyError` with HTTP 409. |
| 3 | `list_attendance` | Loaded all matched documents into Python memory via `list(...)` and sorted them via Python `list.sort()`. | Severe memory consumption and slow response times under large datasets. | Migrated query sorting and pagination natively into MongoDB with `.sort("date", -1).skip(skip).limit(page_size)`. |
| 4 | `compute_overtime` & `compute_late_minutes` | Assumed shifts begin and end on the same calendar day, failing on overnight shifts (`shift_end <= shift_start`). | Night shifts (e.g., 22:00 to 06:00) calculated negative overtime or inaccurate shift windows. | Adjusted overnight shift calculations by advancing the end datetime by +1 day when end time is less than or equal to start time. |
| 5 | Timestamp / Timezone Boundary | Naive datetime conversions via `datetime.fromtimestamp(ts / 1000)` without explicit UTC timezone. | Timestamps drifted depending on the host machine's local timezone. | Explicitly cast timestamps using `datetime.fromtimestamp(ts / 1000, tz=timezone.utc)` and standardized ISO string outputs. |

### Considered Not a Defect

- Storing calendar dates (`date`, `joined_on`) as ISO strings (`YYYY-MM-DD`) and shift times as `HH:MM`: This is intentional design. Dates and shift schedules represent calendar markers rather than specific instants in time, avoiding timezone drift when stored as strings.
