"""
Employee Attendance & Analytics API.

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env may set them; real environment variables win)

All application code lives in this single file on purpose (see PROBLEM_STATEMENT.docx).
The contract and the business rules R1-R10 are in openapi.yaml; the stored shapes are in
DATA_MODEL.md.
"""
import calendar
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, StrictInt, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure

# load_dotenv does not override variables that already exist, so real env vars win.
load_dotenv()

# tz_aware=True means PyMongo hands us timezone-aware UTC datetimes (never naive).
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

EPOCH_MS_MIN = 100000000000
EPOCH_MS_MAX = 4102444800000

# R6: statuses that count as presence.
PRESENCE_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class Status(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def now_utc() -> datetime:
    """Current UTC instant truncated to whole seconds (R1)."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def epoch_ms_to_utc(epoch_ms: Optional[int] = None) -> datetime:
    """Convert epoch milliseconds to a UTC datetime truncated to whole seconds.

    None means "now". Truncation is required by R1: 09:40:00.900 counts as 09:40:00
    and that truncated value is what we store and return.
    """
    if epoch_ms is None:
        return now_utc()
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).replace(microsecond=0)


def dt_to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    """Serialize a UTC datetime to epoch milliseconds."""
    if value is None:
        return None
    return int(value.timestamp() * 1000)


def attendance_date_ist(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: the record's IST calendar date, honouring overnight shifts.

    For an overnight shift (shift_end <= shift_start) a punch-in whose IST time is
    earlier than shift_end belongs to the shift that started the previous day.
    """
    local = punch_in.astimezone(IST)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    overnight = (eh, em) <= (sh, sm)
    if overnight and (local.hour, local.minute) < (eh, em):
        local = local - timedelta(days=1)
    return local.date().isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: str) -> int:
    """R2: late only when strictly more than the 10-minute grace; floor minutes since shift start."""
    local = punch_in.astimezone(IST)
    start_hour, start_minute = map(int, shift_start.split(":"))
    end_hour, end_minute = map(int, shift_end.split(":"))

    shift_start_time = local.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)

    if (end_hour, end_minute) <= (start_hour, start_minute):  # overnight shift
        if (local.hour, local.minute) < (end_hour, end_minute):
            shift_start_time -= timedelta(days=1)

    elapsed = (local - shift_start_time).total_seconds()
    if elapsed <= 600:  # grace: 09:40:00 -> 0, 09:40:01 -> 10
        return 0
    return int(elapsed // 60)


def compute_work_hours(punch_in: datetime, punch_out: Optional[datetime]) -> Optional[float]:
    """R4: (punch_out - punch_in) / 3600, 2 decimals HALF_UP. None until punch-out."""
    if punch_out is None:
        return None
    seconds = (punch_out.replace(microsecond=0) - punch_in.replace(microsecond=0)).total_seconds()
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime_minutes(
    punch_out: datetime, shift_end: str, date_str: str, shift_start: str
) -> int:
    """R3: floor minutes from shift end to punch-out, only when >= 30."""
    local = punch_out.astimezone(IST)
    end_hour, end_minute = map(int, shift_end.split(":"))
    start_hour, start_minute = map(int, shift_start.split(":"))

    shift_end_time = datetime.fromisoformat(date_str).replace(
        hour=end_hour, minute=end_minute, second=0, microsecond=0, tzinfo=IST
    )
    if (end_hour, end_minute) <= (start_hour, start_minute):  # overnight shift ends next day
        shift_end_time += timedelta(days=1)

    overtime_minutes = int((local - shift_end_time).total_seconds() // 60)
    return overtime_minutes if overtime_minutes >= 30 else 0


def round_half_up(value, places: int):
    """R8: round a number to `places` decimals, half-up."""
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def _parse_month(month: str) -> tuple:
    """Validate a YYYY-MM month and return (year, month).

    The year range is bounded so the exclusive next-month boundary used by
    month_bounds (date(year + 1, 1, 1) for December) can always be constructed.
    """
    try:
        year_str, mon_str = month.split("-")
    except (AttributeError, TypeError, ValueError):
        raise HTTPException(status_code=422, detail="month must be a valid YYYY-MM value")
    if (
        len(year_str) != 4
        or len(mon_str) != 2
        or not (year_str.isascii() and year_str.isdigit())
        or not (mon_str.isascii() and mon_str.isdigit())
    ):
        raise HTTPException(status_code=422, detail="month must be a valid YYYY-MM value")
    year = int(year_str)
    mon = int(mon_str)
    if not (1 <= mon <= 12):
        raise HTTPException(status_code=422, detail="month must be a valid YYYY-MM value")
    if not (1 <= year <= 9999) or (year == 9999 and mon == 12):
        raise HTTPException(status_code=422, detail="month is outside the supported range 0001-01 to 9999-11")
    return year, mon


def month_bounds(month: str):
    """Return inclusive start and exclusive next-month-start as YYYY-MM-DD strings."""
    year, mon = _parse_month(month)
    start = date(year, mon, 1)
    next_start = date(year + 1, 1, 1) if mon == 12 else date(year, mon + 1, 1)
    return start.isoformat(), next_start.isoformat()


def working_days_for_month(joined_on: str, month: str) -> int:
    """R7: Mon-Fri days in the month, counting from joined_on for mid-month joiners."""
    year, mon = _parse_month(month)
    first = date(year, mon, 1)
    last = date(year, mon, calendar.monthrange(year, mon)[1])
    joined = date.fromisoformat(joined_on)
    start = max(first, joined)
    if start > last:
        return 0
    count = 0
    current = start
    while current <= last:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


# --------------------------------------------------------------------------- #
# Serialization (MongoDB document -> API document)
# --------------------------------------------------------------------------- #
def _convert_value(value: Any) -> Any:
    """Recursively convert BSON datetimes (e.g. inside history) to epoch milliseconds."""
    if isinstance(value, datetime):
        return dt_to_epoch_ms(value)
    if isinstance(value, dict):
        return {k: _convert_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_convert_value(v) for v in value]
    return value


def serialize_attendance(doc: dict) -> dict:
    """Map a stored attendance document to the AttendanceRecord schema (no _id, no id)."""
    return {
        "emp_code": doc.get("emp_code"),
        "date": doc.get("date"),
        "status": doc.get("status"),
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [_convert_value(entry) for entry in doc.get("history", [])],
    }


def serialize_employee(doc: dict) -> dict:
    """Map a stored employee document to the Employee schema (no _id)."""
    return {
        "emp_code": doc.get("emp_code"),
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": dt_to_epoch_ms(doc.get("created_at")),
    }


# --------------------------------------------------------------------------- #
# Indexes (created idempotently at startup)
# --------------------------------------------------------------------------- #
logger = logging.getLogger(__name__)

# (collection, index name, key spec, unique). These definitions are exact and must not change.
REQUIRED_INDEXES = [
    ("employees", "uniq_emp_code", [("emp_code", 1)], True),
    ("employees", "dept", [("department", 1)], False),
    ("employees", "joined_on", [("joined_on", 1)], False),
    ("employees", "dept_joined_on", [("department", 1), ("joined_on", 1)], False),
    ("attendance_logs", "uniq_emp_code_date", [("emp_code", 1), ("date", 1)], True),
    ("attendance_logs", "date_desc_emp_code", [("date", -1), ("emp_code", 1)], False),
]


def _ensure_index(collection, name: str, keys: list, unique: bool) -> None:
    """Create one index, tolerating only an already-present *compatible* index.

    Genuine failures (connection loss, DuplicateKeyError while building a unique
    index, ...) propagate so startup cannot finish without the indexes the
    concurrency logic depends on.
    """
    try:
        collection.create_index(keys, name=name, unique=unique)
        logger.info("Index %r on %s created or already present", name, collection.name)
    except DuplicateKeyError:
        # Existing data violates the unique constraint: never hide this.
        logger.error("Cannot build unique index %r on %s: duplicate key exists", name, collection.name)
        raise
    except OperationFailure as exc:
        if exc.code not in (68, 85, 86):
            raise
        existing = collection.index_information().get(name)
        if existing is None:
            raise RuntimeError(
                f"Index {name!r} reported a conflict (code {exc.code}) but is not present"
            ) from exc
        existing_key = [tuple(pair) for pair in existing.get("key", [])]
        existing_unique = bool(existing.get("unique", False))
        if existing_key != [tuple(pair) for pair in keys] or existing_unique != unique:
            logger.error(
                "Existing index %r on %s is incompatible: key=%s unique=%s; required key=%s unique=%s",
                name, collection.name, existing_key, existing_unique, keys, unique,
            )
            raise RuntimeError(
                f"Existing index {name!r} is incompatible with the required definition"
            ) from exc
        logger.info("Index %r on %s already exists with a compatible definition", name, collection.name)


def ensure_indexes() -> None:
    for collection_name, name, keys, unique in REQUIRED_INDEXES:
        _ensure_index(db[collection_name], name, keys, unique)
    logger.info("All %d required indexes are present", len(REQUIRED_INDEXES))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: date

    @model_validator(mode="after")
    def _shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)
    status: PresenceStatus = PresenceStatus.PRESENT


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)
    punch_out: Optional[StrictInt] = Field(default=None, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Shared query builders (used by BOTH the endpoint and /admin/explain so the
# explained plan is exactly the plan the endpoint runs)
# --------------------------------------------------------------------------- #
def build_attendance_filter(
    emp_code: Optional[str],
    date_from: Optional[date],
    date_to: Optional[date],
    status: Optional[Status],
) -> dict:
    query: dict = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from.isoformat()
        if date_to:
            query["date"]["$lte"] = date_to.isoformat()
    if status:
        query["status"] = status.value
    return query


def build_monthly_pipeline(emp_code: str, month: str) -> list:
    start, next_start = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lt": next_start}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", PRESENCE_STATUSES]},
                    {"$not": {"$in": [
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [1, 7]
                    ]}},
                ]},
                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                0,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [
                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0
            ]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
        {"$project": {"_id": 0}},
    ]


