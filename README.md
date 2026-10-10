# Employee Attendance & Analytics API

High-performance attendance management and analytics engine built with Python
3.11, FastAPI, and MongoDB 7.0. The implementation conforms to the OpenAPI
contract and the business rules R1–R10, including IST-aware shifts, UTC
timestamp boundaries, concurrency safety, and database-backed analytics.

## Tech Stack

- **Framework:** FastAPI with ASGI, Pydantic v2 validation, and lifespan event
  management.
- **Database:** MongoDB 7.0 through native PyMongo aggregation pipelines and
  compound indexes.
- **Server:** Uvicorn for the production-style ASGI runtime.
- **Configuration:** Python-dotenv and environment variables following
  twelve-factor application principles.

## How This Implementation Stands Out

### Zero-downtime index initialization

The FastAPI `lifespan` handler creates the required indexes before the
application begins serving requests. Index creation is idempotent and includes
the unique attendance key, unique employee key, date/employee query index, and
department index.

### Concurrency and race-condition proof

Punch-in does not rely on a check-then-insert sequence to enforce uniqueness.
MongoDB's unique compound index on `(emp_code, date)` is the authority under
concurrency. If simultaneous requests target the same employee and attendance
date, one insert succeeds and the other raises `DuplicateKeyError` (`E11000`),
which is translated into a predictable HTTP 409 response.

### Native database-level analytics

Attendance listing performs sorting, skipping, and limiting in MongoDB rather
than loading the collection into Python memory. Monthly rollups, department
summaries, late leaderboards, and department trends use MongoDB aggregation
stages such as `$lookup`, `$project`, `$group`, `$setWindowFields`, and
`$densify`. This keeps data-intensive work close to the indexed data.

### Overnight shift engine (R1 and R4)

Shift boundaries are interpreted in IST (`Asia/Kolkata`). For shifts such as
22:00–06:00, the engine assigns after-midnight punches to the shift's start
date and moves the shift-end boundary to the following calendar day. Late
minutes, overtime, and work hours remain correct across midnight.

### Comprehensive audit trail

Manual corrections use an atomic `find_one_and_update` with `$push` to append
exactly one immutable history entry. Each entry records the UTC audit time,
actor, reason, and field-level `{from, to}` changes. Concurrent corrections
cannot silently overwrite another update.

### Strict time and rounding guarantees

API instants are integer epoch milliseconds and are converted to timezone-aware
UTC BSON datetimes. Calendar dates and shift times remain plain strings because
they represent calendar values, not instants. Late calculation applies the
10-minute grace period, work hours use half-up rounding to two decimals, and
half-day status is set when rounded work hours are below 4.0.

## API Capabilities

- Employee creation and filtered, paginated employee listing.
- Atomic punch-in and punch-out workflows.
- Manual attendance regularization with audit history.
- Employee monthly attendance summaries.
- Department summaries that include employees with zero attendance logs.
- Deterministically ordered late leaderboard with department filtering.
- Gap-filled department trends with attendance rates and seven-day moving
  averages.
- MongoDB query-plan diagnostics through `/admin/explain/{endpoint}`.
- Interactive live dashboard at `/`.
- OpenAPI documentation at `/docs`.

## Run Locally

Install dependencies and configure MongoDB:

```powershell
.\.venv\Scripts\pip.exe install -r requirements.txt
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
.\.venv\Scripts\uvicorn.exe app.main:app --port 8000
```

Then open:

- Dashboard: `http://localhost:8000/`
- Swagger UI: `http://localhost:8000/docs`
- Readiness probe: `http://localhost:8000/health`

For local verification only, ignored Docker files are also available:

```powershell
docker compose up --build
```

The compose setup runs MongoDB 7.0 with a persistent volume and exposes the API
on port 8000 and MongoDB on port 27017.

## Configuration

| Variable | Description | Default |
|---|---|---|
| `MONGO_URI` | MongoDB connection string | `mongodb://localhost:27017` |
| `MONGO_DB` | Database name | `attendance_db` |

Secrets and `.env` files are excluded from Git. Supply credentials through the
environment or a local ignored `.env` file.

## Submission Scope

All application code is contained in `app/main.py` and starts with:

```text
uvicorn app.main:app --port 8000
```

The Git submission contains only the required application and assessment
documents: `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md`, and
this README. No credentials, Dockerfile, or generated local artifacts are
committed.
