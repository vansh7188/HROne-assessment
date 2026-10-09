# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `list_employees` | Pagination used `skip = page * page_size`, skipping the first page, and `total` counted the entire collection instead of the filtered query. | The first page is empty or starts with employee 21, and a department-filtered response reports the wrong total. | Changed skip to `(page - 1) * page_size` and counted `query`. |
| 2 | `punch_in` | The endpoint did not handle an unknown employee and used check-then-insert logic, leaving a TOCTOU race between the existence check and insert. | An unknown `emp_code` causes a later attribute/key failure; simultaneous requests can both pass the check and attempt duplicate records. | Added a 404 employee check, created the unique compound index, and mapped `DuplicateKeyError` to 409. |
| 3 | `list_attendance` | It loaded all matching attendance documents into Python, sorted them in memory, and sliced the result there. | Memory and response latency grow with the entire collection, especially near 100,000 records. | Applied sort, skip, and limit directly to the MongoDB cursor and used `count_documents(query)` for totals. |
| 4 | `compute_late_minutes` / `compute_overtime` | Both helpers assumed a same-calendar-day shift and did not account for overnight shifts whose end is on the next day. | A 22:00–06:00 shift calculates an incorrect late or overtime value around midnight. | Built IST shift boundaries from the attendance date and advance the end boundary for overnight shifts. |
| 5 | `punch_in` and time helpers | `datetime.fromtimestamp()` and `datetime.now()` produced naive local datetimes, so results depended on the server's local timezone and could drift from IST/UTC. | The same epoch input produces different dates or stored BSON values on servers in different timezones. | Convert epoch milliseconds explicitly through UTC-aware datetimes, derive dates in IST, and store UTC-aware BSON datetimes. |

## Deliberate design choices that are not defects

Calendar dates (`YYYY-MM-DD`) and shift times (`HH:MM`) remain plain strings. They are not instants: converting them to
datetimes would impose an arbitrary timezone and could distort the employee's intended IST calendar date or shift time.
Only actual timestamps such as punches, creation times, and audit events are stored as UTC BSON datetimes and exposed as
epoch milliseconds.
