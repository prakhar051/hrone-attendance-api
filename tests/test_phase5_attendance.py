"""
Phase 5 verification suite — Attendance listing and regularization.

Imports the REAL FastAPI app and real models/serializers from app.main.
Uses TestClient to test HTTP requests, status codes, query/path parameters,
database cursor execution, concurrency safety (CAS on history size),
and response serialization.

Database isolation:
Uses thread-safe in-memory collection mocks with full compound index sorting,
atomic find_one_and_update with CAS on history length, and count_documents.

Run:
    python -X utf8 tests/test_phase5_attendance.py
"""
import concurrent.futures
import os
import sys
import threading
from datetime import datetime, timezone

# ── Set dummy DB so app.main imports safely ──────────────────────────────────
os.environ["MONGO_URI"] = "mongodb://127.0.0.1:27099"
os.environ["MONGO_DB"]  = "phase5_test_db"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app.main as m
from fastapi.testclient import TestClient
from pymongo import ASCENDING, DESCENDING


# ── In-Memory Collection Mocks ───────────────────────────────────────────────
class MockEmployeesCollection:
    def __init__(self):
        self.docs: list[dict] = []
        self._lock = threading.Lock()

    def find_one(self, q: dict, sort=None) -> dict | None:
        with self._lock:
            for d in self.docs:
                if all(d.get(k) == v for k, v in q.items()):
                    return dict(d)
            return None


class MockCursor:
    def __init__(self, docs, on_iter=None):
        self._docs = list(docs)
        self._sort_spec = None
        self._skip = 0
        self._limit = None
        self._on_iter = on_iter

    def sort(self, key_or_list, direction=None):
        if isinstance(key_or_list, list):
            self._sort_spec = key_or_list
        else:
            self._sort_spec = [(key_or_list, direction or 1)]
        return self

    def skip(self, n):
        self._skip = n
        return self

    def limit(self, n):
        self._limit = n
        return self

    def __iter__(self):
        if self._on_iter:
            self._on_iter()

        docs = list(self._docs)
        if self._sort_spec:
            # Stable multi-pass sort in reverse order of precedence
            for key, direction in reversed(self._sort_spec):
                docs.sort(
                    key=lambda d: d.get(key) if d.get(key) is not None else "",
                    reverse=(direction == DESCENDING),
                )

        if self._skip:
            docs = docs[self._skip:]
        if self._limit is not None:
            docs = docs[:self._limit]
        return iter(docs)


class MockAttendanceLogsCollection:
    """Mock collection supporting query filters, compound sort, and atomic find_one_and_update."""

    def __init__(self):
        self.docs: list[dict] = []
        self._lock = threading.Lock()
        self.last_find_filter = None
        self.last_count_filter = None
        self.pre_update_hook = None

    def _match_field(self, d: dict, k: str, v) -> bool:
        if isinstance(v, dict):
            val = d.get(k)
            for op, target in v.items():
                if op == "$gte":
                    if val is None or val < target:
                        return False
                elif op == "$lte":
                    if val is None or val > target:
                        return False
                elif op == "$size":
                    arr = d.get(k) or []
                    if not isinstance(arr, list) or len(arr) != target:
                        return False
                elif op == "$exists":
                    exists = k in d
                    if exists != target:
                        return False
            return True
        return d.get(k) == v

    def _match_doc(self, d: dict, q: dict) -> bool:
        for k, v in q.items():
            if k == "$or":
                if not any(self._match_doc(d, cond) for cond in v):
                    return False
            elif not self._match_field(d, k, v):
                return False
        return True

    def count_documents(self, q: dict) -> int:
        with self._lock:
            self.last_count_filter = dict(q)
            return len([d for d in self.docs if self._match_doc(d, q)])

    def find(self, q: dict):
        with self._lock:
            self.last_find_filter = dict(q)
            matched = [d for d in self.docs if self._match_doc(d, q)]
            return MockCursor(matched)

    def find_one(self, q: dict, sort=None) -> dict | None:
        with self._lock:
            matched = [d for d in self.docs if self._match_doc(d, q)]
            if not matched:
                return None
            if sort:
                for key, direction in reversed(sort):
                    matched.sort(
                        key=lambda d: d.get(key) if d.get(key) is not None else "",
                        reverse=(direction == DESCENDING),
                    )
            return dict(matched[0])

    def find_one_and_update(self, filter_q: dict, update_q: dict, return_document=None) -> dict | None:
        if self.pre_update_hook:
            self.pre_update_hook()

        with self._lock:
            target = None
            for d in self.docs:
                if self._match_doc(d, filter_q):
                    target = d
                    break

            if not target:
                return None

            if "$set" in update_q:
                for k, v in update_q["$set"].items():
                    target[k] = v

            if "$push" in update_q:
                for k, v in update_q["$push"].items():
                    if k not in target or not isinstance(target[k], list):
                        target[k] = []
                    target[k].append(v)

            return dict(target)


