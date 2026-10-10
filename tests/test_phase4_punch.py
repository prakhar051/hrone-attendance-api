"""
Phase 4 verification suite — Punch-in and punch-out endpoints only.

Imports the REAL FastAPI app and real models/serializers from app.main.
Uses TestClient to test HTTP requests, status codes, query/body validation,
concurrency safety, and response serialization.

Database isolation:
Uses thread-safe in-memory collection mocks with IX4_emp_code_date_unique
constraint simulation and atomic find_one_and_update behavior to isolate
endpoint behavior without requiring an active live MongoDB connection.

Run:
    python -X utf8 tests/test_phase4_punch.py
"""
import concurrent.futures
import os
import sys
import threading
from datetime import datetime, timedelta, timezone

# ── Set dummy DB so app.main imports safely ──────────────────────────────────
os.environ["MONGO_URI"] = "mongodb://127.0.0.1:27099"
os.environ["MONGO_DB"]  = "phase4_test_db"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app.main as m
from fastapi.testclient import TestClient
from pymongo import DESCENDING
from pymongo.errors import DuplicateKeyError


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


class MockAttendanceLogsCollection:
    """Mock collection simulating IX4_emp_code_date_unique and atomic find_one_and_update."""

    def __init__(self):
        self.docs: list[dict] = []
        self._lock = threading.Lock()

    def insert_one(self, doc: dict):
        with self._lock:
            emp_code = doc.get("emp_code")
            date_val = doc.get("date")
            for existing in self.docs:
                if existing.get("emp_code") == emp_code and existing.get("date") == date_val:
                    raise DuplicateKeyError(
                        f"E11000 duplicate key error: IX4_emp_code_date_unique dup key: {{ emp_code: '{emp_code}', date: '{date_val}' }}"
                    )
            doc["_id"] = f"att_oid_{len(self.docs) + 1}"
            self.docs.append(dict(doc))

    def find_one(self, q: dict, sort=None) -> dict | None:
        with self._lock:
            matches = []
            for d in self.docs:
                matched = True
                for k, v in q.items():
                    if isinstance(v, dict) and "$lte" in v:
                        val = d.get(k)
                        if val is None or val > v["$lte"]:
                            matched = False
                            break
                    elif d.get(k) != v:
                        matched = False
                        break
                if matched:
                    matches.append(d)

            if not matches:
                return None

            if sort:
                key, direction = sort[0]
                matches.sort(
                    key=lambda d: d.get(key) or datetime.min.replace(tzinfo=timezone.utc),
                    reverse=(direction == DESCENDING),
                )
            return dict(matches[0])

    def find_one_and_update(self, filter_q: dict, update_q: dict, return_document=None) -> dict | None:
        with self._lock:
            target = None
            for d in self.docs:
                matched = True
                for k, v in filter_q.items():
                    if d.get(k) != v:
                        matched = False
                        break
                if matched:
                    target = d
                    break

            if not target:
                return None

            if "$set" in update_q:
                for k, v in update_q["$set"].items():
                    target[k] = v

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
emp_day = {
    "emp_code":    "EMP0001",
    "name":        "Asha Rao",
    "email":       "asha@example.com",
    "department":  "Engineering",
    "shift_start": "09:30",
    "shift_end":   "18:30",
    "joined_on":   "2026-01-05",
}
emp_night = {
    "emp_code":    "EMP0005",
    "name":        "Vikram Patel",
    "email":       "vikram@example.com",
    "department":  "Operations",
    "shift_start": "22:00",
    "shift_end":   "06:00",
    "joined_on":   "2026-01-05",
}
mock_employees.docs = [emp_day, emp_night]


# ==============================================================================
# PUNCH-IN TESTS (1 to 12)
# ==============================================================================
print("\n-- PUNCH-IN TESTS --")

# 1. Successful punch-in with explicit epoch-ms timestamp (Sample 1 in DATA_MODEL: 09:28 IST = 03:58 UTC)
# 2026-07-06 03:58:00 UTC -> 1783310280000
mock_attendance.docs.clear()
p1_payload = {
    "emp_code":   "EMP0001",
    "punched_at": 1783310280000,
    "status":     "PRESENT",
}
res = client.post("/attendance/punch-in", json=p1_payload)
check("P4-1a: Explicit timestamp punch-in returns HTTP 201", res.status_code == 201)
d1 = res.json()
check("P4-1b: Attendance date is 2026-07-06", d1.get("date") == "2026-07-06")
check("P4-1c: punch_in is 1783310280000", d1.get("punch_in") == 1783310280000)
check("P4-1d: punch_out is None", d1.get("punch_out") is None)
check("P4-1e: work_hours is None", d1.get("work_hours") is None)
check("P4-1f: late_minutes is 0", d1.get("late_minutes") == 0)
check("P4-1g: overtime_minutes is 0", d1.get("overtime_minutes") == 0)
check("P4-1h: half_day is False", d1.get("half_day") is False)
check("P4-1i: history is empty list", d1.get("history") == [])
check("P4-1j: _id is absent", "_id" not in d1 and "id" not in d1)

