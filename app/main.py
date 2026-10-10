"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded for convenience;
       real environment variables take precedence via load_dotenv's
       override=False default)

Original starter written by a colleague who has left; bugs documented in
REVIEW.md.  All application code lives in this single file per the
assignment contract.
"""
# ---------------------------------------------------------------------------
# Standard-library imports
# ---------------------------------------------------------------------------
import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Annotated, Any, Optional

# ---------------------------------------------------------------------------
# Third-party imports
# ---------------------------------------------------------------------------
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import (
    BaseModel,
    Field,
    field_validator,
    model_validator,
)
from pydantic.functional_validators import AfterValidator, BeforeValidator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

# ---------------------------------------------------------------------------
# Environment  (real env vars win; .env fills missing ones)
# ---------------------------------------------------------------------------
load_dotenv()

_MONGO_URI: str = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
_MONGO_DB:  str = os.environ.get("MONGO_DB",  "attendance_db")

# ---------------------------------------------------------------------------
# MongoDB client  — tz_aware=True so PyMongo returns aware datetimes (UTC)
# ---------------------------------------------------------------------------
client = MongoClient(_MONGO_URI, tz_aware=True)
db     = client[_MONGO_DB]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
IST = timezone(timedelta(hours=5, minutes=30))   # UTC+05:30

_EPOCH_MS_MIN: int = 100_000_000_000    # 2001-03-03 — rejects seconds-range values
_EPOCH_MS_MAX: int = 4_102_444_800_000  # 2100-01-01

_HH_MM_PATTERN: re.Pattern = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')
_DATE_PATTERN:  re.Pattern = re.compile(r'^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$')

# ---------------------------------------------------------------------------
# Phase 0 helpers
# ---------------------------------------------------------------------------

def to_ms(dt: datetime) -> int:
    """Convert an aware datetime to integer epoch milliseconds."""
    return int(dt.timestamp() * 1000)


def from_ms(ms: int) -> datetime:
    """Convert integer epoch milliseconds to a UTC-aware datetime,
    truncated to whole seconds (R1)."""
    # Truncate to whole seconds before returning
    ts = ms // 1000
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def half_up(value: float | int | Decimal | str, places: int) -> float:
    """Round *value* to *places* decimal places using ROUND_HALF_UP.

    Python's built-in round() uses banker's rounding (ROUND_HALF_EVEN),
    which violates R4 / R8.  This helper always rounds 0.5 away from zero.

    Examples (2 places):
        half_up(9.125, 2) -> 9.13   (not 9.12 from banker's rounding)
        half_up(4.445, 2) -> 4.45   (not 4.44 from banker's rounding)
    """
    quantizer = Decimal(10) ** -places
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return float(d.quantize(quantizer, rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------
# Index creation  (idempotent; called once at startup)
# ---------------------------------------------------------------------------

def _create_indexes() -> None:
    """Create all required indexes.  PyMongo ignores create_index calls when
    an identical index already exists, so this is safe to call on every
    startup.

    Index inventory
    ---------------
    employees collection
      IX1  emp_code  unique       – uniqueness guarantee + lookup by emp_code
      IX2  department              – GET /employees?department=X, analytics join
      IX3  joined_on               – analytics headcount filter

    attendance_logs collection
      IX4  (emp_code, date) unique – one record per (employee, day); race-safe
                                    punch-in deduplication
      IX5  (date DESC, emp_code ASC)
                                   – GET /attendance sort (mixed directions
                                     require this exact index; IX4 cannot serve
                                     a mixed-direction sort)
      IX6  (emp_code, date, status) – analytics filters (monthly, leaderboard,
                                     summary); also covers punch-out lookup
    """
    # --- employees ---
    db.employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="IX1_emp_code_unique",
    )
    db.employees.create_index(
        [("department", ASCENDING)],
        name="IX2_department",
    )
    db.employees.create_index(
        [("joined_on", ASCENDING)],
        name="IX3_joined_on",
    )

    # --- attendance_logs ---
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="IX4_emp_code_date_unique",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="IX5_date_desc_emp_code_asc",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)],
        name="IX6_emp_code_date_status",
    )


# ---------------------------------------------------------------------------
# FastAPI application lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(application: FastAPI):
    """Startup: create indexes idempotently.  Shutdown: close the client."""
    _create_indexes()
    yield
    client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Phase 2: Pure business-rule helpers (R1 - R10)
# ---------------------------------------------------------------------------

def to_ist(dt: datetime | int) -> datetime:
    """Convert an instant to Asia/Kolkata (IST = UTC+05:30) with sub-seconds truncated (R1).

    Accepts an aware datetime, naive datetime (assumed UTC), or epoch milliseconds integer.
    Always returns a timezone-aware datetime in IST with microsecond=0.
    """
    if isinstance(dt, int):
        dt = from_ms(dt)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).replace(microsecond=0)


def compute_attendance_date(
    punch_in: datetime | int,
    shift_start: str,
    shift_end: str,
) -> str:
    """Calculate the attendance date (YYYY-MM-DD) according to R1.

    - Shift times and attendance dates are IST (UTC+05:30).
    - Normally, a record's date is the IST calendar date of its punch_in instant.
    - Overnight shift exception (shift_end <= shift_start, e.g. 22:00-06:00):
      A punch-in whose IST time is strictly earlier than shift_end belongs to the
      shift that started the previous calendar day.
    - At exactly shift_end, use the current IST calendar date.
    - Before calculation, sub-seconds are truncated.
    """
    punch_ist = to_ist(punch_in)
    punch_time = punch_ist.time()

    end_h, end_m = map(int, shift_end.split(":"))
    start_h, start_m = map(int, shift_start.split(":"))
    end_time = datetime(2000, 1, 1, end_h, end_m, 0).time()
    start_time = datetime(2000, 1, 1, start_h, start_m, 0).time()

    is_overnight = end_time <= start_time
    if is_overnight and punch_time < end_time:
        att_date = punch_ist.date() - timedelta(days=1)
    else:
        att_date = punch_ist.date()

    return att_date.isoformat()

attendance_date = compute_attendance_date


def compute_late_minutes(
    punch_in: datetime | int,
    shift_start: str,
    attendance_date: Optional[str] = None,
) -> int:
    """Calculate late minutes according to R2.

    - Grace period is 10 minutes.
    - Late only when punch-in is strictly more than 10 minutes (600s) after shift start.
    - late_minutes is whole-minute floor measured from shift start, not from grace end.
    - Example (shift 09:30):
        09:40:00 -> 0;
        09:40:01 -> 10;
        10:15:59 -> 45.
    - For overnight shifts, uses attendance_date if supplied, or detects previous-day start.
    """
    punch_ist = to_ist(punch_in)
    h, m = map(int, shift_start.split(":"))

    if attendance_date is not None:
        base_d = datetime.fromisoformat(attendance_date).date()
    else:
        base_d = punch_ist.date()

    start_dt = datetime(base_d.year, base_d.month, base_d.day, h, m, 0, tzinfo=IST)

    # Inferred overnight shift if punch_in is early morning and shift started evening before
    if attendance_date is None and (start_dt - punch_ist) > timedelta(hours=12):
        start_dt -= timedelta(days=1)

    delta_seconds = int((punch_ist - start_dt).total_seconds())
    if delta_seconds <= 600:
        return 0
    return delta_seconds // 60


def compute_overtime(
    punch_out: datetime | int,
    shift_end: str,
    date_str: str,
    shift_start_or_overnight: Optional[str | bool] = None,
    shift_start: Optional[str] = None,
    is_overnight: Optional[bool] = None,
) -> int:
    """Calculate overtime minutes according to R3.

    - Whole minutes (floor) between shift_end and punch_out.
    - Recorded only when the duration after shift_end is at least 30 minutes (>= 30).
    - Otherwise 0.
    - For overnight shifts, shift_end falls on the following calendar day (date_str + 1 day).
    """
    punch_ist = to_ist(punch_out)
    h, m = map(int, shift_end.split(":"))
    base_d = datetime.fromisoformat(date_str).date()

    overnight = False
    if isinstance(shift_start_or_overnight, bool):
        overnight = shift_start_or_overnight
    elif isinstance(shift_start_or_overnight, str):
        overnight = (shift_end <= shift_start_or_overnight)
    elif is_overnight is not None:
        overnight = is_overnight
    elif shift_start is not None:
        overnight = (shift_end <= shift_start)
    elif punch_ist.date() > base_d:
        overnight = True

    if overnight:
        end_d = base_d + timedelta(days=1)
    else:
        end_d = base_d

    end_dt = datetime(end_d.year, end_d.month, end_d.day, h, m, 0, tzinfo=IST)
    delta_seconds = int((punch_ist - end_dt).total_seconds())
    delta_minutes = delta_seconds // 60

    if delta_minutes >= 30:
        return delta_minutes
    return 0


def compute_work_hours(punch_in: datetime | int, punch_out: datetime | int) -> float:
    """Calculate elapsed duration from punch_in to punch_out in hours (R4).

    - Instants are truncated to whole seconds.
    - Rounded to 2 decimal places using Decimal with ROUND_HALF_UP.
    - Returns 0.0 if punch_out <= punch_in.
    """
    if isinstance(punch_in, int):
        punch_in = from_ms(punch_in)
    else:
        punch_in = punch_in.replace(microsecond=0)

    if isinstance(punch_out, int):
        punch_out = from_ms(punch_out)
    else:
        punch_out = punch_out.replace(microsecond=0)

    delta_sec = int((punch_out - punch_in).total_seconds())
    if delta_sec <= 0:
        return 0.0

    d_sec = Decimal(delta_sec)
    d_hours = d_sec / Decimal(3600)
    return half_up(d_hours, 2)


def compute_half_day(work_hours: Optional[float | Decimal]) -> bool:
    """R5: half_day = True only when contract-defined, rounded work_hours is strictly < 4.50.

    Returns False if work_hours is None (e.g. before punch-out or absent/leave).
    """
    if work_hours is None:
        return False
    return float(work_hours) < 4.50

is_half_day = compute_half_day


PRESENCE_STATUSES: frozenset[str] = frozenset({"PRESENT", "WFH", "ON_DUTY"})
NON_PRESENCE_STATUSES: frozenset[str] = frozenset({"ABSENT", "LEAVE"})


def is_present(status: str) -> bool:
    """R6: Return True if status counts as present (PRESENT, WFH, ON_DUTY).

    Returns False for ABSENT, LEAVE, or any other status.
    """
    return status in PRESENCE_STATUSES

is_present_status = is_present


def presence_day_weight(status: str, half_day: bool = False) -> float:
    """Calculate presence day contribution (0.0, 0.5, or 1.0) under R5 and R6."""
    if not is_present(status):
        return 0.0
    return 0.5 if half_day else 1.0


def is_working_day(date_val: str | date | datetime) -> bool:
    """R7: Return True if date is Monday through Friday. No holiday calendar."""
    if isinstance(date_val, str):
        d = datetime.strptime(date_val[:10], "%Y-%m-%d").date()
    elif isinstance(date_val, datetime):
        d = date_val.date()
    else:
        d = date_val
    return d.weekday() < 5


def count_working_days(
    start_date: str | date,
    end_date: str | date,
    joined_on: Optional[str | date] = None,
) -> int:
    """Count Monday-Friday working days between start_date and end_date inclusive (R7).

    - Monday through Friday only; no holiday calendar.
    - If joined_on is provided, working days are counted from max(start_date, joined_on).
    - If joined_on > end_date, returns 0.
    """
    if isinstance(start_date, str):
        start_d = datetime.strptime(start_date[:10], "%Y-%m-%d").date()
    else:
        start_d = start_date

    if isinstance(end_date, str):
        end_d = datetime.strptime(end_date[:10], "%Y-%m-%d").date()
    else:
        end_d = end_date

    if joined_on is not None:
        if isinstance(joined_on, str):
            join_d = datetime.strptime(joined_on[:10], "%Y-%m-%d").date()
        else:
            join_d = joined_on
        if join_d > end_d:
            return 0
        if join_d > start_d:
            start_d = join_d

    if start_d > end_d:
        return 0

    count = 0
    cur = start_d
    one_day = timedelta(days=1)
    while cur <= end_d:
        if cur.weekday() < 5:
            count += 1
        cur += one_day
    return count


def round_2dp(value: float | int | Decimal | str) -> float:
    """Round value to 2 decimal places using ROUND_HALF_UP (R8)."""
    return half_up(value, 2)


def round_4dp(value: float | int | Decimal | str) -> float:
    """Round fractional rate to 4 decimal places using ROUND_HALF_UP (R8)."""
    return half_up(value, 4)


def compute_attendance_pct(
    present_days: float | int,
    working_days: int,
) -> Optional[float]:
    """Calculate attendance percentage (present_days / working_days * 100).

    Rounded to 2 decimal places half-up (R8). Returns None if working_days <= 0.
    """
    if working_days <= 0:
        return None
    d_present = Decimal(str(present_days))
    d_working = Decimal(str(working_days))
    pct = (d_present / d_working) * Decimal(100)
    return half_up(pct, 2)


def compute_attendance_rate(
    present_count: float | int,
    headcount: int,
) -> Optional[float]:
    """Calculate fractional attendance rate (present_count / headcount).

    Rounded to 4 decimal places half-up (R8). Returns None if headcount <= 0.
    """
    if headcount <= 0:
        return None
    d_present = Decimal(str(present_count))
    d_headcount = Decimal(str(headcount))
    rate = d_present / d_headcount
    return half_up(rate, 4)


def compute_moving_avg_7d(rates_window: list[Optional[float | Decimal]]) -> Optional[float]:
    """Calculate 7-day moving average over non-null attendance rates in window.

    - Window contains up to 7 items (current and up to 6 preceding inside requested range).
    - Takes the mean of non-null rates.
    - Returns None if window contains no non-null rate.
    - Rounded to 4 decimal places half-up (R8).
    """
    valid = [Decimal(str(r)) for r in rates_window if r is not None]
    if not valid:
        return None
    mean = sum(valid) / Decimal(len(valid))
    return round_4dp(mean)


def is_employee_eligible(
    joined_on: str | date,
    cutoff_date: str | date,
) -> bool:
    """Check if employee is eligible for headcount for a period/date (R9).

    Eligible when joined_on is on or before cutoff_date.
    cutoff_date can be YYYY-MM-DD, a date object, or YYYY-MM (calendar month).
    """
    if isinstance(joined_on, str):
        j_date = joined_on[:10]
    else:
        j_date = joined_on.isoformat()

    if isinstance(cutoff_date, str):
        if len(cutoff_date) == 7:  # YYYY-MM
            y, m = map(int, cutoff_date.split("-"))
            last_day = calendar.monthrange(y, m)[1]
            c_date = f"{cutoff_date}-{last_day:02d}"
        else:
            c_date = cutoff_date[:10]
    else:
        c_date = cutoff_date.isoformat()

    return j_date <= c_date


def pagination_skip(page: int, page_size: int) -> int:
    """Calculate MongoDB skip offset using (page - 1) * page_size (R10)."""
    return (page - 1) * page_size

get_pagination_skip = pagination_skip
get_pagination_offset = pagination_skip


# ---------------------------------------------------------------------------
# Phase 1: Pydantic types and validators
# ---------------------------------------------------------------------------

def _strict_epoch_ms(v: Any) -> int:
    """Strict validator for EpochMillis.

    Rejects:
    - booleans  (bool is a subclass of int in Python; must check first)
    - floats
    - strings
    - integers outside [100_000_000_000, 4_102_444_800_000]

    Returns the integer unchanged when valid.
    """
    if isinstance(v, bool):
        raise ValueError(
            "epoch milliseconds must be an integer, not a boolean"
        )
    if not isinstance(v, int):
        raise ValueError(
            f"epoch milliseconds must be a strict integer "
            f"(got {type(v).__name__!r}); floats and strings are rejected"
        )
    if v < _EPOCH_MS_MIN or v > _EPOCH_MS_MAX:
        raise ValueError(
            f"epoch milliseconds must be between {_EPOCH_MS_MIN} and "
            f"{_EPOCH_MS_MAX} (got {v}); values below {_EPOCH_MS_MIN} look "
            f"like seconds, not milliseconds"
        )
    return v


# Annotated type: use as a field type for any epoch-ms input
EpochMillisType = Annotated[int, BeforeValidator(_strict_epoch_ms)]


def _validate_hhmm(v: str) -> str:
    """Validate that a string matches HH:MM in 24-hour format."""
    if not _HH_MM_PATTERN.match(v):
        raise ValueError(
            f"shift time must be HH:MM in 24-hour format (e.g. 09:30), got {v!r}"
        )
    return v


def _validate_date_str(v: str) -> str:
    """Validate that a string is a calendar date in YYYY-MM-DD format."""
    if not isinstance(v, str) or not _DATE_PATTERN.match(v):
        raise ValueError(
            f"date must be a valid YYYY-MM-DD string, got {v!r}"
        )
    # Further validate that the day exists (rejects 2026-02-30 etc.)
    try:
        datetime.strptime(v, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"date {v!r} is not a valid calendar date")
    return v


# ---------------------------------------------------------------------------
# Phase 1: Request models
# ---------------------------------------------------------------------------

class EmployeeIn(BaseModel):
    """Request body for POST /employees.

    Fixes D4 (shift equality) and D5 (emp_code pattern).
    """
    emp_code:    str = Field(pattern=r'^EMP\d{4,6}$')
    name:        str = Field(min_length=1, max_length=100)
    email:       str = Field(max_length=120, pattern=r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
    department:  str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30")
    shift_end:   str = Field(default="18:30")
    joined_on:   str  # validated below

    @field_validator("shift_start", "shift_end", mode="before")
    @classmethod
    def _validate_shift_time(cls, v: Any) -> str:
        if not isinstance(v, str):
            raise ValueError("shift time must be a string")
        return _validate_hhmm(v)

    @field_validator("joined_on", mode="before")
    @classmethod
    def _validate_joined_on(cls, v: Any) -> str:
        if not isinstance(v, str):
            raise ValueError("joined_on must be a string")
        return _validate_date_str(v)

    @model_validator(mode="after")
    def _shift_times_must_differ(self) -> "EmployeeIn":
        """D4: shift_start and shift_end must not be equal (contract: 'must differ')."""
        if self.shift_start == self.shift_end:
            raise ValueError(
                "shift_start and shift_end must differ; equal times are not allowed"
            )
        return self


class PunchInIn(BaseModel):
    """Request body for POST /attendance/punch-in.

    Fixes D14 (EpochMillis validation) and D15 (PresenceStatus enum).
    """
    emp_code:   str
    punched_at: Optional[EpochMillisType] = None
    status:     str = "PRESENT"

    @field_validator("status", mode="before")
    @classmethod
    def _validate_presence_status(cls, v: Any) -> str:
        allowed = {"PRESENT", "WFH", "ON_DUTY"}
        if v not in allowed:
            raise ValueError(
                f"status must be one of {sorted(allowed)} for punch-in "
                f"(ABSENT and LEAVE cannot be created via punch-in); got {v!r}"
            )
        return v


class PunchOutIn(BaseModel):
    """Request body for POST /attendance/punch-out (Phase 4 endpoint)."""
    emp_code:   str
    punched_at: Optional[EpochMillisType] = None


class RegularizeIn(BaseModel):
    """Request body for PATCH /attendance/{emp_code}/{date} (Phase 5 endpoint).

    Only the fields listed here can be sent; derived fields are read-only.
    """
    status:         Optional[str] = None
    punch_in:       Optional[EpochMillisType] = None
    punch_out:      Optional[EpochMillisType] = None
    reason:         str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status", mode="before")
    @classmethod
    def _validate_full_status(cls, v: Any) -> Optional[str]:
        if v is None:
            return v
        allowed = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}
        if v not in allowed:
            raise ValueError(
                f"status must be one of {sorted(allowed)}, got {v!r}"
            )
        return v


class EmployeeMonthly(BaseModel):
    """Response model for GET /analytics/employees/{emp_code}/monthly (Phase 6)."""
    emp_code:               str
    month:                  str
    working_days:           int
    present_days:           float
    leave_days:             int
    late_count:             int
    total_late_minutes:     int
    total_overtime_minutes: int
    attendance_pct:         Optional[float] = None


class DepartmentSummaryItem(BaseModel):
    """One department item in DepartmentSummary (Phase 7)."""
    department:         str
    headcount:          int
    present_days:       float
    avg_work_hours:     Optional[float] = None
    late_count:         int
    total_late_minutes: int
    leave_count:        int
    on_duty_count:      int


class DepartmentSummary(BaseModel):
    """Response model for GET /analytics/departments/summary (Phase 7)."""
    month: str
    items: list[DepartmentSummaryItem]


class LeaderboardItem(BaseModel):
    """One ranked employee in Leaderboard (Phase 8)."""
    rank:               int = Field(ge=1)
    emp_code:           str
    name:               str
    department:         str
    total_late_minutes: int
    late_count:         int


class Leaderboard(BaseModel):
    """Response model for GET /analytics/leaderboard/late (Phase 8)."""
    month: str
    items: list[LeaderboardItem]


class TrendItem(BaseModel):
    """One day's attendance trend record in Trend (Phase 9)."""
    date:            str
    is_working_day:  bool
    headcount:       int
    present_count:   float
    late_count:      int
    attendance_rate: Optional[float] = None
    moving_avg_7d:   Optional[float] = None


class Trend(BaseModel):
    """Response model for GET /analytics/departments/{department}/trend (Phase 9)."""
    department: str
    items:      list[TrendItem]


class ExplainResponse(BaseModel):
    """Response model for GET /admin/explain/{endpoint} (Phase 10)."""
    endpoint:   str
    collection: str
    explain:    dict[str, Any]


# ---------------------------------------------------------------------------
# Phase 1: Response serialization helpers
# ---------------------------------------------------------------------------

def _ms_or_none(dt: Optional[datetime]) -> Optional[int]:
    """Serialize a nullable BSON datetime to epoch milliseconds (or None)."""
    if dt is None:
        return None
    return to_ms(dt)


def _serialize_history_entry(entry: dict) -> dict:
    """Convert BSON dates inside a history entry to epoch milliseconds.

    history[].at  is a BSON date → int
    history[].changes.punch_in.from / .to  are BSON dates → int (or None)
    history[].changes.punch_out.from / .to are BSON dates → int (or None)
    """
    out: dict = {
        "at":     to_ms(entry["at"]),
        "by":     entry["by"],
        "reason": entry["reason"],
        "changes": {},
    }
    for field, change in entry.get("changes", {}).items():
        if field in ("punch_in", "punch_out"):
            out["changes"][field] = {
                "from": _ms_or_none(change.get("from")),
                "to":   _ms_or_none(change.get("to")),
            }
        else:
            out["changes"][field] = {
                "from": change.get("from"),
                "to":   change.get("to"),
            }
    return out


def _serialize_employee(doc: dict) -> dict:
    """Convert a MongoDB employee document to the API Employee response shape.

    - Drops _id (internal)
    - Converts created_at datetime → epoch milliseconds integer
    """
    return {
        "emp_code":    doc["emp_code"],
        "name":        doc["name"],
        "email":       doc["email"],
        "department":  doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end":   doc["shift_end"],
        "joined_on":   doc["joined_on"],
        "created_at":  to_ms(doc["created_at"]),
    }


def _serialize_attendance(doc: dict) -> dict:
    """Convert a MongoDB attendance_logs document to the API AttendanceRecord shape.

    - Drops _id (internal); the record is addressed by (emp_code, date)
    - Converts punch_in / punch_out datetimes → epoch ms (or None)
    - Converts history[].at and nested datetime changes → epoch ms
    - Defaults missing legacy fields (half_day, history) per DATA_MODEL.md §9
    """
    return {
        "emp_code":          doc["emp_code"],
        "date":              doc["date"],
        "status":            doc["status"],
        "punch_in":          _ms_or_none(doc.get("punch_in")),
        "punch_out":         _ms_or_none(doc.get("punch_out")),
        "work_hours":        doc.get("work_hours"),
        "late_minutes":      doc.get("late_minutes", 0),
        "overtime_minutes":  doc.get("overtime_minutes", 0),
        "half_day":          doc.get("half_day", False),
        "history":           [
            _serialize_history_entry(e)
            for e in doc.get("history", [])
        ],
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    """Readiness probe.

    Returns 200 {\"status\": \"ok\"} only if MongoDB answers a ping.
    Returns 503 if the ping fails or raises any exception.

    Fix for D1: original starter returned 200 unconditionally.
    """
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    """Create a new employee record (POST /employees).

    - Server sets created_at as UTC BSON datetime.
    - IX1_emp_code_unique enforces emp_code uniqueness.
    - Catches DuplicateKeyError to safely handle concurrent requests, returning 409.
    - Returns serialized Employee with epoch ms created_at and no _id.
    """
    doc = body.model_dump()
    doc["created_at"] = datetime.now(tz=timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return _serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List employees sorted by emp_code ascending (GET /employees).

    - Supports optional case-sensitive department filter.
    - 1-based pagination with page_size bounded to [1, 100].
    - total reflects the filtered count, not the entire collection.
    - Executes sort, skip, and limit directly on MongoDB cursor.
    """
    q: dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    skip = pagination_skip(page, page_size)
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [_serialize_employee(d) for d in cursor]

    return {
        "items":     items,
        "total":     total,
        "page":      page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    """Record employee punch-in (POST /attendance/punch-in).

    - Validates employee exists (404 if not found).
    - Uses supplied punched_at (or server UTC now), truncated to whole seconds.
    - Computes attendance date via compute_attendance_date (R1).
    - Computes late_minutes via compute_late_minutes (R2).
    - Enforces IX4_emp_code_date_unique: catches DuplicateKeyError -> 409.
    - Returns serialized AttendanceRecord (epoch ms, no _id).
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")

    if body.punched_at is not None:
        ts = from_ms(body.punched_at)
    else:
        ts = datetime.now(tz=timezone.utc).replace(microsecond=0)

    d = compute_attendance_date(ts, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(ts, emp["shift_start"], attendance_date=d)

    doc = {
        "emp_code":         body.emp_code,
        "date":             d,
        "status":           body.status,
        "punch_in":         ts,
        "punch_out":        None,
        "work_hours":       None,
        "late_minutes":     late_min,
        "overtime_minutes": 0,
        "half_day":         False,
        "history":          [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return _serialize_attendance(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    """Record employee punch-out (POST /attendance/punch-out).

    - Validates employee exists (404 if not found).
    - Uses supplied punched_at (or server UTC now), truncated to whole seconds.
    - Finds most recent attendance record for the employee (404 if none).
    - Validates punched_at > punch_in and duration <= 24h (422 otherwise).
    - If record already punched out, returns 409.
    - Computes work_hours (R4), half_day (R5), overtime_minutes (R3).
    - Concurrency-safe atomic conditional update (find_one_and_update).
    - Returns serialized AttendanceRecord (epoch ms, no _id).
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")

    if body.punched_at is not None:
        ts = from_ms(body.punched_at)
    else:
        ts = datetime.now(tz=timezone.utc).replace(microsecond=0)

    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code},
        sort=[("punch_in", DESCENDING)],
    )
    if not record or record.get("punch_in") is None:
        raise HTTPException(status_code=404, detail="no punch-in record found")

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    diff_seconds = (ts - punch_in_dt).total_seconds()
    if diff_seconds <= 0:
        raise HTTPException(
            status_code=422,
            detail="punched_at must be strictly greater than punch_in",
        )
    if diff_seconds > 86400:
        raise HTTPException(
            status_code=422,
            detail="punched_at cannot be more than 24 hours after punch_in",
        )

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="already punched out")

    work_hours = compute_work_hours(punch_in_dt, ts)
    half_day = compute_half_day(work_hours)
    overtime_min = compute_overtime(
        ts,
        emp["shift_end"],
        record["date"],
        shift_start=emp["shift_start"],
    )

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out":         ts,
                "work_hours":        work_hours,
                "overtime_minutes":  overtime_min,
                "half_day":          half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="already punched out")

    return _serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List attendance records with filtering and pagination (GET /attendance).

    - Sorted by date descending, then emp_code ascending (supported by IX5).
    - date_from and date_to are inclusive; date_from > date_to -> 422.
    - Executed via database cursor (find, sort, skip, limit, count_documents).
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from must be on or before date_to",
        )

    q: dict[str, Any] = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if status is not None:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    skip = pagination_skip(page, page_size)
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    items = [_serialize_attendance(d) for d in cursor]

    return {
        "items":     items,
        "total":     total,
        "page":      page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeIn,
):
    """Regularize (manually correct) one day's attendance record.

    - Validates employee exists (404).
    - Validates record exists for (emp_code, date) (404).
    - Setting ABSENT or LEAVE clears both punch times; supplying punch_in or punch_out is 422.
    - Presence status requires a punch_in (supplied or already stored) (422).
    - punch_in must stay on record's attendance date (R1) (422).
    - punch_out must be strictly after punch_in and duration <= 24h (422).
    - Recomputes late_minutes (R2), work_hours (R4), half_day (R5), overtime_minutes (R3).
    - Request that changes nothing is 422.
    - Concurrency-safe atomic compare-and-set on history array size (409 on conflict).
    - Appends audit-history entry preserving existing history.
    - Returns serialized AttendanceRecord (epoch ms, no _id).
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="attendance record not found")

    orig_status = record["status"]
    orig_punch_in = record.get("punch_in")
    orig_punch_out = record.get("punch_out")
    orig_work_hours = record.get("work_hours")
    orig_late_minutes = record.get("late_minutes", 0)
    orig_overtime_minutes = record.get("overtime_minutes", 0)
    orig_half_day = record.get("half_day", False)
    orig_history = record.get("history") or []

    new_status = body.status if body.status is not None else orig_status

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="cannot supply punch_in or punch_out when setting status to ABSENT or LEAVE",
            )
        new_punch_in = None
        new_punch_out = None
    else:
        new_punch_in = from_ms(body.punch_in) if body.punch_in is not None else orig_punch_in
        new_punch_out = from_ms(body.punch_out) if body.punch_out is not None else orig_punch_out

        if new_punch_in is None:
            raise HTTPException(
                status_code=422,
                detail="presence status requires a punch_in",
            )

        calc_date = compute_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in falls on attendance date {calc_date}, must remain on {date}",
            )

        if new_punch_out is not None:
            p_in = new_punch_in if new_punch_in.tzinfo else new_punch_in.replace(tzinfo=timezone.utc)
            p_out = new_punch_out if new_punch_out.tzinfo else new_punch_out.replace(tzinfo=timezone.utc)
            diff_sec = (p_out - p_in).total_seconds()
            if diff_sec <= 0:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be strictly after punch_in",
                )
            if diff_sec > 86400:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out cannot be more than 24 hours after punch_in",
                )

    # Recompute derived fields
    if new_status in ("ABSENT", "LEAVE"):
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], attendance_date=date)
        if new_punch_out is not None:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_half_day = compute_half_day(new_work_hours)
            new_overtime_minutes = compute_overtime(
                new_punch_out,
                emp["shift_end"],
                date,
                shift_start=emp["shift_start"],
            )
        else:
            new_work_hours = None
            new_half_day = False
            new_overtime_minutes = 0

    # Build changes dictionary
    changes: dict[str, dict[str, Any]] = {}
    if new_status != orig_status:
        changes["status"] = {"from": orig_status, "to": new_status}
    if new_punch_in != orig_punch_in:
        changes["punch_in"] = {"from": orig_punch_in, "to": new_punch_in}
    if new_punch_out != orig_punch_out:
        changes["punch_out"] = {"from": orig_punch_out, "to": new_punch_out}
    if new_late_minutes != orig_late_minutes:
        changes["late_minutes"] = {"from": orig_late_minutes, "to": new_late_minutes}
    if new_work_hours != orig_work_hours:
        changes["work_hours"] = {"from": orig_work_hours, "to": new_work_hours}
    if new_overtime_minutes != orig_overtime_minutes:
        changes["overtime_minutes"] = {"from": orig_overtime_minutes, "to": new_overtime_minutes}
    if new_half_day != orig_half_day:
        changes["half_day"] = {"from": orig_half_day, "to": new_half_day}

    if not changes:
        raise HTTPException(
            status_code=422,
            detail="request makes no changes to the attendance record",
        )

    now_utc = datetime.now(tz=timezone.utc).replace(microsecond=0)
    history_entry = {
        "at":      now_utc,
        "by":      body.regularized_by,
        "reason":  body.reason,
        "changes": changes,
    }

    # Compare-and-set filter to prevent silent lost updates during concurrent edits
    orig_history_len = len(orig_history)
    if orig_history_len == 0:
        filter_q = {
            "emp_code": emp_code,
            "date":     date,
            "$or":      [{"history": {"$size": 0}}, {"history": {"$exists": False}}],
        }
    else:
        filter_q = {
            "emp_code": emp_code,
            "date":     date,
            "history":  {"$size": orig_history_len},
        }

    update_q = {
        "$set": {
            "status":            new_status,
            "punch_in":          new_punch_in,
            "punch_out":         new_punch_out,
            "work_hours":        new_work_hours,
            "late_minutes":      new_late_minutes,
            "overtime_minutes":  new_overtime_minutes,
            "half_day":          new_half_day,
        },
        "$push": {
            "history": history_entry,
        },
    }

    updated_doc = db.attendance_logs.find_one_and_update(
        filter_q,
        update_q,
        return_document=ReturnDocument.AFTER,
    )
    if not updated_doc:
        raise HTTPException(
            status_code=409,
            detail="concurrent update conflict; record has been modified",
        )

    return _serialize_attendance(updated_doc)


# ---------------------------------------------------------------------------
# Phase 6: Employee monthly analytics
# ---------------------------------------------------------------------------

def get_month_date_range(month: str) -> tuple[str, str]:
    """Return (date_from, date_to) for a 'YYYY-MM' month string."""
    year, month_num = map(int, month.split("-"))
    last_day = calendar.monthrange(year, month_num)[1]
    return f"{month}-01", f"{month}-{last_day:02d}"


def build_employee_monthly_pipeline(emp_code: str, month: str) -> list[dict[str, Any]]:
    """Build the MongoDB aggregation pipeline for employee monthly analytics.

    Stage 1 ($match):
      Filters records by emp_code and date within [YYYY-MM-01, YYYY-MM-last_day].
      Uses IX4 (emp_code, date) or IX6 (emp_code, date, status) for IXSCAN.

    Stage 2 ($project):
      - Determines whether date is a weekday (Mon-Fri) via $dayOfWeek on $dateFromString.
        In MongoDB $dayOfWeek: 1=Sun, 2=Mon, 3=Tue, 4=Wed, 5=Thu, 6=Fri, 7=Sat.
        Weekdays (Mon-Fri) are [2, 3, 4, 5, 6].
      - Checks if status is presence status (PRESENT, WFH, ON_DUTY).
      - Checks if status is LEAVE.
      - Defaults half_day (False), late_minutes (0), overtime_minutes (0).

    Stage 3 ($group):
      - present_days: $sum of 0.5 (half_day) or 1.0 (full day) for Mon-Fri presence records only (R7).
      - leave_days: $sum of LEAVE records.
      - late_count: $sum of records with late_minutes > 0 (includes weekend records per R7).
      - total_late_minutes: $sum of late_minutes (includes weekend records per R7).
      - total_overtime_minutes: $sum of overtime_minutes (includes weekend records per R7).
    """
    date_from, date_to = get_month_date_range(month)
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": date_from, "$lte": date_to},
            }
        },
        {
            "$project": {
                "is_weekday": {
                    "$in": [
                        {
                            "$dayOfWeek": {
                                "$dateFromString": {"dateString": "$date"}
                            }
                        },
                        [2, 3, 4, 5, 6],
                    ]
                },
                "is_present": {
                    "$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]
                },
                "is_leave": {"$eq": ["$status", "LEAVE"]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$is_weekday", "$is_present"]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": ["$is_leave", 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": "$late_minutes"
                },
                "total_overtime_minutes": {
                    "$sum": "$overtime_minutes"
                },
            }
        },
    ]