# ── Test scaffolding ──────────────────────────────────────────────────────────
_passed: list[str] = []
_failed: list[str] = []

def ok(label: str) -> None:
    _passed.append(label)
    print(f"PASS  {label}")

def fail(label: str, msg: str) -> None:
    _failed.append(label)
    print(f"FAIL  {label}: {msg}")

def check(label: str, cond: bool, msg: str = "") -> None:
    if cond:
        ok(label)
    else:
        fail(label, msg or "assertion failed")

assert m.__name__ == "app.main", "Imported wrong module"
print(f"\nImported module: {m.__file__}")
print("=" * 65)

client = TestClient(m.app, raise_server_exceptions=False)
mock_employees = MockEmployeesCollection()
mock_attendance = MockAttendanceLogsCollection()

m.db.employees = mock_employees
m.db.attendance_logs = mock_attendance

# Seed sample employees
emp1 = {
    "emp_code":    "EMP0001",
    "name":        "Asha Rao",
    "email":       "asha@example.com",
    "department":  "Engineering",
    "shift_start": "09:30",
    "shift_end":   "18:30",
    "joined_on":   "2026-01-05",
}
emp2 = {
    "emp_code":    "EMP0002",
    "name":        "John Doe",
    "email":       "john@example.com",
    "department":  "Engineering",
    "shift_start": "09:30",
    "shift_end":   "18:30",
    "joined_on":   "2026-01-05",
}
mock_employees.docs = [emp1, emp2]


# ==============================================================================
# SECTION A: GET /attendance (Tests 1 to 10)
# ==============================================================================
print("\n-- SECTION A: GET /attendance --")

