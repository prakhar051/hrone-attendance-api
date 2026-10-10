"""
Phase 3 verification suite — Employee endpoints only.

Imports the REAL FastAPI app and real models/serializers from app.main.
Uses TestClient to test HTTP requests, status codes, query validation,
headers, and serialization.

Database isolation:
Uses an in-memory collection mock with unique-index constraint simulation
to isolate endpoint behavior without requiring an active live MongoDB connection.

Run:
    python -X utf8 tests/test_phase3_employees.py
"""
import concurrent.futures
import os
import sys
import threading
from datetime import datetime, timezone

# ── Set dummy DB so app.main imports safely ──────────────────────────────────
os.environ["MONGO_URI"] = "mongodb://127.0.0.1:27099"
os.environ["MONGO_DB"]  = "phase3_test_db"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app.main as m
from fastapi.testclient import TestClient
from pymongo.errors import DuplicateKeyError

# ── In-Memory Collection Mock with Unique Constraint Simulation ──────────────
class MockCursor:
    def __init__(self, docs):
        self._docs = list(docs)
        self._sort_key = None
        self._sort_dir = 1
        self._skip = 0
        self._limit = None

    def sort(self, key, direction=1):
        self._sort_key = key
        self._sort_dir = direction
        return self

    def skip(self, n):
        self._skip = n
        return self

    def limit(self, n):
        self._limit = n
        return self

    def __iter__(self):
        docs = list(self._docs)
        if self._sort_key:
            docs.sort(
                key=lambda d: d.get(self._sort_key, ""),
                reverse=(self._sort_dir == -1),
            )
        if self._skip:
            docs = docs[self._skip:]
        if self._limit is not None:
            docs = docs[:self._limit]
        return iter(docs)


class InMemoryEmployeesCollection:
    """Mock MongoDB employees collection simulating IX1_emp_code_unique."""

    def __init__(self):
        self.docs: list[dict] = []
        self._lock = threading.Lock()

    def insert_one(self, doc: dict):
        with self._lock:
            emp_code = doc.get("emp_code")
            for existing in self.docs:
                if existing.get("emp_code") == emp_code:
                    raise DuplicateKeyError(
                        f"E11000 duplicate key error collection: employees index: IX1_emp_code_unique dup key: {{ emp_code: '{emp_code}' }}"
                    )
            # MongoDB mutates doc by assigning _id
            doc["_id"] = f"oid_{len(self.docs) + 1}"
            self.docs.append(dict(doc))

    def count_documents(self, q: dict) -> int:
        return len(self._filter(q))

    def find(self, q: dict):
        return MockCursor(self._filter(q))

    def _filter(self, q: dict) -> list[dict]:
        res = []
        for d in self.docs:
            match = True
            for k, v in q.items():
                if d.get(k) != v:
                    match = False
                    break
            if match:
                res.append(dict(d))
        return res


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

# Initialize TestClient
client = TestClient(m.app, raise_server_exceptions=False)
mock_coll = InMemoryEmployeesCollection()
# Swap db.employees with mock collection
m.db.employees = mock_coll


# ==============================================================================
# 1. POST /employees — Successful Creation
# ==============================================================================
print("\n-- 1. POST /employees: Successful Creation --")

valid_emp = {
    "emp_code":    "EMP0001",
    "name":        "Asha Rao",
    "email":       "asha@example.com",
    "department":  "Engineering",
    "shift_start": "09:30",
    "shift_end":   "18:30",
    "joined_on":   "2026-01-05",
}

res = client.post("/employees", json=valid_emp)
check("P3-1a: POST /employees returns HTTP 201", res.status_code == 201)

data = res.json()
check("P3-1b: Response contains correct emp_code", data.get("emp_code") == "EMP0001")
check("P3-1c: Response contains correct name", data.get("name") == "Asha Rao")
check("P3-1d: Response contains correct email", data.get("email") == "asha@example.com")
check("P3-1e: Response contains correct department", data.get("department") == "Engineering")
check("P3-1f: Response contains correct shift_start", data.get("shift_start") == "09:30")
check("P3-1g: Response contains correct shift_end", data.get("shift_end") == "18:30")
check("P3-1h: Response contains correct joined_on", data.get("joined_on") == "2026-01-05")
check("P3-1i: created_at is integer epoch ms",
      isinstance(data.get("created_at"), int) and data.get("created_at") >= 100_000_000_000)
check("P3-1j: _id is absent from response body", "_id" not in data and "id" not in data)

