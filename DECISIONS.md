# DECISIONS.md

**1. Indexes.** Five indexes on attendance_logs: uniq_emp_date on (emp_code, date) unique — enforces one record per day and powers employee_monthly and duplicate punch-in detection. date_desc_empcode on (date DESC, emp_code ASC) — default list sort. emp_date_desc on (emp_code, date DESC) — date-range by employee. date_status on (date, status) — status filters. date_late on (date, late_minutes DESC) — leaderboard month window. On employees: uniq_emp_code (unique) and dept_empcode. Rejected a lone late_minutes index because analytics queries also filter by date.

**2. Punch-in race.** The unique index on (emp_code, date) means two concurrent inserts collide at the storage layer. MongoDB lets exactly one succeed; the other raises DuplicateKeyError, which I catch and translate to 409. No check-then-insert, so no race window.

**3. Ties at the cutoff.** I compute standard competition ranking in Python on rows already sorted by total_late_minutes desc, emp_code asc. If two employees tie at rank limit, both are returned, so the response can exceed limit rows. Rows are stable-ordered by emp_code.

**4. Headcount with zero logs.** I fetch the employee set with joined_on <= end_of_month; that count is the headcount. Logs are aggregated separately and only fill the metric columns. Employees with no logs still count.

**5. At 100x data.** Push remaining Python-side aggregations (leaderboard, trend) fully into MongoDB $setWindowFields / $densify, cache monthly headcounts, add a date-only compound index for the trend scan, and shard on emp_code.