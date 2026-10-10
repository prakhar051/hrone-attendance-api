# HROne Employee Attendance & Analytics API

Candidate implementation of the HROne Employee Attendance & Analytics API built with Python, FastAPI, and MongoDB.

This service implements a production-grade REST API for employee management, shift attendance tracking, audit logging, and analytical reporting over organizational attendance data.

---

## 1. Key Features

- **Employee Management:** Create and list employees with business identifiers (`emp_code`), shift timings (`shift_start`, `shift_end`), and case-sensitive department filtering.
- **Attendance Punch-In & Punch-Out:** Record attendance punches in integer epoch milliseconds; handles overnight shifts (R1), late arrivals with a 10-minute grace period (R2), overtime with a 30-minute qualification threshold (R3), work duration with whole-second truncation (R4), and half-day status (R5).
- **Concurrency & Race Safety:** Race-safe punch-in via unique compound index (`IX4`); atomic punch-out via `find_one_and_update` on open punches (`punch_out: None`).
- **Attendance Listing & Regularization:** Query and paginate records with mixed-direction sort (`date DESC, emp_code ASC`); manual record corrections with optimistic concurrency control (Compare-And-Swap on `history` length) and append-only audit trail logging only changed fields.
- **Monthly Employee Analytics:** Monthly summary calculation via aggregation pipeline computing working days (R7), present days (Mon–Fri, half-day 0.5), leave days, late count, total late/overtime minutes, and half-up rounded attendance percentage.
- **Department Summary Analytics:** Cross-collection aggregation computing eligible department headcount (including employees with zero logs per R9), present days, and average work hours across records.
- **Late Leaderboard:** Rank employees by total monthly late minutes using standard competition ranking (`$rank` in `$setWindowFields`, e.g. 1, 2, 2, 4) with post-ranking limit filtering and deterministic tie-breaking.
- **Department Trend Analytics:** Daily attendance rate and 7-day rolling moving average using MongoDB's `$densify` to produce exact calendar day series even for zero-log departments and weekend days.
- **Admin Query-Plan Analysis:** Inspect query execution plans (`explain` with `executionStats`) across the 5 core endpoints to verify `IXSCAN` index utilization without data mutation.
- **Readiness Health Endpoint:** Health check pinging MongoDB directly (`db.command("ping")`), returning HTTP 503 if the database is unreachable.

---

## 2. Technology Stack

- **Framework:** FastAPI 0.110+
- **ASGI Server:** Uvicorn 0.29+
- **Database:** MongoDB 6.0+ (Tested with MongoDB 7.0 Community Edition)
- **Database Driver:** PyMongo 4.7+
- **Data Validation & Schemas:** Pydantic v2.6+
- **Configuration:** python-dotenv 1.0+
- **Language:** Python 3.10+ (Developed and tested on Python 3.14; compatible with Python 3.11+)

---

## 3. Prerequisites

- **Python 3.11+** (or Python 3.10+ / 3.14)
- **MongoDB 6.0 or newer** running locally on port 27017 (or MongoDB Atlas)
- **Git**

---

## 4. Installation & Virtual Environment Setup

### Windows (PowerShell)
```powershell
# Navigate to the candidate kit directory
cd candidate_kit

# Create virtual environment
python -m venv .venv

# Activate virtual environment
.\.venv\Scripts\Activate.ps1

# Install required dependencies
pip install -r requirements.txt
```

### macOS / Linux (bash/zsh)
```bash
# Navigate to the candidate kit directory
cd candidate_kit

# Create virtual environment
python3 -m venv .venv

# Activate virtual environment
source .venv/bin/activate

# Install required dependencies
pip install -r requirements.txt
```

---

## 5. Environment Configuration

The application reads configuration from environment variables or a local `.env` file (real environment variables take precedence):

| Variable Name | Default Value | Description |
| :--- | :--- | :--- |
| `MONGO_URI` | `mongodb://localhost:27017` | MongoDB connection URI |
| `MONGO_DB` | `attendance_db` | Target MongoDB database name |

**Safe local configuration example (PowerShell):**
```powershell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
```