# 2. Successful punch-in using current time (punched_at omitted)
# Seed new employee for today test
emp_today = dict(emp_day, emp_code="EMP0002")
mock_employees.docs.append(emp_today)
res_now = client.post("/attendance/punch-in", json={"emp_code": "EMP0002"})
check("P4-2a: Omitted punched_at returns HTTP 201", res_now.status_code == 201)
check("P4-2b: punch_in is valid epoch ms integer",
      isinstance(res_now.json().get("punch_in"), int) and res_now.json().get("punch_in") >= 100_000_000_000)

# 3. Invalid employee code / request body (422)
res_bad_ms = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": "not-a-number"})
check("P4-3a: Non-integer punched_at returns 422", res_bad_ms.status_code == 422)
res_bad_bool = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": True})
check("P4-3b: Boolean punched_at returns 422", res_bad_bool.status_code == 422)

# 4. Employee does not exist (404)
res_no_emp = client.post("/attendance/punch-in", json={"emp_code": "EMP9999", "punched_at": 1783310280000})
check("P4-4: Unknown employee returns HTTP 404", res_no_emp.status_code == 404)

# 5. Valid PRESENT, WFH, ON_DUTY statuses
emp_status = dict(emp_day, emp_code="EMP0003")
mock_employees.docs.append(emp_status)
res_wfh = client.post("/attendance/punch-in", json={"emp_code": "EMP0003", "punched_at": 1783310280000, "status": "WFH"})
check("P4-5a: WFH status accepted (201)", res_wfh.status_code == 201 and res_wfh.json().get("status") == "WFH")

emp_status_od = dict(emp_day, emp_code="EMP0004")
mock_employees.docs.append(emp_status_od)
res_od = client.post("/attendance/punch-in", json={"emp_code": "EMP0004", "punched_at": 1783310280000, "status": "ON_DUTY"})
check("P4-5b: ON_DUTY status accepted (201)", res_od.status_code == 201 and res_od.json().get("status") == "ON_DUTY")

# 6. Rejection of ABSENT and LEAVE at punch-in (422)
res_absent = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "ABSENT"})
check("P4-6a: ABSENT rejected at punch-in with 422", res_absent.status_code == 422)
res_leave = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "LEAVE"})
check("P4-6b: LEAVE rejected at punch-in with 422", res_leave.status_code == 422)

# 7. Duplicate (emp_code, date) returns 409 conflict
# EMP0001 already punched in on 2026-07-06 (step 1)
res_dup = client.post("/attendance/punch-in", json=p1_payload)
check("P4-7a: Duplicate punch-in on same date returns HTTP 409", res_dup.status_code == 409)
check("P4-7b: Conflict detail matches contract",
      res_dup.json().get("detail") == "already punched in for this date")

# 8. Concurrent duplicate punch-in attempts
emp_conc = dict(emp_day, emp_code="EMP0008")
mock_employees.docs.append(emp_conc)
conc_payload = {"emp_code": "EMP0008", "punched_at": 1783310280000}
conc_responses = []

def send_punch_in():
    return client.post("/attendance/punch-in", json=conc_payload)

with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
    futures = [executor.submit(send_punch_in) for _ in range(10)]
    for f in concurrent.futures.as_completed(futures):
        conc_responses.append(f.result())

in_201 = [r for r in conc_responses if r.status_code == 201]
in_409 = [r for r in conc_responses if r.status_code == 409]
check("P4-8a: Exactly one concurrent punch-in returns 201", len(in_201) == 1)
check("P4-8b: Remaining 9 concurrent punch-ins return 409", len(in_409) == 9)

# 9. Correct IST attendance date for ordinary shifts
# 2026-07-08 04:30:00 UTC = 10:00:00 IST -> 1783485000000
emp_ord = dict(emp_day, emp_code="EMP0009")
mock_employees.docs.append(emp_ord)
res_ist_date = client.post("/attendance/punch-in", json={"emp_code": "EMP0009", "punched_at": 1783485000000})
check("P4-9: Ordinary shift attendance date is 2026-07-08", res_ist_date.json().get("date") == "2026-07-08")