# Seed 15 records across 3 days and 2 employees
# Days: 2026-07-06, 2026-07-07, 2026-07-08
mock_attendance.docs.clear()
for day in ["2026-07-06", "2026-07-07", "2026-07-08"]:
    for code in ["EMP0001", "EMP0002"]:
        status = "PRESENT" if day != "2026-07-08" or code != "EMP0002" else "WFH"
        dt_in = datetime.strptime(f"{day}T03:58:00Z", "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        dt_out = datetime.strptime(f"{day}T13:05:00Z", "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        mock_attendance.docs.append({
            "_id":              f"oid_{day}_{code}",
            "emp_code":         code,
            "date":             day,
            "status":           status,
            "punch_in":         dt_in,
            "punch_out":        dt_out,
            "work_hours":       9.12,
            "late_minutes":     0,
            "overtime_minutes": 0,
            "half_day":         False,
            "history":          [],
        })

# 1. Listing with no filters
res_all = client.get("/attendance")
check("P5-1a: GET /attendance returns 200", res_all.status_code == 200)
d_all = res_all.json()
check("P5-1b: Total records is 6", d_all.get("total") == 6)
check("P5-1c: Default page is 1, page_size is 20", d_all.get("page") == 1 and d_all.get("page_size") == 20)
check("P5-1d: All 6 items returned", len(d_all.get("items", [])) == 6)

# 2. Every documented filter independently
# A: emp_code
res_emp = client.get("/attendance?emp_code=EMP0001").json()
check("P5-2a: Filter emp_code=EMP0001 returns 3 items", res_emp.get("total") == 3 and len(res_emp["items"]) == 3)
check("P5-2b: All items match EMP0001", all(it["emp_code"] == "EMP0001" for it in res_emp["items"]))

# B: date_from
res_dfrom = client.get("/attendance?date_from=2026-07-07").json()
check("P5-2c: Filter date_from=2026-07-07 returns 4 items", res_dfrom.get("total") == 4)
check("P5-2d: All items date >= 2026-07-07", all(it["date"] >= "2026-07-07" for it in res_dfrom["items"]))

# C: date_to
res_dto = client.get("/attendance?date_to=2026-07-07").json()
check("P5-2e: Filter date_to=2026-07-07 returns 4 items", res_dto.get("total") == 4)
check("P5-2f: All items date <= 2026-07-07", all(it["date"] <= "2026-07-07" for it in res_dto["items"]))

# D: status
res_wfh = client.get("/attendance?status=WFH").json()
check("P5-2g: Filter status=WFH returns 1 item", res_wfh.get("total") == 1 and res_wfh["items"][0]["status"] == "WFH")

# 3. Multiple filters together
res_combo = client.get("/attendance?emp_code=EMP0001&date_from=2026-07-06&date_to=2026-07-07&status=PRESENT").json()
check("P5-3: Multiple filters together match exactly 2 items", res_combo.get("total") == 2 and len(res_combo["items"]) == 2)

# 4. Correct sorting: date DESCENDING, then emp_code ASCENDING
# Check order across the 6 records
items = d_all["items"]
expected_pairs = [
    ("2026-07-08", "EMP0001"),
    ("2026-07-08", "EMP0002"),
    ("2026-07-07", "EMP0001"),
    ("2026-07-07", "EMP0002"),
    ("2026-07-06", "EMP0001"),
    ("2026-07-06", "EMP0002"),
]
actual_pairs = [(it["date"], it["emp_code"]) for it in items]
check("P5-4: Compound sort is date DESCENDING, emp_code ASCENDING", actual_pairs == expected_pairs)

# 5. Page 1, page 2, max page size, out-of-range page
res_p1 = client.get("/attendance?page=1&page_size=2").json()
res_p2 = client.get("/attendance?page=2&page_size=2").json()
res_p3 = client.get("/attendance?page=3&page_size=2").json()
res_p4 = client.get("/attendance?page=4&page_size=2").json()

check("P5-5a: Page 1 size 2 returns first 2 records",
      len(res_p1["items"]) == 2 and (res_p1["items"][0]["date"], res_p1["items"][0]["emp_code"]) == ("2026-07-08", "EMP0001"))
check("P5-5b: Page 2 size 2 returns records 3 and 4",
      len(res_p2["items"]) == 2 and (res_p2["items"][0]["date"], res_p2["items"][0]["emp_code"]) == ("2026-07-07", "EMP0001"))
check("P5-5c: Page 3 size 2 returns records 5 and 6", len(res_p3["items"]) == 2)
check("P5-5d: Out-of-range Page 4 returns empty items list", len(res_p4["items"]) == 0 and res_p4["total"] == 6)

res_max = client.get("/attendance?page_size=100")
check("P5-5e: Max page size 100 accepted (HTTP 200)", res_max.status_code == 200)

# 6. Filtered total count
check("P5-6: Filtered total reflects matching records only (status=WFH total=1)", res_wfh.get("total") == 1)

# 7. Invalid pagination and date range (422)
check("P5-7a: page_size=101 returns 422", client.get("/attendance?page_size=101").status_code == 422)
check("P5-7b: page_size=0 returns 422", client.get("/attendance?page_size=0").status_code == 422)
check("P5-7c: page=0 returns 422", client.get("/attendance?page=0").status_code == 422)
check("P5-7d: date_from > date_to returns 422",
      client.get("/attendance?date_from=2026-07-10&date_to=2026-07-05").status_code == 422)

# 8. Empty results
res_empty = client.get("/attendance?emp_code=EMP9999").json()
check("P5-8: Empty query returns total 0 and empty items list",
      res_empty.get("total") == 0 and len(res_empty.get("items", [])) == 0)

# 9. Timestamp serialization and absence of _id
first_item = d_all["items"][0]
check("P5-9a: punch_in is integer epoch ms", isinstance(first_item["punch_in"], int))
check("P5-9b: punch_out is integer epoch ms", isinstance(first_item["punch_out"], int))
check("P5-9c: _id is absent from items", "_id" not in first_item and "id" not in first_item)

# 10. Database interface verification (find and count_documents received queries)
check("P5-10: Query filter was passed to count_documents and find",
      mock_attendance.last_count_filter is not None and mock_attendance.last_find_filter is not None)


# ==============================================================================
# SECTION B: PATCH /attendance/{emp_code}/{date} (Tests 11 to 22)
# ==============================================================================
print("\n-- SECTION B: PATCH /attendance/{emp_code}/{date} --")

# Seed a base record for regularization testing
# EMP0001 on 2026-07-06: Punched in 10:05 IST (35 late min), out 19:10 IST (40 OT min)
# 10:05 IST = 04:35 UTC -> 1783312500000
# 19:10 IST = 13:40 UTC -> 1783345200000
mock_attendance.docs.clear()
base_doc = {
    "_id":              "base_rec_01",
    "emp_code":         "EMP0001",
    "date":             "2026-07-06",
    "status":           "PRESENT",
    "punch_in":         datetime(2026, 7, 6, 4, 35, 0, tzinfo=timezone.utc),
    "punch_out":        datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc),
    "work_hours":       9.08,
    "late_minutes":     35,
    "overtime_minutes": 40,
    "half_day":         False,
    "history":          [],
}
mock_attendance.docs.append(base_doc)

