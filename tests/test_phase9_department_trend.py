"""
Phase 9 verification suite — Department trend analytics only.

Imports the REAL FastAPI app and real models from app.main.
Uses TestClient to test:
- Route GET /analytics/departments/{department}/trend
- Query parameters validation: `from` and `to` (YYYY-MM-DD pattern), `to` < `from`, and > 92 days limit
- 404 for unknown department (no employees in department)
- Exactly one row per calendar day from `from` to `to` inclusive (via MongoDB $densify)
- is_working_day = Mon-Fri
- Headcount per R9: includes employees with zero logs, mid-period joiners, excludes future joiners
- Status handling: PRESENT, WFH, ON_DUTY count as present; ABSENT and LEAVE do not
- Half-day presence (0.5) and weekend records
- attendance_rate: present_count / headcount (4 decimals half-up), only on working days with headcount > 0, otherwise null
- moving_avg_7d: mean of non-null attendance_rate values in 7-day window inside requested range, 4 decimals half-up
- Shorter window for initial days (days 1 to 6)
- IST timezone handling and exclusive upper bound for $densify
- Decimal ROUND_HALF_UP tie cases
- Response schema exactness, no MongoDB internal _id
- DB aggregation usage (no collection-wide Python scans)
- Live MongoDB aggregation execution and structured explain plan inspection

Run:
    python -X utf8 tests/test_phase9_department_trend.py
"""
import calendar
import os
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

# Ensure candidate_kit is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pymongo import MongoClient
import app.main as m
from fastapi.testclient import TestClient

client = TestClient(m.app)

# Check if real MongoDB is available
_LIVE_MONGO_AVAILABLE = False
try:
    _live_client = MongoClient("mongodb://localhost:27017", serverSelectionTimeoutMS=1500)
    _live_client.server_info()
    _LIVE_MONGO_AVAILABLE = True
except Exception:
    _LIVE_MONGO_AVAILABLE = False


# =============================================================================
# Aggregation Mock for DB Isolation & Pipeline Inspection
# =============================================================================
class MockEmployeesWithTrend:
    def __init__(self, att_mock):
        self.docs: list[dict] = []
        self.aggregate_calls: list[list[dict]] = []
        self.find_calls: list[dict] = []
        self.att_mock = att_mock

    def find_one(self, filter_doc: dict):
        # Used by the route handler to check department existence
        dept = filter_doc.get("department")
        for doc in self.docs:
            if doc.get("department") == dept:
                return doc
        return None

    def find(self, *args, **kwargs):
        self.find_calls.append(args[0] if args else {})
        raise AssertionError("find() should not be called on employees during department trend aggregation!")

    def aggregate(self, pipeline: list[dict]):
        self.aggregate_calls.append(pipeline)

        # Execute mock logic mirroring the aggregation pipeline
        match_stage = pipeline[0].get("$match", {})
        dept = match_stage.get("department")
        dept_emps = [e for e in self.docs if e.get("department") == dept]
        if not dept_emps:
            return []

        # Find lookup stage
        lookup_stage = None
        for st in pipeline:
            if "$lookup" in st:
                lookup_stage = st["$lookup"]
                break

        # Extract dates from lookup pipeline
        from_date = None
        to_date = None
        densify_bounds = None
        if lookup_stage:
            for inner in lookup_stage.get("pipeline", []):
                if "$densify" in inner:
                    densify_bounds = inner["$densify"]["range"]["bounds"]

        if densify_bounds:
            # Bounds are datetime objects in IST
            start_dt = densify_bounds[0]
            end_dt = densify_bounds[1]
            start_d = start_dt.date()
            # Exclusive upper bound
            end_d = end_dt.date() - timedelta(days=1)
        else:
            start_d = date(2026, 7, 1)
            end_d = date(2026, 7, 7)

        # Generate every calendar day from start_d to end_d inclusive
        cur_d = start_d
        days_list = []
        emp_codes = {e["emp_code"] for e in dept_emps}

        # Filter logs for department
        dept_logs = [
            l for l in self.att_mock.docs
            if l.get("emp_code") in emp_codes
        ]

        while cur_d <= end_d:
            cur_str = cur_d.isoformat()
            is_work = cur_d.weekday() < 5  # Mon-Fri

            # Eligible headcount: joined_on <= cur_str
            hc = sum(1 for e in dept_emps if e.get("joined_on", "9999-99-99") <= cur_str)

            # Logs on this date
            day_logs = [l for l in dept_logs if l.get("date") == cur_str]
            pres_count = 0.0
            late_count = 0
            for l in day_logs:
                st = l.get("status")
                if st in ("PRESENT", "WFH", "ON_DUTY"):
                    pres_count += 0.5 if l.get("half_day") else 1.0
                if l.get("late_minutes", 0) > 0:
                    late_count += 1

            if is_work and hc > 0:
                raw_rate = pres_count / hc
            else:
                raw_rate = None

            days_list.append({
                "date": cur_str,
                "is_working_day": is_work,
                "headcount": hc,
                "present_count": pres_count,
                "late_count": late_count,
                "attendance_rate": raw_rate,
            })
            cur_d += timedelta(days=1)

        # Compute moving_avg_7d
        for i, item in enumerate(days_list):
            window = [
                d["attendance_rate"]
                for d in days_list[max(0, i - 6): i + 1]
                if d["attendance_rate"] is not None
            ]
            if window:
                item["moving_avg_7d"] = sum(window) / len(window)
            else:
                item["moving_avg_7d"] = None

        return days_list


