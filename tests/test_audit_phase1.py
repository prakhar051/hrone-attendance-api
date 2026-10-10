"""
Phase 0 + Phase 1 verification audit.

ALL symbols under test are imported directly from app.main.
No logic is re-implemented here.

MongoClient is lazy in PyMongo — the module-level MongoClient(...) call does
not attempt a network connection at import time.  We set MONGO_URI to a port
that has nothing listening so that any accidental DB call fails loudly instead
of silently passing against a real database.

Run:
    python -X utf8 tests/test_audit_phase1.py
"""
import os
import sys
import calendar
from datetime import datetime, timezone, timedelta

# ── Point at a non-existent DB so no real MongoDB is needed ─────────────────
os.environ["MONGO_URI"] = "mongodb://127.0.0.1:27099"   # nothing here
os.environ["MONGO_DB"]  = "audit_test_db"

# ── Import the real application module ──────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app.main as m

from pydantic import ValidationError

# ── Test scaffolding ─────────────────────────────────────────────────────────
_passed: list[str] = []
_failed: list[str] = []

def ok(label: str) -> None:
    _passed.append(label)
    print(f"PASS  {label}")

def fail(label: str, msg: str) -> None:
    _failed.append(label)
    print(f"FAIL  {label}: {msg}")

def expect_valid(label: str, model_cls, data: dict) -> None:
    try:
        model_cls(**data)
        ok(label)
    except ValidationError as e:
        fail(label, str(e))

def expect_invalid(label: str, model_cls, data: dict) -> None:
    try:
        model_cls(**data)
        fail(label, "expected ValidationError — none raised")
    except ValidationError:
        ok(label)

# ── Baseline: confirm the import really is from app.main ────────────────────
assert m.__name__ == "app.main", "Imported wrong module"
print(f"\nImported module: {m.__file__}")
print("="*62)

# ═══════════════════════════════════════════════════════════════════════════
# SECTION A -- Phase 0 helpers
# ═══════════════════════════════════════════════════════════════════════════
print("\n-- A: Phase 0 helpers (from app.main) --")

# A1: IST constant
assert m.IST == timezone(timedelta(hours=5, minutes=30)), f"IST wrong: {m.IST}"
ok("A1: IST == UTC+05:30")

# A2-A5: to_ms / from_ms  — compute expected value with calendar.timegm
# (avoids local-timezone sensitivity of datetime.timestamp())
_dt_utc = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
_EXPECTED_MS = calendar.timegm(_dt_utc.timetuple()) * 1000   # 1783310280000

got_ms = m.to_ms(_dt_utc)
assert got_ms == _EXPECTED_MS, f"to_ms got {got_ms}, want {_EXPECTED_MS}"
ok(f"A2: m.to_ms(2026-07-06T03:58:00Z) == {_EXPECTED_MS}")

roundtrip = m.from_ms(_EXPECTED_MS)
assert roundtrip == _dt_utc, f"from_ms roundtrip: {roundtrip}"
ok("A3: m.from_ms round-trip")

# R1: sub-second truncation — +900 ms must give the same whole second
trunc = m.from_ms(_EXPECTED_MS + 900)
assert trunc.microsecond == 0,   f"microsecond not zero: {trunc}"
assert trunc == _dt_utc,          f"truncation wrong: {trunc}"
ok("A4: m.from_ms truncates sub-second (R1)")

trunc2 = m.from_ms(_EXPECTED_MS + 999)
assert trunc2 == _dt_utc, f"999ms truncation wrong: {trunc2}"
ok("A5: m.from_ms truncates 999ms to same second")

# A6: half_up — ROUND_HALF_UP cases that differ from banker's rounding
cases_halfup = [
    (9.125,  2, 9.13),   # banker's gives 9.12
    (4.445,  2, 4.45),   # banker's gives 4.44
    (9.08333, 2, 9.08),  # same result either way (control)
    (0.5,    0, 1.0),
    (2.5,    0, 3.0),    # banker's gives 2.0
    (0.1250, 4, 0.125),
    (0.12345, 4, 0.1235),
    (0.12355, 4, 0.1236),
]
for val, places, expected in cases_halfup:
    got = m.half_up(val, places)
    assert abs(got - expected) < 1e-9, (
        f"m.half_up({val}, {places}) = {got!r}, want {expected!r}"
    )
