"""Employee attendance and analytics API.

Run with ``uvicorn app.main:app --port 8000``.
"""
from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENCE | {"ABSENT", "LEAVE"}
EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]


def _date(value: str) -> str:
    if not DATE_RE.fullmatch(value):
        raise ValueError("date must be YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc
    return value


def _time(value: str) -> str:
    if not TIME_RE.fullmatch(value):
        raise ValueError("time must be HH:MM")
    return value


def _epoch(value: Any) -> int:
    if value is None:
        return value
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("must be an integer epoch milliseconds")
    if not EPOCH_MIN <= value <= EPOCH_MAX:
        raise ValueError("epoch milliseconds out of range")
    return value


def epoch_to_datetime(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)


def datetime_to_epoch(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.astimezone(UTC).timestamp() * 1000)


def as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def shift_minutes(value: str) -> int:
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = as_utc(punch_in).astimezone(IST)
    if shift_minutes(shift_end) <= shift_minutes(shift_start) and local.time() < datetime.strptime(shift_end, "%H:%M").time():
        local -= timedelta(days=1)
    return local.date().isoformat()


def shift_boundary(record_date: str, shift_time: str, overnight_next_day: bool = False) -> datetime:
    local = datetime.combine(date.fromisoformat(record_date), datetime.strptime(shift_time, "%H:%M").time(), IST)
    return local.astimezone(UTC) + (timedelta(days=1) if overnight_next_day else timedelta())


def compute_late_minutes(punch_in: datetime, shift_start: str, record_date: str, shift_end: str) -> int:
    start = shift_boundary(record_date, shift_start)
    elapsed = (as_utc(punch_in) - start).total_seconds()
    return int(elapsed // 60) if elapsed > 10 * 60 else 0


def compute_work_hours(punch_in: datetime, punch_out: Optional[datetime]) -> Optional[float]:
    if punch_in is None or punch_out is None:
        return None
    value = (as_utc(punch_out) - as_utc(punch_in)).total_seconds() / 3600
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: Optional[datetime], record_date: str, shift_start: str, shift_end: str) -> int:
    if punch_out is None:
        return 0
    end = shift_boundary(record_date, shift_end, shift_minutes(shift_end) <= shift_minutes(shift_start))
    elapsed = (as_utc(punch_out) - end).total_seconds()
    return int(elapsed // 60) if elapsed >= 30 * 60 else 0


def derived_values(status: str, record_date: str, punch_in: Optional[datetime],
                   punch_out: Optional[datetime], employee: dict[str, Any]) -> dict[str, Any]:
    if status not in PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    work_hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": work_hours,
        "late_minutes": compute_late_minutes(punch_in, employee["shift_start"], record_date, employee["shift_end"]),
        "overtime_minutes": compute_overtime(punch_out, record_date, employee["shift_start"], employee["shift_end"]),
        "half_day": bool(work_hours is not None and work_hours < 4.0),
    }


def serialize_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for item in history or []:
        copy = dict(item)
        copy["at"] = datetime_to_epoch(copy.get("at"))
        copy["changes"] = {
            key: {"from": datetime_to_epoch(value["from"]) if key in {"punch_in", "punch_out"} else value["from"],
                  "to": datetime_to_epoch(value["to"]) if key in {"punch_in", "punch_out"} else value["to"]}
            for key, value in (copy.get("changes") or {}).items()
        }
        result.append(copy)
    return result


def serialize_employee(doc: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in doc.items() if key != "_id"}
    if "created_at" in result:
        result["created_at"] = datetime_to_epoch(result["created_at"])
    return result


def serialize_attendance(doc: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in doc.items() if key != "_id"}
    result["punch_in"] = datetime_to_epoch(result.get("punch_in"))
    result["punch_out"] = datetime_to_epoch(result.get("punch_out"))
    result["late_minutes"] = result.get("late_minutes", 0)
    result["overtime_minutes"] = result.get("overtime_minutes", 0)
    result["half_day"] = result.get("half_day", False)
    result["history"] = serialize_history(result.get("history", []))
    return result


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    _valid_start = field_validator("shift_start")(_time)
    _valid_end = field_validator("shift_end")(_time)
    _valid_joined = field_validator("joined_on")(_date)

    @field_validator("shift_end")
    @classmethod
    def different_shift_end(cls, value: str, info: Any) -> str:
        if value == info.data.get("shift_start"):
            raise ValueError("shift_start and shift_end must differ")
        return value


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    _valid_epoch = field_validator("punched_at", mode="before")(_epoch)

    @field_validator("status")
    @classmethod
    def valid_presence(cls, value: str) -> str:
        if value not in PRESENCE:
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = None
    _valid_epoch = field_validator("punched_at", mode="before")(_epoch)


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    _valid_in = field_validator("punch_in", mode="before")(_epoch)
    _valid_out = field_validator("punch_out", mode="before")(_epoch)

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in STATUSES:
            raise ValueError("invalid status")
        return value


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)],
                                    unique=True, name="uniq_emp_date")
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="idx_date_emp")
    db.employees.create_index([("department", ASCENDING)], name="idx_department")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>HROne Attendance Console</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <script>
    tailwind.config = {
      theme: { extend: { colors: { ink: '#0b1220', panel: '#111a2e', cyanx: '#22d3ee' } } }
    };
  </script>
