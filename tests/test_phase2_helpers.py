"""
Phase 2 verification suite — Pure business-rule helpers only.

ALL functions under test are imported directly from app.main.
No logic is re-implemented in this file.

Run:
    python -X utf8 tests/test_phase2_helpers.py
"""
import calendar
import os
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

# ── Set dummy DB so app.main imports safely without live MongoDB ─────────────
os.environ["MONGO_URI"] = "mongodb://127.0.0.1:27099"
os.environ["MONGO_DB"]  = "phase2_test_db"

# ── Import real application module ───────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app.main as m

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

# Confirm module origin
assert m.__name__ == "app.main", "Imported wrong module"
print(f"\nImported module: {m.__file__}")
print("=" * 65)


# ==============================================================================
# 1. R1: IST attendance-date and timezone conversion
# ==============================================================================
print("\n-- 1. R1: IST Attendance-Date & Timezone Conversion --")

# A: Timezone conversions to Asia/Kolkata
utc_dt = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
ist_dt = m.to_ist(utc_dt)
check("R1-A1: UTC 03:58:00 converts to IST 09:28:00",
      ist_dt.year == 2026 and ist_dt.month == 7 and ist_dt.day == 6 and
      ist_dt.hour == 9 and ist_dt.minute == 28 and ist_dt.second == 0 and
      ist_dt.tzinfo == m.IST)

# Truncates sub-seconds
sub_dt = datetime(2026, 7, 6, 9, 28, 0, 999999, tzinfo=m.IST)
check("R1-A2: Sub-seconds truncated to microsecond 0",
      m.to_ist(sub_dt).microsecond == 0)

# Epoch ms integer input
epoch_ms = 1783310280000  # 2026-07-06 03:58:00 UTC = 09:28:00 IST
ist_from_ms = m.to_ist(epoch_ms)
check("R1-A3: to_ist accepts epoch ms integer",
      ist_from_ms.hour == 9 and ist_from_ms.minute == 28 and ist_from_ms.tzinfo == m.IST)

# Host machine timezone independence: different offsets all yield same IST
ny_tz = timezone(timedelta(hours=-4))
ny_dt = datetime(2026, 7, 5, 23, 58, 0, tzinfo=ny_tz)  # same instant as 03:58 UTC
check("R1-A4: UTC-04:00 instant converts to same IST datetime",
      m.to_ist(ny_dt) == ist_dt)

# B: Ordinary shifts (shift_end > shift_start, e.g. 09:30 - 18:30)
p_norm = datetime(2026, 7, 6, 9, 28, 0, tzinfo=m.IST)
check("R1-B1: Ordinary shift punch-in yields current IST date",
      m.compute_attendance_date(p_norm, "09:30", "18:30") == "2026-07-06")

p_late = datetime(2026, 7, 6, 10, 5, 0, tzinfo=m.IST)
check("R1-B2: Ordinary shift late punch-in yields current IST date",
      m.compute_attendance_date(p_late, "09:30", "18:30") == "2026-07-06")

# C: Overnight shifts (shift_end <= shift_start, e.g. 22:00 - 06:00)
# Evening punch-in before midnight: belongs to current calendar day
p_eve = datetime(2026, 7, 6, 21, 55, 0, tzinfo=m.IST)
check("R1-C1: Overnight shift evening punch-in (21:55) -> 2026-07-06",
      m.compute_attendance_date(p_eve, "22:00", "06:00") == "2026-07-06")

p_midnight_eve = datetime(2026, 7, 6, 23, 59, 59, tzinfo=m.IST)
check("R1-C2: Overnight shift 23:59:59 punch-in -> 2026-07-06",
      m.compute_attendance_date(p_midnight_eve, "22:00", "06:00") == "2026-07-06")

# Early morning punch-in strictly before shift_end: belongs to PREVIOUS calendar day
p_morn_00 = datetime(2026, 7, 7, 0, 0, 0, tzinfo=m.IST)
check("R1-C3: Overnight shift 00:00:00 next day -> 2026-07-06 (previous day)",
      m.compute_attendance_date(p_morn_00, "22:00", "06:00") == "2026-07-06")