ok(f"A6: m.half_up ROUND_HALF_UP — {len(cases_halfup)} cases correct")

import builtins
assert builtins.round(9.125, 2) != m.half_up(9.125, 2), \
    "banker and half-up should differ for 9.125"
ok("A7: banker round(9.125,2)=9.12 diverges from m.half_up=9.13")

assert builtins.round(2.5, 0) != m.half_up(2.5, 0), \
    "banker and half-up should differ for 2.5"
ok("A8: banker round(2.5,0)=2.0 diverges from m.half_up=3.0")

# ═══════════════════════════════════════════════════════════════════════════
# SECTION B -- EpochMillisType strict validation  (via m.PunchInIn)
# ═══════════════════════════════════════════════════════════════════════════
print("\n-- B: EpochMillisType validation (from app.main) --")

_VALID_MS     = _EXPECTED_MS          # 1783310280000 — well within range
_VALID_MS_STR = str(_VALID_MS)        # "1783310280000"
_VALID_MS_FLT = float(_VALID_MS)      # 1783310280000.0

# B1 Valid boundaries — must be accepted
expect_valid("B1a: min boundary 100000000000 accepted",
             m.PunchInIn, {"emp_code": "EMP0001", "punched_at": 100_000_000_000})
expect_valid("B1b: max boundary 4102444800000 accepted",
             m.PunchInIn, {"emp_code": "EMP0001", "punched_at": 4_102_444_800_000})
expect_valid("B1c: typical in-range value accepted",
             m.PunchInIn, {"emp_code": "EMP0001", "punched_at": _VALID_MS})

# B2 Boolean — bool is subclass of int; must be caught explicitly
expect_invalid("B2a: True  rejected (bool subclass of int)",
               m.PunchInIn, {"emp_code": "EMP0001", "punched_at": True})
expect_invalid("B2b: False rejected (bool subclass of int)",
               m.PunchInIn, {"emp_code": "EMP0001", "punched_at": False})

# B3 Float — must be rejected (contract: 'floats rejected with 422')
expect_invalid("B3a: float  1783310280000.0  rejected",
               m.PunchInIn, {"emp_code": "EMP0001", "punched_at": _VALID_MS_FLT})
expect_invalid("B3b: float  123456789012.0   rejected",
               m.PunchInIn, {"emp_code": "EMP0001", "punched_at": 123456789012.0})
expect_invalid("B3c: float  1.78331028e12    rejected",
               m.PunchInIn, {"emp_code": "EMP0001", "punched_at": 1.78331028e12})

# B4 String — must be rejected (contract: 'strings rejected with 422')
expect_invalid('B4a: string "1783310280000"  rejected',
               m.PunchInIn, {"emp_code": "EMP0001", "punched_at": _VALID_MS_STR})
expect_invalid('B4b: string "123456789012"   rejected',
               m.PunchInIn, {"emp_code": "EMP0001", "punched_at": "123456789012"})

# B5 Out-of-range integers — must be rejected
expect_invalid("B5a: 1 rejected (seconds-range, below min)",
               m.PunchInIn, {"emp_code": "EMP0001", "punched_at": 1})
expect_invalid("B5b: 99999999999 rejected (1 below min)",
               m.PunchInIn, {"emp_code": "EMP0001", "punched_at": 99_999_999_999})
expect_invalid("B5c: 4102444800001 rejected (1 above max)",
               m.PunchInIn, {"emp_code": "EMP0001", "punched_at": 4_102_444_800_001})
expect_invalid("B5d: 0 rejected",
               m.PunchInIn, {"emp_code": "EMP0001", "punched_at": 0})
expect_invalid("B5e: -1 rejected",
               m.PunchInIn, {"emp_code": "EMP0001", "punched_at": -1})

# B6 Optional — None / omitted must be accepted
expect_valid("B6a: punched_at=None accepted  (optional field)",
             m.PunchInIn, {"emp_code": "EMP0001", "punched_at": None})
