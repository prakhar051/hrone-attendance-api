# DECISIONS.md

1. **Indexes.**
We created six indexes: `IX1_emp_code_unique` (uniqueness and employee lookups), `IX2_department` (department filtering and joins), and `IX3_joined_on` (headcount eligibility) on `employees`. On `attendance_logs`, `IX4_emp_code_date_unique` enforces the one-record-per-day rule and concurrency safety, `IX5_date_desc_emp_code_asc` serves mixed-direction listing sorts without in-memory sorting, and `IX6_emp_code_date_status` powers monthly employee analytics. We rejected an index on `status` alone because its low cardinality makes index scans less efficient than compound indexes.

2. **Punch-in race.**
Both concurrent requests execute an insert into `attendance_logs`. MongoDB evaluates the unique compound index `IX4_emp_code_date_unique`. Exactly one write succeeds with HTTP 201; the second triggers a `DuplicateKeyError` (E11000). Our endpoint catches this exception and returns HTTP 409 Conflict with the documented error message.

3. **Ties.**
We compute standard competition ranking via `$rank` in `$setWindowFields`. Filtering `rank <= limit` occurs after rank assignment. If two employees tie at rank 3 with `limit=3`, both satisfy `rank <= 3`, so both are returned (e.g. 4 rows, skipping rank 4). Tied rows sort by `emp_code` ascending.

4. **Headcount.**
The pipeline starts on `employees` (matching `joined_on <= month_end`) and groups by department to establish true headcount before executing `$lookup` into `attendance_logs`. Employees with zero logs are preserved with `headcount >= 1`, contributing 0 present days and `null` average work hours.

5. **One thing you would change for 100x data.**
Precompute daily department rollups incrementally via change streams or scheduled batch jobs instead of running multi-stage `$lookup` and `$densify` on raw log collections at query time.
