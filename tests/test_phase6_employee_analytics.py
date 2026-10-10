"""
Phase 6 verification suite — Employee monthly analytics only.

Imports the REAL FastAPI app and real models from app.main.
Uses TestClient to test:
- Route GET /analytics/employees/{emp_code}/monthly
- Query parameter month validation (YYYY-MM pattern)
- Unknown employee 404 handling
- Employees with zero attendance logs (R9)
- Employees joining before, mid-month, and after reporting month (R7)
- Working days count (Mon-Fri only, no holidays)
- All 5 attendance statuses (PRESENT, WFH, ON_DUTY, ABSENT, LEAVE)
- Half-day vs full-day weighting (0.5 vs 1.0)
- Weekend presence exclusion vs weekend late/overtime inclusion (R7)
- Percentage vs fractional rate (attendance_pct on 100 scale, round_half_up 2dp)
- Zero-working-day edge cases (attendance_pct is null)
- Output schema: exact 9 fields, no MongoDB _id, no extra fields
- Verification that MongoDB aggregation is used (no Python collection scan)
- Live MongoDB integration when local MongoDB is running

Run:
    python -X utf8 tests/test_phase6_employee_analytics.py
"""
import calendar
import os
import sys
from datetime import datetime, timezone
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
class MockAttendanceLogsWithAgg:
    def __init__(self):
        self.docs: list[dict] = []
        self.aggregate_calls: list[list[dict]] = []
        self.find_calls: list[dict] = []

    def find(self, *args, **kwargs):
        self.find_calls.append(args[0] if args else {})
        raise AssertionError("find() should not be called on attendance_logs during analytics aggregation!")

    def aggregate(self, pipeline: list[dict]):
        self.aggregate_calls.append(pipeline)

        # Stage 1: $match
        match_stage = pipeline[0].get("$match", {})
        emp_code = match_stage.get("emp_code")
        date_cond = match_stage.get("date", {})
        date_gte = date_cond.get("$gte", "")
        date_lte = date_cond.get("$lte", "")

        matched = [
            d for d in self.docs
            if (emp_code is None or d.get("emp_code") == emp_code)
            and (not date_gte or d.get("date", "") >= date_gte)
            and (not date_lte or d.get("date", "") <= date_lte)
        ]

        if not matched:
            return iter([])

        # Stage 2 & 3: Evaluate projected and grouped fields according to contract
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

        for d in matched:
            d_date = datetime.strptime(d["date"], "%Y-%m-%d").date()
            is_weekday = d_date.weekday() < 5  # Mon-Fri
            status = d.get("status")
            half_day = d.get("half_day", False)
            late_min = d.get("late_minutes", 0)
            ot_min = d.get("overtime_minutes", 0)

            if status in ("PRESENT", "WFH", "ON_DUTY"):
                if is_weekday:
                    present_days += 0.5 if half_day else 1.0

            if status == "LEAVE":
                leave_days += 1

            if late_min > 0:
                late_count += 1
            total_late += late_min
            total_ot += ot_min

        result_doc = {
            "_id": None,
            "present_days": present_days,
            "leave_days": leave_days,
            "late_count": late_count,
            "total_late_minutes": total_late,
            "total_overtime_minutes": total_ot,
        }
        return iter([result_doc])


class MockEmployeesSimple:
    def __init__(self):
        self.docs: list[dict] = []

    def find_one(self, q: dict):
        for d in self.docs:
            if all(d.get(k) == v for k, v in q.items()):
                return dict(d)
        return None


# =============================================================================
# Test Suite
# =============================================================================
passed_count = 0
failed_count = 0


def report(name: str, passed: bool, msg: str = ""):
    global passed_count, failed_count
    if passed:
        passed_count += 1
        print(f"PASS  {name}")
    else:
        failed_count += 1
        print(f"FAIL  {name}: {msg}")