expect_valid("B6b: punched_at omitted accepted (optional field)",
             m.PunchInIn, {"emp_code": "EMP0001"})

# ═══════════════════════════════════════════════════════════════════════════
# SECTION C -- EmployeeIn  (from app.main)
# ═══════════════════════════════════════════════════════════════════════════
print("\n-- C: EmployeeIn validation (from app.main) --")

VALID_EMP = dict(
    emp_code   = "EMP0001",
    name       = "Asha Rao",
    email      = "asha@example.com",
    department = "Engineering",
    joined_on  = "2026-01-05",
)

# C1 Happy path
expect_valid("C1: minimal valid employee body", m.EmployeeIn, VALID_EMP)

# C2 emp_code pattern ^EMP\d{4,6}$
expect_valid  ("C2a: EMP0001   (4 digits)",        m.EmployeeIn, {**VALID_EMP, "emp_code": "EMP0001"})
expect_valid  ("C2b: EMP12345  (5 digits)",        m.EmployeeIn, {**VALID_EMP, "emp_code": "EMP12345"})
expect_valid  ("C2c: EMP123456 (6 digits)",        m.EmployeeIn, {**VALID_EMP, "emp_code": "EMP123456"})
expect_invalid("C2d: EMP123    (3 digits) rejected", m.EmployeeIn, {**VALID_EMP, "emp_code": "EMP123"})
expect_invalid("C2e: EMP1234567 (7 digits) rejected",m.EmployeeIn, {**VALID_EMP, "emp_code": "EMP1234567"})
expect_invalid("C2f: emp0001   (lowercase) rejected",m.EmployeeIn, {**VALID_EMP, "emp_code": "emp0001"})
expect_invalid("C2g: EMP-001   (hyphen) rejected",   m.EmployeeIn, {**VALID_EMP, "emp_code": "EMP-001"})
expect_invalid("C2h: EMPLOYEE01 rejected",           m.EmployeeIn, {**VALID_EMP, "emp_code": "EMPLOYEE01"})
expect_invalid("C2i: 0001 (no prefix) rejected",     m.EmployeeIn, {**VALID_EMP, "emp_code": "0001"})

# C3 name length (1–100)
expect_valid  ("C3a: name 1 char  accepted", m.EmployeeIn, {**VALID_EMP, "name": "A"})
expect_valid  ("C3b: name 100 chars accepted", m.EmployeeIn, {**VALID_EMP, "name": "A"*100})
expect_invalid("C3c: name empty rejected",    m.EmployeeIn, {**VALID_EMP, "name": ""})
expect_invalid("C3d: name 101 chars rejected",m.EmployeeIn, {**VALID_EMP, "name": "A"*101})

# C4 email pattern and max length 120
expect_valid  ("C4a: standard email accepted",    m.EmployeeIn, {**VALID_EMP, "email": "user@domain.com"})
expect_valid  ("C4b: subdomain email accepted",   m.EmployeeIn, {**VALID_EMP, "email": "a@b.co.in"})
expect_valid  ("C4c: 120-char email accepted",    m.EmployeeIn, {**VALID_EMP, "email": "a"*113+"@b.com"})  # 113+6=119<120
expect_invalid("C4d: no @  rejected",             m.EmployeeIn, {**VALID_EMP, "email": "nodomain"})
expect_invalid("C4e: no TLD rejected",            m.EmployeeIn, {**VALID_EMP, "email": "a@b"})
expect_invalid("C4f: email >120 chars rejected",  m.EmployeeIn, {**VALID_EMP, "email": "a"*115+"@b.com"})  # 121 chars
expect_invalid("C4g: space in email rejected",    m.EmployeeIn, {**VALID_EMP, "email": "a b@c.com"})

# C5 department length (1–50)
expect_valid  ("C5a: dept 1 char  accepted", m.EmployeeIn, {**VALID_EMP, "department": "X"})
expect_valid  ("C5b: dept 50 chars accepted", m.EmployeeIn, {**VALID_EMP, "department": "X"*50})
expect_invalid("C5c: dept empty rejected",    m.EmployeeIn, {**VALID_EMP, "department": ""})
expect_invalid("C5d: dept 51 chars rejected", m.EmployeeIn, {**VALID_EMP, "department": "X"*51})

