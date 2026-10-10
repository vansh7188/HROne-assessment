# HROne Employee Attendance API

FastAPI and PyMongo attendance API with IST-aware shift calculations, atomic
attendance corrections, MongoDB analytics, and a live dashboard at `/`.

## Run locally

```powershell
.\.venv\Scripts\pip.exe install -r requirements.txt
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
.\.venv\Scripts\uvicorn.exe app.main:app --port 8000
```

Open `http://localhost:8000/` for the dashboard or `/docs` for Swagger.

For local MongoDB verification only, use the ignored `docker-compose.yml`:

```powershell
docker compose up --build
```

The Git submission contains only `app/main.py`, `requirements.txt`,
`REVIEW.md`, `DECISIONS.md`, and this README. No credentials are committed.
