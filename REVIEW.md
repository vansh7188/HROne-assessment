# REVIEW.md

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `list_employees` | The pagination offset used `skip = page * page_size`, skipping page 1, and `total` counted all employees instead of applying the department filter. | `page=1` starts at the second page, while a department-filtered response reports the collection-wide total. | Changed the offset to `(page - 1) * page_size` and counted the filtered query. |
| 2 | `punch_in` | The starter did not verify that `emp_code` existed and used check-then-insert logic, creating a TOCTOU race that could allow duplicate inserts. | An invalid code causes an unhandled 500; simultaneous punch-ins can both pass the pre-check. | Added a 404 employee check, enforced a unique `(emp_code, date)` index, and mapped `DuplicateKeyError` to HTTP 409. |
| 3 | `list_attendance` | It loaded matching documents into Python, sorted them in memory, and manually sliced the list instead of using MongoDB `.sort().skip().limit()` on the query. | Memory use and latency grow with the full matching collection, especially on unindexed large datasets. | Moved sorting, skipping, and limiting into the MongoDB cursor and counted with the same filter. |
| 4 | `compute_overtime` / `compute_late_minutes` | Both helpers assumed shift start and end were on the same calendar day and failed when `shift_end <= shift_start` for overnight shifts. | A 22:00–06:00 shift produces incorrect late or overtime boundaries around midnight. | Added IST shift-boundary construction and advanced overnight shift ends by one calendar day. |
| 5 | Datetime / timezone boundary | Naive `datetime.fromtimestamp(...)` and `datetime.now()` depended on the local machine timezone rather than explicit UTC BSON dates and epoch-millisecond API boundaries. | Identical inputs produce different attendance dates or stored timestamps on hosts in different timezones. | Converted API milliseconds through UTC-aware datetimes, derived calendar dates in IST, and stored UTC BSON datetimes. |

## Intentional design choice

Storing calendar dates (`date`, `joined_on`) and shift hours (`HH:MM`) as plain
strings was evaluated and is **not a defect**. They are calendar values rather
than instants; converting them to datetimes would impose an arbitrary timezone
and introduce avoidable distortion.