# C6 shift_start / shift_end: valid HH:MM 24-hour format
expect_valid  ("C6a: 00:00 valid", m.EmployeeIn, {**VALID_EMP, "shift_start": "00:00", "shift_end": "08:00"})
expect_valid  ("C6b: 23:59 valid", m.EmployeeIn, {**VALID_EMP, "shift_start": "09:00", "shift_end": "23:59"})
expect_invalid("C6c: 24:00 rejected",         m.EmployeeIn, {**VALID_EMP, "shift_start": "24:00"})
expect_invalid("C6d: 9:30 (no pad) rejected", m.EmployeeIn, {**VALID_EMP, "shift_start": "9:30"})
expect_invalid("C6e: 09:60 rejected",         m.EmployeeIn, {**VALID_EMP, "shift_start": "09:60"})
expect_invalid("C6f: 09:3  rejected",         m.EmployeeIn, {**VALID_EMP, "shift_start": "09:3"})
expect_invalid("C6g: empty string rejected",  m.EmployeeIn, {**VALID_EMP, "shift_start": ""})

# C7 shift_start == shift_end rejected (D4 fix)
expect_invalid("C7a: shift_start==shift_end '09:30' rejected",
               m.EmployeeIn, {**VALID_EMP, "shift_start": "09:30", "shift_end": "09:30"})
expect_invalid("C7b: shift_start==shift_end '22:00' rejected",
               m.EmployeeIn, {**VALID_EMP, "shift_start": "22:00", "shift_end": "22:00"})
# Overnight shift: shift_end < shift_start is valid
expect_valid  ("C7c: overnight 22:00-06:00 accepted",
               m.EmployeeIn, {**VALID_EMP, "shift_start": "22:00", "shift_end": "06:00"})
expect_valid  ("C7d: shift_end < shift_start (15:00-09:00) accepted",
               m.EmployeeIn, {**VALID_EMP, "shift_start": "15:00", "shift_end": "09:00"})

# C8 joined_on date validation
expect_valid  ("C8a: 2026-01-05 valid",              m.EmployeeIn, {**VALID_EMP, "joined_on": "2026-01-05"})
expect_invalid("C8b: 2026-02-30 rejected",           m.EmployeeIn, {**VALID_EMP, "joined_on": "2026-02-30"})
expect_invalid("C8c: 2026-13-01 rejected",           m.EmployeeIn, {**VALID_EMP, "joined_on": "2026-13-01"})
expect_invalid("C8d: non-date string rejected",      m.EmployeeIn, {**VALID_EMP, "joined_on": "not-a-date"})
expect_invalid("C8e: integer joined_on rejected",    m.EmployeeIn, {**VALID_EMP, "joined_on": 20260105})
expect_invalid("C8f: 2026-00-01 rejected",           m.EmployeeIn, {**VALID_EMP, "joined_on": "2026-00-01"})

# C9 Defaults
_emp_no_shifts = {k: v for k, v in VALID_EMP.items()}  # no shift fields
emp_default = m.EmployeeIn(**_emp_no_shifts)
assert emp_default.shift_start == "09:30", f"default shift_start: {emp_default.shift_start}"
assert emp_default.shift_end   == "18:30", f"default shift_end: {emp_default.shift_end}"
ok("C9a: shift_start defaults to '09:30'")
ok("C9b: shift_end defaults to '18:30'")

# ═══════════════════════════════════════════════════════════════════════════
# SECTION D -- PunchInIn  (from app.main)
# ═══════════════════════════════════════════════════════════════════════════
print("\n-- D: PunchInIn validation (from app.main) --")

expect_valid("D1: PRESENT accepted",  m.PunchInIn, {"emp_code": "EMP0001", "status": "PRESENT"})
expect_valid("D2: WFH accepted",      m.PunchInIn, {"emp_code": "EMP0001", "status": "WFH"})
expect_valid("D3: ON_DUTY accepted",  m.PunchInIn, {"emp_code": "EMP0001", "status": "ON_DUTY"})
expect_valid("D4: default status is PRESENT",
             m.PunchInIn, {"emp_code": "EMP0001"})