def build_department_summary_pipeline(month: str, department: Optional[str]) -> list:
    start, next_start = month_bounds(month)
    match: dict = {"joined_on": {"$lt": next_start}}
    if department:
        match["department"] = department

    # Per-employee roll-up over that employee's month logs, then sum per department.
    def sum_over_logs(condition, value_expr):
        return {"$sum": {"$map": {
            "input": {"$filter": {"input": "$logs", "as": "l", "cond": condition}},
            "as": "l",
            "in": value_expr,
        }}}

    presence_cond = {"$in": ["$$l.status", PRESENCE_STATUSES]}
    weekday_cond = {"$not": {"$in": [
        {"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}}, [1, 7]
    ]}}

    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"ec": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$ec"]},
                    {"$gte": ["$date", start]},
                    {"$lt": ["$date", next_start]},
                ]}}},
            ],
            "as": "logs",
        }},
        {"$project": {
            "department": 1,
            "present_days": sum_over_logs(
                {"$and": [presence_cond, weekday_cond]},
                {"$cond": [{"$ifNull": ["$$l.half_day", False]}, 0.5, 1]},
            ),
            "work_sum": sum_over_logs(
                {"$and": [presence_cond, {"$ne": [{"$ifNull": ["$$l.work_hours", None]}, None]}]},
                "$$l.work_hours",
            ),
            "work_count": sum_over_logs(
                {"$and": [presence_cond, {"$ne": [{"$ifNull": ["$$l.work_hours", None]}, None]}]},
                1,
            ),
            "late_count": sum_over_logs(
                {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}, 1
            ),
            "total_late_minutes": sum_over_logs(True, {"$ifNull": ["$$l.late_minutes", 0]}),
            "leave_count": sum_over_logs({"$eq": ["$$l.status", "LEAVE"]}, 1),
            "on_duty_count": sum_over_logs({"$eq": ["$$l.status", "ON_DUTY"]}, 1),
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present_days"},
            "work_sum": {"$sum": "$work_sum"},
            "work_count": {"$sum": "$work_count"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
        }},
        {"$sort": {"_id": 1}},
    ]