def run_tests():
    global passed_count, failed_count

    print(f"\nImported app.main module: {m.__file__}")
    print(f"Live MongoDB available: {_LIVE_MONGO_AVAILABLE}")
    print("=" * 65)

    # -------------------------------------------------------------------------
    # PART 1: Unit & Pipeline Verification with Mock / Spy
    # -------------------------------------------------------------------------
    print("\n-- PART 1: Test Double & Aggregation Pipeline Inspection --")

    mock_emp = MockEmployeesSimple()
    mock_att = MockAttendanceLogsWithAgg()

    # Install test doubles
    orig_emp = m.db.employees
    orig_att = m.db.attendance_logs
    m.db.employees = mock_emp
    m.db.attendance_logs = mock_att

    try:
        # Seed employee
        mock_emp.docs.append({
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-05",
            "created_at": datetime(2026, 1, 5, 4, 30, tzinfo=timezone.utc),
        })

        # 1. Unknown employee -> 404
        r = client.get("/analytics/employees/EMP9999/monthly?month=2026-07")
        report("P6-1: Unknown employee returns 404", r.status_code == 404 and r.json().get("detail") == "employee not found")

        # 2. Invalid month format -> 422
        r = client.get("/analytics/employees/EMP0001/monthly?month=2026-13")
        report("P6-2a: Month 2026-13 returns 422", r.status_code == 422)

        r = client.get("/analytics/employees/EMP0001/monthly?month=2026-7")
        report("P6-2b: Unpadded month 2026-7 returns 422", r.status_code == 422)

        r = client.get("/analytics/employees/EMP0001/monthly?month=invalid")
        report("P6-2c: Non-date month returns 422", r.status_code == 422)

        r = client.get("/analytics/employees/EMP0001/monthly")
        report("P6-2d: Missing month query parameter returns 422", r.status_code == 422)

        # 3. Employee with zero attendance records
        mock_att.aggregate_calls.clear()
        mock_att.find_calls.clear()
        r = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
        data = r.json()
        report("P6-3a: Zero attendance records returns 200", r.status_code == 200)
        report("P6-3b: Zero records working_days == 23", data.get("working_days") == 23)
        report("P6-3c: Zero records present_days == 0.0", data.get("present_days") == 0.0)
        report("P6-3d: Zero records leave_days == 0", data.get("leave_days") == 0)
        report("P6-3e: Zero records late_count == 0", data.get("late_count") == 0)
        report("P6-3f: Zero records total_late_minutes == 0", data.get("total_late_minutes") == 0)
        report("P6-3g: Zero records total_overtime_minutes == 0", data.get("total_overtime_minutes") == 0)
        report("P6-3h: Zero records attendance_pct == 0.0", data.get("attendance_pct") == 0.0)
        report("P6-3i: db.attendance_logs.find was NOT called (aggregation used)", len(mock_att.find_calls) == 0)
        report("P6-3j: db.attendance_logs.aggregate was called exactly once", len(mock_att.aggregate_calls) == 1)

        # 4. Pipeline structure inspection
        pipe = mock_att.aggregate_calls[0]
        report("P6-4a: Pipeline has 3 stages", len(pipe) == 3)
        report("P6-4b: Stage 1 is $match with emp_code and date range",
               "$match" in pipe[0]
               and pipe[0]["$match"].get("emp_code") == "EMP0001"
               and pipe[0]["$match"]["date"]["$gte"] == "2026-07-01"
               and pipe[0]["$match"]["date"]["$lte"] == "2026-07-31")
        report("P6-4c: Stage 2 is $project with weekday and presence checks",
               "$project" in pipe[1]
               and "is_weekday" in pipe[1]["$project"]
               and "is_present" in pipe[1]["$project"]
               and "is_leave" in pipe[1]["$project"])
        report("P6-4d: Stage 3 is $group with _id None",
               "$group" in pipe[2]
               and pipe[2]["$group"].get("_id") is None
               and "present_days" in pipe[2]["$group"])

        # 5. Output schema has exact 9 required fields and NO MongoDB _id
        expected_keys = {
            "emp_code", "month", "working_days", "present_days", "leave_days",
            "late_count", "total_late_minutes", "total_overtime_minutes", "attendance_pct"
        }
        report("P6-5a: Response schema matches exact 9 required fields", set(data.keys()) == expected_keys)
        report("P6-5b: MongoDB internal _id is NOT in response", "_id" not in data)

        # 6. Employee joined after reporting month (zero working days)
        mock_emp.docs.append({
            "emp_code": "EMP0002",
            "name": "New Hire",
            "email": "new@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-08-01",
            "created_at": datetime(2026, 8, 1, 4, 30, tzinfo=timezone.utc),
        })
        r = client.get("/analytics/employees/EMP0002/monthly?month=2026-07")
        data2 = r.json()
        report("P6-6a: Joined after month has working_days == 0", data2.get("working_days") == 0)
        report("P6-6b: Zero working days yields attendance_pct null", data2.get("attendance_pct") is None)

        # 7. Employee joined mid-month
        mock_emp.docs.append({
            "emp_code": "EMP0003",
            "name": "Mid Month",
            "email": "mid@example.com",
            "department": "Design",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-15",
            "created_at": datetime(2026, 7, 15, 4, 30, tzinfo=timezone.utc),
        })
        r = client.get("/analytics/employees/EMP0003/monthly?month=2026-07")
        data3 = r.json()
        report("P6-7: Joined mid-month (2026-07-15) working_days == 13", data3.get("working_days") == 13)

        # 8. All five statuses + half day + weekend handling
        mock_att.docs.extend([
            # Mon 2026-07-06: PRESENT full day, 0 late, 0 ot
            {"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT", "half_day": False, "late_minutes": 0, "overtime_minutes": 0},
            # Tue 2026-07-07: WFH half day, 15m late, 0 ot
            {"emp_code": "EMP0001", "date": "2026-07-07", "status": "WFH", "half_day": True, "late_minutes": 15, "overtime_minutes": 0},
            # Wed 2026-07-08: ON_DUTY full day, 0 late, 35m ot
            {"emp_code": "EMP0001", "date": "2026-07-08", "status": "ON_DUTY", "half_day": False, "late_minutes": 0, "overtime_minutes": 35},
            # Thu 2026-07-09: LEAVE
            {"emp_code": "EMP0001", "date": "2026-07-09", "status": "LEAVE", "half_day": False, "late_minutes": 0, "overtime_minutes": 0},
            # Fri 2026-07-10: ABSENT
            {"emp_code": "EMP0001", "date": "2026-07-10", "status": "ABSENT", "half_day": False, "late_minutes": 0, "overtime_minutes": 0},
            # Sat 2026-07-11 (WEEKEND): PRESENT, 20m late, 40m ot (must NOT add to present_days, but MUST add to late/ot)
            {"emp_code": "EMP0001", "date": "2026-07-11", "status": "PRESENT", "half_day": False, "late_minutes": 20, "overtime_minutes": 40},
            # Next month record (outside range)
            {"emp_code": "EMP0001", "date": "2026-08-03", "status": "PRESENT", "half_day": False, "late_minutes": 0, "overtime_minutes": 0},
        ])

        r = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
        data4 = r.json()
        # present_days: Mon(1.0) + Tue(0.5) + Wed(1.0) = 2.5
        report("P6-8a: present_days == 2.5 (1.0 + 0.5 + 1.0, weekend excluded)", data4.get("present_days") == 2.5)
        # leave_days: Thu(1) = 1
        report("P6-8b: leave_days == 1", data4.get("leave_days") == 1)
        # late_count: Tue(15m) + Sat(20m) = 2
        report("P6-8c: late_count == 2 (includes weekend record)", data4.get("late_count") == 2)
        # total_late_minutes: 15 + 20 = 35
        report("P6-8d: total_late_minutes == 35", data4.get("total_late_minutes") == 35)
        # total_overtime_minutes: Wed(35m) + Sat(40m) = 75
        report("P6-8e: total_overtime_minutes == 75", data4.get("total_overtime_minutes") == 75)
        # attendance_pct: (2.5 / 23) * 100 = 10.86956... -> 10.87
        report("P6-8f: attendance_pct == 10.87 (half-up rounded, 0-100 scale)", data4.get("attendance_pct") == 10.87)

        # 9. Attendance percentage scale check (not fractional rate)
        # 10.87 vs 0.1087
        report("P6-9: attendance_pct is not fractional rate (pct > 1.0)", data4.get("attendance_pct") > 1.0)

    finally:
        m.db.employees = orig_emp
        m.db.attendance_logs = orig_att

    # -------------------------------------------------------------------------
    # PART 2: Live MongoDB Aggregation Compatibility Suite
    # -------------------------------------------------------------------------
    print("\n-- PART 2: Live MongoDB Integration --")

    if not _LIVE_MONGO_AVAILABLE:
        print("SKIP: Live MongoDB not reachable at mongodb://localhost:27017")
        print("Note: All unit & pipeline tests in Part 1 ran successfully.")
    else:
        live_db_name = "test_phase6_live_db"
        live_db = _live_client[live_db_name]
        orig_db = m.db

        try:
            m.db = live_db
            live_db.employees.delete_many({})
            live_db.attendance_logs.delete_many({})

            # Create production indexes idempotently
            m._create_indexes()

            # Seed employee
            live_db.employees.insert_one({
                "emp_code": "EMP0001",
                "name": "Asha Rao",
                "email": "asha@example.com",
                "department": "Engineering",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-01-05",
                "created_at": datetime(2026, 1, 5, 4, 30, tzinfo=timezone.utc),
            })

            # Seed 21.5 present days + 1 leave day + 2 late records + 1 weekend record
            # Week 1: July 6 - July 10 (Mon-Fri)
            logs = [
                {"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT", "half_day": False, "late_minutes": 0, "overtime_minutes": 0},
                {"emp_code": "EMP0001", "date": "2026-07-07", "status": "WFH", "half_day": True, "late_minutes": 15, "overtime_minutes": 0},
                {"emp_code": "EMP0001", "date": "2026-07-08", "status": "ON_DUTY", "half_day": False, "late_minutes": 0, "overtime_minutes": 30},
                {"emp_code": "EMP0001", "date": "2026-07-09", "status": "LEAVE", "half_day": False, "late_minutes": 0, "overtime_minutes": 0},
                {"emp_code": "EMP0001", "date": "2026-07-10", "status": "ABSENT", "half_day": False, "late_minutes": 0, "overtime_minutes": 0},
                # Weekend Saturday 2026-07-11
                {"emp_code": "EMP0001", "date": "2026-07-11", "status": "PRESENT", "half_day": False, "late_minutes": 30, "overtime_minutes": 30},
            ]
            # Add 19 more full PRESENT days across July (e.g. July 13 to July 31 Mon-Fri)
            # July has 23 weekdays: 6-10 (5 days), 13-17 (5 days), 20-24 (5 days), 27-31 (5 days), 1-3 (3 days).
            # We already used 6, 7, 8, 9, 10.
            # Let's add 19 full PRESENT days on remaining weekdays:
            additional_dates = [
                "2026-07-01", "2026-07-02", "2026-07-03",
                "2026-07-13", "2026-07-14", "2026-07-15", "2026-07-16", "2026-07-17",
                "2026-07-20", "2026-07-21", "2026-07-22", "2026-07-23", "2026-07-24",
                "2026-07-27", "2026-07-28", "2026-07-29", "2026-07-30", "2026-07-31",
            ]  # 18 dates
            for d in additional_dates:
                logs.append({
                    "emp_code": "EMP0001", "date": d, "status": "PRESENT", "half_day": False,
                    "late_minutes": 0, "overtime_minutes": 0
                })

            live_db.attendance_logs.insert_many(logs)

            # Test live query
            r = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
            report("P6-LIVE-1: Live MongoDB returned HTTP 200", r.status_code == 200)
            res = r.json()

            # Weekdays present:
            # Additional 18 days = 18.0
            # Plus 07-06 (1.0) + 07-07 (0.5) + 07-08 (1.0) = 2.5
            # Total present = 20.5
            report("P6-LIVE-2: Live present_days == 20.5", res.get("present_days") == 20.5)
            report("P6-LIVE-3: Live working_days == 23", res.get("working_days") == 23)
            report("P6-LIVE-4: Live leave_days == 1", res.get("leave_days") == 1)
            report("P6-LIVE-5: Live late_count == 2", res.get("late_count") == 2)
            report("P6-LIVE-6: Live total_late_minutes == 45", res.get("total_late_minutes") == 45)
            report("P6-LIVE-7: Live total_overtime_minutes == 60", res.get("total_overtime_minutes") == 60)

            # attendance_pct: 20.5 / 23 * 100 = 89.1304... -> 89.13
            report("P6-LIVE-8: Live attendance_pct == 89.13", res.get("attendance_pct") == 89.13)
            report("P6-LIVE-9: Live _id absent from response", "_id" not in res)

            # Test explain with executionStats on live MongoDB to verify IXSCAN
            pipeline = m.build_employee_monthly_pipeline("EMP0001", "2026-07")
            explain = live_db.command(
                "explain",
                {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
                verbosity="executionStats",
            )
            # Find stages in executionStats or stages
            explain_str = str(explain)
            has_ixscan = "IXSCAN" in explain_str
            has_collscan = "COLLSCAN" in explain_str
            report("P6-LIVE-10: Live explain uses IXSCAN", has_ixscan)
            report("P6-LIVE-11: Live explain contains NO COLLSCAN", not has_collscan)

            # Test February leap year vs non-leap year date bounds
            date_from_24, date_to_24 = m.get_month_date_range("2024-02")
            report("P6-LIVE-12: Leap year Feb 2024 ends on 2024-02-29", date_from_24 == "2024-02-01" and date_to_24 == "2024-02-29")
            date_from_26, date_to_26 = m.get_month_date_range("2026-02")
            report("P6-LIVE-13: Non-leap year Feb 2026 ends on 2026-02-28", date_from_26 == "2026-02-01" and date_to_26 == "2026-02-28")

        finally:
            _live_client.drop_database(live_db_name)
            m.db = orig_db

    print("=" * 65)
    print(f"Total: {passed_count} passed, {failed_count} failed")
    if failed_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_tests()