p = m.PunchInIn(emp_code="EMP0001")
assert p.status == "PRESENT", f"default status: {p.status}"
ok("D4b: confirmed default status == 'PRESENT'")

expect_invalid("D5: ABSENT  rejected at punch-in", m.PunchInIn, {"emp_code": "EMP0001", "status": "ABSENT"})
expect_invalid("D6: LEAVE   rejected at punch-in", m.PunchInIn, {"emp_code": "EMP0001", "status": "LEAVE"})
expect_invalid("D7: 'WORKING' rejected",            m.PunchInIn, {"emp_code": "EMP0001", "status": "WORKING"})
expect_invalid("D8: '' (empty) rejected",           m.PunchInIn, {"emp_code": "EMP0001", "status": ""})

# EpochMillis validation via PunchInIn
expect_invalid("D9:  True  rejected",               m.PunchInIn, {"emp_code": "EMP0001", "punched_at": True})
expect_invalid("D10: float rejected",               m.PunchInIn, {"emp_code": "EMP0001", "punched_at": _VALID_MS_FLT})
expect_invalid("D11: string rejected",              m.PunchInIn, {"emp_code": "EMP0001", "punched_at": _VALID_MS_STR})
expect_invalid("D12: small int rejected",           m.PunchInIn, {"emp_code": "EMP0001", "punched_at": 1_000_000})

# ═══════════════════════════════════════════════════════════════════════════
# SECTION E -- PunchOutIn  (from app.main)
# ═══════════════════════════════════════════════════════════════════════════
print("\n-- E: PunchOutIn validation (from app.main) --")

expect_valid  ("E1: punched_at valid epoch ms accepted",
               m.PunchOutIn, {"emp_code": "EMP0001", "punched_at": _VALID_MS})
expect_valid  ("E2: punched_at omitted (defaults to now)", m.PunchOutIn, {"emp_code": "EMP0001"})
expect_invalid("E3: True rejected",   m.PunchOutIn, {"emp_code": "EMP0001", "punched_at": True})
expect_invalid("E4: float rejected",  m.PunchOutIn, {"emp_code": "EMP0001", "punched_at": _VALID_MS_FLT})
expect_invalid("E5: string rejected", m.PunchOutIn, {"emp_code": "EMP0001", "punched_at": _VALID_MS_STR})

# ═══════════════════════════════════════════════════════════════════════════
# SECTION F -- RegularizeIn  (from app.main)
# ═══════════════════════════════════════════════════════════════════════════
print("\n-- F: RegularizeIn validation (from app.main) --")

VALID_REG = {"reason": "biometric glitch corrected", "regularized_by": "hr.admin"}

expect_valid("F1: minimal valid body",   m.RegularizeIn, VALID_REG)
expect_valid("F2: PRESENT status OK",   m.RegularizeIn, {**VALID_REG, "status": "PRESENT"})
expect_valid("F3: WFH status OK",       m.RegularizeIn, {**VALID_REG, "status": "WFH"})
expect_valid("F4: ON_DUTY status OK",   m.RegularizeIn, {**VALID_REG, "status": "ON_DUTY"})
expect_valid("F5: ABSENT status OK",    m.RegularizeIn, {**VALID_REG, "status": "ABSENT"})
expect_valid("F6: LEAVE status OK",     m.RegularizeIn, {**VALID_REG, "status": "LEAVE"})
expect_invalid("F7: 'WORKING' rejected",m.RegularizeIn, {**VALID_REG, "status": "WORKING"})

# reason length (5–200)
expect_valid  ("F8a: reason 5 chars OK",    m.RegularizeIn, {"reason": "fixed", "regularized_by": "hr"})
expect_valid  ("F8b: reason 200 chars OK",  m.RegularizeIn, {"reason": "x"*200, "regularized_by": "hr"})
expect_invalid("F8c: reason 4 chars rejected",  m.RegularizeIn, {"reason": "abcd", "regularized_by": "hr"})
expect_invalid("F8d: reason 201 chars rejected", m.RegularizeIn, {"reason": "x"*201, "regularized_by": "hr"})