p_morn_05 = datetime(2026, 7, 7, 5, 45, 0, tzinfo=m.IST)
check("R1-C4: Overnight shift 05:45:00 next day -> 2026-07-06 (previous day)",
      m.compute_attendance_date(p_morn_05, "22:00", "06:00") == "2026-07-06")

p_morn_edge_before = datetime(2026, 7, 7, 5, 59, 59, tzinfo=m.IST)
check("R1-C5: Overnight shift 05:59:59 next day -> 2026-07-06 (strictly earlier than shift_end)",
      m.compute_attendance_date(p_morn_edge_before, "22:00", "06:00") == "2026-07-06")

# EXACT overnight shift_end boundary:
p_morn_exact = datetime(2026, 7, 7, 6, 0, 0, tzinfo=m.IST)
check("R1-C6: Overnight shift at EXACTLY shift_end (06:00:00) -> 2026-07-07 (current IST date)",
      m.compute_attendance_date(p_morn_exact, "22:00", "06:00") == "2026-07-07")

p_morn_edge_after = datetime(2026, 7, 7, 6, 0, 1, tzinfo=m.IST)
check("R1-C7: Overnight shift 06:00:01 -> 2026-07-07 (after shift_end)",
      m.compute_attendance_date(p_morn_edge_after, "22:00", "06:00") == "2026-07-07")

# Alias check
check("R1-D: attendance_date alias is callable and matches",
      m.attendance_date(p_eve, "22:00", "06:00") == "2026-07-06")


# ==============================================================================
# 2. R2: Late minutes
# ==============================================================================
print("\n-- 2. R2: Late Minutes --")

# Shift 09:30
# Early arrival
p_early = datetime(2026, 7, 6, 9, 15, 0, tzinfo=m.IST)
check("R2-1: Early arrival (09:15 for 09:30) -> 0 late minutes",
      m.compute_late_minutes(p_early, "09:30") == 0)

# Exact start
p_start = datetime(2026, 7, 6, 9, 30, 0, tzinfo=m.IST)
check("R2-2: Exactly on start (09:30:00) -> 0 late minutes",
      m.compute_late_minutes(p_start, "09:30") == 0)

# Mid-grace (09:35)
p_grace = datetime(2026, 7, 6, 9, 35, 0, tzinfo=m.IST)
check("R2-3: Inside grace (09:35:00) -> 0 late minutes",
      m.compute_late_minutes(p_grace, "09:30") == 0)

# Grace boundary: exactly 10 minutes (09:40:00)
p_grace_edge = datetime(2026, 7, 6, 9, 40, 0, tzinfo=m.IST)
check("R2-4: Exact 10-minute grace boundary (09:40:00) -> 0 late minutes",
      m.compute_late_minutes(p_grace_edge, "09:30") == 0)

# Strictly > 10 minutes: 1 second past grace (09:40:01) -> 10 minutes
p_late_1s = datetime(2026, 7, 6, 9, 40, 1, tzinfo=m.IST)
check("R2-5: 09:40:01 (1s past grace) -> 10 late minutes (measured from start)",
      m.compute_late_minutes(p_late_1s, "09:30") == 10)

# R2 contract example: 10:15:59 -> 45
p_contract_1 = datetime(2026, 7, 6, 10, 15, 59, tzinfo=m.IST)
check("R2-6: Contract example 10:15:59 -> 45 late minutes",
      m.compute_late_minutes(p_contract_1, "09:30") == 45)

# 10:16:00 -> 46
p_contract_2 = datetime(2026, 7, 6, 10, 16, 0, tzinfo=m.IST)
check("R2-7: 10:16:00 -> 46 late minutes",
      m.compute_late_minutes(p_contract_2, "09:30") == 46)

# DATA_MODEL Sample 2: 10:05 -> 35
p_sample2 = datetime(2026, 7, 6, 10, 5, 0, tzinfo=m.IST)
check("R2-8: Sample 2 punch-in 10:05:00 -> 35 late minutes",
      m.compute_late_minutes(p_sample2, "09:30") == 35)

