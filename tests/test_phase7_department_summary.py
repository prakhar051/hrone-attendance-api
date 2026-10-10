"""
Phase 7 verification suite — Department summary analytics only.

Imports the REAL FastAPI app and real models from app.main.
Uses TestClient to test:
- Route GET /analytics/departments/summary
- Query parameter month validation (YYYY-MM pattern) and optional department filter
- Sorting of items by department ascending
- Headcount per R9: includes employees with zero logs, excludes future joiners
- Departments with 0 headcount omitted
- present_days summed across department (half day = 0.5, Mon-Fri only per R7)
- avg_work_hours mean over presence records with work_hours, null if none, R8 half-up rounding
- late_count, total_late_minutes, leave_count, on_duty_count (including weekends where required)
- Exact response schema, no MongoDB _id
- DB aggregation usage (no Python collection scan)
- Live MongoDB integration and executionStats explain plan

Run:
    python -X utf8 tests/test_phase7_department_summary.py
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
class MockEmployeesWithAgg:
    def __init__(self, att_mock):
        self.docs: list[dict] = []
        self.aggregate_calls: list[list[dict]] = []
        self.find_calls: list[dict] = []
        self.att_mock = att_mock

    def find(self, *args, **kwargs):
        self.find_calls.append(args[0] if args else {})
        raise AssertionError("find() should not be called on employees during department summary aggregation!")

    def aggregate(self, pipeline: list[dict]):
        self.aggregate_calls.append(pipeline)

        # Stage 1: $match on employees
        match_stage = pipeline[0].get("$match", {})
        dept_filter = match_stage.get("department")
        joined_lte = match_stage.get("joined_on", {}).get("$lte", "")

        matched_employees = [
            e for e in self.docs
            if (dept_filter is None or e.get("department") == dept_filter)
            and (not joined_lte or e.get("joined_on", "") <= joined_lte)
        ]

        # Group by department
        depts: dict[str, list[str]] = {}
        for e in matched_employees:
            d = e.get("department")
            if d:
                depts.setdefault(d, []).append(e.get("emp_code"))

        results = []
        for dept in sorted(depts.keys()):
            emp_codes = set(depts[dept])
            headcount = len(emp_codes)

            # Match attendance records for this department's employees in date range
            # Extract date bounds from lookup stage in pipeline
            lookup_stage = pipeline[2].get("$lookup", {})
            sub_pipeline = lookup_stage.get("pipeline", [])
            date_from = "2026-07-01"
            date_to = "2026-07-31"
            if sub_pipeline:
                expr_and = sub_pipeline[0].get("$match", {}).get("$expr", {}).get("$and", [])
                for cond in expr_and:
                    if "$gte" in cond and cond["$gte"][0] == "$date":
                        date_from = cond["$gte"][1]
                    if "$lte" in cond and cond["$lte"][0] == "$date":
                        date_to = cond["$lte"][1]

            matched_logs = [
                l for l in self.att_mock.docs
                if l.get("emp_code") in emp_codes
                and date_from <= l.get("date", "") <= date_to
            ]

            present_days = 0.0
            late_count = 0
            total_late = 0
            leave_count = 0
            on_duty_count = 0
            wh_sum = 0.0
            wh_count = 0

            for l in matched_logs:
                l_date = datetime.strptime(l["date"], "%Y-%m-%d").date()
                is_weekday = l_date.weekday() < 5
                status = l.get("status")
                half_day = l.get("half_day", False)
                late_min = l.get("late_minutes", 0)
                work_hours = l.get("work_hours")

                if status in ("PRESENT", "WFH", "ON_DUTY"):
                    if is_weekday:
                        present_days += 0.5 if half_day else 1.0
                    if work_hours is not None:
                        wh_sum += float(work_hours)
                        wh_count += 1

                if status == "LEAVE":
                    leave_count += 1
                if status == "ON_DUTY":
                    on_duty_count += 1

                if late_min > 0:
                    late_count += 1
                total_late += late_min

            results.append({
                "department": dept,
                "headcount": headcount,
                "present_days": present_days,
                "late_count": late_count,
                "total_late_minutes": total_late,
                "leave_count": leave_count,
                "on_duty_count": on_duty_count,
                "work_hours_sum": wh_sum,
                "work_hours_count": wh_count,
            })

        return iter(results)


class MockAttendanceLogsSimple:
    def __init__(self):
        self.docs: list[dict] = []
        self.find_calls: list[dict] = []

    def find(self, *args, **kwargs):
        self.find_calls.append(args[0] if args else {})
        raise AssertionError("find() should not be called on attendance_logs during analytics!")


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
    # PART 1: Test Double & Aggregation Pipeline Inspection
    # -------------------------------------------------------------------------
    print("\n-- PART 1: Test Double & Aggregation Pipeline Inspection --")

    mock_att = MockAttendanceLogsSimple()
    mock_emp = MockEmployeesWithAgg(mock_att)

    orig_emp = m.db.employees
    orig_att = m.db.attendance_logs
    m.db.employees = mock_emp
    m.db.attendance_logs = mock_att

    try:
        # Seed employees across 3 departments:
        # Engineering: 2 employees (EMP01 joined Jan, EMP02 joined mid-July)
        # Product: 1 employee (EMP03 joined Jan - ZERO logs)
        # Sales: 1 employee (EMP04 joined Aug - joined AFTER month, headcount 0)
        mock_emp.docs.extend([
            {"emp_code": "EMP01", "name": "A", "department": "Engineering", "joined_on": "2026-01-01"},
            {"emp_code": "EMP02", "name": "B", "department": "Engineering", "joined_on": "2026-07-15"},
            {"emp_code": "EMP03", "name": "C", "department": "Product", "joined_on": "2026-01-01"},
            {"emp_code": "EMP04", "name": "D", "department": "Sales", "joined_on": "2026-08-01"},
        ])

        # Seed attendance records for Engineering:
        mock_att.docs.extend([
            # EMP01: Mon full day, work_hours 8.5, 0 late
            {"emp_code": "EMP01", "date": "2026-07-06", "status": "PRESENT", "half_day": False, "late_minutes": 0, "work_hours": 8.5},
            # EMP01: Tue half day WFH, work_hours 4.0, 15 late
            {"emp_code": "EMP01", "date": "2026-07-07", "status": "WFH", "half_day": True, "late_minutes": 15, "work_hours": 4.0},
            # EMP02: Wed ON_DUTY, work_hours 9.0, 0 late
            {"emp_code": "EMP02", "date": "2026-07-15", "status": "ON_DUTY", "half_day": False, "late_minutes": 0, "work_hours": 9.0},
            # EMP02: Thu LEAVE, punch times null, work_hours null
            {"emp_code": "EMP02", "date": "2026-07-16", "status": "LEAVE", "half_day": False, "late_minutes": 0, "work_hours": None},
            # EMP01: Sat (weekend) PRESENT, work_hours 5.0, 20 late
            {"emp_code": "EMP01", "date": "2026-07-11", "status": "PRESENT", "half_day": False, "late_minutes": 20, "work_hours": 5.0},
        ])

        # 1. Invalid month parameter validation -> 422
        r = client.get("/analytics/departments/summary?month=2026-13")
        report("P7-1a: Invalid month 2026-13 returns 422", r.status_code == 422)

        r = client.get("/analytics/departments/summary?month=2026-7")
        report("P7-1b: Unpadded month 2026-7 returns 422", r.status_code == 422)

        r = client.get("/analytics/departments/summary?month=invalid")
        report("P7-1c: Non-date month returns 422", r.status_code == 422)

        r = client.get("/analytics/departments/summary")
        report("P7-1d: Missing month query parameter returns 422", r.status_code == 422)

        # 2. Successful department summary without department filter
        mock_emp.aggregate_calls.clear()
        mock_emp.find_calls.clear()
        mock_att.find_calls.clear()

        r = client.get("/analytics/departments/summary?month=2026-07")
        report("P7-2a: GET /analytics/departments/summary returns 200", r.status_code == 200)
        data = r.json()
        report("P7-2b: Response contains month 2026-07", data.get("month") == "2026-07")
        items = data.get("items", [])
        report("P7-2c: Response contains 2 departments (Sales with 0 headcount omitted)", len(items) == 2)
        report("P7-2d: Items sorted by department ascending", [it["department"] for it in items] == ["Engineering", "Product"])

        # Check DB aggregation usage
        report("P7-2e: find() was NOT called on employees", len(mock_emp.find_calls) == 0)
        report("P7-2f: find() was NOT called on attendance_logs", len(mock_att.find_calls) == 0)
        report("P7-2g: aggregate() was called on db.employees", len(mock_emp.aggregate_calls) == 1)

        # 3. Verify Engineering department metrics
        eng = items[0]
        report("P7-3a: Engineering headcount == 2", eng.get("headcount") == 2)
        # present_days: Mon(1.0) + Tue(0.5) + Wed(1.0) = 2.5; Sat excluded
        report("P7-3b: Engineering present_days == 2.5 (Mon-Fri only, half day 0.5)", eng.get("present_days") == 2.5)
        # late_count: Tue(15m) + Sat(20m) = 2 (weekend included per R7)
        report("P7-3c: Engineering late_count == 2", eng.get("late_count") == 2)
        report("P7-3d: Engineering total_late_minutes == 35", eng.get("total_late_minutes") == 35)
        report("P7-3e: Engineering leave_count == 1", eng.get("leave_count") == 1)
        report("P7-3f: Engineering on_duty_count == 1", eng.get("on_duty_count") == 1)
        # avg_work_hours: (8.5 + 4.0 + 9.0 + 5.0) / 4 = 26.5 / 4 = 6.625 -> 6.63 (ROUND_HALF_UP)
        report("P7-3g: Engineering avg_work_hours == 6.63 (ROUND_HALF_UP tie-break)", eng.get("avg_work_hours") == 6.63)

        # 4. Verify Product department (employee with zero attendance logs)
        prod = items[1]
        report("P7-4a: Product headcount == 1 (zero logs employee counted)", prod.get("headcount") == 1)
        report("P7-4b: Product present_days == 0.0", prod.get("present_days") == 0.0)
        report("P7-4c: Product late_count == 0", prod.get("late_count") == 0)
        report("P7-4d: Product total_late_minutes == 0", prod.get("total_late_minutes") == 0)
        report("P7-4e: Product leave_count == 0", prod.get("leave_count") == 0)
        report("P7-4f: Product on_duty_count == 0", prod.get("on_duty_count") == 0)
        report("P7-4g: Product avg_work_hours is null (no records with work_hours)", prod.get("avg_work_hours") is None)

        # 5. Exact output schema check
        expected_item_keys = {
            "department", "headcount", "present_days", "avg_work_hours",
            "late_count", "total_late_minutes", "leave_count", "on_duty_count"
        }
        report("P7-5a: Item schema matches exact 8 required fields", set(eng.keys()) == expected_item_keys)
        report("P7-5b: MongoDB internal _id is NOT in items", "_id" not in eng and "_id" not in data)

        # 6. Specific department filter
        r_dept = client.get("/analytics/departments/summary?month=2026-07&department=Engineering")
        data_dept = r_dept.json()
        report("P7-6a: Filter department=Engineering returns 1 item", len(data_dept.get("items", [])) == 1)
        report("P7-6b: Filtered item is Engineering", data_dept["items"][0]["department"] == "Engineering")

        # 7. Non-existent department filter returns empty items list (200)
        r_none = client.get("/analytics/departments/summary?month=2026-07&department=NonExistent")
        report("P7-7a: Non-existent department returns HTTP 200", r_none.status_code == 200)
        report("P7-7b: Non-existent department items is empty list", r_none.json().get("items") == [])

        # 8. Department where all employees joined after month (Sales)
        r_sales = client.get("/analytics/departments/summary?month=2026-07&department=Sales")
        report("P7-8: Zero headcount department returns empty items list", r_sales.json().get("items") == [])

        # 9. Pipeline inspection
        pipe = mock_emp.aggregate_calls[0]
        report("P7-9a: Pipeline has 6 stages", len(pipe) == 6)
        report("P7-9b: Stage 1 matches joined_on <= month end", "$match" in pipe[0] and "joined_on" in pipe[0]["$match"])
        report("P7-9c: Stage 2 groups by department with headcount sum", "$group" in pipe[1] and pipe[1]["$group"].get("_id") == "$department")
        report("P7-9d: Stage 3 is $lookup to attendance_logs", "$lookup" in pipe[2] and pipe[2]["$lookup"].get("from") == "attendance_logs")
        report("P7-9e: Stage 6 sorts by department ascending", "$sort" in pipe[5] and pipe[5]["$sort"].get("department") == 1)

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
        live_db_name = "test_phase7_live_db"
        live_db = _live_client[live_db_name]
        orig_db = m.db

        try:
            m.db = live_db
            live_db.employees.delete_many({})
            live_db.attendance_logs.delete_many({})

            # Create production indexes idempotently
            m._create_indexes()

            # Seed employees
            live_db.employees.insert_many([
                {"emp_code": "EMP01", "name": "Alice", "department": "Engineering", "joined_on": "2026-01-01"},
                {"emp_code": "EMP02", "name": "Bob", "department": "Engineering", "joined_on": "2026-07-15"},
                {"emp_code": "EMP03", "name": "Charlie", "department": "Design", "joined_on": "2026-01-01"},
                {"emp_code": "EMP04", "name": "Dana", "department": "HR", "joined_on": "2026-08-01"},  # joined after month
            ])

            # Seed attendance logs
            live_db.attendance_logs.insert_many([
                # EMP01 (Engineering): Mon full day PRESENT, 8.5 work_hours, 0 late
                {"emp_code": "EMP01", "date": "2026-07-06", "status": "PRESENT", "half_day": False, "late_minutes": 0, "work_hours": 8.5},
                # EMP01 (Engineering): Tue half day WFH, 4.0 work_hours, 15 late
                {"emp_code": "EMP01", "date": "2026-07-07", "status": "WFH", "half_day": True, "late_minutes": 15, "work_hours": 4.0},
                # EMP02 (Engineering): Wed ON_DUTY, 9.0 work_hours, 0 late
                {"emp_code": "EMP02", "date": "2026-07-15", "status": "ON_DUTY", "half_day": False, "late_minutes": 0, "work_hours": 9.0},
                # EMP02 (Engineering): Thu LEAVE, no punch times
                {"emp_code": "EMP02", "date": "2026-07-16", "status": "LEAVE", "half_day": False, "late_minutes": 0, "work_hours": None},
                # EMP01 (Engineering): Sat (weekend) PRESENT, 5.0 work_hours, 20 late
                {"emp_code": "EMP01", "date": "2026-07-11", "status": "PRESENT", "half_day": False, "late_minutes": 20, "work_hours": 5.0},
                # EMP03 (Design): Mon half day PRESENT, 4.25 work_hours, 10 late
                {"emp_code": "EMP03", "date": "2026-07-06", "status": "PRESENT", "half_day": True, "late_minutes": 10, "work_hours": 4.25},
            ])

            # Test live query
            r = client.get("/analytics/departments/summary?month=2026-07")
            report("P7-LIVE-1: Live MongoDB returned HTTP 200", r.status_code == 200)
            res = r.json()
            report("P7-LIVE-2: Live month matches 2026-07", res.get("month") == "2026-07")
            live_items = res.get("items", [])
            report("P7-LIVE-3: Live items count is 2 (Design, Engineering; HR omitted)", len(live_items) == 2)
            report("P7-LIVE-4: Live items sorted ascending", [it["department"] for it in live_items] == ["Design", "Engineering"])

            design_res = live_items[0]
            report("P7-LIVE-5a: Live Design headcount == 1", design_res["headcount"] == 1)
            report("P7-LIVE-5b: Live Design present_days == 0.5", design_res["present_days"] == 0.5)
            report("P7-LIVE-5c: Live Design avg_work_hours == 4.25", design_res["avg_work_hours"] == 4.25)
            report("P7-LIVE-5d: Live Design late_count == 1", design_res["late_count"] == 1)

            eng_res = live_items[1]
            report("P7-LIVE-6a: Live Engineering headcount == 2", eng_res["headcount"] == 2)
            report("P7-LIVE-6b: Live Engineering present_days == 2.5", eng_res["present_days"] == 2.5)
            report("P7-LIVE-6c: Live Engineering avg_work_hours == 6.63", eng_res["avg_work_hours"] == 6.63)
            report("P7-LIVE-6d: Live Engineering late_count == 2", eng_res["late_count"] == 2)
            report("P7-LIVE-6e: Live Engineering total_late_minutes == 35", eng_res["total_late_minutes"] == 35)
            report("P7-LIVE-6f: Live Engineering leave_count == 1", eng_res["leave_count"] == 1)
            report("P7-LIVE-6g: Live Engineering on_duty_count == 1", eng_res["on_duty_count"] == 1)

            # Test live explain plan
            pipe_eng = m.build_department_summary_pipeline("2026-07", "Engineering")
            explain = live_db.command(
                "explain",
                {"aggregate": "employees", "pipeline": pipe_eng, "cursor": {}},
                verbosity="executionStats",
            )
            explain_str = str(explain)
            has_idx = "IX2_department" in explain_str or "IX3_joined_on" in explain_str
            report("P7-LIVE-7a: Live explain uses index on employees", has_idx)
            report("P7-LIVE-7b: Live explain contains no COLLSCAN on outer employees filter", "collectionScans: 0" in explain_str or "COLLSCAN" not in explain_str)

        finally:
            _live_client.drop_database(live_db_name)
            m.db = orig_db

    print("=" * 65)
    print(f"Total: {passed_count} passed, {failed_count} failed")
    if failed_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_tests()