**Safe local configuration example (macOS/Linux):**
```bash
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
```
*(Never commit `.env` files or secret-bearing connection strings to version control.)*

---

## 6. Starting the Application

Seed sample test documents (optional):
```bash
python sample_seed.py
```

Start the API server with Uvicorn:
```bash
uvicorn app.main:app --port 8000 --reload
```
The application initializes database indexes idempotently during startup.

---

## 7. Interactive API Documentation

Once the server is running, access interactive OpenAPI documentation at:
- **Swagger UI:** [http://localhost:8000/docs](http://localhost:8000/docs)
- **ReDoc:** [http://localhost:8000/redoc](http://localhost:8000/redoc)

---

## 8. API Endpoint Reference

| Method | Route | Purpose | Key Status Codes |
| :--- | :--- | :--- | :---: |
| `GET` | `/health` | Service and MongoDB readiness check | `200`, `503` |
| `POST` | `/employees` | Create a new employee | `201`, `409`, `422` |
| `GET` | `/employees` | List employees with pagination and department filter | `200`, `422` |
| `POST` | `/attendance/punch-in` | Record employee shift punch-in | `201`, `404`, `409`, `422` |
| `POST` | `/attendance/punch-out` | Record punch-out and compute duration/overtime | `200`, `404`, `409`, `422` |
| `GET` | `/attendance` | List and filter attendance records | `200`, `422` |
| `PATCH` | `/attendance/{emp_code}/{date}` | Regularize attendance record with audit history | `200`, `404`, `409`, `422` |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly attendance summary for one employee | `200`, `404`, `422` |
| `GET` | `/analytics/departments/summary` | Monthly department attendance rollup | `200`, `422` |
| `GET` | `/analytics/leaderboard/late` | Competition ranking leaderboard of late employees | `200`, `422` |
| `GET` | `/analytics/departments/{department}/trend` | Daily attendance trend and 7-day moving average | `200`, `404`, `422` |
| `GET` | `/admin/explain/{endpoint}` | MongoDB execution plan analysis for query inspection | `200`, `422`, `503` |

---

## 9. Important Business Rules & Design Choices (R1–R10)

- **R1 (IST Attendance-Date & Overnight Shifts):** All calendar dates and shift times are anchored in Indian Standard Time (IST = UTC+05:30). If an employee's shift is overnight (`shift_end <= shift_start`, e.g. 22:00–06:00) and punch-in occurs before `shift_end`, the record is attributed to the start day (`date - 1 day`).
- **R2 (Late Minutes):** Arrival has a 10-minute grace period (`diff <= 10 -> 0`). If arrival exceeds 10 minutes (`diff > 10`), the penalty is measured as full integer minutes elapsed from `shift_start`.
- **R3 (Overtime Minutes):** Punch-outs past `shift_end` require a minimum of 30 minutes to qualify (`diff < 30 -> 0`). Once qualified, the full integer floor minutes count as overtime.
- **R4 (Work Hours):** Calculated between `punch_in` and `punch_out` after truncating timestamps to whole seconds. Rounded to 2 decimal places using `ROUND_HALF_UP` (never Python's banker's rounding).
- **R5 (Half-Day Status):** A closed record is marked `half_day = True` strictly if `work_hours < 4.50`. Open records and days with `>= 4.50` hours have `half_day = False`.
- **R6 (Presence Classification):** `PRESENT`, `WFH`, and `ON_DUTY` count as present. Full-day records contribute 1.0; half-day records contribute 0.5. `ABSENT` and `LEAVE` contribute 0.0.
- **R7 (Working Days):** Monday through Friday are working days. Weekends are not working days.
- **R8 (Rounding):** All floating-point rates, percentages, and hours use `Decimal.quantize(..., rounding=ROUND_HALF_UP)` to avoid Banker's Rounding divergences on `.5` boundaries.
- **R9 (Headcount Eligibility):** An employee is eligible for a period if `joined_on <= period_end`. In department analytics, employees with zero attendance records are strictly counted in headcount.
- **R10 (Pagination):** Offset calculation `skip = (page - 1) * page_size`. Filtered totals reflect matching query counts, not entire collection sizes.

---

## 10. Database Indexing & Aggregation Design

The application initializes six indexes at startup:

### `employees` Collection
1. **`IX1_emp_code_unique`** (`emp_code` ASC, unique): Enforces unique business identifiers and provides $O(1)$ lookup.
2. **`IX2_department`** (`department` ASC): Powers department listing filters and analytics `$match` / `$lookup` stages.
3. **`IX3_joined_on`** (`joined_on` ASC): Optimizes headcount eligibility filtering (`joined_on <= cutoff`).

### `attendance_logs` Collection
4. **`IX4_emp_code_date_unique`** (`(emp_code, date)` ASC, unique): Enforces at most one record per employee/day and provides race-safe duplicate punch prevention.
5. **`IX5_date_desc_emp_code_asc`** (`date` DESC, `emp_code` ASC): Fulfills mixed-direction attendance listing sorts without in-memory sort stages.
6. **`IX6_emp_code_date_status`** (`(emp_code, date, status)` ASC): Powers employee monthly analytics and covered status queries.

### MongoDB Aggregation Pipelines
- **Monthly Employee Analytics:** 3-stage pipeline (`$match`, `$project`, `$group`) aggregating counts directly in the database.
- **Department Summary:** Pipelined from `employees` to preserve employees with zero logs in headcount before `$lookup` into `attendance_logs`.
- **Late Leaderboard:** Uses `$setWindowFields` with `$rank` for standard competition ranking (`1, 2, 2, 4`), applying cutoff filtering post-ranking.
- **Department Trend:** Combines `$lookup` with inner `$densify` on `date_inst` (1-day step) and `$setWindowFields` with rolling `$avg` for 7-day moving averages.

---

## 11. Running the Test Suite

Each phase has a dedicated test script testing both mock test doubles and live MongoDB operations:

```bash
python -X utf8 tests/test_audit_phase1.py
python -X utf8 tests/test_phase2_helpers.py
python -X utf8 tests/test_phase3_employees.py
python -X utf8 tests/test_phase4_punch.py
python -X utf8 tests/test_phase5_attendance.py
python -X utf8 tests/test_phase6_employee_analytics.py
python -X utf8 tests/test_phase7_department_summary.py
python -X utf8 tests/test_phase8_late_leaderboard.py
python -X utf8 tests/test_phase9_department_trend.py
python -X utf8 tests/test_phase10_admin_explain.py
```

### Overall Test Suite Results
- **Phase 1 (Audit & Schema Validation):** 127 passed, 0 failed
- **Phase 2 (R1–R10 Pure Helpers):** 96 passed, 0 failed
- **Phase 3 (Employees Endpoint):** 45 passed, 0 failed
- **Phase 4 (Punch-in / Punch-out):** 56 passed, 0 failed
- **Phase 5 (Attendance & Regularization):** 67 passed, 0 failed
- **Phase 6 (Monthly Analytics):** 44 passed, 0 failed
- **Phase 7 (Department Summary):** 54 passed, 0 failed
- **Phase 8 (Late Leaderboard):** 44 passed, 0 failed
- **Phase 9 (Department Trend):** 119 passed, 0 failed
- **Phase 10 (Admin Explain):** 73 passed, 0 failed
- **Total:** **725 passed, 0 failed (597 mocked/unit tests, 128 live MongoDB integration tests)**

---

## 12. Supplementary Documentation

- [`REVIEW.md`](REVIEW.md): Detailed catalog of starter bugs (D1–D16), symptoms, fixes, and architectural non-defects.
- [`DECISIONS.md`](DECISIONS.md): Architectural decisions covering indexing, race handling, competition ranking ties, zero-log headcount, and scaling strategies.

---

## 13. Limitations & Considerations

- **MongoDB 6.0+ Requirement:** Analytics pipelines use `$densify` and `$setWindowFields`, requiring MongoDB 6.0 or newer (tested on MongoDB 7.0).
- **Scale Rollups:** For deployments exceeding 100× current volume, real-time multi-stage aggregations on raw collections should transition to precomputed daily summary collections maintained via change streams or scheduled rollups.