# Overnight shift late minutes (22:00 shift, punch-in next day 00:05)
p_overnight_late = datetime(2026, 7, 7, 0, 5, 0, tzinfo=m.IST)
check("R2-9: Overnight shift punch-in (00:05 on July 7, shift July 6 22:00) -> 125 late minutes",
      m.compute_late_minutes(p_overnight_late, "22:00", attendance_date="2026-07-06") == 125)


# ==============================================================================
# 3. R3: Overtime
# ==============================================================================
print("\n-- 3. R3: Overtime Minutes --")

# Ordinary shift: 09:30 - 18:30 on 2026-07-06
# Early punch-out
out_early = datetime(2026, 7, 6, 18, 0, 0, tzinfo=m.IST)
check("R3-1: Early punch-out (18:00) -> 0 overtime",
      m.compute_overtime(out_early, "18:30", "2026-07-06") == 0)

# Exact shift end
out_exact = datetime(2026, 7, 6, 18, 30, 0, tzinfo=m.IST)
check("R3-2: Exactly at shift end (18:30:00) -> 0 overtime",
      m.compute_overtime(out_exact, "18:30", "2026-07-06") == 0)

# Sample 1: 5 minutes overtime (< 30) -> 0
out_sample1 = datetime(2026, 7, 6, 18, 35, 0, tzinfo=m.IST)
check("R3-3: Sample 1 (18:35:00, 5m overtime) -> 0 overtime (below 30m threshold)",
      m.compute_overtime(out_sample1, "18:30", "2026-07-06") == 0)

# Immediately below threshold: 18:59:59 (29m 59s)
out_sub30 = datetime(2026, 7, 6, 18, 59, 59, tzinfo=m.IST)
check("R3-4: 18:59:59 (29m 59s) -> 0 overtime",
      m.compute_overtime(out_sub30, "18:30", "2026-07-06") == 0)

# Exactly at 30 minutes threshold: 19:00:00 -> 30
out_exact30 = datetime(2026, 7, 6, 19, 0, 0, tzinfo=m.IST)
check("R3-5: Exactly at 30m threshold (19:00:00) -> 30 overtime",
      m.compute_overtime(out_exact30, "18:30", "2026-07-06") == 30)

# Floor semantics above threshold: 19:00:59 -> 30
out_floor30 = datetime(2026, 7, 6, 19, 0, 59, tzinfo=m.IST)
check("R3-6: 19:00:59 -> 30 overtime (floor semantics)",
      m.compute_overtime(out_floor30, "18:30", "2026-07-06") == 30)

# Sample 2: 19:10:00 -> 40
out_sample2 = datetime(2026, 7, 6, 19, 10, 0, tzinfo=m.IST)
check("R3-7: Sample 2 (19:10:00) -> 40 overtime",
      m.compute_overtime(out_sample2, "18:30", "2026-07-06") == 40)

# Overnight shift overtime: shift 22:00 - 06:00 on 2026-07-06
# Shift end falls on 2026-07-07 06:00:00 IST
# Punch-out at 06:20 next day: 20 min overtime < 30 -> 0
out_on_sub = datetime(2026, 7, 7, 6, 20, 0, tzinfo=m.IST)
check("R3-8: Overnight shift out at 06:20 (20m overtime) -> 0",
      m.compute_overtime(out_on_sub, "06:00", "2026-07-06", shift_start="22:00") == 0)

# Punch-out at 06:29:59 next day -> 0
out_on_sub29 = datetime(2026, 7, 7, 6, 29, 59, tzinfo=m.IST)
check("R3-9: Overnight shift out at 06:29:59 -> 0",
      m.compute_overtime(out_on_sub29, "06:00", "2026-07-06", is_overnight=True) == 0)

# Punch-out at 06:30:00 next day -> 30
out_on_exact30 = datetime(2026, 7, 7, 6, 30, 0, tzinfo=m.IST)
check("R3-10: Overnight shift out at 06:30:00 -> 30 overtime",
      m.compute_overtime(out_on_exact30, "06:00", "2026-07-06", is_overnight=True) == 30)

# Sample 7: Punch-out at 06:40 next day -> 40 overtime
out_sample7 = datetime(2026, 7, 7, 6, 40, 0, tzinfo=m.IST)
check("R3-11: Sample 7 overnight out at 06:40:00 -> 40 overtime",
      m.compute_overtime(out_sample7, "06:00", "2026-07-06", shift_start="22:00") == 40)