# Verify database storage
stored_doc = mock_coll.docs[0]
check("P3-1k: Stored document has created_at as UTC datetime",
      isinstance(stored_doc["created_at"], datetime) and stored_doc["created_at"].tzinfo == timezone.utc)


# ==============================================================================
# 2. POST /employees — Request Validation Failures (422)
# ==============================================================================
print("\n-- 2. POST /employees: Request Validation Failures (422) --")

# Invalid emp_code format (3 digits)
bad_emp_1 = dict(valid_emp, emp_code="EMP123")
res = client.post("/employees", json=bad_emp_1)
check("P3-2a: 3-digit emp_code returns 422", res.status_code == 422)

# Lowercase emp_code
bad_emp_2 = dict(valid_emp, emp_code="emp0002")
res = client.post("/employees", json=bad_emp_2)
check("P3-2b: Lowercase emp_code returns 422", res.status_code == 422)

# Empty name
bad_emp_3 = dict(valid_emp, emp_code="EMP0002", name="")
res = client.post("/employees", json=bad_emp_3)
check("P3-2c: Empty name returns 422", res.status_code == 422)

# Invalid email
bad_emp_4 = dict(valid_emp, emp_code="EMP0002", email="not-an-email")
res = client.post("/employees", json=bad_emp_4)
check("P3-2d: Malformed email returns 422", res.status_code == 422)

# Missing required department
bad_emp_5 = dict(valid_emp, emp_code="EMP0002")
del bad_emp_5["department"]
res = client.post("/employees", json=bad_emp_5)
check("P3-2e: Missing department returns 422", res.status_code == 422)

# Equal shift_start and shift_end (D4)
bad_emp_6 = dict(valid_emp, emp_code="EMP0002", shift_start="09:30", shift_end="09:30")
res = client.post("/employees", json=bad_emp_6)
check("P3-2f: shift_start == shift_end returns 422", res.status_code == 422)

# Invalid joined_on date (Feb 30)
bad_emp_7 = dict(valid_emp, emp_code="EMP0002", joined_on="2026-02-30")
res = client.post("/employees", json=bad_emp_7)
check("P3-2g: Non-existent date 2026-02-30 returns 422", res.status_code == 422)


# ==============================================================================
# 3. POST /employees — Duplicate Employee Code Handling (409)
# ==============================================================================
print("\n-- 3. POST /employees: Duplicate Employee Code Handling (409) --")

# Re-attempt inserting EMP0001 (already created in section 1)
dup_res = client.post("/employees", json=valid_emp)
check("P3-3a: Duplicate emp_code returns HTTP 409", dup_res.status_code == 409)
check("P3-3b: Detail message matches contract",
      dup_res.json().get("detail") == "emp_code already exists")


# ==============================================================================
# 4. POST /employees — Concurrent Duplicate Creation Behavior
# ==============================================================================
print("\n-- 4. POST /employees: Concurrent Duplicate Creation --")

concurrent_emp = dict(valid_emp, emp_code="EMP9999", email="concurrent@example.com")
responses = []

def send_create():
    return client.post("/employees", json=concurrent_emp)

with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
    futures = [executor.submit(send_create) for _ in range(10)]
    for f in concurrent.futures.as_completed(futures):
        responses.append(f.result())

status_counts = [r.status_code for r in responses]
success_count = status_counts.count(201)
conflict_count = status_counts.count(409)

check("P3-4a: Exactly one concurrent insert returns HTTP 201", success_count == 1)
check("P3-4b: Remaining 9 concurrent inserts return HTTP 409", conflict_count == 9)
check("P3-4c: Total mock documents for EMP9999 is exactly 1",
      len([d for d in mock_coll.docs if d["emp_code"] == "EMP9999"]) == 1)


# ==============================================================================
# 5. GET /employees — Listing, Filtering, Sorting, Pagination
# ==============================================================================
print("\n-- 5. GET /employees: Listing, Filtering, Sorting, Pagination --")

# Reset collection and seed with 25 employees across 2 departments
mock_coll.docs.clear()
departments = ["Engineering", "Product"]
for i in range(1, 26):
    code = f"EMP{i:04d}"
    dept = departments[0] if i <= 15 else departments[1]  # 15 Eng, 10 Prod
    doc = {
        "emp_code":    code,
        "name":        f"Employee {i}",
        "email":       f"emp{i}@example.com",
        "department":  dept,
        "shift_start": "09:30",
        "shift_end":   "18:30",
        "joined_on":   "2026-01-05",
        "created_at":  datetime(2026, 1, 5, 4, 30, 0, tzinfo=timezone.utc),
        "_id":         f"oid_{i}",
    }
    mock_coll.docs.append(doc)