# 10. Overnight attendance-date behavior: EMP0005 (shift 22:00 - 06:00)
# A: Punch-in at 2026-07-07 05:45:00 IST (00:15:00 UTC) -> strictly before 06:00 -> date is 2026-07-06
ms_night_early = int(datetime(2026, 7, 7, 0, 15, 0, tzinfo=timezone.utc).timestamp() * 1000)
res_night_early = client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms_night_early})
check("P4-10a: Overnight punch-in strictly before 06:00 (05:45 IST) belongs to previous day 2026-07-06",
      res_night_early.status_code == 201 and res_night_early.json().get("date") == "2026-07-06")

# B: Punch-in at exactly shift_end boundary: 2026-07-07 06:00:00 IST (00:30:00 UTC) -> date is 2026-07-07
ms_night_boundary = int(datetime(2026, 7, 7, 0, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
res_night_bound = client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms_night_boundary})
check("P4-10b: Overnight punch-in at EXACTLY shift_end (06:00:00 IST) belongs to current day 2026-07-07",
      res_night_bound.status_code == 201 and res_night_bound.json().get("date") == "2026-07-07")

# 11. Late-minute boundaries around 10-minute grace period
# Shift 09:30. Grace ends at 09:40:00.
# 09:40:00 IST = 04:10:00 UTC on 2026-07-10 -> 0 late minutes
emp_late1 = dict(emp_day, emp_code="EMP0011")
mock_employees.docs.append(emp_late1)
ms_094000 = int(datetime(2026, 7, 10, 4, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
res_late_0 = client.post("/attendance/punch-in", json={"emp_code": "EMP0011", "punched_at": ms_094000})
check("P4-11a: Punch-in exactly at 09:40:00 IST produces 0 late minutes",
      res_late_0.json().get("late_minutes") == 0)

# 09:40:01 IST = 04:10:01 UTC on 2026-07-10 -> 10 late minutes
emp_late2 = dict(emp_day, emp_code="EMP0012")
mock_employees.docs.append(emp_late2)
ms_094001 = int(datetime(2026, 7, 10, 4, 10, 1, tzinfo=timezone.utc).timestamp() * 1000)
res_late_10 = client.post("/attendance/punch-in", json={"emp_code": "EMP0012", "punched_at": ms_094001})
check("P4-11b: Punch-in at 09:40:01 IST produces 10 late minutes",
      res_late_10.json().get("late_minutes") == 10)

# 12. UTC datetime storage and epoch-millisecond response serialization
stored_att = [d for d in mock_attendance.docs if d["emp_code"] == "EMP0001"][0]
check("P4-12a: punch_in stored as UTC datetime",
      isinstance(stored_att["punch_in"], datetime) and stored_att["punch_in"].tzinfo == timezone.utc)
check("P4-12b: punch_out stored initially as None", stored_att["punch_out"] is None)


# ==============================================================================
# PUNCH-OUT TESTS (13 to 22)
# ==============================================================================
print("\n-- PUNCH-OUT TESTS --")

# 13. Successful punch-out with explicit timestamp (Sample 1: in 09:28 IST, out 18:35 IST = 13:05 UTC)
# 1783343100000 = 2026-07-06 13:05:00 UTC
res_out1 = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": 1783343100000})
check("P4-13a: Successful punch-out returns HTTP 200", res_out1.status_code == 200)
d_out1 = res_out1.json()
check("P4-13b: punch_out serialized as epoch ms 1783343100000", d_out1.get("punch_out") == 1783343100000)
check("P4-13c: Sample 1 work_hours == 9.12", d_out1.get("work_hours") == 9.12)
check("P4-13d: Sample 1 overtime_minutes == 0 (5m < 30m)", d_out1.get("overtime_minutes") == 0)
check("P4-13e: Sample 1 half_day == False", d_out1.get("half_day") is False)
check("P4-13f: _id is absent", "_id" not in d_out1 and "id" not in d_out1)

# 14. Successful punch-out using current time (punched_at omitted)
emp_out_now = dict(emp_day, emp_code="EMP0014")
mock_employees.docs.append(emp_out_now)
ms_4h_ago = int((datetime.now(tz=timezone.utc) - timedelta(hours=4)).timestamp() * 1000)
client.post("/attendance/punch-in", json={"emp_code": "EMP0014", "punched_at": ms_4h_ago})
res_out2 = client.post("/attendance/punch-out", json={"emp_code": "EMP0014"})
check("P4-14a: Punch-out with omitted timestamp returns HTTP 200", res_out2.status_code == 200)
check("P4-14b: punch_out is integer epoch ms",
      isinstance(res_out2.json().get("punch_out"), int) and res_out2.json().get("punch_out") >= 100_000_000_000)

# 15. No attendance record exists (404)
emp_noop = dict(emp_day, emp_code="EMP0015")
mock_employees.docs.append(emp_noop)
res_no_rec = client.post("/attendance/punch-out", json={"emp_code": "EMP0015"})
check("P4-15: Punch-out with no open attendance record returns HTTP 404", res_no_rec.status_code == 404)

# 16. Attendance record already has punch-out (409)
# EMP0001 was already closed in step 13
res_already_out = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": 1783343100000 + 1000})
check("P4-16a: Punch-out on already-closed record returns HTTP 409", res_already_out.status_code == 409)
check("P4-16b: Conflict detail matches contract", res_already_out.json().get("detail") == "already punched out")

# 17. Concurrent punch-out attempts: only one update succeeds (409 on rest)
emp_conc_out = dict(emp_day, emp_code="EMP0017")
mock_employees.docs.append(emp_conc_out)
# Create open punch-in
t_in_ms = int(datetime(2026, 7, 11, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
t_out_ms = int(datetime(2026, 7, 11, 13, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
client.post("/attendance/punch-in", json={"emp_code": "EMP0017", "punched_at": t_in_ms})

conc_out_res = []
def send_punch_out():
    return client.post("/attendance/punch-out", json={"emp_code": "EMP0017", "punched_at": t_out_ms})

with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
    futures = [executor.submit(send_punch_out) for _ in range(10)]
    for f in concurrent.futures.as_completed(futures):
        conc_out_res.append(f.result())

out_200 = [r for r in conc_out_res if r.status_code == 200]
out_409 = [r for r in conc_out_res if r.status_code == 409]
check("P4-17a: Exactly one concurrent punch-out returns HTTP 200", len(out_200) == 1)
check("P4-17b: Remaining 9 concurrent punch-outs return HTTP 409", len(out_409) == 9)

# 18. Correct work-hours rounding and half-day threshold
# Case A: 3.5 hours worked (< 4.50) -> half_day == True (Sample 4)
emp_hday = dict(emp_day, emp_code="EMP0018")
mock_employees.docs.append(emp_hday)
in_3h = int(datetime(2026, 7, 12, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
out_3h = int(datetime(2026, 7, 12, 7, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
client.post("/attendance/punch-in", json={"emp_code": "EMP0018", "punched_at": in_3h, "status": "WFH"})
res_hday = client.post("/attendance/punch-out", json={"emp_code": "EMP0018", "punched_at": out_3h})
check("P4-18a: 3.5 work_hours -> half_day is True",
      res_hday.json().get("work_hours") == 3.50 and res_hday.json().get("half_day") is True)

# Case B: Exactly 4.50 work_hours -> half_day is False (strictly < 4.50)
emp_hday_edge = dict(emp_day, emp_code="EMP0019")
mock_employees.docs.append(emp_hday_edge)
in_45 = int(datetime(2026, 7, 13, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
out_45 = int(datetime(2026, 7, 13, 8, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
client.post("/attendance/punch-in", json={"emp_code": "EMP0019", "punched_at": in_45})
res_45 = client.post("/attendance/punch-out", json={"emp_code": "EMP0019", "punched_at": out_45})
check("P4-18b: Exactly 4.50 work_hours -> half_day is False (boundary)",
      res_45.json().get("work_hours") == 4.50 and res_45.json().get("half_day") is False)

# 19. Overtime thresholds: immediately below, exactly at, and above 30 minutes
# Shift end 18:30 IST = 13:00 UTC.
# A: 29m 59s overtime -> 13:29:59 UTC -> 0 overtime
emp_ot1 = dict(emp_day, emp_code="EMP0020")
mock_employees.docs.append(emp_ot1)
in_ot1 = int(datetime(2026, 7, 14, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
out_sub30 = int(datetime(2026, 7, 14, 13, 29, 59, tzinfo=timezone.utc).timestamp() * 1000)
client.post("/attendance/punch-in", json={"emp_code": "EMP0020", "punched_at": in_ot1})
res_sub30 = client.post("/attendance/punch-out", json={"emp_code": "EMP0020", "punched_at": out_sub30})
check("P4-19a: 29m 59s after shift end -> 0 overtime",
      res_sub30.json().get("overtime_minutes") == 0)

# B: Exactly 30m 00s overtime -> 13:30:00 UTC -> 30 overtime
emp_ot2 = dict(emp_day, emp_code="EMP0021")
mock_employees.docs.append(emp_ot2)
in_ot2 = int(datetime(2026, 7, 14, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
out_exact30 = int(datetime(2026, 7, 14, 13, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
client.post("/attendance/punch-in", json={"emp_code": "EMP0021", "punched_at": in_ot2})
res_exact30 = client.post("/attendance/punch-out", json={"emp_code": "EMP0021", "punched_at": out_exact30})
check("P4-19b: Exactly 30m after shift end -> 30 overtime",
      res_exact30.json().get("overtime_minutes") == 30)

# C: 40m overtime -> 13:40:00 UTC -> 40 overtime (Sample 2)
emp_ot3 = dict(emp_day, emp_code="EMP0022")
mock_employees.docs.append(emp_ot3)
in_ot3 = int(datetime(2026, 7, 14, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
out_ot40 = int(datetime(2026, 7, 14, 13, 40, 0, tzinfo=timezone.utc).timestamp() * 1000)
client.post("/attendance/punch-in", json={"emp_code": "EMP0022", "punched_at": in_ot3})
res_ot40 = client.post("/attendance/punch-out", json={"emp_code": "EMP0022", "punched_at": out_ot40})
check("P4-19c: 40m after shift end -> 40 overtime",
      res_ot40.json().get("overtime_minutes") == 40)

# 20. Overnight shift-end and overtime calculations (Sample 7 in DATA_MODEL)
# EMP0005: 22:00 - 06:00 shift.
# Punch in: 2026-07-06 21:55 IST = 16:25 UTC -> 1783355100000
# Punch out: 2026-07-07 06:40 IST = 01:10 UTC -> 1783386600000
emp_s7 = dict(emp_night, emp_code="EMP0025")
mock_employees.docs.append(emp_s7)
ms_s7_in = int(datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc).timestamp() * 1000)
ms_s7_out = int(datetime(2026, 7, 7, 1, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
client.post("/attendance/punch-in", json={"emp_code": "EMP0025", "punched_at": ms_s7_in})
res_s7 = client.post("/attendance/punch-out", json={"emp_code": "EMP0025", "punched_at": ms_s7_out})
check("P4-20a: Overnight shift Sample 7 work_hours == 8.75",
      res_s7.json().get("work_hours") == 8.75)
check("P4-20b: Overnight shift Sample 7 overtime_minutes == 40 (06:00 to 06:40 on day 2)",
      res_s7.json().get("overtime_minutes") == 40)
check("P4-20c: Overnight shift date is start day 2026-07-06",
      res_s7.json().get("date") == "2026-07-06")

# 21. Response serialization & absence of _id
data_final = res_s7.json()
check("P4-21a: All 10 AttendanceRecord fields present",
      set(data_final.keys()) == {
          "emp_code", "date", "status", "punch_in", "punch_out",
          "work_hours", "late_minutes", "overtime_minutes", "half_day", "history",
      })
check("P4-21b: _id is absent", "_id" not in data_final and "id" not in data_final)

# 22. Punch-out validation (422)
emp_val = dict(emp_day, emp_code="EMP0026")
mock_employees.docs.append(emp_val)
ms_val_in = int(datetime(2026, 7, 15, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
client.post("/attendance/punch-in", json={"emp_code": "EMP0026", "punched_at": ms_val_in})

# Case A: punched_at == punch_in (not strictly greater)
res_eq = client.post("/attendance/punch-out", json={"emp_code": "EMP0026", "punched_at": ms_val_in})
check("P4-22a: punched_at == punch_in returns HTTP 422", res_eq.status_code == 422)

# Case B: punched_at < punch_in
res_lt = client.post("/attendance/punch-out", json={"emp_code": "EMP0026", "punched_at": ms_val_in - 1000})
check("P4-22b: punched_at < punch_in returns HTTP 422", res_lt.status_code == 422)

# Case C: punched_at > punch_in + 24 hours
ms_gt_24h = ms_val_in + (25 * 3600 * 1000)
res_24h = client.post("/attendance/punch-out", json={"emp_code": "EMP0026", "punched_at": ms_gt_24h})
check("P4-22c: punched_at > punch_in + 24h returns HTTP 422", res_24h.status_code == 422)


# ==============================================================================
# Summary
# ==============================================================================
print("\n" + "=" * 65)
print(f"Results: {len(_passed)} passed, {len(_failed)} failed")
if _failed:
    print(f"FAILED tests: {_failed}")
    sys.exit(1)
else:
    print("ALL Phase 4 punch-in and punch-out endpoint tests PASSED!")