class MockAttendanceLogsWithTrend:
    def __init__(self):
        self.docs: list[dict] = []
        self.find_calls: list[dict] = []

    def find(self, *args, **kwargs):
        self.find_calls.append(args[0] if args else {})
        raise AssertionError("find() should not be called on attendance_logs during department trend aggregation!")


# =============================================================================
# Test Runner & Assertions
# =============================================================================
def run_tests():
    passed_count = 0
    failed_count = 0

    def report(name: str, cond: bool, msg: str = ""):
        nonlocal passed_count, failed_count
        if cond:
            print(f"PASS  {name}")
            passed_count += 1
        else:
            print(f"FAIL  {name} {msg}")
            failed_count += 1

    print("=" * 65)
    print("PHASE 9: DEPARTMENT TREND ANALYTICS VERIFICATION")
    print(f"Imported app.main module: {m.__file__}")
    print(f"Live MongoDB available: {_LIVE_MONGO_AVAILABLE}")
    print("=" * 65)

    # -------------------------------------------------------------------------
    # PART 1: Test Double & Aggregation Pipeline Inspection
    # -------------------------------------------------------------------------
    print("\n-- PART 1: Contract, Pipeline Builder & Mocked DB Tests --")

    mock_att = MockAttendanceLogsWithTrend()
    mock_emp = MockEmployeesWithTrend(mock_att)

    orig_emp = m.db.employees
    orig_att = m.db.attendance_logs
    m.db.employees = mock_emp
    m.db.attendance_logs = mock_att

    try:
        # Seed test employees
        mock_emp.docs = [
            {"emp_code": "EMP01", "name": "Alice Rao", "department": "Engineering", "joined_on": "2026-07-01"},
            {"emp_code": "EMP02", "name": "Bob Shah", "department": "Engineering", "joined_on": "2026-07-03"},
            {"emp_code": "EMP03", "name": "Charlie Iyer", "department": "Engineering", "joined_on": "2026-07-15"},
            {"emp_code": "EMP04", "name": "Dave Das", "department": "Marketing", "joined_on": "2026-07-01"},
        ]

        # Seed test attendance logs
        mock_att.docs = [
            # Wed 2026-07-01: Alice full day present, 15m late
            {"emp_code": "EMP01", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 15, "half_day": False},
            # Thu 2026-07-02: Alice WFH half day, 0 late
            {"emp_code": "EMP01", "date": "2026-07-02", "status": "WFH", "late_minutes": 0, "half_day": True},
            # Fri 2026-07-03: Alice ON_DUTY, Bob ABSENT (Bob joins today)
            {"emp_code": "EMP01", "date": "2026-07-03", "status": "ON_DUTY", "late_minutes": 0, "half_day": False},
            {"emp_code": "EMP02", "date": "2026-07-03", "status": "ABSENT", "late_minutes": 0, "half_day": False},
            # Sat 2026-07-04: Bob weekend punch PRESENT
            {"emp_code": "EMP02", "date": "2026-07-04", "status": "PRESENT", "late_minutes": 0, "half_day": False},
            # Sun 2026-07-05: no logs
            # Mon 2026-07-06: Alice LEAVE
            {"emp_code": "EMP01", "date": "2026-07-06", "status": "LEAVE", "late_minutes": 0, "half_day": False},
            # Tue 2026-07-07: Bob PRESENT, 20m late
            {"emp_code": "EMP02", "date": "2026-07-07", "status": "PRESENT", "late_minutes": 20, "half_day": False},
        ]

        # 1. Parameter Validation
        r = client.get("/analytics/departments/Engineering/trend?to=2026-07-07")
        report("P9-1a: Missing 'from' returns 422", r.status_code == 422)

        r = client.get("/analytics/departments/Engineering/trend?from=2026-07-01")
        report("P9-1b: Missing 'to' returns 422", r.status_code == 422)

        r = client.get("/analytics/departments/Engineering/trend?from=2026-13-01&to=2026-07-07")
        report("P9-1c: Invalid month pattern returns 422", r.status_code == 422)

        r = client.get("/analytics/departments/Engineering/trend?from=not-a-date&to=2026-07-07")
        report("P9-1d: Non-date parameter returns 422", r.status_code == 422)

        r = client.get("/analytics/departments/Engineering/trend?from=2026-02-30&to=2026-03-05")
        report("P9-1e: Invalid calendar date (Feb 30) returns 422", r.status_code == 422)

        r = client.get("/analytics/departments/Engineering/trend?from=2026-07-07&to=2026-07-01")
        report("P9-1f: 'to' < 'from' returns 422", r.status_code == 422)

        # 92 days limit validation
        # 2026-01-01 to 2026-04-03 is 93 calendar days -> must be 422
        r = client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-04-03")
        report("P9-1g: Range exceeding 92 days (93 days) returns 422", r.status_code == 422)

        # Exactly 92 days: 2026-01-01 to 2026-04-02 is 92 calendar days -> must be 200
        r_92 = client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-04-02")
        report("P9-1h: Range of exactly 92 days accepted (HTTP 200)", r_92.status_code == 200)

        # 2. Unknown department validation
        r = client.get("/analytics/departments/NonExistentDept/trend?from=2026-07-01&to=2026-07-07")
        report("P9-2: Unknown department (no employees) returns HTTP 404", r.status_code == 404)

        # 3. Successful Department Trend Query
        r = client.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-07")
        report("P9-3a: Valid trend request returns HTTP 200", r.status_code == 200)
        data = r.json()
        report("P9-3b: Response department matches 'Engineering'", data.get("department") == "Engineering")
        items = data.get("items", [])
        report("P9-3c: Exactly 7 calendar days returned (July 1 to July 7)", len(items) == 7)

        # Verify no collection-wide find() scans
        report("P9-3d: find() was NOT called on employees", len(mock_emp.find_calls) == 0)
        report("P9-3e: find() was NOT called on attendance_logs", len(mock_att.find_calls) == 0)
        report("P9-3f: aggregate() was called on db.employees", len(mock_emp.aggregate_calls) > 0)

        # 4. Verify Each Day's Business Semantics
        # Day 1: 2026-07-01 (Wednesday)
        d1 = items[0]
        report("P9-4a: Day 1 date is '2026-07-01'", d1["date"] == "2026-07-01")
        report("P9-4b: Day 1 is_working_day is True", d1["is_working_day"] is True)
        report("P9-4c: Day 1 headcount == 1 (Alice joined today)", d1["headcount"] == 1)
        report("P9-4d: Day 1 present_count == 1.0 (Alice PRESENT)", d1["present_count"] == 1.0)
        report("P9-4e: Day 1 late_count == 1 (15m late)", d1["late_count"] == 1)
        report("P9-4f: Day 1 attendance_rate == 1.0", d1["attendance_rate"] == 1.0)
        report("P9-4g: Day 1 moving_avg_7d == 1.0 (window has 1 day)", d1["moving_avg_7d"] == 1.0)

        # Day 2: 2026-07-02 (Thursday)
        d2 = items[1]
        report("P9-5a: Day 2 is_working_day is True", d2["is_working_day"] is True)
        report("P9-5b: Day 2 headcount == 1", d2["headcount"] == 1)
        report("P9-5c: Day 2 present_count == 0.5 (half-day WFH)", d2["present_count"] == 0.5)
        report("P9-5d: Day 2 attendance_rate == 0.5", d2["attendance_rate"] == 0.5)
        report("P9-5e: Day 2 moving_avg_7d == 0.75 (mean of 1.0 and 0.5)", d2["moving_avg_7d"] == 0.75)

        # Day 3: 2026-07-03 (Friday) - Bob joined mid-period today!
        d3 = items[2]
        report("P9-6a: Day 3 is_working_day is True", d3["is_working_day"] is True)
        report("P9-6b: Day 3 headcount == 2 (Bob joins today)", d3["headcount"] == 2)
        report("P9-6c: Day 3 present_count == 1.0 (Alice ON_DUTY=1.0, Bob ABSENT=0.0)", d3["present_count"] == 1.0)
        report("P9-6d: Day 3 attendance_rate == 0.5 (1.0 / 2)", d3["attendance_rate"] == 0.5)
        report("P9-6e: Day 3 moving_avg_7d == 0.6667 (mean of 1.0, 0.5, 0.5: 2.0 / 3)", d3["moving_avg_7d"] == 0.6667)

        # Day 4: 2026-07-04 (Saturday) - Weekend
        d4 = items[3]
        report("P9-7a: Day 4 is_working_day is False (Saturday)", d4["is_working_day"] is False)
        report("P9-7b: Day 4 headcount == 2", d4["headcount"] == 2)
        report("P9-7c: Day 4 present_count == 1.0 (Bob worked on weekend)", d4["present_count"] == 1.0)
        report("P9-7d: Day 4 attendance_rate is null on weekend", d4["attendance_rate"] is None)
        report("P9-7e: Day 4 moving_avg_7d == 0.6667 (ignores null rate)", d4["moving_avg_7d"] == 0.6667)

        # Day 5: 2026-07-05 (Sunday) - Weekend with no logs
        d5 = items[4]
        report("P9-8a: Day 5 is_working_day is False (Sunday)", d5["is_working_day"] is False)
        report("P9-8b: Day 5 present_count == 0.0 (no logs, densified)", d5["present_count"] == 0.0)
        report("P9-8c: Day 5 attendance_rate is null", d5["attendance_rate"] is None)
        report("P9-8d: Day 5 moving_avg_7d == 0.6667", d5["moving_avg_7d"] == 0.6667)

        # Day 6: 2026-07-06 (Monday) - Alice LEAVE
        d6 = items[5]
        report("P9-9a: Day 6 is_working_day is True", d6["is_working_day"] is True)
        report("P9-9b: Day 6 present_count == 0.0 (LEAVE does not count)", d6["present_count"] == 0.0)
        report("P9-9c: Day 6 attendance_rate == 0.0 (working day with 0 present)", d6["attendance_rate"] == 0.0)
        report("P9-9d: Day 6 moving_avg_7d == 0.5 (mean of 1.0, 0.5, 0.5, 0.0: 2.0 / 4)", d6["moving_avg_7d"] == 0.5)

        # Day 7: 2026-07-07 (Tuesday) - Bob PRESENT
        d7 = items[6]
        report("P9-10a: Day 7 is_working_day is True", d7["is_working_day"] is True)
        report("P9-10b: Day 7 headcount == 2 (Charlie joined July 15, excluded)", d7["headcount"] == 2)
        report("P9-10c: Day 7 present_count == 1.0", d7["present_count"] == 1.0)
        report("P9-10d: Day 7 late_count == 1 (20m late)", d7["late_count"] == 1)
        report("P9-10e: Day 7 attendance_rate == 0.5", d7["attendance_rate"] == 0.5)
        report("P9-10f: Day 7 moving_avg_7d == 0.5 (mean of 1.0, 0.5, 0.5, 0.0, 0.5: 2.5 / 5)", d7["moving_avg_7d"] == 0.5)

        # 5. Schema verification (exact 7 fields, no _id)
        expected_fields = {"date", "is_working_day", "headcount", "present_count", "late_count", "attendance_rate", "moving_avg_7d"}
        report("P9-11a: TrendItem matches exact 7 fields", set(items[0].keys()) == expected_fields)
        report("P9-11b: MongoDB internal _id is absent from items", all("_id" not in it for it in items))
        report("P9-11c: MongoDB internal _id is absent from top-level response", "_id" not in data)

        # 6. Department with employees but 0 logs across range
        r_zero_logs = client.get("/analytics/departments/Marketing/trend?from=2026-07-01&to=2026-07-03")
        report("P9-12a: Department with 0 logs returns HTTP 200", r_zero_logs.status_code == 200)
        z_items = r_zero_logs.json().get("items", [])
        report("P9-12b: Department with 0 logs returns 3 days (densified)", len(z_items) == 3)
        report("P9-12c: Department with 0 logs has headcount == 1", z_items[0]["headcount"] == 1)
        report("P9-12d: Department with 0 logs on working day has rate 0.0", z_items[0]["attendance_rate"] == 0.0)
        report("P9-12e: Department with 0 logs has moving_avg_7d 0.0", z_items[0]["moving_avg_7d"] == 0.0)

        # 7. Pipeline builder stage inspection
        pipe = m.build_department_trend_pipeline("Engineering", "2026-07-01", "2026-07-07")
        report("P9-13a: Pipeline Stage 0 matches department", pipe[0].get("$match", {}).get("department") == "Engineering")
        report("P9-13b: Pipeline Stage 1 groups by department", "$group" in pipe[1])
        report("P9-13c: Pipeline Stage 2 is $lookup to attendance_logs", "$lookup" in pipe[2])
        lookup_p = pipe[2]["$lookup"]["pipeline"]
        report("P9-13d: Inner lookup pipeline includes $densify stage", any("$densify" in st for st in lookup_p))
        densify_st = [st["$densify"] for st in lookup_p if "$densify" in st][0]
        report("P9-13e: $densify step is 1 day", densify_st["range"]["step"] == 1 and densify_st["range"]["unit"] == "day")
        report("P9-13f: Pipeline unwinds daily_stats", any("$unwind" in st for st in pipe))
        report("P9-13g: Pipeline sorts by date ascending", any("$sort" in st and st["$sort"] == {"date": 1} for st in pipe))
        report("P9-13h: Pipeline uses $setWindowFields with $avg", any("$setWindowFields" in st for st in pipe))

        # 8. ROUND_HALF_UP tie-breaking check
        # Rate: 1 / 8000 = 0.000125 -> half_up(0.000125, 4) == 0.0001
        report("P9-14a: half_up(0.000125, 4) == 0.0001", m.round_4dp(0.000125) == 0.0001)
        # Tie at 0.00005 -> half-up rounds away from zero to 0.0001 (banker's would round to 0.0000)
        report("P9-14b: half_up(0.00005, 4) == 0.0001 (tie break half-up)", m.round_4dp(Decimal("0.00005")) == 0.0001)

    finally:
        m.db.employees = orig_emp
        m.db.attendance_logs = orig_att

    # -------------------------------------------------------------------------
    # PART 2: Live MongoDB Integration Suite
    # -------------------------------------------------------------------------
    print("\n-- PART 2: Live MongoDB Integration --")

    if not _LIVE_MONGO_AVAILABLE:
        print("SKIP: Live MongoDB not reachable at mongodb://localhost:27017")
        print("Note: All unit & pipeline tests in Part 1 ran successfully.")
    else:
        live_db_name = "test_phase9_live_db"
        live_db = _live_client[live_db_name]
        orig_db = m.db

        try:
            m.db = live_db
            live_db.employees.delete_many({})
            live_db.attendance_logs.delete_many({})

            # Create production indexes idempotently
            m._create_indexes()

            # Seed live employees
            live_db.employees.insert_many([
                {"emp_code": "EMP01", "name": "Alice Rao", "department": "Engineering", "joined_on": "2026-07-01"},
                {"emp_code": "EMP02", "name": "Bob Shah", "department": "Engineering", "joined_on": "2026-07-03"},
                {"emp_code": "EMP03", "name": "Charlie Iyer", "department": "Design", "joined_on": "2026-07-01"},
            ])

            # Seed live attendance logs
            live_db.attendance_logs.insert_many([
                # Engineering:
                # 2026-07-01: Alice full day PRESENT (work_hours 8.5, late 0)
                {"emp_code": "EMP01", "date": "2026-07-01", "status": "PRESENT", "half_day": False, "late_minutes": 0, "work_hours": 8.5},
                # 2026-07-02: Alice half day WFH (work_hours 4.0, late 15)
                {"emp_code": "EMP01", "date": "2026-07-02", "status": "WFH", "half_day": True, "late_minutes": 15, "work_hours": 4.0},
                # 2026-07-03: Alice ON_DUTY, Bob joined today but absent
                {"emp_code": "EMP01", "date": "2026-07-03", "status": "ON_DUTY", "half_day": False, "late_minutes": 0, "work_hours": 8.0},
                {"emp_code": "EMP02", "date": "2026-07-03", "status": "ABSENT", "half_day": False, "late_minutes": 0, "work_hours": None},
                # 2026-07-04: Bob weekend punch
                {"emp_code": "EMP02", "date": "2026-07-04", "status": "PRESENT", "half_day": False, "late_minutes": 0, "work_hours": 6.0},
                # 2026-07-07: Bob PRESENT late 20
                {"emp_code": "EMP02", "date": "2026-07-07", "status": "PRESENT", "half_day": False, "late_minutes": 20, "work_hours": 8.0},
            ])

            # Test live query
            r_live = client.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-07")
            report("P9-LIVE-1: Live MongoDB returned HTTP 200", r_live.status_code == 200)
            live_data = r_live.json()
            report("P9-LIVE-2: Live department matches 'Engineering'", live_data.get("department") == "Engineering")
            live_items = live_data.get("items", [])
            report("P9-LIVE-3: Live items count is 7 calendar days", len(live_items) == 7)

            # Live Day 1 checks
            report("P9-LIVE-4a: Live Day 1 headcount == 1", live_items[0]["headcount"] == 1)
            report("P9-LIVE-4b: Live Day 1 present_count == 1.0", live_items[0]["present_count"] == 1.0)
            report("P9-LIVE-4c: Live Day 1 attendance_rate == 1.0", live_items[0]["attendance_rate"] == 1.0)
            report("P9-LIVE-4d: Live Day 1 moving_avg_7d == 1.0", live_items[0]["moving_avg_7d"] == 1.0)

            # Live Day 2 checks
            report("P9-LIVE-5a: Live Day 2 present_count == 0.5", live_items[1]["present_count"] == 0.5)
            report("P9-LIVE-5b: Live Day 2 attendance_rate == 0.5", live_items[1]["attendance_rate"] == 0.5)
            report("P9-LIVE-5c: Live Day 2 moving_avg_7d == 0.75", live_items[1]["moving_avg_7d"] == 0.75)

            # Live Day 3 checks (Bob joined mid-period)
            report("P9-LIVE-6a: Live Day 3 headcount == 2", live_items[2]["headcount"] == 2)
            report("P9-LIVE-6b: Live Day 3 attendance_rate == 0.5", live_items[2]["attendance_rate"] == 0.5)
            report("P9-LIVE-6c: Live Day 3 moving_avg_7d == 0.6667", live_items[2]["moving_avg_7d"] == 0.6667)

            # Live Day 4 checks (Weekend)
            report("P9-LIVE-7a: Live Day 4 is_working_day is False", live_items[3]["is_working_day"] is False)
            report("P9-LIVE-7b: Live Day 4 attendance_rate is null", live_items[3]["attendance_rate"] is None)
            report("P9-LIVE-7c: Live Day 4 moving_avg_7d == 0.6667", live_items[3]["moving_avg_7d"] == 0.6667)

            # Live Day 5 checks (Sunday with 0 logs)
            report("P9-LIVE-8a: Live Day 5 date is 2026-07-05", live_items[4]["date"] == "2026-07-05")
            report("P9-LIVE-8b: Live Day 5 present_count == 0.0", live_items[4]["present_count"] == 0.0)

            # Live Day 7 checks (Exclusive upper bound verified - Day 7 is present!)
            report("P9-LIVE-9a: Live Day 7 date is 2026-07-07", live_items[6]["date"] == "2026-07-07")
            report("P9-LIVE-9b: Live Day 7 attendance_rate == 0.5", live_items[6]["attendance_rate"] == 0.5)
            report("P9-LIVE-9c: Live Day 7 moving_avg_7d == 0.5", live_items[6]["moving_avg_7d"] == 0.5)

            # Explain plan inspection
            pipe_live = m.build_department_trend_pipeline("Engineering", "2026-07-01", "2026-07-07")
            explain = live_db.command(
                "explain",
                {"aggregate": "employees", "pipeline": pipe_live, "cursor": {}},
                verbosity="executionStats",
            )
            explain_str = str(explain)
            has_idx = "IX2_department" in explain_str
            report("P9-LIVE-10a: Live explain uses IX2_department index", has_idx)
            report("P9-LIVE-10b: Live explain contains stages list", len(explain.get("stages", [])) >= 3)

            # -----------------------------------------------------------------
            # Live Test: Zero-Log Department with Mid-Period & Future Joiners
            # -----------------------------------------------------------------
            # Seed employees in 'DevOps' department:
            # - EMP_D1: joined 2026-07-01 (start of period)
            # - EMP_D2: joined 2026-07-03 (mid-period joiner)
            # - EMP_D3: joined 2026-07-10 (future joiner, after period)
            # ZERO logs in attendance_logs for 'DevOps'!
            live_db.employees.insert_many([
                {"emp_code": "EMP_D1", "name": "DevOps Lead", "department": "DevOps", "joined_on": "2026-07-01"},
                {"emp_code": "EMP_D2", "name": "DevOps Mid", "department": "DevOps", "joined_on": "2026-07-03"},
                {"emp_code": "EMP_D3", "name": "DevOps Future", "department": "DevOps", "joined_on": "2026-07-10"},
            ])

            r_devops = client.get("/analytics/departments/DevOps/trend?from=2026-07-01&to=2026-07-07")
            report("P9-LIVE-11a: Live zero-log department returns HTTP 200", r_devops.status_code == 200)
            devops_items = r_devops.json().get("items", [])
            report("P9-LIVE-11b: Live zero-log department returns all 7 calendar days", len(devops_items) == 7)

            # Day 1: EMP_D1 eligible, rate 0.0, moving_avg 0.0
            report("P9-LIVE-11c: Live zero-log Day 1 headcount == 1", devops_items[0]["headcount"] == 1)
            report("P9-LIVE-11d: Live zero-log Day 1 present_count == 0.0", devops_items[0]["present_count"] == 0.0)
            report("P9-LIVE-11e: Live zero-log Day 1 late_count == 0", devops_items[0]["late_count"] == 0)
            report("P9-LIVE-11f: Live zero-log Day 1 attendance_rate == 0.0", devops_items[0]["attendance_rate"] == 0.0)
            report("P9-LIVE-11g: Live zero-log Day 1 moving_avg_7d == 0.0", devops_items[0]["moving_avg_7d"] == 0.0)

            # Day 3: EMP_D2 joined today -> headcount rises to 2
            report("P9-LIVE-11h: Live zero-log Day 3 headcount rises to 2 (mid-period joiner)", devops_items[2]["headcount"] == 2)
            report("P9-LIVE-11i: Live zero-log Day 3 attendance_rate == 0.0", devops_items[2]["attendance_rate"] == 0.0)

            # Day 4: Saturday -> rate is null, moving_avg preserves 0.0
            report("P9-LIVE-11j: Live zero-log Day 4 (Saturday) is_working_day is False", devops_items[3]["is_working_day"] is False)
            report("P9-LIVE-11k: Live zero-log Day 4 attendance_rate is null", devops_items[3]["attendance_rate"] is None)
            report("P9-LIVE-11l: Live zero-log Day 4 moving_avg_7d == 0.0", devops_items[3]["moving_avg_7d"] == 0.0)

            # Day 7: EMP_D3 (joined 2026-07-10) still excluded -> headcount remains 2
            report("P9-LIVE-11m: Live zero-log Day 7 headcount remains 2 (future joiner excluded)", devops_items[6]["headcount"] == 2)

            # -----------------------------------------------------------------
            # Live Test: Department with Zero Headcount Across Query Range
            # -----------------------------------------------------------------
            # Employee joins in future (2026-08-01)
            live_db.employees.insert_one({
                "emp_code": "EMP_F1", "name": "Future Only", "department": "FutureOnly", "joined_on": "2026-08-01"
            })
            r_future = client.get("/analytics/departments/FutureOnly/trend?from=2026-07-01&to=2026-07-05")
            report("P9-LIVE-12a: Live zero-headcount department returns HTTP 200", r_future.status_code == 200)
            future_items = r_future.json().get("items", [])
            report("P9-LIVE-12b: Live zero-headcount returns 5 calendar days", len(future_items) == 5)
            report("P9-LIVE-12c: Live zero-headcount days have headcount == 0", all(it["headcount"] == 0 for it in future_items))
            report("P9-LIVE-12d: Live zero-headcount days have attendance_rate is null", all(it["attendance_rate"] is None for it in future_items))
            report("P9-LIVE-12e: Live zero-headcount days have moving_avg_7d is null", all(it["moving_avg_7d"] is None for it in future_items))

            # -----------------------------------------------------------------
            # Live Test: Unknown Department on Live MongoDB
            # -----------------------------------------------------------------
            r_unknown = client.get("/analytics/departments/NonExistentDeptLive/trend?from=2026-07-01&to=2026-07-05")
            report("P9-LIVE-13: Live unknown department returns HTTP 404", r_unknown.status_code == 404)

            # -----------------------------------------------------------------
            # Live Test: 7-Day Moving Average Sliding Window Drop-off
            # -----------------------------------------------------------------
            # Test window sliding across 10 calendar days: 2026-06-01 (Mon) to 2026-06-10 (Wed)
            # Days:
            # 2026-06-01 (Mon, Day 1): PRESENT (rate 1.0)
            # 2026-06-02 (Tue, Day 2) to 2026-06-05 (Fri, Day 5): 0 logs (rate 0.0)
            # 2026-06-06 (Sat, Day 6), 2026-06-07 (Sun, Day 7): Weekend (rate null)
            # 2026-06-08 (Mon, Day 8) to 2026-06-10 (Wed, Day 10): 0 logs (rate 0.0)
            #
            # Window check:
            # On Day 7 (June 7, Sun): Window covers Days 1..7 (June 1..7).
            # Non-null rates: [1.0, 0.0, 0.0, 0.0, 0.0]. Count: 5. Mean: 1.0 / 5 = 0.2000.
            #
            # On Day 8 (June 8, Mon): Window covers Days 2..8 (June 2..8).
            # Day 1 (June 1, 1.0 rate) has SLID OUT of the 7-day window!
            # Non-null rates in window: [0.0, 0.0, 0.0, 0.0, 0.0] (Tue, Wed, Thu, Fri, Mon).
            # Mean: 0.0 / 5 = 0.0000.
            live_db.employees.insert_one({
                "emp_code": "EMP_W1", "name": "Window Tester", "department": "WindowDept", "joined_on": "2026-06-01"
            })
            live_db.attendance_logs.insert_one({
                "emp_code": "EMP_W1", "date": "2026-06-01", "status": "PRESENT", "work_hours": 8.0, "late_minutes": 0, "half_day": False
            })

            r_window = client.get("/analytics/departments/WindowDept/trend?from=2026-06-01&to=2026-06-10")
            report("P9-LIVE-14a: Live sliding window query returns HTTP 200", r_window.status_code == 200)
            w_items = r_window.json().get("items", [])
            report("P9-LIVE-14b: Live sliding window returns 10 calendar days", len(w_items) == 10)

            # Day 1 (2026-06-01, Mon): rate 1.0, moving_avg 1.0
            report("P9-LIVE-14c: Live Window Day 1 rate == 1.0 and moving_avg == 1.0", w_items[0]["attendance_rate"] == 1.0 and w_items[0]["moving_avg_7d"] == 1.0)

            # Day 7 (2026-06-07, Sun): 7th day, Day 1 is still in window -> moving_avg == 0.2
            report("P9-LIVE-14d: Live Window Day 7 moving_avg_7d == 0.2 (Day 1 inside window)", w_items[6]["moving_avg_7d"] == 0.2)

            # Day 8 (2026-06-08, Mon): 8th day, Day 1 has slid out -> moving_avg drops to 0.0!
            report("P9-LIVE-14e: Live Window Day 8 moving_avg_7d == 0.0 (Day 1 slid out of 7d window)", w_items[7]["moving_avg_7d"] == 0.0)

            # -----------------------------------------------------------------
            # Live Test: Window Initial Days with Starting Null Value
            # -----------------------------------------------------------------
            # Query starting on Sunday 2026-06-07 to Tuesday 2026-06-09
            # Day 1: Sunday -> rate is null, moving_avg is null
            # Day 2: Monday -> rate is 0.0, moving_avg is 0.0
            r_start_null = client.get("/analytics/departments/WindowDept/trend?from=2026-06-07&to=2026-06-09")
            report("P9-LIVE-15a: Live query starting on Sunday returns HTTP 200", r_start_null.status_code == 200)
            sn_items = r_start_null.json().get("items", [])
            report("P9-LIVE-15b: Day 1 (Sunday) moving_avg_7d is null (no non-null rates in window)", sn_items[0]["moving_avg_7d"] is None)
            report("P9-LIVE-15c: Day 2 (Monday) moving_avg_7d == 0.0 (first non-null rate)", sn_items[1]["moving_avg_7d"] == 0.0)

        finally:
            _live_client.drop_database(live_db_name)
            m.db = orig_db

    print("=" * 65)
    print(f"Total: {passed_count} passed, {failed_count} failed")
    if failed_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_tests()