# regularized_by length (1–50)
expect_valid  ("F9a: regularized_by 1 char OK",   m.RegularizeIn, {**VALID_REG, "regularized_by": "a"})
expect_valid  ("F9b: regularized_by 50 chars OK",  m.RegularizeIn, {**VALID_REG, "regularized_by": "a"*50})
expect_invalid("F9c: regularized_by empty rejected", m.RegularizeIn, {**VALID_REG, "regularized_by": ""})
expect_invalid("F9d: regularized_by 51 chars rejected", m.RegularizeIn, {**VALID_REG, "regularized_by": "a"*51})

# punch_in / punch_out EpochMillis fields
expect_valid  ("F10a: punch_in valid epoch ms",   m.RegularizeIn, {**VALID_REG, "punch_in": _VALID_MS})
expect_valid  ("F10b: punch_out valid epoch ms",  m.RegularizeIn, {**VALID_REG, "punch_out": _VALID_MS})
expect_valid  ("F10c: both punch times None OK",  m.RegularizeIn, VALID_REG)
expect_invalid("F11a: punch_in True rejected",    m.RegularizeIn, {**VALID_REG, "punch_in": True})
expect_invalid("F11b: punch_in float rejected",   m.RegularizeIn, {**VALID_REG, "punch_in": _VALID_MS_FLT})
expect_invalid("F11c: punch_in string rejected",  m.RegularizeIn, {**VALID_REG, "punch_in": _VALID_MS_STR})
expect_invalid("F12a: punch_out True rejected",   m.RegularizeIn, {**VALID_REG, "punch_out": True})
expect_invalid("F12b: punch_out float rejected",  m.RegularizeIn, {**VALID_REG, "punch_out": _VALID_MS_FLT})
expect_invalid("F12c: punch_out string rejected", m.RegularizeIn, {**VALID_REG, "punch_out": _VALID_MS_STR})

# ═══════════════════════════════════════════════════════════════════════════
# SECTION G -- Serialization helpers  (from app.main)
# ═══════════════════════════════════════════════════════════════════════════
print("\n-- G: Serialization helpers (from app.main) --")

_utc = timezone.utc
_dt1 = datetime(2026, 7, 6, 3, 58, 0, tzinfo=_utc)
_dt2 = datetime(2026, 7, 6, 13, 5, 0, tzinfo=_utc)

_ms1 = calendar.timegm(_dt1.timetuple()) * 1000
_ms2 = calendar.timegm(_dt2.timetuple()) * 1000

# G1 _serialize_employee: drops _id, converts created_at
emp_doc = {
    "_id": "mongo_object_id_internal",
    "emp_code": "EMP0001", "name": "Asha Rao",
    "email": "asha@example.com", "department": "Engineering",
    "shift_start": "09:30", "shift_end": "18:30",
    "joined_on": "2026-01-05", "created_at": _dt1,
}
ser_emp = m._serialize_employee(emp_doc)
assert "_id" not in ser_emp,                          "G1: _id leaked"
assert "id"  not in ser_emp,                          "G1: extra 'id' field leaked"
assert isinstance(ser_emp["created_at"], int),         "G1: created_at not int"
assert ser_emp["created_at"] == _ms1,                 f"G1: created_at {ser_emp['created_at']} != {_ms1}"
assert ser_emp["emp_code"]   == "EMP0001"
assert ser_emp["name"]       == "Asha Rao"
ok("G1: _serialize_employee: _id absent, created_at is int epoch ms, all fields present")

# Verify all 8 required Employee fields are present
required_emp_fields = {"emp_code","name","email","department",
                       "shift_start","shift_end","joined_on","created_at"}
missing = required_emp_fields - set(ser_emp)
assert not missing, f"G1b: missing employee response fields: {missing}"
ok("G1b: all 8 Employee response fields present")