# 11. Successful regularization of punch_in (Sample 8 in DATA_MODEL: correct punch_in to 09:28 IST)
# 09:28 IST = 03:58 UTC -> 1783310280000
reg_payload_1 = {
    "punch_in":       1783310280000,
    "reason":         "biometric glitch at entrance gate",
    "regularized_by": "hr.admin",
}
res_reg1 = client.patch("/attendance/EMP0001/2026-07-06", json=reg_payload_1)
check("P5-11a: Successful regularization returns HTTP 200", res_reg1.status_code == 200)
d_reg1 = res_reg1.json()

# 18. Recalculation of late_minutes, work_hours, overtime
check("P5-18a: late_minutes recomputed to 0 (09:28 is inside grace)", d_reg1.get("late_minutes") == 0)
check("P5-18b: work_hours recomputed to 9.70 (09:28 to 19:10)", d_reg1.get("work_hours") == 9.70)
check("P5-18c: overtime_minutes preserved at 40", d_reg1.get("overtime_minutes") == 40)
check("P5-18d: half_day is False", d_reg1.get("half_day") is False)

# 15. Correct before/after audit-history entries
check("P5-15a: Exactly 1 history entry", len(d_reg1.get("history", [])) == 1)
h1 = d_reg1["history"][0]
check("P5-15b: history by matches regularized_by", h1.get("by") == "hr.admin")
check("P5-15c: history reason matches", h1.get("reason") == "biometric glitch at entrance gate")
check("P5-15d: history changes contains punch_in, late_minutes, work_hours",
      set(h1.get("changes", {}).keys()) == {"punch_in", "late_minutes", "work_hours"})
check("P5-15e: late_minutes change has from 35 to 0",
      h1["changes"]["late_minutes"] == {"from": 35, "to": 0})
check("P5-15f: work_hours change has from 9.08 to 9.70",
      h1["changes"]["work_hours"] == {"from": 9.08, "to": 9.70})

# 17. Timestamp conversion inside history entries
check("P5-17a: history at is epoch ms integer", isinstance(h1.get("at"), int))
check("P5-17b: punch_in from/to inside changes are epoch ms integers",
      h1["changes"]["punch_in"] == {"from": 1783312500000, "to": 1783310280000})