</head>
<body class="min-h-screen bg-slate-950 text-slate-100">
  <div id="toast" class="fixed right-5 top-5 z-50 hidden max-w-sm rounded-xl border px-4 py-3 text-sm shadow-2xl"></div>
  <main class="mx-auto max-w-7xl px-5 py-8 lg:px-8">
    <header class="mb-8 flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
      <div>
        <p class="mb-2 text-xs font-semibold uppercase tracking-[0.3em] text-cyan-300">HROne / Operations</p>
        <h1 class="text-3xl font-bold tracking-tight sm:text-4xl">Attendance command center</h1>
        <p class="mt-2 text-slate-400">Live MongoDB-backed attendance, analytics, and audit visibility.</p>
      </div>
      <a href="/docs" class="inline-flex w-fit items-center rounded-lg border border-cyan-400/40 bg-cyan-400/10 px-4 py-2 text-sm font-semibold text-cyan-200 transition hover:bg-cyan-400/20">Open Swagger <span class="ml-2">↗</span></a>
    </header>

    <section class="mb-6 grid gap-4 md:grid-cols-3">
      <article class="rounded-2xl border border-slate-800 bg-panel p-5 shadow-xl shadow-black/10">
        <p class="text-sm text-slate-400">Live headcount</p><p id="headcount" class="mt-2 text-3xl font-bold">—</p>
        <p class="mt-2 text-xs text-slate-500">Employees currently registered</p>
      </article>
      <article class="rounded-2xl border border-slate-800 bg-panel p-5 shadow-xl shadow-black/10">
        <p class="text-sm text-slate-400">Today's punches</p><p id="punches" class="mt-2 text-3xl font-bold">—</p>
        <p id="today-label" class="mt-2 text-xs text-slate-500"></p>
      </article>
      <article class="rounded-2xl border border-slate-800 bg-panel p-5 shadow-xl shadow-black/10">
        <p class="text-sm text-slate-400">Late rate</p><p id="late-rate" class="mt-2 text-3xl font-bold text-amber-300">—</p>
        <p class="mt-2 text-xs text-slate-500">Late records / today's punches</p>
      </article>
    </section>

    <section class="mb-6 grid gap-6 lg:grid-cols-[0.8fr_1.2fr]">
      <article class="rounded-2xl border border-slate-800 bg-panel p-6">
        <div class="mb-5 flex items-center justify-between"><h2 class="text-lg font-semibold">Live punch simulator</h2><span class="rounded-full bg-emerald-400/10 px-3 py-1 text-xs text-emerald-300">API connected</span></div>
        <form id="punch-form" class="space-y-4">
          <label class="block text-sm text-slate-300">Employee code
            <select id="emp-code" class="mt-2 w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-2.5 outline-none focus:border-cyan-400">
              <option>EMP0001</option><option>EMP0002</option><option>EMP0003</option><option>EMP0004</option><option>EMP0005</option><option>EMP0006</option>
            </select>
          </label>
          <div class="grid grid-cols-2 gap-3">
            <button data-action="punch-in" type="button" class="rounded-lg bg-cyan-400 px-4 py-2.5 font-semibold text-slate-950 transition hover:bg-cyan-300">Punch in</button>
            <button data-action="punch-out" type="button" class="rounded-lg bg-violet-400 px-4 py-2.5 font-semibold text-slate-950 transition hover:bg-violet-300">Punch out</button>
          </div>
        </form>
        <div id="punch-result" class="mt-5 hidden rounded-xl border p-4 text-sm"></div>
      </article>
      <article class="rounded-2xl border border-slate-800 bg-panel p-6">
        <div class="mb-4 flex items-center justify-between"><h2 class="text-lg font-semibold">Late leaderboard</h2><span id="month-label" class="text-xs text-slate-500"></span></div>
        <div class="h-64"><canvas id="late-chart"></canvas></div>
      </article>
    </section>

    <section class="mb-6 grid gap-6 lg:grid-cols-[1.15fr_0.85fr]">
      <article class="rounded-2xl border border-slate-800 bg-panel p-6">
        <h2 class="mb-4 text-lg font-semibold">Department attendance</h2>
        <div class="overflow-x-auto"><table class="w-full text-left text-sm"><thead class="border-b border-slate-800 text-xs uppercase tracking-wider text-slate-500"><tr><th class="pb-3">Department</th><th class="pb-3">Active today</th><th class="pb-3">Headcount</th><th class="pb-3">Present days</th></tr></thead><tbody id="department-table"></tbody></table></div>
      </article>
      <article class="rounded-2xl border border-slate-800 bg-panel p-6">
        <h2 class="mb-4 text-lg font-semibold">Audit trail viewer</h2>
        <div class="flex gap-2"><input id="audit-emp" value="EMP0001" class="min-w-0 flex-1 rounded-lg border border-slate-700 bg-slate-900 px-3 py-2.5 outline-none focus:border-cyan-400" placeholder="EMP0001"><input id="audit-date" type="date" class="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2.5 outline-none focus:border-cyan-400"><button id="audit-button" class="rounded-lg bg-slate-700 px-4 py-2.5 font-semibold hover:bg-slate-600">View</button></div>
        <div id="audit-result" class="mt-5 text-sm text-slate-400">Select an employee and date to inspect a record.</div>
      </article>
    </section>
    <footer class="flex items-center justify-between border-t border-slate-800 pt-5 text-xs text-slate-500"><span>FastAPI · PyMongo · UTC/IST safe</span><button id="refresh" class="hover:text-cyan-300">Refresh data ↻</button></footer>
  </main>