@app.get(
    "/analytics/employees/{emp_code}/monthly",
    response_model=EmployeeMonthly,
    tags=["analytics"],
    operation_id="employeeMonthly",
)
def employee_monthly_analytics(
    emp_code: str,
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
        description="Calendar month, YYYY-MM.",
    ),
):
    """Monthly attendance summary for one employee (GET /analytics/employees/{emp_code}/monthly).

    - Backed by MongoDB aggregation over attendance_logs.
    - working_days per R7 (Mon-Fri only, counted from max(first_day, joined_on)).
    - present_days per R5-R7 (half_day = 0.5, full day = 1.0, Mon-Fri only).
    - leave_days = number of LEAVE records.
    - late_count = records with late_minutes > 0 (includes weekend records).
    - total_late_minutes = sum of late_minutes (includes weekend records).
    - total_overtime_minutes = sum of overtime_minutes (includes weekend records).
    - attendance_pct = (present_days / working_days) * 100 with R8 half-up rounding, or null when working_days == 0.
    - 404 for unknown employee.
    - 422 for invalid month format.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")

    date_from, date_to = get_month_date_range(month)
    working_days = count_working_days(date_from, date_to, joined_on=emp.get("joined_on"))

    pipeline = build_employee_monthly_pipeline(emp_code, month)
    agg_results = list(db.attendance_logs.aggregate(pipeline))

    if agg_results:
        agg = agg_results[0]
        present_days = float(agg.get("present_days", 0.0) or 0.0)
        leave_days = int(agg.get("leave_days", 0) or 0)
        late_count = int(agg.get("late_count", 0) or 0)
        total_late_minutes = int(agg.get("total_late_minutes", 0) or 0)
        total_overtime_minutes = int(agg.get("total_overtime_minutes", 0) or 0)
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    attendance_pct = compute_attendance_pct(present_days, working_days)

    return {
        "emp_code":               emp_code,
        "month":                  month,
        "working_days":           working_days,
        "present_days":           round_2dp(present_days),
        "leave_days":             leave_days,
        "late_count":             late_count,
        "total_late_minutes":     total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct":         attendance_pct,
    }


# ---------------------------------------------------------------------------
# Phase 7: Department summary analytics
# ---------------------------------------------------------------------------

def build_department_summary_pipeline(
    month: str,
    department: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Build the MongoDB aggregation pipeline for department summary analytics.

    Stage 1 ($match on employees):
      Filters employees by joined_on <= date_to (R9: eligible headcount).
      If department filter is given, matches department exactly.
      Uses IX2_department or IX3_joined_on.

    Stage 2 ($group by department):
      Groups by department to calculate headcount ($sum: 1) and collect
      emp_codes ($push: $emp_code).
      Departments with headcount == 0 are naturally omitted per contract.

    Stage 3 ($lookup on attendance_logs):
      Correlated subpipeline joining attendance records for the department's
      eligible employees within [date_from, date_to].
      Computes present_days (Mon-Fri only per R7), work_hours sum and count (for mean),
      late_count and total_late_minutes (including weekends per R7),
      leave_count, and on_duty_count.

    Stage 4 & 5 ($project):
      Flattens stats and applies default zeros for departments with no logs.

    Stage 6 ($sort):
      Sorts by department ascending.
    """
    date_from, date_to = get_month_date_range(month)
    match_stage: dict[str, Any] = {"joined_on": {"$lte": date_to}}
    if department is not None:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"codes": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$codes"]},
                                    {"$gte": ["$date", date_from]},
                                    {"$lte": ["$date", date_to]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "is_weekday": {
                                "$in": [
                                    {
                                        "$dayOfWeek": {
                                            "$dateFromString": {"dateString": "$date"}
                                        }
                                    },
                                    [2, 3, 4, 5, 6],
                                ]
                            },
                            "is_present": {
                                "$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]
                            },
                            "is_leave": {"$eq": ["$status", "LEAVE"]},
                            "is_on_duty": {"$eq": ["$status", "ON_DUTY"]},
                            "half_day": {"$ifNull": ["$half_day", False]},
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                            "work_hours": 1,
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {
                                "$sum": {
                                    "$cond": [
                                        {"$and": ["$is_weekday", "$is_present"]},
                                        {"$cond": ["$half_day", 0.5, 1.0]},
                                        0.0,
                                    ]
                                }
                            },
                            "work_hours_sum": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                "$is_present",
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        "$work_hours",
                                        0.0,
                                    ]
                                }
                            },
                            "work_hours_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                "$is_present",
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                                }
                            },
                            "total_late_minutes": {"$sum": "$late_minutes"},
                            "leave_count": {
                                "$sum": {"$cond": ["$is_leave", 1, 0]}
                            },
                            "on_duty_count": {
                                "$sum": {"$cond": ["$is_on_duty", 1, 0]}
                            },
                        }
                    },
                ],
                "as": "stats",
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "stats": {"$arrayElemAt": ["$stats", 0]},
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": {"$ifNull": ["$stats.present_days", 0.0]},
                "late_count": {"$ifNull": ["$stats.late_count", 0]},
                "total_late_minutes": {"$ifNull": ["$stats.total_late_minutes", 0]},
                "leave_count": {"$ifNull": ["$stats.leave_count", 0]},
                "on_duty_count": {"$ifNull": ["$stats.on_duty_count", 0]},
                "work_hours_sum": {"$ifNull": ["$stats.work_hours_sum", 0.0]},
                "work_hours_count": {"$ifNull": ["$stats.work_hours_count", 0]},
            }
        },
        {"$sort": {"department": 1}},
    ]