# 16. Previous history entries preserved on second regularization
# Correct status from PRESENT to WFH
reg_payload_2 = {
    "status":         "WFH",
    "reason":         "employee was working from home in morning",
    "regularized_by": "manager.lead",
}
res_reg2 = client.patch("/attendance/EMP0001/2026-07-06", json=reg_payload_2)
check("P5-16a: Second regularization returns 200", res_reg2.status_code == 200)
d_reg2 = res_reg2.json()
check("P5-16b: history now contains 2 entries (previous preserved)", len(d_reg2.get("history", [])) == 2)
check("P5-16c: First history entry remains unchanged", d_reg2["history"][0]["by"] == "hr.admin")
check("P5-16d: Second history entry recorded properly",
      d_reg2["history"][1]["by"] == "manager.lead" and d_reg2["history"][1]["changes"]["status"] == {"from": "PRESENT", "to": "WFH"})

# 12. Missing employee and missing attendance record (404)
res_no_emp = client.patch("/attendance/EMP9999/2026-07-06", json=reg_payload_2)
check("P5-12a: Non-existent employee returns 404", res_no_emp.status_code == 404)
res_no_rec = client.patch("/attendance/EMP0001/2026-07-15", json=reg_payload_2)
check("P5-12b: Non-existent record date returns 404", res_no_rec.status_code == 404)

# 13. Invalid dates, statuses, timestamps (422)
res_bad_stat = client.patch("/attendance/EMP0001/2026-07-06", json={"status": "INVALID", "reason": "test reason", "regularized_by": "admin"})
check("P5-13a: Invalid status returns 422", res_bad_stat.status_code == 422)

# Request that changes nothing returns 422
res_no_change = client.patch("/attendance/EMP0001/2026-07-06", json={"status": "WFH", "reason": "no change here", "regularized_by": "admin"})
check("P5-13b: Request that changes nothing returns 422", res_no_change.status_code == 422)

# punch_out <= punch_in returns 422
res_bad_out = client.patch("/attendance/EMP0001/2026-07-06", json={
    "punch_out": 1783310280000,  # equal to punch_in
    "reason": "invalid punch out time",
    "regularized_by": "admin",
})
check("P5-13c: punch_out <= punch_in returns 422", res_bad_out.status_code == 422)

