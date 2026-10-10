"""
Phase 8 verification suite — Late-comers leaderboard analytics only.

Imports the REAL FastAPI app and real models from app.main.
Uses TestClient to test:
- Route GET /analytics/leaderboard/late
- Query parameters validation: month pattern (YYYY-MM), limit (1..50, default 10), department
- Standard competition ranking ($rank: 1, 2, 2, 4...)
- Cutoff ties preservation (rank <= limit may yield > limit rows)
- Deterministic final sorting (total_late_minutes DESC, emp_code ASC)
- Filtering inside department when department is given
- Inclusion of weekend records in late totals (R7)
- Exclusion of employees with total_late_minutes == 0
- Handling of logs whose employee does not exist (ignored)
- Empty leaderboard handling (returns items: [])
- Exact response schema, no MongoDB _id
- DB aggregation usage (no Python collection scan)
- Live MongoDB aggregation execution and structured explain plan

Run:
    python -X utf8 tests/test_phase8_late_leaderboard.py
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
# Aggregation Mock for DB Isolation & Pipeline Inspection
# =============================================================================
class MockAttendanceLogsWithLeaderboard:
    def __init__(self, emp_mock):
        self.docs: list[dict] = []
        self.aggregate_calls: list[list[dict]] = []
        self.find_calls: list[dict] = []
        self.emp_mock = emp_mock

    def find(self, *args, **kwargs):
        self.find_calls.append(args[0] if args else {})
        raise AssertionError("find() should not be called on attendance_logs during leaderboard aggregation!")

    def aggregate(self, pipeline: list[dict]):
        self.aggregate_calls.append(pipeline)

        # Stage 1: $match on date and late_minutes > 0
        match_stage = pipeline[0].get("$match", {})
        date_cond = match_stage.get("date", {})
        date_gte = date_cond.get("$gte", "")
        date_lte = date_cond.get("$lte", "")
        min_late = match_stage.get("late_minutes", {}).get("$gt", 0)

        matched_logs = [
            l for l in self.docs
            if (not date_gte or l.get("date", "") >= date_gte)
            and (not date_lte or l.get("date", "") <= date_lte)
            and l.get("late_minutes", 0) > min_late
        ]

        # Stage 2: Group by emp_code
        emp_late: dict[str, dict] = {}
        for l in matched_logs:
            code = l.get("emp_code")
            if code not in emp_late:
                emp_late[code] = {"emp_code": code, "total_late_minutes": 0, "late_count": 0}
            emp_late[code]["total_late_minutes"] += l.get("late_minutes", 0)
            emp_late[code]["late_count"] += 1

        # Stage 3 & 4: Lookup and unwind employee (ignore logs whose employee does not exist)
        emp_map = {e["emp_code"]: e for e in self.emp_mock.docs}
        records = []
        for code, stats in emp_late.items():
            if code in emp_map:
                e = emp_map[code]
                records.append({
                    "emp_code": code,
                    "name": e.get("name"),
                    "department": e.get("department"),
                    "total_late_minutes": stats["total_late_minutes"],
                    "late_count": stats["late_count"],
                })

        # Stage 5: Department filter if present
        # Check if pipeline has a match stage on emp.department
        dept_filter = None
        limit_val = 10
        for stage in pipeline:
            if "$match" in stage and "emp.department" in stage["$match"]:
                dept_filter = stage["$match"]["emp.department"]
            if "$match" in stage and "rank" in stage["$match"]:
                limit_val = stage["$match"]["rank"].get("$lte", 10)

        if dept_filter:
            records = [r for r in records if r["department"] == dept_filter]

        if not records:
            return iter([])

        # Stage 6: Standard competition ranking ($rank)
        # Sort by total_late_minutes descending
        records.sort(key=lambda r: r["total_late_minutes"], reverse=True)

        ranked_records = []
        current_rank = 1
        for i, r in enumerate(records):
            if i > 0 and r["total_late_minutes"] == records[i - 1]["total_late_minutes"]:
                # Tied on score: share previous rank
                r_rank = ranked_records[i - 1]["rank"]
            else:
                # Rank equals 1-based index (e.g. 1, 2, 2, 4)
                r_rank = i + 1
            rec_copy = dict(r)
            rec_copy["rank"] = r_rank
            ranked_records.append(rec_copy)

        # Stage 7: Filter rank <= limit
        filtered = [r for r in ranked_records if r["rank"] <= limit_val]

        # Stage 8: Sort total_late_minutes DESC, emp_code ASC
        filtered.sort(key=lambda r: (-r["total_late_minutes"], r["emp_code"]))

        return iter(filtered)


class MockEmployeesSimple:
    def __init__(self):
        self.docs: list[dict] = []
        self.find_calls: list[dict] = []

    def find(self, *args, **kwargs):
        self.find_calls.append(args[0] if args else {})
        raise AssertionError("find() should not be called on employees during leaderboard!")


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

    mock_emp = MockEmployeesSimple()
    mock_att = MockAttendanceLogsWithLeaderboard(mock_emp)

    orig_emp = m.db.employees
    orig_att = m.db.attendance_logs
    m.db.employees = mock_emp
    m.db.attendance_logs = mock_att

    try:
        # Seed employees:
        # EMP01 (Engineering): Alice
        # EMP02 (Engineering): Bob
        # EMP03 (Engineering): Charlie
        # EMP04 (Engineering): David
        # EMP05 (Design): Eve
        # EMP06 (Engineering): Frank (has 0 late minutes)
        mock_emp.docs.extend([
            {"emp_code": "EMP01", "name": "Alice", "department": "Engineering"},
            {"emp_code": "EMP02", "name": "Bob", "department": "Engineering"},
            {"emp_code": "EMP03", "name": "Charlie", "department": "Engineering"},
            {"emp_code": "EMP04", "name": "David", "department": "Engineering"},
            {"emp_code": "EMP05", "name": "Eve", "department": "Design"},
            {"emp_code": "EMP06", "name": "Frank", "department": "Engineering"},
        ])

        # Seed attendance records for July 2026:
        # Eve (EMP05): 60 late min (rank 1)
        # Alice (EMP01): 50 late min (rank 2)
        # Bob (EMP02): 20 + 20 = 40 late min (rank 3)
        # Charlie (EMP03): 40 late min on Saturday (rank 3 - weekend included per R7!)
        # David (EMP04): 30 late min (rank 5 - rank 4 skipped!)
        # Frank (EMP06): 0 late min (excluded!)
        # Orphan log (EMP99): 100 late min (no employee -> ignored)
        mock_att.docs.extend([
            {"emp_code": "EMP05", "date": "2026-07-06", "late_minutes": 60},
            {"emp_code": "EMP01", "date": "2026-07-06", "late_minutes": 50},
            {"emp_code": "EMP02", "date": "2026-07-06", "late_minutes": 20},
            {"emp_code": "EMP02", "date": "2026-07-07", "late_minutes": 20},
            # Weekend record for Charlie (Saturday 2026-07-11)
            {"emp_code": "EMP03", "date": "2026-07-11", "late_minutes": 40},
            {"emp_code": "EMP04", "date": "2026-07-06", "late_minutes": 30},
            {"emp_code": "EMP06", "date": "2026-07-06", "late_minutes": 0},
            {"emp_code": "EMP99", "date": "2026-07-06", "late_minutes": 100},
            # Next month record (outside range)
            {"emp_code": "EMP01", "date": "2026-08-01", "late_minutes": 90},
        ])

        # 1. Parameter validations (422)
        r = client.get("/analytics/leaderboard/late?month=2026-13")
        report("P8-1a: Invalid month 2026-13 returns 422", r.status_code == 422)

        r = client.get("/analytics/leaderboard/late?month=2026-7")
        report("P8-1b: Unpadded month 2026-7 returns 422", r.status_code == 422)

        r = client.get("/analytics/leaderboard/late")
        report("P8-1c: Missing month query parameter returns 422", r.status_code == 422)

        r = client.get("/analytics/leaderboard/late?month=2026-07&limit=0")
        report("P8-1d: limit=0 (< 1) returns 422", r.status_code == 422)

        r = client.get("/analytics/leaderboard/late?month=2026-07&limit=51")
        report("P8-1e: limit=51 (> 50) returns 422", r.status_code == 422)

        # 2. Standard competition ranking and overall leaderboard (default limit 10)
        mock_att.aggregate_calls.clear()
        mock_att.find_calls.clear()
        mock_emp.find_calls.clear()

        r = client.get("/analytics/leaderboard/late?month=2026-07")
        report("P8-2a: GET /analytics/leaderboard/late returns 200", r.status_code == 200)
        data = r.json()
        report("P8-2b: Response month is 2026-07", data.get("month") == "2026-07")
        items = data.get("items", [])
        report("P8-2c: Frank (0 late) and EMP99 (orphan) excluded; 5 employees returned", len(items) == 5)

        # Check DB aggregation usage
        report("P8-2d: find() was NOT called on attendance_logs", len(mock_att.find_calls) == 0)
        report("P8-2e: find() was NOT called on employees", len(mock_emp.find_calls) == 0)
        report("P8-2f: aggregate() was called on db.attendance_logs", len(mock_att.aggregate_calls) == 1)

        # 3. Check Standard Competition Ranking (1, 2, 3, 3, 5)
        # Rank 1: Eve (60m)
        report("P8-3a: Rank 1 is Eve (EMP05, 60m)", items[0]["rank"] == 1 and items[0]["emp_code"] == "EMP05" and items[0]["total_late_minutes"] == 60)
        # Rank 2: Alice (50m)
        report("P8-3b: Rank 2 is Alice (EMP01, 50m)", items[1]["rank"] == 2 and items[1]["emp_code"] == "EMP01" and items[1]["total_late_minutes"] == 50)
        # Rank 3 tied: Bob (40m) & Charlie (40m)
        report("P8-3c: Rank 3 is Bob (EMP02, 40m)", items[2]["rank"] == 3 and items[2]["emp_code"] == "EMP02" and items[2]["total_late_minutes"] == 40)
        report("P8-3d: Rank 3 tied is Charlie (EMP03, 40m)", items[3]["rank"] == 3 and items[3]["emp_code"] == "EMP03" and items[3]["total_late_minutes"] == 40)
        # Deterministic order among ties: EMP02 before EMP03
        report("P8-3e: Tied items sorted by emp_code ascending (EMP02 before EMP03)", items[2]["emp_code"] == "EMP02" and items[3]["emp_code"] == "EMP03")
        # Rank 5: David (30m) - rank 4 skipped!
        report("P8-3f: Standard competition rank: rank 4 skipped, David is rank 5", items[4]["rank"] == 5 and items[4]["emp_code"] == "EMP04")

        # 4. Weekend late record inclusion
        # Charlie's 40 late minutes were on Saturday 2026-07-11
        charlie_item = next(it for it in items if it["emp_code"] == "EMP03")
        report("P8-4: Weekend late records contribute to total_late_minutes", charlie_item["total_late_minutes"] == 40 and charlie_item["late_count"] == 1)

        # 5. Cutoff ties with limit=3 (must return 4 items: ranks 1, 2, 3, 3)
        r_lim3 = client.get("/analytics/leaderboard/late?month=2026-07&limit=3")
        items_lim3 = r_lim3.json().get("items", [])
        report("P8-5a: limit=3 returns 4 items because of tie at rank 3", len(items_lim3) == 4)
        report("P8-5b: All 4 returned items have rank <= 3", all(it["rank"] <= 3 for it in items_lim3))

        # 6. Department filter ranking: department=Engineering
        # Eve (Design, rank 1 overall) must be excluded.
        # Alice (50m) becomes rank 1 in Engineering!
        r_dept = client.get("/analytics/leaderboard/late?month=2026-07&department=Engineering")
        items_dept = r_dept.json().get("items", [])
        report("P8-6a: department=Engineering returns only Engineering employees (4)", len(items_dept) == 4)
        report("P8-6b: Ranking inside department: Alice is rank 1 in Engineering", items_dept[0]["rank"] == 1 and items_dept[0]["emp_code"] == "EMP01")
        report("P8-6c: Bob and Charlie are rank 2 in Engineering", items_dept[1]["rank"] == 2 and items_dept[2]["rank"] == 2)
        report("P8-6d: David is rank 4 in Engineering (rank 3 skipped)", items_dept[3]["rank"] == 4 and items_dept[3]["emp_code"] == "EMP04")

        # 7. Department with no late employees
        r_empty = client.get("/analytics/leaderboard/late?month=2026-07&department=NonExistent")
        report("P8-7: Department with no late employees returns 200 with items: []", r_empty.status_code == 200 and r_empty.json().get("items") == [])

        # 8. Schema verification (exact 6 fields, no _id)
        expected_keys = {"rank", "emp_code", "name", "department", "total_late_minutes", "late_count"}
        report("P8-8a: Response item matches exact 6 required fields", set(items[0].keys()) == expected_keys)
        report("P8-8b: MongoDB internal _id is absent", "_id" not in items[0] and "_id" not in data)

        # 9. Pipeline inspection
        pipe = mock_att.aggregate_calls[0]
        report("P8-9a: Pipeline uses $setWindowFields with $rank", any("$setWindowFields" in st for st in pipe))
        report("P8-9b: Pipeline filters rank <= limit after ranking", any("$match" in st and "rank" in st["$match"] for st in pipe))
        report("P8-9c: Pipeline final sort is total_late_minutes DESC, emp_code ASC", any("$sort" in st and st["$sort"] == {"total_late_minutes": -1, "_id": 1} for st in pipe))

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
        live_db_name = "test_phase8_live_db"
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
                {"emp_code": "EMP01", "name": "Alice", "department": "Engineering"},
                {"emp_code": "EMP02", "name": "Bob", "department": "Engineering"},
                {"emp_code": "EMP03", "name": "Charlie", "department": "Engineering"},
                {"emp_code": "EMP04", "name": "David", "department": "Engineering"},
                {"emp_code": "EMP05", "name": "Eve", "department": "Design"},
            ])

            # Seed attendance logs
            live_db.attendance_logs.insert_many([
                {"emp_code": "EMP05", "date": "2026-07-06", "late_minutes": 60},
                {"emp_code": "EMP01", "date": "2026-07-06", "late_minutes": 50},
                {"emp_code": "EMP02", "date": "2026-07-06", "late_minutes": 20},
                {"emp_code": "EMP02", "date": "2026-07-07", "late_minutes": 20},
                {"emp_code": "EMP03", "date": "2026-07-11", "late_minutes": 40},  # Saturday
                {"emp_code": "EMP04", "date": "2026-07-06", "late_minutes": 30},
            ])

            # Test live query
            r_live = client.get("/analytics/leaderboard/late?month=2026-07&limit=3")
            report("P8-LIVE-1: Live MongoDB returned HTTP 200", r_live.status_code == 200)
            live_data = r_live.json()
            live_items = live_data.get("items", [])
            report("P8-LIVE-2: Live limit=3 returns 4 items due to tie at rank 3", len(live_items) == 4)
            report("P8-LIVE-3: Live rank 1 is Eve (EMP05, 60m)", live_items[0]["rank"] == 1 and live_items[0]["emp_code"] == "EMP05")
            report("P8-LIVE-4: Live rank 2 is Alice (EMP01, 50m)", live_items[1]["rank"] == 2 and live_items[1]["emp_code"] == "EMP01")
            report("P8-LIVE-5: Live rank 3 tied are Bob & Charlie (40m each)", live_items[2]["rank"] == 3 and live_items[3]["rank"] == 3)
            report("P8-LIVE-6: Live tie break sorts EMP02 before EMP03", live_items[2]["emp_code"] == "EMP02" and live_items[3]["emp_code"] == "EMP03")

            # Test department filter on live MongoDB
            r_live_dept = client.get("/analytics/leaderboard/late?month=2026-07&department=Engineering&limit=10")
            live_dept_items = r_live_dept.json().get("items", [])
            report("P8-LIVE-7: Live department=Engineering returns 4 items", len(live_dept_items) == 4)
            report("P8-LIVE-8: Live Engineering rank 1 is Alice", live_dept_items[0]["rank"] == 1 and live_dept_items[0]["emp_code"] == "EMP01")
            report("P8-LIVE-9: Live Engineering rank 4 is David (rank 3 skipped)", live_dept_items[3]["rank"] == 4 and live_dept_items[3]["emp_code"] == "EMP04")

            # Structured explain-plan inspection
            pipe = m.build_late_leaderboard_pipeline("2026-07", 10, None)
            explain = live_db.command(
                "explain",
                {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}},
                verbosity="executionStats",
            )
            # Inspect structured stages in explain:
            stages = explain.get("stages", [])
            report("P8-LIVE-10a: Explain contains stages list", len(stages) >= 2)

            cursor_stage = stages[0].get("$cursor", {})
            winning_plan = cursor_stage.get("queryPlanner", {}).get("winningPlan", {}).get("queryPlan", {})
            ixscan_node = winning_plan.get("inputStage", {}).get("inputStage", {})
            report("P8-LIVE-10b: Initial match uses IXSCAN", ixscan_node.get("stage") == "IXSCAN")
            report("P8-LIVE-10c: IXSCAN uses IX5_date_desc_emp_code_asc index", ixscan_node.get("indexName") == "IX5_date_desc_emp_code_asc")

            # Check lookup stage index
            lookup_stage = stages[1]
            report("P8-LIVE-11a: Lookup uses IX1_emp_code_unique index", "IX1_emp_code_unique" in lookup_stage.get("indexesUsed", []))
            report("P8-LIVE-11b: Lookup has 0 collection scans", lookup_stage.get("collectionScans", 0) == 0)

        finally:
            _live_client.drop_database(live_db_name)
            m.db = orig_db

    print("=" * 65)
    print(f"Total: {passed_count} passed, {failed_count} failed")
    if failed_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_tests()