# A: Listing with no filters (default page 1, size 20)
res = client.get("/employees")
check("P3-5a: GET /employees returns HTTP 200", res.status_code == 200)

data = res.json()
check("P3-5b: Default total is 25", data.get("total") == 25)
check("P3-5c: Default page is 1", data.get("page") == 1)
check("P3-5d: Default page_size is 20", data.get("page_size") == 20)
check("P3-5e: Items length is 20 on page 1", len(data.get("items", [])) == 20)

# Sort check: items must be sorted by emp_code ascending
item_codes = [it["emp_code"] for it in data["items"]]
check("P3-5f: Items sorted by emp_code ascending",
      item_codes == sorted(item_codes) and item_codes[0] == "EMP0001" and item_codes[-1] == "EMP0020")

# Verify no _id in any listed item
has_id = any("_id" in it or "id" in it for it in data["items"])
check("P3-5g: _id is absent from all listed items", not has_id)

# Verify created_at is int epoch ms in all listed items
all_epoch_ms = all(isinstance(it["created_at"], int) for it in data["items"])
check("P3-5h: created_at is int epoch ms in all listed items", all_epoch_ms)


# B: Pagination boundaries (page 1, page 2, page 3)
res_p1 = client.get("/employees?page=1&page_size=10").json()
res_p2 = client.get("/employees?page=2&page_size=10").json()
res_p3 = client.get("/employees?page=3&page_size=10").json()
res_p4 = client.get("/employees?page=4&page_size=10").json()

codes_p1 = [it["emp_code"] for it in res_p1["items"]]
codes_p2 = [it["emp_code"] for it in res_p2["items"]]
codes_p3 = [it["emp_code"] for it in res_p3["items"]]

check("P3-5i: Page 1 size 10 returns EMP0001..EMP0010",
      len(codes_p1) == 10 and codes_p1[0] == "EMP0001" and codes_p1[-1] == "EMP0010")
check("P3-5j: Page 2 size 10 returns EMP0011..EMP0020",
      len(codes_p2) == 10 and codes_p2[0] == "EMP0011" and codes_p2[-1] == "EMP0020")
check("P3-5k: Page 3 size 10 returns EMP0021..EMP0025 (remaining 5)",
      len(codes_p3) == 5 and codes_p3[0] == "EMP0021" and codes_p3[-1] == "EMP0025")
check("P3-5l: Page 4 beyond total returns empty items list",
      len(res_p4["items"]) == 0 and res_p4["total"] == 25)


# C: Department filter and filtered total
res_eng = client.get("/employees?department=Engineering").json()
check("P3-5m: department=Engineering total is 15 (filtered total, not all 25)",
      res_eng.get("total") == 15)
check("P3-5n: All returned items belong to Engineering",
      all(it["department"] == "Engineering" for it in res_eng["items"]))

res_prod = client.get("/employees?department=Product").json()
check("P3-5o: department=Product total is 10", res_prod.get("total") == 10)
check("P3-5p: All returned items belong to Product",
      all(it["department"] == "Product" for it in res_prod["items"]))

# Non-existent department filter
res_none = client.get("/employees?department=Finance").json()
check("P3-5q: Non-existent department returns total 0 and empty items",
      res_none.get("total") == 0 and len(res_none.get("items", [])) == 0)


# D: Maximum page size and invalid pagination inputs (422)
# Max page size = 100
res_100 = client.get("/employees?page_size=100")
check("P3-5r: page_size=100 is accepted (HTTP 200)", res_100.status_code == 200)

# page_size > 100
res_101 = client.get("/employees?page_size=101")
check("P3-5s: page_size=101 returns 422 (maximum 100)", res_101.status_code == 422)

# page_size < 1
res_sz0 = client.get("/employees?page_size=0")
check("P3-5t: page_size=0 returns 422 (minimum 1)", res_sz0.status_code == 422)

# page < 1
res_pg0 = client.get("/employees?page=0")
check("P3-5u: page=0 returns 422 (minimum 1)", res_pg0.status_code == 422)

# Non-integer page
res_str = client.get("/employees?page=first")
check("P3-5v: page='first' returns 422", res_str.status_code == 422)


# ==============================================================================
# Summary
# ==============================================================================
print("\n" + "=" * 65)
print(f"Results: {len(_passed)} passed, {len(_failed)} failed")
if _failed:
    print(f"FAILED tests: {_failed}")
    sys.exit(1)
else:
    print("ALL Phase 3 employee endpoint tests PASSED!")