# ==============================================================================
# 4. R4: Work hours calculation & Decimal half-up rounding
# ==============================================================================
print("\n-- 4. R4: Work Hours Calculation --")

# Sample 1: 09:28 to 18:35 (9h 7m = 32820s / 3600 = 9.11666...) -> 9.12
p_in_s1 = datetime(2026, 7, 6, 9, 28, 0, tzinfo=m.IST)
p_out_s1 = datetime(2026, 7, 6, 18, 35, 0, tzinfo=m.IST)
check("R4-1: Sample 1 work_hours == 9.12",
      m.compute_work_hours(p_in_s1, p_out_s1) == 9.12)

# Sample 2: 10:05 to 19:10 (9h 5m = 32700s / 3600 = 9.08333...) -> 9.08
p_in_s2 = datetime(2026, 7, 6, 10, 5, 0, tzinfo=m.IST)
p_out_s2 = datetime(2026, 7, 6, 19, 10, 0, tzinfo=m.IST)
check("R4-2: Sample 2 work_hours == 9.08",
      m.compute_work_hours(p_in_s2, p_out_s2) == 9.08)

# Sample 4: 09:30 to 13:00 (3.5 hours) -> 3.5
p_in_s4 = datetime(2026, 7, 7, 9, 30, 0, tzinfo=m.IST)
p_out_s4 = datetime(2026, 7, 7, 13, 0, 0, tzinfo=m.IST)
check("R4-3: Sample 4 work_hours == 3.50",
      m.compute_work_hours(p_in_s4, p_out_s4) == 3.50)

# Sample 7: overnight 21:55 to 06:40 next day (8.75 hours) -> 8.75
p_in_s7 = datetime(2026, 7, 6, 21, 55, 0, tzinfo=m.IST)
p_out_s7 = datetime(2026, 7, 7, 6, 40, 0, tzinfo=m.IST)
check("R4-4: Sample 7 overnight work_hours == 8.75",
      m.compute_work_hours(p_in_s7, p_out_s7) == 8.75)

# Half-up boundary test: 32850 seconds / 3600 = exactly 9.125 hours
# Banker's rounding gives 9.12; Decimal ROUND_HALF_UP gives 9.13!
t0 = datetime(2026, 7, 6, 0, 0, 0, tzinfo=m.IST)
t_9125 = t0 + timedelta(seconds=32850)
check("R4-5: 9.125 hours rounds half-up to 9.13 (not banker's 9.12)",
      m.compute_work_hours(t0, t_9125) == 9.13)

# 4.445 hours: 16002 seconds / 3600 = 4.445 hours -> rounds half-up to 4.45
t_4445 = t0 + timedelta(seconds=16002)
check("R4-6: 4.445 hours rounds half-up to 4.45 (not banker's 4.44)",
      m.compute_work_hours(t0, t_4445) == 4.45)

# Truncation of sub-seconds before calculation
t_sub1 = datetime(2026, 7, 6, 9, 0, 0, 500000, tzinfo=m.IST)
t_sub2 = datetime(2026, 7, 6, 10, 0, 0, 900000, tzinfo=m.IST)
check("R4-7: Sub-seconds truncated, exactly 1.0 hour",
      m.compute_work_hours(t_sub1, t_sub2) == 1.0)

# Zero / negative duration
check("R4-8: Punch out before punch in returns 0.0",
      m.compute_work_hours(t_sub2, t_sub1) == 0.0)


# ==============================================================================
# 5. R5: Half-day flag
# ==============================================================================
print("\n-- 5. R5: Half-Day Flag --")

check("R5-1: None work_hours -> half_day = False",
      m.compute_half_day(None) is False)

check("R5-2: 0.0 work_hours -> half_day = True (< 4.50)",
      m.compute_half_day(0.0) is True)

check("R5-3: 3.5 work_hours (Sample 4) -> half_day = True",
      m.compute_half_day(3.5) is True)

check("R5-4: 4.49 work_hours -> half_day = True (< 4.50)",
      m.compute_half_day(4.49) is True)