@app.get(
    "/analytics/departments/summary",
    response_model=DepartmentSummary,
    tags=["analytics"],
    operation_id="departmentSummary",
)
def department_summary_analytics(
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
        description="Calendar month, YYYY-MM.",
    ),
    department: Optional[str] = Query(
        None,
        description="Restrict to one department (exact match).",
    ),
):
    """Department summary for a month (GET /analytics/departments/summary).

    - Sorted by department ascending.
    - headcount per R9 (people with joined_on <= month end, even with zero logs).
    - present_days summed across department (Mon-Fri only, half day = 0.5).
    - avg_work_hours = mean work_hours over records that are present-status and have work_hours (null if none, R8 half-up rounding).
    - late_count = records with late_minutes > 0 (includes weekend records).
    - total_late_minutes = sum of late_minutes (includes weekend records).
    - leave_count = LEAVE records.
    - on_duty_count = ON_DUTY records.
    - Departments with headcount == 0 are omitted.
    """
    pipeline = build_department_summary_pipeline(month, department)
    cursor = db.employees.aggregate(pipeline)
    items = []
    for doc in cursor:
        wh_count = int(doc.get("work_hours_count", 0))
        if wh_count > 0:
            wh_sum = Decimal(str(doc.get("work_hours_sum", 0.0)))
            avg_wh = half_up(wh_sum / Decimal(str(wh_count)), 2)
        else:
            avg_wh = None

        items.append({
            "department":         doc["department"],
            "headcount":          int(doc["headcount"]),
            "present_days":       round_2dp(doc["present_days"]),
            "avg_work_hours":     avg_wh,
            "late_count":         int(doc["late_count"]),
            "total_late_minutes": int(doc["total_late_minutes"]),
            "leave_count":        int(doc["leave_count"]),
            "on_duty_count":      int(doc["on_duty_count"]),
        })

    return {
        "month": month,
        "items": items,
    }