# punch_in moves to different attendance date returns 422
# 2026-07-07 10:00:00 IST -> moves date to 2026-07-07
ms_diff_day = int(datetime(2026, 7, 7, 4, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
res_diff_day = client.patch("/attendance/EMP0001/2026-07-06", json={
    "punch_in": ms_diff_day,
    "reason": "move day attempt",
    "regularized_by": "admin",
})
check("P5-13d: punch_in on different attendance date returns 422", res_diff_day.status_code == 422)

# 14. Reason and regularizer length boundaries (422)
check("P5-14a: Reason 4 chars (< 5) returns 422",
      client.patch("/attendance/EMP0001/2026-07-06", json={"status": "PRESENT", "reason": "four", "regularized_by": "admin"}).status_code == 422)
check("P5-14b: Reason 201 chars (> 200) returns 422",
      client.patch("/attendance/EMP0001/2026-07-06", json={"status": "PRESENT", "reason": "a" * 201, "regularized_by": "admin"}).status_code == 422)
check("P5-14c: Regularized_by empty returns 422",
      client.patch("/attendance/EMP0001/2026-07-06", json={"status": "PRESENT", "reason": "valid reason", "regularized_by": ""}).status_code == 422)
check("P5-14d: Regularized_by 51 chars (> 50) returns 422",
      client.patch("/attendance/EMP0001/2026-07-06", json={"status": "PRESENT", "reason": "valid reason", "regularized_by": "x" * 51}).status_code == 422)

# 21. Explicit-null / setting ABSENT or LEAVE clears both punch times
# Supplying punch times with ABSENT is 422
res_abs_with_punch = client.patch("/attendance/EMP0001/2026-07-06", json={
    "status": "ABSENT",
    "punch_in": 1783310280000,
    "reason": "absent marked",
    "regularized_by": "admin",
})
check("P5-21a: Setting ABSENT with punch_in supplied returns 422", res_abs_with_punch.status_code == 422)

# Setting ABSENT cleanly clears both punch times and zeros metrics
res_absent = client.patch("/attendance/EMP0001/2026-07-06", json={
    "status": "ABSENT",
    "reason": "approved leave not taken, marked absent",
    "regularized_by": "admin",
})
check("P5-21b: Setting ABSENT succeeds (200)", res_absent.status_code == 200)
d_abs = res_absent.json()
check("P5-21c: ABSENT clears punch_in to None", d_abs.get("punch_in") is None)
check("P5-21d: ABSENT clears punch_out to None", d_abs.get("punch_out") is None)
check("P5-21e: ABSENT sets work_hours to None", d_abs.get("work_hours") is None)
check("P5-21f: ABSENT sets late_minutes to 0", d_abs.get("late_minutes") == 0)
check("P5-21g: ABSENT sets overtime_minutes to 0", d_abs.get("overtime_minutes") == 0)

# Changing back to PRESENT without punch_in returns 422 (presence status requires punch_in)
res_pres_no_punch = client.patch("/attendance/EMP0001/2026-07-06", json={
    "status": "PRESENT",
    "reason": "attempt restore without punch",
    "regularized_by": "admin",
})
check("P5-21h: Changing ABSENT to PRESENT without punch_in returns 422", res_pres_no_punch.status_code == 422)

# 19 & 20. Concurrent regularization attempts (CAS on history array size)
# Seed an open record with history: []
conc_doc = {
    "_id":              "conc_rec_01",
    "emp_code":         "EMP0002",
    "date":             "2026-07-06",
    "status":           "PRESENT",
    "punch_in":         datetime(2026, 7, 6, 4, 35, 0, tzinfo=timezone.utc),
    "punch_out":        datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc),
    "work_hours":       9.08,
    "late_minutes":     35,
    "overtime_minutes": 40,
    "half_day":         False,
    "history":          [],
}
mock_attendance.docs.append(conc_doc)

barrier = threading.Barrier(2)

def hook_barrier():
    try:
        barrier.wait(timeout=2.0)
    except Exception:
        pass

mock_attendance.pre_update_hook = hook_barrier

ms_30 = int(datetime(2026, 7, 6, 4, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
ms_25 = int(datetime(2026, 7, 6, 4, 25, 0, tzinfo=timezone.utc).timestamp() * 1000)

results = []
def run_t1():
    res = client.patch(
        "/attendance/EMP0002/2026-07-06",
        json={"punch_in": ms_30, "reason": "concurrent update thread 1", "regularized_by": "admin_1"},
    )
    results.append(res)

def run_t2():
    res = client.patch(
        "/attendance/EMP0002/2026-07-06",
        json={"punch_in": ms_25, "reason": "concurrent update thread 2", "regularized_by": "admin_2"},
    )
    results.append(res)

t1 = threading.Thread(target=run_t1)
t2 = threading.Thread(target=run_t2)
t1.start()
t2.start()
t1.join()
t2.join()

mock_attendance.pre_update_hook = None

status_codes = sorted([r.status_code for r in results])
check("P5-19a: One concurrent regularization succeeds with 200", 200 in status_codes)
check("P5-19b: Competing concurrent regularization fails with 409 conflict", 409 in status_codes)

conflict_res = [r for r in results if r.status_code == 409][0]
check("P5-20: CAS conflict detail message matches contract",
      conflict_res.json().get("detail") == "concurrent update conflict; record has been modified")

# 22. Absence of _id in any regularization response
check("P5-22: _id is absent from regularization response",
      "_id" not in d_abs and "id" not in d_abs)


# ==============================================================================
# Summary
# ==============================================================================
print("\n" + "=" * 65)
print(f"Results: {len(_passed)} passed, {len(_failed)} failed")
if _failed:
    print(f"FAILED tests: {_failed}")
    sys.exit(1)
else:
    print("ALL Phase 5 attendance listing and regularization tests PASSED!")