check("R5-5: 4.50 work_hours -> half_day = False (boundary: strictly < 4.50)",
      m.compute_half_day(4.50) is False)

check("R5-6: 4.51 work_hours -> half_day = False",
      m.compute_half_day(4.51) is False)

check("R5-7: 9.12 work_hours (Sample 1) -> half_day = False",
      m.compute_half_day(9.12) is False)

# Alias check
check("R5-8: is_half_day alias matches compute_half_day",
      m.is_half_day(4.49) is True and m.is_half_day(4.50) is False)


# ==============================================================================
# 6. R6: Presence classification
# ==============================================================================
print("\n-- 6. R6: Presence Classification --")

check("R6-1: PRESENT counts as present", m.is_present("PRESENT") is True)
check("R6-2: WFH counts as present", m.is_present("WFH") is True)
check("R6-3: ON_DUTY counts as present", m.is_present("ON_DUTY") is True)
check("R6-4: ABSENT does NOT count as present", m.is_present("ABSENT") is False)
check("R6-5: LEAVE does NOT count as present", m.is_present("LEAVE") is False)
check("R6-6: Invalid status does NOT count as present", m.is_present("RANDOM") is False)

# Weights
check("R6-7: PRESENT full-day weight == 1.0", m.presence_day_weight("PRESENT", False) == 1.0)
check("R6-8: PRESENT half-day weight == 0.5", m.presence_day_weight("PRESENT", True) == 0.5)
check("R6-9: WFH half-day weight == 0.5", m.presence_day_weight("WFH", True) == 0.5)
check("R6-10: ABSENT weight == 0.0 regardless of half_day flag", m.presence_day_weight("ABSENT", False) == 0.0)


# ==============================================================================
# 7. R7: Working-day calculations
# ==============================================================================
print("\n-- 7. R7: Working-Day Calculations --")

# Weekday check (Mon-Fri)
# 2026-07-06 is Monday, 2026-07-10 is Friday, 2026-07-11 is Saturday, 2026-07-12 is Sunday
check("R7-1: Monday 2026-07-06 is working day", m.is_working_day("2026-07-06") is True)
check("R7-2: Wednesday 2026-07-08 is working day", m.is_working_day("2026-07-08") is True)
check("R7-3: Friday 2026-07-10 is working day", m.is_working_day("2026-07-10") is True)
check("R7-4: Saturday 2026-07-11 is NOT working day", m.is_working_day("2026-07-11") is False)
check("R7-5: Sunday 2026-07-12 is NOT working day", m.is_working_day("2026-07-12") is False)

# Month working days: July 2026 has 31 days (8 weekend days = 23 working days)
check("R7-6: July 2026 total working days == 23",
      m.count_working_days("2026-07-01", "2026-07-31") == 23)

# Single weekend day
check("R7-7: Single weekend day range count == 0",
      m.count_working_days("2026-07-11", "2026-07-11") == 0)

# Single weekday
check("R7-8: Single weekday range count == 1",
      m.count_working_days("2026-07-10", "2026-07-10") == 1)

# Across weekends and month boundary: 2026-01-30 (Fri) to 2026-02-02 (Mon)
# Fri (1), Sat (0), Sun (0), Mon (1) -> total 2 working days
check("R7-9: Across month boundary (Jan 30 Fri to Feb 02 Mon) == 2 working days",
      m.count_working_days("2026-01-30", "2026-02-02") == 2)

# Respect joined_on mid-month: joined 2026-07-15
# July 15 to July 31: 17 days. Weekends: 18-19 (2), 25-26 (2) = 4 weekend days. Working days: 17 - 4 = 13.
check("R7-10: Employee joined 2026-07-15 mid-month -> 13 working days in July 2026",
      m.count_working_days("2026-07-01", "2026-07-31", joined_on="2026-07-15") == 13)

# Joined after period end
check("R7-11: Joined after month end (2026-08-01) -> 0 working days",
      m.count_working_days("2026-07-01", "2026-07-31", joined_on="2026-08-01") == 0)

# Joined before month start
check("R7-12: Joined before month start (2026-01-01) -> 23 working days in July 2026",
      m.count_working_days("2026-07-01", "2026-07-31", joined_on="2026-01-01") == 23)