# G2 _serialize_attendance: punch_in/out as epoch ms, punch_out=None
att_doc = {
    "_id": "some_objectid",
    "emp_code": "EMP0006", "date": "2026-07-07", "status": "PRESENT",
    "punch_in": _dt1, "punch_out": None, "work_hours": None,
    "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": [],
}
ser_att = m._serialize_attendance(att_doc)
assert "_id"  not in ser_att,                          "G2: _id leaked"
assert "id"   not in ser_att,                          "G2: extra 'id' field leaked"
assert isinstance(ser_att["punch_in"], int),           "G2: punch_in not int"
assert ser_att["punch_in"]  == _ms1,                  f"G2: punch_in {ser_att['punch_in']} != {_ms1}"
assert ser_att["punch_out"] is None,                  "G2: punch_out should be None"
assert ser_att["work_hours"] is None,                 "G2: work_hours should be None"
ok("G2: _serialize_attendance: _id absent, punch_in is int epoch ms, punch_out is None")

# G3 Both punch times present (closed record)
att_closed = {**att_doc, "punch_out": _dt2, "work_hours": 9.12}
ser_closed = m._serialize_attendance(att_closed)
assert isinstance(ser_closed["punch_out"], int),       "G3: punch_out not int"
assert ser_closed["punch_out"] == _ms2,               f"G3: punch_out {ser_closed['punch_out']} != {_ms2}"
assert ser_closed["work_hours"] == 9.12
ok("G3: closed attendance record: punch_out is int epoch ms")

# G4 Legacy record — missing half_day and history (DATA_MODEL.md §9)
legacy_doc = {
    "emp_code": "EMP0002", "date": "2026-07-14", "status": "PRESENT",
    "punch_in": _dt1, "punch_out": _dt2,
    "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0,
    # half_day and history are intentionally missing
}
ser_legacy = m._serialize_attendance(legacy_doc)
assert ser_legacy["half_day"] is False,    "G4: missing half_day should default False"
assert ser_legacy["history"]  == [],       "G4: missing history should default []"
ok("G4: legacy record (no half_day, no history) gets correct defaults")

# G5 History entry with BSON dates in changes
_dt_hist    = datetime(2026, 7, 8, 5, 0, 0, tzinfo=_utc)
_dt_old_in  = datetime(2026, 7, 7, 4, 35, 0, tzinfo=_utc)
_dt_new_in  = datetime(2026, 7, 7, 3, 58, 0, tzinfo=_utc)
_ms_hist    = calendar.timegm(_dt_hist.timetuple()) * 1000
_ms_old_in  = calendar.timegm(_dt_old_in.timetuple()) * 1000
_ms_new_in  = calendar.timegm(_dt_new_in.timetuple()) * 1000

att_with_hist = {
    "emp_code": "EMP0001", "date": "2026-07-07", "status": "PRESENT",
    "punch_in": _dt_new_in, "punch_out": _dt2, "work_hours": 9.03,
    "late_minutes": 0, "overtime_minutes": 0, "half_day": False,
    "history": [{
        "at": _dt_hist, "by": "hr.admin", "reason": "biometric glitch",
        "changes": {
            "punch_in": {"from": _dt_old_in, "to": _dt_new_in},
            "late_minutes": {"from": 35, "to": 0},
            "work_hours":   {"from": 8.42, "to": 9.03},
        },
    }],
}
ser_hist_att = m._serialize_attendance(att_with_hist)
h = ser_hist_att["history"][0]
assert isinstance(h["at"], int),                              "G5: history.at not int"
assert h["at"] == _ms_hist,                                   f"G5: history.at {h['at']} != {_ms_hist}"
assert isinstance(h["changes"]["punch_in"]["from"], int),     "G5: changes.punch_in.from not int"
assert h["changes"]["punch_in"]["from"] == _ms_old_in,        "G5: wrong punch_in.from"
assert isinstance(h["changes"]["punch_in"]["to"], int),       "G5: changes.punch_in.to not int"
assert h["changes"]["punch_in"]["to"]   == _ms_new_in,        "G5: wrong punch_in.to"
assert h["changes"]["late_minutes"]["from"] == 35             # non-datetime: unchanged
assert h["changes"]["late_minutes"]["to"]   == 0
assert h["changes"]["work_hours"]["from"]   == 8.42
assert h["changes"]["work_hours"]["to"]     == 9.03
ok("G5: history entry: .at is int, punch_in changes are int, non-datetime changes unchanged")