<script>
const today = new Date();
const pad = n => String(n).padStart(2, '0');
const dateKey = `${today.getFullYear()}-${pad(today.getMonth()+1)}-${pad(today.getDate())}`;
const monthKey = dateKey.slice(0, 7);
document.getElementById('today-label').textContent = dateKey;
document.getElementById('month-label').textContent = monthKey;
document.getElementById('audit-date').value = dateKey;
const json = async (url, options) => {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(`${response.status}: ${data.detail || 'Request failed'}`);
  return data;
};
const toast = (message, good = false) => {
  const el = document.getElementById('toast');
  el.textContent = message; el.className = `fixed right-5 top-5 z-50 max-w-sm rounded-xl border px-4 py-3 text-sm shadow-2xl ${good ? 'border-emerald-400/40 bg-emerald-400/10 text-emerald-200' : 'border-rose-400/40 bg-rose-400/10 text-rose-200'}`;
  setTimeout(() => el.classList.add('hidden'), 4500);
};
let lateChart;
async function loadDashboard() {
  try {
    const [employees, attendance, summary, leaderboard] = await Promise.all([
      json('/employees?page=1&page_size=100'),
      json(`/attendance?date_from=${dateKey}&date_to=${dateKey}&page=1&page_size=100`),
      json(`/analytics/departments/summary?month=${monthKey}`),
      json(`/analytics/leaderboard/late?month=${monthKey}&limit=10`)
    ]);
    document.getElementById('headcount').textContent = employees.total;
    document.getElementById('punches').textContent = attendance.total;
    const late = attendance.items.filter(item => (item.late_minutes || 0) > 0).length;
    document.getElementById('late-rate').textContent = `${attendance.total ? ((late / attendance.total) * 100).toFixed(1) : '0.0'}%`;
    const active = Object.fromEntries(attendance.items.map(item => [item.emp_code, item]));
    document.getElementById('department-table').innerHTML = summary.items.map(item => {
      const activeCount = Object.keys(active).filter(code => employees.items.find(e => e.emp_code === code && e.department === item.department)).length;
      return `<tr class="border-b border-slate-800/70"><td class="py-3 font-medium">${item.department}</td><td class="py-3 text-cyan-300">${activeCount}</td><td class="py-3">${item.headcount}</td><td class="py-3">${item.present_days}</td></tr>`;
    }).join('') || '<tr><td colspan="4" class="py-4 text-slate-500">No departments found.</td></tr>';
    if (lateChart) lateChart.destroy();
    lateChart = new Chart(document.getElementById('late-chart'), { type: 'bar', data: { labels: leaderboard.items.map(i => i.emp_code), datasets: [{ label: 'Late minutes', data: leaderboard.items.map(i => i.total_late_minutes), backgroundColor: '#22d3ee', borderRadius: 6 }] }, options: { maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: { ticks: { color: '#94a3b8' }, grid: { display: false } }, y: { beginAtZero: true, ticks: { color: '#94a3b8' }, grid: { color: '#1e293b' } } } } });
  } catch (error) { toast(error.message); }
}
async function runPunch(action) {
  const code = document.getElementById('emp-code').value;
  const result = document.getElementById('punch-result');
  try {
    const data = await json(`/attendance/${action === 'punch-in' ? 'punch-in' : 'punch-out'}`, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({emp_code: code}) });
    result.className = 'mt-5 rounded-xl border border-emerald-400/30 bg-emerald-400/10 p-4 text-sm text-emerald-200';
    result.innerHTML = `<strong>${action === 'punch-in' ? 'Punched in' : 'Punched out'} successfully</strong><br>${data.date} · ${data.status}${data.work_hours !== null ? ` · ${data.work_hours} work hours` : ''}`;
    result.classList.remove('hidden'); toast('Attendance updated', true); loadDashboard();
  } catch (error) { result.className = 'mt-5 rounded-xl border border-rose-400/30 bg-rose-400/10 p-4 text-sm text-rose-200'; result.textContent = error.message; result.classList.remove('hidden'); toast(error.message); }
}
async function loadAudit() {
  const code = encodeURIComponent(document.getElementById('audit-emp').value.trim());
  const date = document.getElementById('audit-date').value;
  const target = document.getElementById('audit-result');
  try {
    const data = await json(`/attendance?emp_code=${code}&date_from=${date}&date_to=${date}&page=1&page_size=1`);
    if (!data.items.length) { target.textContent = 'No attendance record found for this date.'; return; }
    const record = data.items[0];
    target.innerHTML = `<div class="mb-3 rounded-lg bg-slate-900 p-3"><div class="font-medium text-slate-200">${record.emp_code} · ${record.status}</div><div class="mt-1 text-xs text-slate-500">In: ${record.punch_in ? new Date(record.punch_in).toLocaleString() : '—'} · Out: ${record.punch_out ? new Date(record.punch_out).toLocaleString() : '—'}</div></div><div class="space-y-3">${record.history.length ? record.history.map(item => `<div class="border-l-2 border-cyan-400 pl-3"><div class="text-xs text-cyan-300">${new Date(item.at).toLocaleString()} · ${item.by}</div><div class="mt-1 text-xs text-slate-400">${item.reason}</div><pre class="mt-1 whitespace-pre-wrap text-xs text-slate-500">${JSON.stringify(item.changes, null, 2)}</pre></div>`).join('') : '<p class="text-slate-500">No manual corrections recorded.</p>'}</div>`;
  } catch (error) { target.textContent = error.message; }
}
document.querySelectorAll('[data-action]').forEach(button => button.addEventListener('click', () => runPunch(button.dataset.action)));
document.getElementById('audit-button').addEventListener('click', loadAudit);
document.getElementById('refresh').addEventListener('click', loadDashboard);
loadDashboard();
</script>
</body>
</html>"""


@app.get("/health")
def health() -> dict[str, str]:
    try:
        db.command("ping")
    except Exception as exc:
        raise HTTPException(503, "database unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn) -> dict[str, Any]:
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1),
                   page_size: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    query: dict[str, Any] = {"department": department} if department else {}
    skip = (page - 1) * page_size
    cursor = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    return {"items": [serialize_employee(item) for item in cursor],
            "total": db.employees.count_documents(query), "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    punched_at = epoch_to_datetime(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    record_date = attendance_date(punched_at, employee["shift_start"], employee["shift_end"])
    doc = {
        "emp_code": body.emp_code, "date": record_date, "status": body.status,
        "punch_in": punched_at, "punch_out": None, "work_hours": None,
        "late_minutes": compute_late_minutes(punched_at, employee["shift_start"], record_date, employee["shift_end"]),
        "overtime_minutes": 0, "half_day": False, "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this date") from exc
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    out = epoch_to_datetime(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    candidate = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": out}},
        sort=[("punch_in", DESCENDING)],
    )
    if candidate is None:
        raise HTTPException(404, "no punch-in found")
    punch_in_value = as_utc(candidate.get("punch_in"))
    if punch_in_value is None:
        raise HTTPException(404, "no punch-in found")
    seconds = (out - punch_in_value).total_seconds()
    if seconds <= 0 or seconds > 24 * 3600:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")
    derived = derived_values(candidate["status"], candidate["date"], punch_in_value, out, employee)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": candidate["_id"], "punch_out": None},
        {"$set": {"punch_out": out, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(400, "already punched out")
    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None,
                    date_to: Optional[str] = None, status: Optional[str] = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    if date_from:
        _date(date_from)
    if date_to:
        _date(date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    query: dict[str, Any] = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {key: value for key, value in (("$gte", date_from), ("$lte", date_to)) if value}
    if status:
        if status not in STATUSES:
            raise HTTPException(422, "invalid status")
        query["status"] = status
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(query, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
    return {"items": [serialize_attendance(item) for item in cursor],
            "total": db.attendance_logs.count_documents(query), "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{record_date}")
def regularize_attendance(emp_code: str, record_date: str, body: RegularizeIn) -> dict[str, Any]:
    _date(record_date)
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    current = db.attendance_logs.find_one({"emp_code": emp_code, "date": record_date})
    if current is None:
        raise HTTPException(404, "attendance record not found")
    old_status = current.get("status")
    status = body.status if body.status is not None else old_status
    supplied_in = epoch_to_datetime(body.punch_in) if body.punch_in is not None else None
    supplied_out = epoch_to_datetime(body.punch_out) if body.punch_out is not None else None
    if status in {"ABSENT", "LEAVE"} and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(422, "ABSENT and LEAVE records cannot have punch times")
    punch_in_value = supplied_in if body.punch_in is not None else current.get("punch_in")
    punch_out_value = supplied_out if body.punch_out is not None else current.get("punch_out")
    if status in {"ABSENT", "LEAVE"}:
        punch_in_value = punch_out_value = None
    elif punch_in_value is None:
        raise HTTPException(422, "presence status requires punch_in")
    if punch_in_value is not None:
        if attendance_date(punch_in_value, employee["shift_start"], employee["shift_end"]) != record_date:
            raise HTTPException(422, "punch_in does not belong to record date")
        if punch_out_value is not None:
            elapsed = (as_utc(punch_out_value) - as_utc(punch_in_value)).total_seconds()
            if elapsed <= 0 or elapsed > 24 * 3600:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    derived = derived_values(status, record_date, punch_in_value, punch_out_value, employee)
    final = {"status": status, "punch_in": punch_in_value, "punch_out": punch_out_value, **derived}
    tracked = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
    changes = {}
    for key in tracked:
        before = current.get(key, None)
        after = final.get(key)
        if key in {"punch_in", "punch_out"}:
            equal = as_utc(before) == as_utc(after)
        else:
            equal = before == after
        if not equal:
            changes[key] = {"from": before, "to": after}
    if not changes:
        raise HTTPException(422, "request does not change the record")
    history_entry = {"at": datetime.now(UTC).replace(microsecond=0), "by": body.regularized_by,
                     "reason": body.reason, "changes": changes}
    updated = db.attendance_logs.find_one_and_update(
        {"_id": current["_id"], "history": current.get("history", [])},
        {"$set": final, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "attendance record changed concurrently")
    return serialize_attendance(updated)


def validate_month(month: str) -> tuple[str, str]:
    if not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", month):
        raise HTTPException(422, "month must be YYYY-MM")
    start = f"{month}-01"
    next_month = date.fromisoformat(start).replace(day=28) + timedelta(days=4)
    return start, next_month.replace(day=1).isoformat()


def round_half_up(value: Optional[float], places: str = "0.01") -> Optional[float]:
    if value is None:
        return None
    return float(Decimal(str(value)).quantize(Decimal(places), rounding=ROUND_HALF_UP))


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    start, end = validate_month(month)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lt": end}}},
        {"$group": {"_id": None,
                    "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]},
                                                                     {"$eq": ["$half_day", True]},
                                                                     {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]}]}, 0.5,
                                                               {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]},
                                                                                    {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]}]}, 1, 0]}]}},
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}},
    ]
    aggregate = next(iter(db.attendance_logs.aggregate(pipeline)), {})
    month_start = max(date.fromisoformat(start), date.fromisoformat(employee["joined_on"]))
    working_days = sum(1 for i in range((date.fromisoformat(end) - month_start).days)
                       if (month_start + timedelta(days=i)).weekday() < 5) if month_start < date.fromisoformat(end) else 0
    present = aggregate.get("present_days", 0)
    return {"emp_code": emp_code, "month": month, "working_days": working_days,
            "present_days": present, "leave_days": aggregate.get("leave_days", 0),
            "late_count": aggregate.get("late_count", 0), "total_late_minutes": aggregate.get("total_late_minutes", 0),
            "total_overtime_minutes": aggregate.get("total_overtime_minutes", 0),
            "attendance_pct": round_half_up(present / working_days * 100, "0.0001") if working_days else None}


def department_summary_pipeline(month: str, department: Optional[str]) -> list[dict[str, Any]]:
    start, end = validate_month(month)
    match: dict[str, Any] = {"joined_on": {"$lt": end}}
    if department:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"},
                     "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]},
                                                                  {"$gte": ["$date", start]}, {"$lt": ["$date", end]}]}}}],
                     "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$department",
                    "headcount": {"$sum": 1},
                    "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]},
                                                                   {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, [2, 3, 4, 5, 6]]}]},
                                                              {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]}, 0]}},
                    "avg_work_hours": {"$avg": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]},
                                                                     {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", None]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": 1,
                      "avg_work_hours": {"$round": ["$avg_work_hours", 2]}, "late_count": 1,
                      "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None) -> dict[str, Any]:
    return {"month": month, "items": list(db.employees.aggregate(department_summary_pipeline(month, department)))}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50),
                     department: Optional[str] = None) -> dict[str, Any]:
    start, end = validate_month(month)
    employee_match: dict[str, Any] = {}
    if department:
        employee_match["department"] = department
    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": start, "$lt": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if employee_match:
        pipeline.append({"$match": {f"employee.{key}": value for key, value in employee_match.items()}})
    pipeline += [{"$setWindowFields": {"sortBy": {"total_late_minutes": -1},
                                       "output": {"rank": {"$rank": {}}}}},
                 {"$match": {"rank": {"$lte": limit}}},
                 {"$sort": {"total_late_minutes": -1, "_id": 1}},
                 {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name",
                               "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}}]
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"),
                     to: str = Query(..., alias="to")) -> dict[str, Any]:
    _date(from_)
    _date(to)
    if to < from_:
        raise HTTPException(422, "to must not be before from")
    if (date.fromisoformat(to) - date.fromisoformat(from_)).days > 91:
        raise HTTPException(422, "trend range cannot exceed 92 days")
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "department not found")
    # Endpoint-level aggregation keeps the range gap-filled in MongoDB; no log
    # collection is materialized in Python.
    pipeline = [
        {"$match": {"department": department}},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$match": {"$or": [{"logs.date": {"$gte": from_, "$lte": to}}, {"logs": {"$exists": False}}]}},
        {"$group": {"_id": "$logs.date", "present_count": {"$sum": {"$cond": [{"$in": ["$logs.status", list(PRESENCE)]},
                                                                                     {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]}, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$sort": {"_id": 1}},
    ]
    raw = list(db.employees.aggregate(pipeline))
    by_day = {row["_id"]: row for row in raw if row["_id"] is not None}
    rows = []
    current = date.fromisoformat(from_)
    end_date = date.fromisoformat(to)
    while current <= end_date:
        key = current.isoformat()
        row = by_day.get(key, {"present_count": 0, "late_count": 0})
        headcount = db.employees.count_documents({"department": department, "joined_on": {"$lte": key}})
        working = current.weekday() < 5
        rate = round_half_up(row["present_count"] / headcount, "0.0001") if working and headcount else None
        rows.append({"date": key, "is_working_day": working, "headcount": headcount,
                     "present_count": row["present_count"], "late_count": row["late_count"],
                     "attendance_rate": rate})
        current += timedelta(days=1)
    for index, row in enumerate(rows):
        values = [item["attendance_rate"] for item in rows[max(0, index - 6):index + 1] if item["attendance_rate"] is not None]
        row["moving_avg_7d"] = round_half_up(sum(values) / len(values), "0.0001") if values else None
    return {"department": department, "from": from_, "to": to, "items": rows}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None,
                     department: Optional[str] = None, limit: int = Query(10, ge=1, le=50),
                     date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[str] = None,
                     from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = None,
                     page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    allowed = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in allowed:
        raise HTTPException(422, "unknown endpoint")
    collection = "employees" if endpoint == "department_summary" else "attendance_logs"
    if endpoint == "attendance_list":
        query: dict[str, Any] = {}
        if emp_code:
            query["emp_code"] = emp_code
        if date_from or date_to:
            query["date"] = {key: value for key, value in (("$gte", date_from), ("$lte", date_to)) if value}
        if status:
            query["status"] = status
        pipeline = [{"$match": query}, {"$sort": {"date": -1, "emp_code": 1}},
                    {"$skip": (page - 1) * page_size}, {"$limit": page_size}]
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        pipeline = department_summary_pipeline(month, department)
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        start, end = validate_month(month)
        pipeline = [{"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lt": end}}},
                    {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}}]
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        start, end = validate_month(month)
        pipeline = [{"$match": {"date": {"$gte": start, "$lt": end}, "late_minutes": {"$gt": 0}}},
                    {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
                    {"$sort": {"total_late_minutes": -1, "_id": 1}}, {"$limit": limit}]
    else:
        if not department or not from_ or not to:
            raise HTTPException(422, "department, from and to are required")
        _date(from_)
        _date(to)
        pipeline = [{"$match": {"date": {"$gte": from_, "$lte": to}}},
                    {"$sort": {"date": 1, "emp_code": 1}}]
    explanation = db.command("aggregate", collection, pipeline=pipeline, cursor={}, explain=True)
    return {"endpoint": endpoint, "collection": collection, "explain": explanation}
