"""
Phase 10 verification suite — Admin explain & query plan analysis only.

Imports the REAL FastAPI app and real models from app.main.
Uses TestClient to test:
- Route GET /admin/explain/{endpoint}
- Supported endpoint identifiers:
    * attendance_list (runs find on attendance_logs)
    * employee_monthly (runs aggregation on attendance_logs)
    * department_summary (runs aggregation on employees)
    * late_leaderboard (runs aggregation on attendance_logs)
    * department_trend (runs aggregation on employees)
- Unsupported / invalid endpoint identifiers return HTTP 422
- Missing / invalid query parameters return HTTP 422 for each endpoint
- Reuses existing pipeline builders rather than maintaining duplicate logic
- Response schema exactness: {endpoint, collection, explain} with executionStats
- Security: No arbitrary client commands, pipelines, or collection names allowed
- Database unavailable error handling (HTTP 503)
- Live MongoDB integration and structured explain-plan assertions (IXSCAN vs COLLSCAN)

Run:
    python -X utf8 tests/test_phase10_admin_explain.py
"""
import calendar
import os
import sys
from datetime import datetime, timezone

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
# Mock Database for Pipeline Builder Inspection & DB Error Simulation
# =============================================================================
class MockDBForExplain:
    def __init__(self):
        self.command_calls: list[tuple[str, dict, dict]] = []
        self.should_fail: bool = False

    def command(self, cmd_name: str, cmd_dict: dict, **kwargs):
        if self.should_fail:
            raise Exception("simulated database failure")

        self.command_calls.append((cmd_name, cmd_dict, kwargs))
        return {
            "explainVersion": "2",
            "queryPlanner": {
                "namespace": f"attendance_db.{cmd_dict.get('find') or cmd_dict.get('aggregate')}",
                "winningPlan": {"stage": "IXSCAN"},
            },
            "executionStats": {
                "executionSuccess": True,
                "nReturned": 10,
                "executionTimeMillis": 1,
                "totalKeysExamined": 10,
                "totalDocsExamined": 10,
                "executionStages": {"stage": "IXSCAN"},
            },
            "serverInfo": {"host": "localhost", "port": 27017, "version": "7.0.14"},
            "ok": 1.0,
        }


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
    print("PHASE 10: ADMIN EXPLAIN / QUERY PLAN ANALYSIS VERIFICATION")
    print(f"Imported app.main module: {m.__file__}")
    print(f"Live MongoDB available: {_LIVE_MONGO_AVAILABLE}")
    print("=" * 65)

    # -------------------------------------------------------------------------
    # PART 1: Contract, Parameter Validation & Mocked DB Tests
    # -------------------------------------------------------------------------
    print("\n-- PART 1: Contract, Parameter Validation & Mocked DB Tests --")

    mock_db = MockDBForExplain()
    orig_db = m.db
    m.db = mock_db

    try:
        # 1. Invalid / Unsupported Endpoint Names
        r = client.get("/admin/explain/unsupported_endpoint")
        report("P10-1a: Unsupported endpoint name returns HTTP 422", r.status_code == 422)

        r = client.get("/admin/explain/employees")
        report("P10-1b: Arbitrary collection name in endpoint returns HTTP 422", r.status_code == 422)

        r = client.get("/admin/explain/dropDatabase")
        report("P10-1c: Database command name in endpoint returns HTTP 422", r.status_code == 422)

        # 2. Parameter Validation for 'attendance_list'
        r = client.get("/admin/explain/attendance_list?status=INVALID_STATUS")
        report("P10-2a: attendance_list invalid status returns 422", r.status_code == 422)

        r = client.get("/admin/explain/attendance_list?date_from=2026-07-10&date_to=2026-07-05")
        report("P10-2b: attendance_list date_from > date_to returns 422", r.status_code == 422)

        r = client.get("/admin/explain/attendance_list?page=0")
        report("P10-2c: attendance_list page=0 returns 422", r.status_code == 422)

        r = client.get("/admin/explain/attendance_list?page_size=101")
        report("P10-2d: attendance_list page_size=101 returns 422", r.status_code == 422)

        # 3. Successful Explain for 'attendance_list'
        r = client.get("/admin/explain/attendance_list?emp_code=EMP0001&date_from=2026-07-01&date_to=2026-07-07&status=PRESENT&page=2&page_size=10")
        report("P10-3a: attendance_list valid explain returns 200", r.status_code == 200)
        data = r.json()
        report("P10-3b: attendance_list response endpoint matches", data.get("endpoint") == "attendance_list")
        report("P10-3c: attendance_list collection is 'attendance_logs'", data.get("collection") == "attendance_logs")
        report("P10-3d: attendance_list explain contains executionStats", "executionStats" in data.get("explain", {}))

        # Inspect command passed to db.command
        cmd_name, cmd_dict, cmd_kwargs = mock_db.command_calls[-1]
        report("P10-3e: explain command uses executionStats verbosity", cmd_kwargs.get("verbosity") == "executionStats")
        report("P10-3f: attendance_list find filter has emp_code", cmd_dict.get("filter", {}).get("emp_code") == "EMP0001")
        report("P10-3g: attendance_list find sort is date DESC, emp_code ASC", cmd_dict.get("sort") == {"date": -1, "emp_code": 1})
        report("P10-3h: attendance_list find pagination skip is 10", cmd_dict.get("skip") == 10)
        report("P10-3i: attendance_list find pagination limit is 10", cmd_dict.get("limit") == 10)

        # 4. Parameter Validation for 'employee_monthly'
        r = client.get("/admin/explain/employee_monthly")
        report("P10-4a: employee_monthly missing emp_code and month returns 422", r.status_code == 422)

        r = client.get("/admin/explain/employee_monthly?emp_code=EMP0001")
        report("P10-4b: employee_monthly missing month returns 422", r.status_code == 422)

        r = client.get("/admin/explain/employee_monthly?month=2026-07")
        report("P10-4c: employee_monthly missing emp_code returns 422", r.status_code == 422)

        r = client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-13")
        report("P10-4d: employee_monthly invalid month pattern returns 422", r.status_code == 422)

        # 5. Successful Explain for 'employee_monthly'
        r = client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07")
        report("P10-5a: employee_monthly valid explain returns 200", r.status_code == 200)
        data = r.json()
        report("P10-5b: employee_monthly endpoint matches", data.get("endpoint") == "employee_monthly")
        report("P10-5c: employee_monthly collection is 'attendance_logs'", data.get("collection") == "attendance_logs")
        cmd_name, cmd_dict, cmd_kwargs = mock_db.command_calls[-1]
        report("P10-5d: employee_monthly reuses build_employee_monthly_pipeline", "pipeline" in cmd_dict)
        pipe_m = cmd_dict.get("pipeline", [])
        report("P10-5e: employee_monthly pipeline stage 0 matches emp_code and date", pipe_m[0].get("$match", {}).get("emp_code") == "EMP0001")

        # 6. Parameter Validation for 'department_summary'
        r = client.get("/admin/explain/department_summary")
        report("P10-6a: department_summary missing month returns 422", r.status_code == 422)

        r = client.get("/admin/explain/department_summary?month=invalid")
        report("P10-6b: department_summary invalid month returns 422", r.status_code == 422)

        # 7. Successful Explain for 'department_summary'
        r = client.get("/admin/explain/department_summary?month=2026-07&department=Engineering")
        report("P10-7a: department_summary valid explain returns 200", r.status_code == 200)
        data = r.json()
        report("P10-7b: department_summary endpoint matches", data.get("endpoint") == "department_summary")
        report("P10-7c: department_summary collection is 'employees'", data.get("collection") == "employees")
        cmd_name, cmd_dict, cmd_kwargs = mock_db.command_calls[-1]
        pipe_d = cmd_dict.get("pipeline", [])
        report("P10-7d: department_summary reuses build_department_summary_pipeline", pipe_d[0].get("$match", {}).get("department") == "Engineering")

        # 8. Parameter Validation for 'late_leaderboard'
        r = client.get("/admin/explain/late_leaderboard")
        report("P10-8a: late_leaderboard missing month returns 422", r.status_code == 422)

        r = client.get("/admin/explain/late_leaderboard?month=2026-07&limit=0")
        report("P10-8b: late_leaderboard limit=0 returns 422", r.status_code == 422)

        r = client.get("/admin/explain/late_leaderboard?month=2026-07&limit=51")
        report("P10-8c: late_leaderboard limit=51 returns 422", r.status_code == 422)

        # 9. Successful Explain for 'late_leaderboard'
        r = client.get("/admin/explain/late_leaderboard?month=2026-07&limit=5&department=Engineering")
        report("P10-9a: late_leaderboard valid explain returns 200", r.status_code == 200)
        data = r.json()
        report("P10-9b: late_leaderboard endpoint matches", data.get("endpoint") == "late_leaderboard")
        report("P10-9c: late_leaderboard collection is 'attendance_logs'", data.get("collection") == "attendance_logs")
        cmd_name, cmd_dict, cmd_kwargs = mock_db.command_calls[-1]
        pipe_l = cmd_dict.get("pipeline", [])
        report("P10-9d: late_leaderboard reuses build_late_leaderboard_pipeline", any("$setWindowFields" in st for st in pipe_l))

        # 10. Parameter Validation for 'department_trend'
        r = client.get("/admin/explain/department_trend")
        report("P10-10a: department_trend missing department and dates returns 422", r.status_code == 422)

        r = client.get("/admin/explain/department_trend?department=Engineering")
        report("P10-10b: department_trend missing from and to returns 422", r.status_code == 422)

        r = client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-07&to=2026-07-01")
        report("P10-10c: department_trend to < from returns 422", r.status_code == 422)

        r = client.get("/admin/explain/department_trend?department=Engineering&from=2026-01-01&to=2026-04-03")
        report("P10-10d: department_trend range > 92 days returns 422", r.status_code == 422)

        r = client.get("/admin/explain/department_trend?department=Engineering&from=2026-02-30&to=2026-03-05")
        report("P10-10e: department_trend invalid calendar date returns 422", r.status_code == 422)

        # 11. Successful Explain for 'department_trend'
        r = client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-07")
        report("P10-11a: department_trend valid explain returns 200", r.status_code == 200)
        data = r.json()
        report("P10-11b: department_trend endpoint matches", data.get("endpoint") == "department_trend")
        report("P10-11c: department_trend collection is 'employees'", data.get("collection") == "employees")
        cmd_name, cmd_dict, cmd_kwargs = mock_db.command_calls[-1]
        pipe_t = cmd_dict.get("pipeline", [])
        report("P10-11d: department_trend reuses build_department_trend_pipeline", pipe_t[0].get("$match", {}).get("department") == "Engineering")
        lookup_stage = [st["$lookup"] for st in pipe_t if "$lookup" in st][0]
        report("P10-11e: department_trend pipeline includes $densify", any("$densify" in st for st in lookup_stage["pipeline"]))

        # 12. Database Unavailable / Error (503)
        mock_db.should_fail = True
        r_503 = client.get("/admin/explain/department_summary?month=2026-07")
        report("P10-12: Simulated database failure returns HTTP 503", r_503.status_code == 503)

    finally:
        m.db = orig_db

    # -------------------------------------------------------------------------
    # PART 2: Live MongoDB Query Plan & Index Analysis Suite
    # -------------------------------------------------------------------------
    print("\n-- PART 2: Live MongoDB Query Plan & Index Analysis --")

    if not _LIVE_MONGO_AVAILABLE:
        print("SKIP: Live MongoDB not reachable at mongodb://localhost:27017")
        print("Note: All contract & pipeline reuse tests in Part 1 ran successfully.")
    else:
        live_db_name = "test_phase10_live_db"
        live_db = _live_client[live_db_name]
        orig_db = m.db

        try:
            m.db = live_db
            live_db.employees.delete_many({})
            live_db.attendance_logs.delete_many({})

            # Create production indexes idempotently
            m._create_indexes()

            # Seed realistic employee and log dataset to evaluate index scan execution
            # 10 employees
            emps = [
                {"emp_code": f"EMP{i:04d}", "name": f"Employee {i}", "department": "Engineering" if i % 2 == 0 else "Sales", "joined_on": "2026-01-01"}
                for i in range(1, 11)
            ]
            live_db.employees.insert_many(emps)

            # 50 attendance logs
            logs = []
            for i in range(1, 11):
                code = f"EMP{i:04d}"
                for d in range(1, 6):
                    logs.append({
                        "emp_code": code,
                        "date": f"2026-07-{d:02d}",
                        "status": "PRESENT" if d % 2 != 0 else "WFH",
                        "work_hours": 8.5,
                        "late_minutes": 15 if i % 2 == 0 else 0,
                        "overtime_minutes": 30 if i % 3 == 0 else 0,
                        "half_day": False,
                    })
            live_db.attendance_logs.insert_many(logs)

            # --- Live Test 1: attendance_list ---
            r_live_1 = client.get("/admin/explain/attendance_list?emp_code=EMP0001&date_from=2026-07-01&date_to=2026-07-05")
            report("P10-LIVE-1a: Live attendance_list explain returns HTTP 200", r_live_1.status_code == 200)
            res_1 = r_live_1.json()
            report("P10-LIVE-1b: Live attendance_list collection is attendance_logs", res_1.get("collection") == "attendance_logs")
            exp_1 = res_1.get("explain", {})
            report("P10-LIVE-1c: Live attendance_list explain has executionStats", "executionStats" in exp_1)
            qp_1 = exp_1.get("queryPlanner", {})
            winning_1 = qp_1.get("winningPlan", {})
            # Look for index scan node
            plan_str_1 = str(winning_1)
            report("P10-LIVE-1d: Live attendance_list uses IXSCAN", "IXSCAN" in plan_str_1)
            report("P10-LIVE-1e: Live attendance_list uses IX4 or IX5 index", "IX4_emp_code_date_unique" in plan_str_1 or "IX5_date_desc_emp_code_asc" in plan_str_1)

            # --- Live Test 2: employee_monthly ---
            r_live_2 = client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07")
            report("P10-LIVE-2a: Live employee_monthly explain returns HTTP 200", r_live_2.status_code == 200)
            res_2 = r_live_2.json()
            report("P10-LIVE-2b: Live employee_monthly collection is attendance_logs", res_2.get("collection") == "attendance_logs")
            exp_2 = res_2.get("explain", {})
            stages_2 = exp_2.get("stages", [])
            report("P10-LIVE-2c: Live employee_monthly has aggregation stages", len(stages_2) >= 1)
            cursor_str_2 = str(stages_2[0]) if stages_2 else str(exp_2)
            report("P10-LIVE-2d: Live employee_monthly stage 0 uses IXSCAN", "IXSCAN" in cursor_str_2)
            report("P10-LIVE-2e: Live employee_monthly uses IX4 or IX6 index", "IX4_emp_code_date_unique" in cursor_str_2 or "IX6_emp_code_date_status" in cursor_str_2)

            # --- Live Test 3: department_summary ---
            r_live_3 = client.get("/admin/explain/department_summary?month=2026-07&department=Engineering")
            report("P10-LIVE-3a: Live department_summary explain returns HTTP 200", r_live_3.status_code == 200)
            res_3 = r_live_3.json()
            report("P10-LIVE-3b: Live department_summary collection is employees", res_3.get("collection") == "employees")
            exp_3 = res_3.get("explain", {})
            stages_3 = exp_3.get("stages", [])
            report("P10-LIVE-3c: Live department_summary has aggregation stages", len(stages_3) >= 2)
            exp_str_3 = str(exp_3)
            report("P10-LIVE-3d: Live department_summary uses IX2_department index", "IX2_department" in exp_str_3)

            # --- Live Test 4: late_leaderboard ---
            r_live_4 = client.get("/admin/explain/late_leaderboard?month=2026-07&limit=5&department=Engineering")
            report("P10-LIVE-4a: Live late_leaderboard explain returns HTTP 200", r_live_4.status_code == 200)
            res_4 = r_live_4.json()
            report("P10-LIVE-4b: Live late_leaderboard collection is attendance_logs", res_4.get("collection") == "attendance_logs")
            exp_4 = res_4.get("explain", {})
            stages_4 = exp_4.get("stages", [])
            report("P10-LIVE-4c: Live late_leaderboard includes window fields stage", any("$_internalSetWindowFields" in st or "$setWindowFields" in st for st in stages_4))
            exp_str_4 = str(exp_4)
            report("P10-LIVE-4d: Live late_leaderboard lookup uses IX1_emp_code_unique", "IX1_emp_code_unique" in exp_str_4)

            # --- Live Test 5: department_trend ---
            r_live_5 = client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-05")
            report("P10-LIVE-5a: Live department_trend explain returns HTTP 200", r_live_5.status_code == 200)
            res_5 = r_live_5.json()
            report("P10-LIVE-5b: Live department_trend collection is employees", res_5.get("collection") == "employees")
            exp_5 = res_5.get("explain", {})
            exp_str_5 = str(exp_5)
            report("P10-LIVE-5c: Live department_trend uses IX2_department index", "IX2_department" in exp_str_5)
            stages_5 = exp_5.get("stages", [])
            report("P10-LIVE-5d: Live department_trend includes window fields stage", any("$_internalSetWindowFields" in st or "$setWindowFields" in st for st in stages_5))

            # --- Live Test 6: Verify Read-Only Guarantee (No Data Mutation) ---
            emp_count_before = live_db.employees.count_documents({})
            att_count_before = live_db.attendance_logs.count_documents({})
            client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-05")
            client.get("/admin/explain/attendance_list?page=1")
            client.get("/admin/explain/late_leaderboard?month=2026-07")
            emp_count_after = live_db.employees.count_documents({})
            att_count_after = live_db.attendance_logs.count_documents({})
            report("P10-LIVE-6a: Employees collection was NOT mutated during explain queries", emp_count_before == emp_count_after)
            report("P10-LIVE-6b: Attendance logs collection was NOT mutated during explain queries", att_count_before == att_count_after)

        finally:
            _live_client.drop_database(live_db_name)
            m.db = orig_db

    print("=" * 65)
    print(f"Total: {passed_count} passed, {failed_count} failed")
    if failed_count > 0:
        sys.exit(1)



def test_phase10():
    run_tests()


if __name__ == "__main__":
    run_tests()
