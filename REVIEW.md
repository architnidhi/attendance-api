# REVIEW.md

Defects found in the starter `app/main.py` and their fixes:

| # | Where | What is wrong | How you'd notice it | How you fixed it |
|---|---|---|---|---|
| 1 | /health | Never pings MongoDB, always returns 200 | Stop Mongo, still 200 | Added ping; 503 on failure |
| 2 | compute_late_minutes | Python round(), no floor | 09:40:00.900 mis-handled | Truncate; int() floor |
| 3 | compute_late_minutes | Breaks for overnight shifts | 21:55 punch-in gives negative | Anchor to IST date |
| 4 | compute_overtime | Wrong across midnight | Negative OT | shift_end_dt_after adds a day |
| 5 | compute_overtime | Missing 30-min minimum | 10 min OT returned | Return 0 unless >= 30 |
| 6 | compute_work_hours | Banker's rounding | 2.5 -> 2 | Decimal ROUND_HALF_UP |
| 7 | compute_work_hours | Not rounded 2dp half-up | Sample 7 wrong | Same fix |
| 8 | punch_in | No employee existence check | Unknown emp_code -> 500 | 404 |
| 9 | punch_in | fromtimestamp uses local time | Wrong date | Use utcfromtimestamp |
| 10 | punch_in | Check-then-insert race | Two concurrent -> both 201 | Unique index + DuplicateKeyError -> 409 |
| 11 | punch_in | Local date, not IST | Wrong date near midnight | IST + overnight rule |
| 12 | punch_in | Accepts ABSENT/LEAVE | Accepted | Validator restricts |
| 13 | punch_in | No punched_at validation | Seconds accepted | Validator >= 1e11, int only |
| 14 | create_employee | No unique index | Duplicates allowed | Unique index |
| 15 | create_employee | shift_start == shift_end allowed | Accepted | 422 |
| 16 | create_employee | created_at returned as datetime | Not epoch ms | Serializer converts |
| 17 | list_employees | skip = page * page_size | Page 1 skips 20 | (page - 1) * page_size |
| 18 | list_employees | total ignores filter | Full count shown | Count with filter |
| 19 | list_employees | No sort | Undefined order | Sort by emp_code |
| 20 | list_employees | No pagination bounds | page_size 10000 allowed | Query(ge=1, le=100) |
| 21 | list_attendance | Loads all docs | OOM at 100k | DB-side sort/skip/limit |
| 22 | list_attendance | No tie-break sort | Non-deterministic | (date DESC, emp_code ASC) |
| 23 | list_attendance | No date_from > date_to check | Silent | 422 |
| 24 | list_attendance | No status validation | Silent empty | Validate enum |
| 25 | list_attendance | Bad query dict | Filter ignored | Rebuilt q |
| 26 | list_attendance | Returns id field | Contract forbids | Removed |
| 27 | Startup | No indexes | COLLSCAN | Startup indexes |
| 28 | Timezone | Naive/aware mix | TypeError | Naive UTC internally |
| 29 | Missing endpoints | punch-out, PATCH, analytics absent | Contract fails | Implemented all |
| 30 | punch-out race | No atomic guard | Both 200 | Conditional update; loser 409 |

**Examined and decided were NOT defects:** load_dotenv (real env wins), fallback URIs (dev only), no response_model (hand-written contract).