def build_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    start, next_start = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": start, "$lt": next_start}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [
                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0
            ]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$unwind": "$emp"},  # logs whose employee does not exist are ignored
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        {"$set": {
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
        }},
        # Competition ranking on late minutes only, then order ties by emp_code (R: 1,2,2,4).
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": 1, "name": 1, "department": 1,
            "total_late_minutes": 1, "late_count": 1,
        }},
    ]
    return pipeline


def _round_expr(expr, places: int) -> dict:
    """Aggregation expression: round half-up to `places` decimals (values here are >= 0)."""
    factor = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, factor]}, 0.5]}}, factor]}


def build_trend_pipeline(department: str, from_d: date, to_d: date) -> list:
    from_dt = datetime(from_d.year, from_d.month, from_d.day, tzinfo=timezone.utc)
    n_days = (to_d - from_d).days + 1
    return [
        # Base: the department itself (an employee-less department is already a 404 at the
        # endpoint). We only need a single seed document to run the $facet once; the day
        # rows themselves are generated below.
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$facet": {
            # One document per calendar day in [from, to] (server-side gap filling).
            "days": [
                {"$project": {"day": {"$map": {
                    "input": {"$range": [0, n_days]},
                    "as": "i",
                    "in": {"$dateAdd": {"startDate": from_dt, "unit": "day", "amount": "$$i"}},
                }}}},
                {"$unwind": "$day"},
                {"$project": {"_id": 0, "dateObj": "$day"}},
                {"$addFields": {"present_count": 0, "late_count": 0}},
            ],
            # Per-day present/late sums for the department, in a single pass over the logs.
            "att": [
                {"$lookup": {
                    "from": "attendance_logs",
                    "pipeline": [
                        {"$match": {"date": {"$gte": from_d.isoformat(), "$lte": to_d.isoformat()}}},
                        {"$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "emp",
                        }},
                        {"$unwind": "$emp"},
                        {"$match": {"emp.department": department}},
                        {"$group": {
                            "_id": "$date",
                            "present_count": {"$sum": {"$cond": [
                                {"$in": ["$status", PRESENCE_STATUSES]},
                                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                                0,
                            ]}},
                            "late_count": {"$sum": {"$cond": [
                                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0
                            ]}},
                        }},
                        {"$project": {
                            "_id": 0,
                            "dateObj": {"$dateFromString": {"dateString": "$_id"}},
                            "present_count": 1,
                            "late_count": 1,
                        }},
                    ],
                    "as": "rows",
                }},
                {"$unwind": "$rows"},
                {"$replaceRoot": {"newRoot": "$rows"}},
            ],
        }},
        {"$project": {"rows": {"$concatArrays": ["$days", "$att"]}}},
        {"$unwind": "$rows"},
        {"$replaceRoot": {"newRoot": "$rows"}},
        {"$group": {
            "_id": "$dateObj",
            "present_count": {"$max": {"$ifNull": ["$present_count", 0]}},
            "late_count": {"$max": {"$ifNull": ["$late_count", 0]}},
        }},
        {"$sort": {"_id": 1}},
        {"$addFields": {
            "dateObj": "$_id",
            "dateStr": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}},
        }},
        # Headcount per day: department employees who had joined by that day (R9).
        {"$lookup": {
            "from": "employees",
            "let": {"dayStr": "$dateStr"},
            "pipeline": [
                {"$match": {"department": department}},  # equality match uses the dept index
                {"$match": {"$expr": {"$lte": ["$joined_on", "$$dayStr"]}}},
            ],
            "as": "dept_emps",
        }},
        {"$addFields": {"headcount": {"$size": "$dept_emps"}}},
        {"$addFields": {
            "is_working_day": {"$not": {"$in": [{"$dayOfWeek": "$dateObj"}, [1, 7]]}},
        }},
        {"$addFields": {
            "attendance_rate": {"$cond": [
                {"$and": [{"$eq": ["$is_working_day", True]}, {"$gt": ["$headcount", 0]}]},
                _round_expr({"$divide": ["$present_count", "$headcount"]}, 4),
                None,
            ]},
        }},
        # 7-day moving average over the non-null attendance rates (MongoDB window function).
        {"$setWindowFields": {
            "sortBy": {"dateObj": 1},
            "output": {"moving_avg_7d": {
                "$avg": "$attendance_rate",
                "window": {"documents": [-6, 0]},
            }},
        }},
        {"$addFields": {"moving_avg_7d": _round_expr({"$ifNull": ["$moving_avg_7d", None]}, 4)}},
        {"$project": {
            "_id": 0, "date": "$dateStr", "is_working_day": 1, "headcount": 1,
            "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1,
        }},
        {"$sort": {"date": 1}},
    ]


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok", "database": "connected"}
    except Exception as exc:
        print("MongoDB error:", type(exc).__name__, str(exc))
        raise HTTPException(status_code=503, detail="Database connection failed")


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on.isoformat(),
        "created_at": now_utc(),
    }
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        # The unique index, not a check-then-insert race, guarantees uniqueness.
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query: dict = {}
    if department:
        query["department"] = department
    total = db.employees.count_documents(query)
    cursor = (
        db.employees.find(query).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    )
    return {
        "items": [serialize_employee(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")

    punched_at = epoch_ms_to_utc(body.punched_at)
    attendance_date = attendance_date_ist(punched_at, emp["shift_start"], emp["shift_end"])

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status.value,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punched_at, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        # Unique index on (emp_code, date) turns a simultaneous duplicate into 409.
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")

    punched_at = epoch_ms_to_utc(body.punched_at)

    # Contract: punch-out closes the employee's most recent record whose punch_in is at
    # or before punched_at. A later punch-in is not a candidate; punching out mid-shift
    # (before that later record's punch_in) closes the earlier, still-open record instead.
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punched_at}},
        sort=[("punch_in", -1)],
    )
    if record is None:
        # No punch-in at or before punched_at. If the only records are in the future and
        # still open, this is the documented "punched_at <= punch_in" case, not a miss.
        future_open = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$gt": punched_at}, "punch_out": None},
            sort=[("punch_in", 1)],
        )
        if future_open is not None:
            raise HTTPException(422, "Punch-out must be after punch-in")
        raise HTTPException(404, "No punch-in record found for this employee")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "Attendance record already punched out")

    elapsed = (punched_at - record["punch_in"]).total_seconds()
    if elapsed <= 0:
        raise HTTPException(422, "Punch-out must be after punch-in")
    if elapsed > 24 * 60 * 60:
        raise HTTPException(422, "Punch-out cannot be more than 24 hours after punch-in")

    work_hours = compute_work_hours(record["punch_in"], punched_at)
    overtime_minutes = compute_overtime_minutes(
        punched_at, emp["shift_end"], record["date"], emp["shift_start"]
    )
    half_day = work_hours < 4.50

    # Atomic: only one of N simultaneous punch-outs can match "punch_out is still null".
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": punched_at,
            "work_hours": work_hours,
            "overtime_minutes": overtime_minutes,
            "half_day": half_day,
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "Attendance record already punched out")
    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")

    query = build_attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    cursor = (
        db.attendance_logs.find(query)
        .sort([("date", -1), ("emp_code", 1)])  # DB-side sort + pagination (100k-safe)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_attendance(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Regularization (PATCH)
# --------------------------------------------------------------------------- #
@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    body: RegularizeIn,
    record_date: date = Path(alias="date"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")
    date_str = record_date.isoformat()
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if record is None:
        raise HTTPException(404, "No attendance record for that date")

    current_history = record.get("history", [])
    cur_status = record.get("status")
    cur_pin = record.get("punch_in")
    cur_pout = record.get("punch_out")
    cur_work = record.get("work_hours")
    cur_late = record.get("late_minutes", 0)
    cur_ot = record.get("overtime_minutes", 0)
    cur_half = record.get("half_day", False)

    new_status = body.status.value if body.status is not None else cur_status
    provided_pin = epoch_ms_to_utc(body.punch_in) if body.punch_in is not None else None
    provided_pout = epoch_ms_to_utc(body.punch_out) if body.punch_out is not None else None

    if new_status in ("ABSENT", "LEAVE"):
        if provided_pin is not None or provided_pout is not None:
            raise HTTPException(422, "ABSENT/LEAVE records cannot have punch times")
        new_pin, new_pout = None, None
        new_late, new_work, new_ot, new_half = 0, None, 0, False
    else:
        new_pin = provided_pin if body.punch_in is not None else cur_pin
        if new_pin is None:
            raise HTTPException(422, "A presence status requires a punch_in")
        new_pout = provided_pout if body.punch_out is not None else cur_pout

        if provided_pin is not None:
            expected = attendance_date_ist(new_pin, emp["shift_start"], emp["shift_end"])
            if expected != date_str:
                raise HTTPException(422, "punch_in must fall on the record's attendance date")
        if new_pout is not None:
            delta = (new_pout - new_pin).total_seconds()
            if delta <= 0:
                raise HTTPException(422, "punch_out must be after punch_in")
            if delta > 24 * 60 * 60:
                raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

        new_late = compute_late_minutes(new_pin, emp["shift_start"], emp["shift_end"])
        if new_pout is not None:
            new_work = compute_work_hours(new_pin, new_pout)
            new_ot = compute_overtime_minutes(new_pout, emp["shift_end"], date_str, emp["shift_start"])
            new_half = new_work < 4.50
        else:
            new_work, new_ot, new_half = None, 0, False

    changes: dict = {}

    def record_change(field, old, new):
        if old != new:
            changes[field] = {"from": old, "to": new}

    record_change("status", cur_status, new_status)
    record_change("punch_in", cur_pin, new_pin)
    record_change("punch_out", cur_pout, new_pout)
    record_change("work_hours", cur_work, new_work)
    record_change("late_minutes", cur_late, new_late)
    record_change("overtime_minutes", cur_ot, new_ot)
    record_change("half_day", cur_half, new_half)

    if not changes:
        raise HTTPException(422, "The request changes nothing")

    history_entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic lock: only apply if no other PATCH appended to history in the meantime.
    guard = {
        "_id": record["_id"],
        "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(current_history)]},
    }
    updated = db.attendance_logs.find_one_and_update(
        guard,
        {
            "$set": {
                "status": new_status,
                "punch_in": new_pin,
                "punch_out": new_pout,
                "work_hours": new_work,
                "late_minutes": new_late,
                "overtime_minutes": new_ot,
                "half_day": new_half,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "Attendance record was modified concurrently")
    return serialize_attendance(updated)


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")

    rows = list(db.attendance_logs.aggregate(build_monthly_pipeline(emp_code, month)))
    summary = rows[0] if rows else {
        "present_days": 0, "leave_days": 0, "late_count": 0,
        "total_late_minutes": 0, "total_overtime_minutes": 0,
    }

    working_days = working_days_for_month(emp["joined_on"], month)
    present_days = summary["present_days"]
    if working_days == 0:
        attendance_pct = None
    else:
        attendance_pct = float(
            (Decimal(present_days) * 100 / Decimal(working_days)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": summary["leave_days"],
        "late_count": summary["late_count"],
        "total_late_minutes": summary["total_late_minutes"],
        "total_overtime_minutes": summary["total_overtime_minutes"],
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=MONTH_PATTERN),
    department: Optional[str] = None,
):
    rows = list(db.employees.aggregate(build_department_summary_pipeline(month, department)))
    items = []
    for row in rows:
        if row["headcount"] == 0:
            continue
        if row["work_count"] > 0:
            avg_work_hours = float(
                (Decimal(row["work_sum"]) / Decimal(row["work_count"])).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            )
        else:
            avg_work_hours = None
        items.append({
            "department": row["_id"],
            "headcount": row["headcount"],
            "present_days": row["present_days"],
            "avg_work_hours": avg_work_hours,
            "late_count": row["late_count"],
            "total_late_minutes": row["total_late_minutes"],
            "leave_count": row["leave_count"],
            "on_duty_count": row["on_duty_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    rows = list(db.attendance_logs.aggregate(build_leaderboard_pipeline(month, limit, department)))
    items = [{
        "rank": row["rank"],
        "emp_code": row["emp_code"],
        "name": row["name"],
        "department": row["department"],
        "total_late_minutes": row["total_late_minutes"],
        "late_count": row["late_count"],
    } for row in rows]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: date = Query(alias="from"),
    to: date = Query(...),
):
    if to < from_:
        raise HTTPException(422, "'to' must not be before 'from'")
    if (to - from_).days + 1 > 92:
        raise HTTPException(422, "The date range must not exceed 92 days")
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "Unknown department")

    rows = list(db.employees.aggregate(build_trend_pipeline(department, from_, to)))
    return {"department": department, "items": rows}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
def _jsonify_explain(raw: dict) -> dict:
    """Return a MongoDB explain reply as an ordinary JSON-compatible dict.

    Replica-set/Atlas replies may carry BSON-only metadata (``operationTime`` and
    ``$clusterTime``, which hold ``Timestamp``/``Binary`` values) that FastAPI's
    encoder cannot serialize. ``json_util`` renders those as Extended JSON so the
    result is a plain dict, while the plan and execution statistics are preserved.
    """
    return json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))


def _explain_find(collection: str, filt: dict, sort: dict, skip: int, limit: int) -> dict:
    raw = db.command(
        "explain",
        {"find": collection, "filter": filt, "sort": sort, "skip": skip, "limit": limit},
        verbosity="executionStats",
    )
    return _jsonify_explain(raw)


def _explain_aggregate(collection: str, pipeline: list) -> dict:
    raw = db.command(
        "explain",
        {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
        verbosity="executionStats",
    )
    return _jsonify_explain(raw)


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Status] = None,
    from_: Optional[date] = Query(None, alias="from"),
    to: Optional[date] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Explain the main query/pipeline of an endpoint, built exactly as that endpoint builds it."""
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must not be after date_to")
        filt = build_attendance_filter(emp_code, date_from, date_to, status)
        explain = _explain_find(
            "attendance_logs", filt, {"date": -1, "emp_code": 1},
            (page - 1) * page_size, page_size,
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        explain = _explain_aggregate("attendance_logs", build_monthly_pipeline(emp_code, month))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        explain = _explain_aggregate(
            "employees", build_department_summary_pipeline(month, department)
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": explain}

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        explain = _explain_aggregate(
            "attendance_logs", build_leaderboard_pipeline(month, limit, department)
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "department_trend":
        if not department or from_ is None or to is None:
            raise HTTPException(422, "department, from and to are required")
        if to < from_:
            raise HTTPException(422, "'to' must not be before 'from'")
        if (to - from_).days + 1 > 92:
            raise HTTPException(422, "The date range must not exceed 92 days")
        explain = _explain_aggregate(
            "employees", build_trend_pipeline(department, from_, to)
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": explain}

    raise HTTPException(422, "Unknown endpoint")