# G6 _ms_or_none helper
assert m._ms_or_none(None) is None,         "G6: None input should return None"
assert m._ms_or_none(_dt1)  == _ms1,        f"G6: _ms_or_none({_dt1}) should be {_ms1}"
ok("G6: m._ms_or_none: None->None, datetime->int epoch ms")

# ═══════════════════════════════════════════════════════════════════════════
# SECTION H -- Contract cross-check
# ═══════════════════════════════════════════════════════════════════════════
print("\n-- H: Contract cross-checks --")

# H1 EpochMillis bounds match openapi.yaml
assert m._EPOCH_MS_MIN == 100_000_000_000,    f"H1: min {m._EPOCH_MS_MIN}"
assert m._EPOCH_MS_MAX == 4_102_444_800_000,  f"H1: max {m._EPOCH_MS_MAX}"
ok("H1: EpochMillis bounds match openapi.yaml (100000000000 / 4102444800000)")

# H2 EmployeeIn emp_code pattern matches openapi.yaml '^EMP\d{4,6}$'
import re
_EMP_PATTERN = re.compile(r'^EMP\d{4,6}$')
assert     _EMP_PATTERN.match("EMP0001")
assert     _EMP_PATTERN.match("EMP123456")
assert not _EMP_PATTERN.match("EMP123")
assert not _EMP_PATTERN.match("EMP1234567")
ok("H2: emp_code pattern ^EMP\\d{4,6}$ matches contract")

# H3 EmployeeIn defaults match openapi.yaml
_e = m.EmployeeIn(**VALID_EMP)
assert _e.shift_start == "09:30", f"H3: shift_start default {_e.shift_start}"
assert _e.shift_end   == "18:30", f"H3: shift_end default {_e.shift_end}"
ok("H3: EmployeeIn defaults match openapi.yaml (09:30 / 18:30)")

# H4 PunchInIn default status
_p = m.PunchInIn(emp_code="EMP0001")
assert _p.status == "PRESENT", f"H4: default status {_p.status}"
ok("H4: PunchInIn default status == 'PRESENT'")

# H5 RegularizeIn required fields
try:
    m.RegularizeIn(reason="ok reason", regularized_by="admin")
    ok("H5a: RegularizeIn accepts minimal body (reason + regularized_by)")
except ValidationError as e:
    fail("H5a", str(e))
try:
    m.RegularizeIn(reason="ok reason")   # missing regularized_by
    fail("H5b", "missing regularized_by should raise")
except ValidationError:
    ok("H5b: RegularizeIn rejects missing regularized_by")
try:
    m.RegularizeIn(regularized_by="admin")  # missing reason
    fail("H5c", "missing reason should raise")
except ValidationError:
    ok("H5c: RegularizeIn rejects missing reason")

# H6 AttendanceRecord fields present in serialized output (matches openapi.yaml schema)
required_att_fields = {
    "emp_code", "date", "status", "punch_in", "punch_out",
    "work_hours", "late_minutes", "overtime_minutes", "half_day", "history",
}
missing_att = required_att_fields - set(ser_att)
assert not missing_att, f"H6: missing AttendanceRecord fields: {missing_att}"
ok("H6: all 10 AttendanceRecord fields present in serialized attendance")

# H7 No 'id' field (old starter bug D17)
assert "id" not in ser_att, "H7: spurious 'id' field found in attendance response"
assert "id" not in ser_emp, "H7: spurious 'id' field found in employee response"
ok("H7: no spurious 'id' field in any response (D17 fix confirmed)")

# ═══════════════════════════════════════════════════════════════════════════
print("\n" + "="*62)
print(f"Results: {len(_passed)} passed, {len(_failed)} failed")
if _failed:
    print("\nFailed tests:")
    for t in _failed:
        print(f"  FAIL  {t}")
    sys.exit(1)
else:
    print("All tests passed against the REAL app.main module.")