# ---------------------------------------------------------------------------
# Phase 8: Late leaderboard analytics
# ---------------------------------------------------------------------------

def build_late_leaderboard_pipeline(
    month: str,
    limit: int = 10,
    department: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Build the MongoDB aggregation pipeline for late leaderboard analytics.

    Stage 1 ($match on attendance_logs):
      Filters records by date within [YYYY-MM-01, YYYY-MM-last_day] and late_minutes > 0.
      Supported by IX5_date_emp_code index.

    Stage 2 ($group by emp_code):
      Aggregates total_late_minutes ($sum: late_minutes) and late_count ($sum: 1) per employee.

    Stage 3 ($lookup on employees):
      Joins employee record on emp_code to obtain name and department.
      Supported by IX1_emp_code_unique.

    Stage 4 ($unwind):
      Unwinds the joined employee record. Logs whose employee does not exist are ignored.

    Stage 5 (Optional $match on emp.department):
      If department filter is given, restricts to that department before ranking
      (ranking happens inside that department only).

    Stage 6 ($setWindowFields with $rank):
      Computes standard competition ranking (ties share rank, next rank is skipped)
      ordered by total_late_minutes descending.

    Stage 7 ($match on rank):
      Filters rank <= limit (all employees tied at cutoff are included).

    Stage 8 ($sort):
      Deterministic final ordering: total_late_minutes descending, then emp_code ascending.

    Stage 9 ($project):
      Outputs exact fields: rank, emp_code, name, department, total_late_minutes, late_count.
    """
    date_from, date_to = get_month_date_range(month)

    pipeline: list[dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": date_from, "$lte": date_to},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])

    return pipeline


@app.get(
    "/analytics/leaderboard/late",
    response_model=Leaderboard,
    tags=["analytics"],
    operation_id="lateLeaderboard",
)
def late_leaderboard(
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
        description="Calendar month, YYYY-MM.",
    ),
    limit: int = Query(
        10,
        ge=1,
        le=50,
        description="Maximum rank to include.",
    ),
    department: Optional[str] = Query(
        None,
        description="Restrict to one department.",
    ),
):
    """Late-comers leaderboard for a month (GET /analytics/leaderboard/late).

    - Standard competition ranking ($rank).
    - limit applies after ranking: returns every employee whose rank <= limit.
    - Ties at cutoff are preserved.
    - Ordered by total_late_minutes descending, then emp_code ascending.
    - Ranking inside department only when department is given.
    - Logs whose employee does not exist are ignored.
    - Employees with total_late_minutes == 0 are excluded.
    """
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    cursor = db.attendance_logs.aggregate(pipeline)
    items = [
        {
            "rank":               int(doc["rank"]),
            "emp_code":           doc["emp_code"],
            "name":               doc["name"],
            "department":         doc["department"],
            "total_late_minutes": int(doc["total_late_minutes"]),
            "late_count":         int(doc["late_count"]),
        }
        for doc in cursor
    ]

    return {
        "month": month,
        "items": items,
    }


# ---------------------------------------------------------------------------
# Phase 9: Department trend analytics
# ---------------------------------------------------------------------------

def build_department_trend_pipeline(
    department: str,
    from_date: str,
    to_date: str,
) -> list[dict[str, Any]]:
    """Build the MongoDB aggregation pipeline for daily department trend analytics (Phase 9).

    Pipeline stages:
    1. $match (employees):
       Matches department using IX2_department index (0 COLLSCAN).
    2. $group:
       Collects all employees of the department into dept_employees (emp_code, joined_on)
       and collects emp_codes for attendance logs lookup.
    3. $lookup (attendance_logs):
       - Inner $match: Filters records where emp_code in $$codes and date in [from_date, to_date].
       - Inner $group: Groups by date to sum present_count (R6 presence, R5 half_day=0.5)
         and late_count (late_minutes > 0).
       - Inner $project: Projects date and converts date_dt with Asia/Kolkata timezone.
       - Inner $densify: Fills missing dates in [from_dt, to_exclusive_dt] with step 1 day,
         respecting the documented exclusive upper date bound.
    4. $unwind:
       Flattens daily_stats so each day is one document in the pipeline.
    5. $project:
       Projects calendar date (YYYY-MM-DD in Asia/Kolkata), is_working_day (Mon-Fri),
       present_count (default 0.0), late_count (default 0), and computes eligible headcount
       using $filter on dept_employees where joined_on <= date (R9).
    6. $project:
       Computes fractional attendance_rate (present_count / headcount) on working days
       with headcount > 0, otherwise None.
    7. $sort:
       Orders days strictly by date ascending before window calculation.
    8. $setWindowFields:
       Computes 7-day moving average (moving_avg_7d) using $avg over attendance_rate
       in window documents [-6, "current"] (ignoring nulls per MongoDB spec).
    """
    from_dt = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=IST)
    to_exclusive_dt = datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=IST) + timedelta(days=1)

    return [
        {"$match": {"department": department}},
        {
            "$group": {
                "_id": "$department",
                "dept_employees": {
                    "$push": {
                        "emp_code": "$emp_code",
                        "joined_on": "$joined_on",
                    }
                },
                "emp_codes": {"$push": "$emp_code"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"codes": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$codes"]},
                                    {"$gte": ["$date", from_date]},
                                    {"$lte": ["$date", to_date]},
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": "$date",
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$cond": ["$half_day", 0.5, 1.0]},
                                        0.0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                                }
                            },
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "date": "$_id",
                            "date_dt": {
                                "$dateFromString": {
                                    "dateString": "$_id",
                                    "timezone": "Asia/Kolkata",
                                }
                            },
                            "present_count": 1,
                            "late_count": 1,
                        }
                    },
                    {
                        "$densify": {
                            "field": "date_dt",
                            "range": {
                                "step": 1,
                                "unit": "day",
                                "bounds": [from_dt, to_exclusive_dt],
                            },
                        }
                    },
                ],
                "as": "daily_stats",
            }
        },
        {"$unwind": "$daily_stats"},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$daily_stats.date_dt",
                        "timezone": "Asia/Kolkata",
                    }
                },
                "is_working_day": {
                    "$in": [
                        {
                            "$dayOfWeek": {
                                "date": "$daily_stats.date_dt",
                                "timezone": "Asia/Kolkata",
                            }
                        },
                        [2, 3, 4, 5, 6],
                    ]
                },
                "present_count": {"$ifNull": ["$daily_stats.present_count", 0.0]},
                "late_count": {"$ifNull": ["$daily_stats.late_count", 0]},
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$dept_employees",
                            "as": "e",
                            "cond": {
                                "$lte": [
                                    "$$e.joined_on",
                                    {
                                        "$dateToString": {
                                            "format": "%Y-%m-%d",
                                            "date": "$daily_stats.date_dt",
                                            "timezone": "Asia/Kolkata",
                                        }
                                    },
                                ]
                            },
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
    ]


@app.get(
    "/analytics/departments/{department}/trend",
    response_model=Trend,
    tags=["analytics"],
    operation_id="departmentTrend",
)
def department_trend(
    department: str,
    from_date: str = Query(
        ...,
        alias="from",
        pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$",
        description="Start date, YYYY-MM-DD.",
    ),
    to_date: str = Query(
        ...,
        alias="to",
        pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$",
        description="End date, YYYY-MM-DD.",
    ),
):
    """Daily attendance trend for a department (GET /analytics/departments/{department}/trend).

    - Returns exactly one row per calendar day from `from` to `to` inclusive.
    - Weekends and days with no records at all included via MongoDB $densify.
    - is_working_day = Mon-Fri.
    - headcount = department employees with joined_on <= that day (R9).
    - present_count = presence-status records that day (half day = 0.5), any weekday or weekend.
    - late_count = records that day with late_minutes > 0.
    - attendance_rate = present_count / headcount, 4 decimals half-up, only on working days with headcount > 0, otherwise null. Working day with no records has 0.0.
    - moving_avg_7d = mean of non-null attendance_rate values among current row and up to 6 preceding rows inside requested range. null if window holds no non-null rate. 4 decimals half-up.
    - Errors: 404 unknown department (no employees); 422 if to < from or range exceeds 92 days.
    """
    try:
        f_d = datetime.strptime(from_date, "%Y-%m-%d").date()
        t_d = datetime.strptime(to_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="invalid calendar date in from or to parameter",
        )

    if t_d < f_d:
        raise HTTPException(
            status_code=422,
            detail="to date must be on or after from date",
        )

    if (t_d - f_d).days + 1 > 92:
        raise HTTPException(
            status_code=422,
            detail="date range exceeds 92 days",
        )

    # 404 if department has no employees
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(
            status_code=404,
            detail=f"unknown department: {department!r}",
        )

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    cursor = db.employees.aggregate(pipeline)

    items: list[dict[str, Any]] = []
    for doc in cursor:
        raw_rate = doc.get("attendance_rate")
        if raw_rate is not None:
            att_rate = round_4dp(raw_rate)
        else:
            att_rate = None

        # Build window rates from up to 6 preceding returned items + current item
        window_rates = [
            it["attendance_rate"]
            for it in items[-6:]
            if it["attendance_rate"] is not None
        ]
        if att_rate is not None:
            window_rates.append(att_rate)

        # Apply Python Decimal ROUND_HALF_UP to ensure exact precision (avoiding MongoDB $round half-even)
        mavg = compute_moving_avg_7d(window_rates)

        items.append({
            "date":            doc["date"],
            "is_working_day":  bool(doc["is_working_day"]),
            "headcount":       int(doc["headcount"]),
            "present_count":   round_2dp(doc["present_count"]),
            "late_count":      int(doc["late_count"]),
            "attendance_rate": att_rate,
            "moving_avg_7d":   mavg,
        })

    return {
        "department": department,
        "items":      items,
    }


# ---------------------------------------------------------------------------
# Phase 10: Admin explain query analysis
# ---------------------------------------------------------------------------

class ExplainEndpointName(str, Enum):
    attendance_list    = "attendance_list"
    employee_monthly   = "employee_monthly"
    department_summary = "department_summary"
    late_leaderboard   = "late_leaderboard"
    department_trend   = "department_trend"


@app.get(
    "/admin/explain/{endpoint}",
    response_model=ExplainResponse,
    tags=["admin"],
    operation_id="explainEndpoint",
)
def explain_endpoint(
    endpoint: ExplainEndpointName,
    emp_code: Optional[str] = Query(None, description="Employee code."),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="Calendar month, YYYY-MM."),
    department: Optional[str] = Query(None, description="Department name."),
    limit: Optional[int] = Query(10, ge=1, le=50, description="Rank limit for late_leaderboard."),
    date_from: Optional[str] = Query(None, description="Start date for attendance_list."),
    date_to: Optional[str] = Query(None, description="End date for attendance_list."),
    status: Optional[str] = Query(None, description="Status filter for attendance_list."),
    from_date: Optional[str] = Query(None, alias="from", description="Start date for department_trend."),
    to_date: Optional[str] = Query(None, alias="to", description="End date for department_trend."),
    page: int = Query(1, ge=1, description="Page number."),
    page_size: int = Query(20, ge=1, le=100, description="Page size."),
):
    """MongoDB explain for an endpoint's main query or aggregation (GET /admin/explain/{endpoint}).

    - Runs explain with executionStats verbosity.
    - Built exactly as that endpoint builds it (reusing exact pipeline builders).
    - Returns endpoint, collection, and raw explain output document.
    - Validates required parameters for each supported endpoint (422 if missing or invalid).
    - Supports only the 5 documented endpoint identifiers; rejects all others with 422.
    """
    ep_name = endpoint.value

    if ep_name == "attendance_list":
        if status is not None:
            if status not in (PRESENCE_STATUSES | NON_PRESENCE_STATUSES):
                raise HTTPException(
                    status_code=422,
                    detail=f"invalid status: {status!r}",
                )
        if date_from and not _DATE_PATTERN.match(date_from):
            raise HTTPException(status_code=422, detail="invalid date_from format")
        if date_to and not _DATE_PATTERN.match(date_to):
            raise HTTPException(status_code=422, detail="invalid date_to format")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(
                status_code=422,
                detail="date_from must be on or before date_to",
            )

        q: dict[str, Any] = {}
        if emp_code is not None:
            q["emp_code"] = emp_code
        if status is not None:
            q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to

        skip = pagination_skip(page, page_size)
        explain_cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }
        collection = "attendance_logs"

    elif ep_name == "employee_monthly":
        if not emp_code:
            raise HTTPException(status_code=422, detail="emp_code is required for employee_monthly")
        if not month:
            raise HTTPException(status_code=422, detail="month is required for employee_monthly")

        pipeline = build_employee_monthly_pipeline(emp_code, month)
        explain_cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        collection = "attendance_logs"

    elif ep_name == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")

        pipeline = build_department_summary_pipeline(month, department)
        explain_cmd = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
        }
        collection = "employees"

    elif ep_name == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")

        limit_val = limit if limit is not None else 10
        pipeline = build_late_leaderboard_pipeline(month, limit_val, department)
        explain_cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        collection = "attendance_logs"

    elif ep_name == "department_trend":
        if not department:
            raise HTTPException(status_code=422, detail="department is required for department_trend")
        if not from_date or not to_date:
            raise HTTPException(status_code=422, detail="from and to parameters are required for department_trend")

        try:
            f_d = datetime.strptime(from_date, "%Y-%m-%d").date()
            t_d = datetime.strptime(to_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid calendar date in from or to parameter")

        if t_d < f_d:
            raise HTTPException(status_code=422, detail="to date must be on or after from date")

        if (t_d - f_d).days + 1 > 92:
            raise HTTPException(status_code=422, detail="date range exceeds 92 days")

        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        explain_cmd = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
        }
        collection = "employees"

    else:
        raise HTTPException(status_code=422, detail=f"unsupported endpoint: {ep_name!r}")

    try:
        explain_output = db.command("explain", explain_cmd, verbosity="executionStats")
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable or explain error: {exc}")

    return {
        "endpoint":   ep_name,
        "collection": collection,
        "explain":    explain_output,
    }