# ==============================================================================
# 8. R8: Rounding helpers
# ==============================================================================
print("\n-- 8. R8: Rounding Helpers --")

check("R8-1: half_up(9.125, 2) == 9.13", m.half_up(9.125, 2) == 9.13)
check("R8-2: half_up(4.445, 2) == 4.45", m.half_up(4.445, 2) == 4.45)
check("R8-3: half_up(2.5, 0) == 3.0", m.half_up(2.5, 0) == 3.0)
check("R8-4: half_up(1.5, 0) == 2.0", m.half_up(1.5, 0) == 2.0)
check("R8-5: half_up(0.005, 2) == 0.01", m.half_up(0.005, 2) == 0.01)
check("R8-6: half_up(0.004, 2) == 0.0", m.half_up(0.004, 2) == 0.0)

check("R8-7: round_2dp(9.125) == 9.13", m.round_2dp(9.125) == 9.13)
check("R8-8: round_4dp(0.12345) == 0.1235", m.round_4dp(0.12345) == 0.1235)
check("R8-9: round_4dp(0.12344) == 0.1234", m.round_4dp(0.12344) == 0.1234)

# Attendance pct (percentage, 2 decimal places)
# 21.5 / 23 * 100 = 93.47826... -> 93.48
check("R8-10: compute_attendance_pct(21.5, 23) == 93.48",
      m.compute_attendance_pct(21.5, 23) == 93.48)
check("R8-11: compute_attendance_pct(23, 23) == 100.0",
      m.compute_attendance_pct(23, 23) == 100.0)
check("R8-12: compute_attendance_pct(0, 0) == None",
      m.compute_attendance_pct(0, 0) is None)

# Attendance rate (fractional rate, 4 decimal places)
# 21.5 / 23 = 0.9347826... -> 0.9348
check("R8-13: compute_attendance_rate(21.5, 23) == 0.9348",
      m.compute_attendance_rate(21.5, 23) == 0.9348)
check("R8-14: compute_attendance_rate(0, 0) == None",
      m.compute_attendance_rate(0, 0) is None)


# ==============================================================================
# 9. R9: Employment eligibility
# ==============================================================================
print("\n-- 9. R9: Employment Eligibility --")

# Joined mid-month, cutoff is month end
check("R9-1: Joined 2026-07-15 is eligible for July 2026",
      m.is_employee_eligible("2026-07-15", "2026-07") is True)

# Joined on last day of month
check("R9-2: Joined 2026-07-31 is eligible for July 2026",
      m.is_employee_eligible("2026-07-31", "2026-07") is True)

# Joined next month
check("R9-3: Joined 2026-08-01 is NOT eligible for July 2026",
      m.is_employee_eligible("2026-08-01", "2026-07") is False)

# Day cutoff
check("R9-4: Joined 2026-07-15 is NOT eligible on 2026-07-14",
      m.is_employee_eligible("2026-07-15", "2026-07-14") is False)

check("R9-5: Joined 2026-07-15 is eligible on 2026-07-15 (same day)",
      m.is_employee_eligible("2026-07-15", "2026-07-15") is True)

check("R9-6: Joined 2026-07-15 is eligible on 2026-07-16",
      m.is_employee_eligible("2026-07-15", "2026-07-16") is True)


# ==============================================================================
# 10. R10: Pagination helper
# ==============================================================================
print("\n-- 10. R10: Pagination Helper --")

check("R10-1: page 1, size 20 -> skip 0", m.pagination_skip(1, 20) == 0)
check("R10-2: page 2, size 20 -> skip 20", m.pagination_skip(2, 20) == 20)
check("R10-3: page 3, size 50 -> skip 100", m.pagination_skip(3, 50) == 100)
check("R10-4: get_pagination_offset alias matches", m.get_pagination_offset(2, 20) == 20)


# ==============================================================================
# Summary
# ==============================================================================
print("\n" + "=" * 65)
print(f"Results: {len(_passed)} passed, {len(_failed)} failed")
if _failed:
    print(f"FAILED tests: {_failed}")
    sys.exit(1)
else:
    print("ALL Phase 2 pure business-rule helper tests PASSED!")
