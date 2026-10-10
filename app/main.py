"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import os
import re
from calendar import monthrange
from datetime import datetime, timedelta, timezone, date as date_cls
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List, Dict, Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, Field, field_validator, ConfigDict
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

# --- Mongo client ---
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ===========================================================================
# Startup: create indexes idempotently
# ===========================================================================
@app.on_event("startup")
def ensure_indexes() -> None:
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)],
                              name="dept_empcode")

    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)],
                                    unique=True, name="uniq_emp_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)],
                                    name="date_desc_empcode")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)],
                                    name="emp_date_desc")
    db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING)],
                                    name="date_status")
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", DESCENDING)],
                                    name="date_late")


# ===========================================================================
# Helpers
# ===========================================================================
def epoch_ms_to_dt(ms: int) -> datetime:
    return datetime.utcfromtimestamp(ms / 1000).replace(microsecond=0)


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def now_utc() -> datetime:
    return datetime.utcnow().replace(microsecond=0)


def truncate_seconds(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def to_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def round_half_up(value: float, digits: int) -> float:
    q = Decimal(10) ** -digits
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def parse_hhmm(s: str) -> tuple:
    h, m = s.split(":")
    return int(h), int(m)


def shift_start_dt_on(punch_in_utc: datetime, shift_start: str) -> datetime:
    ist = to_ist(punch_in_utc)
    h, m = parse_hhmm(shift_start)
    ist_start = ist.replace(hour=h, minute=m, second=0, microsecond=0)
    return ist_start.astimezone(timezone.utc).replace(tzinfo=None)


def shift_end_dt_after(punch_in_utc: datetime, shift_start: str, shift_end: str) -> datetime:
    ist = to_ist(punch_in_utc)
    sh, sm = parse_hhmm(shift_start)
    eh, em = parse_hhmm(shift_end)
    ist_end = ist.replace(hour=eh, minute=em, second=0, microsecond=0)
    if (eh, em) <= (sh, sm):
        ist_end = ist_end + timedelta(days=1)
    return ist_end.astimezone(timezone.utc).replace(tzinfo=None)


def compute_late_minutes(punch_in_utc: datetime, shift_start: str) -> int:
    start = shift_start_dt_on(punch_in_utc, shift_start)
    delta_min = (punch_in_utc - start).total_seconds() / 60.0
    if delta_min <= 10.0:
        return 0
    return int(delta_min)


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    seconds = (punch_out_utc - punch_in_utc).total_seconds()
    return round_half_up(seconds / 3600.0, 2)


def compute_overtime(punch_out_utc: datetime, shift_start: str, shift_end: str,
                     punch_in_utc: datetime) -> int:
    end = shift_end_dt_after(punch_in_utc, shift_start, shift_end)
    mins = int((punch_out_utc - end).total_seconds() / 60.0)
    return mins if mins >= 30 else 0


def attendance_date_for(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    ist = to_ist(punch_in_utc)
    sh, sm = parse_hhmm(shift_start)
    eh, em = parse_hhmm(shift_end)
    is_overnight = (eh, em) <= (sh, sm)
    if is_overnight:
        if (ist.hour, ist.minute) < (eh, em):
            ist = ist - timedelta(days=1)
    return ist.date().isoformat()


# ===========================================================================
# Pydantic models
# ===========================================================================
EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def valid_date(cls, v):
        datetime.strptime(v, "%Y-%m-%d")
        return v


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=EPOCH_MIN, le=EPOCH_MAX)
    status: str = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def reject_seconds_or_floats(cls, v):
        if v is None:
            return v
        if isinstance(v, float) or isinstance(v, str) or isinstance(v, bool):
            raise ValueError("punched_at must be an integer epoch in milliseconds")
        if not isinstance(v, int):
            raise ValueError("punched_at must be an integer epoch in milliseconds")
        if v < EPOCH_MIN or v > EPOCH_MAX:
            raise ValueError("punched_at out of range")
        return v

    @field_validator("status")
    @classmethod
    def presence_only(cls, v):
        if v not in ("PRESENT", "WFH", "ON_DUTY"):
            raise ValueError("status must be PRESENT, WFH or ON_DUTY")
        return v


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=EPOCH_MIN, le=EPOCH_MAX)

    @field_validator("punched_at", mode="before")
    @classmethod
    def reject_seconds_or_floats(cls, v):
        if v is None:
            return v
        if not isinstance(v, int) or isinstance(v, bool):
            raise ValueError("punched_at must be an integer epoch in milliseconds")
        if v < EPOCH_MIN or v > EPOCH_MAX:
            raise ValueError("punched_at out of range")
        return v


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def int_only(cls, v):
        if v is None:
            return v
        if not isinstance(v, int) or isinstance(v, bool):
            raise ValueError("must be an integer epoch in milliseconds")
        if v < EPOCH_MIN or v > EPOCH_MAX:
            raise ValueError("out of range")
        return v

    @field_validator("status")
    @classmethod
    def valid_status(cls, v):
        if v is None:
            return v
        if v not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
            raise ValueError("invalid status")
        return v


# ===========================================================================
# Serialization helpers
# ===========================================================================
def serialize_employee(doc: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(doc)
    out.pop("_id", None)
    if isinstance(out.get("created_at"), datetime):
        out["created_at"] = dt_to_epoch_ms(out["created_at"])
    return out


def serialize_history_entry(h: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(h)
    if isinstance(out.get("at"), datetime):
        out["at"] = dt_to_epoch_ms(out["at"])
    changes = out.get("changes") or {}
    new_changes = {}
    for k, v in changes.items():
        nv = dict(v)
        for side in ("from", "to"):
            if isinstance(nv.get(side), datetime):
                nv[side] = dt_to_epoch_ms(nv[side])
        new_changes[k] = nv
    out["changes"] = new_changes
    return out


def serialize_attendance(doc: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(doc)
    out.pop("_id", None)
    if isinstance(out.get("punch_in"), datetime):
        out["punch_in"] = dt_to_epoch_ms(out["punch_in"])
    else:
        out["punch_in"] = None
    if isinstance(out.get("punch_out"), datetime):
        out["punch_out"] = dt_to_epoch_ms(out["punch_out"])
    else:
        out["punch_out"] = None
    out.setdefault("work_hours", None)
    out.setdefault("late_minutes", 0)
    out.setdefault("overtime_minutes", 0)
    out.setdefault("half_day", False)
    hist = out.get("history") or []
    out["history"] = [serialize_history_entry(h) for h in hist]
    return out


# ===========================================================================
# System
# ===========================================================================
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")
    return {"status": "ok"}


# ===========================================================================
# Employees
# ===========================================================================
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start and shift_end must differ")
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None,
                   page: int = Query(1, ge=1),
                   page_size: int = Query(20, ge=1, le=100)):
    q = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = list(db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size))
    return {"items": [serialize_employee(d) for d in items],
            "total": total, "page": page, "page_size": page_size}


# ===========================================================================
# Attendance: punch-in
# ===========================================================================
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")

    if body.punched_at is not None:
        ts = truncate_seconds(epoch_ms_to_dt(body.punched_at))
    else:
        ts = now_utc()

    att_date = attendance_date_for(ts, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(ts, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return serialize_attendance(doc)


# ===========================================================================
# Attendance: punch-out
# ===========================================================================
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")

    if body.punched_at is not None:
        ts = truncate_seconds(epoch_ms_to_dt(body.punched_at))
    else:
        ts = now_utc()

    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}, "punch_out": None},
        sort=[("punch_in", DESCENDING)],
    )
    if not rec:
        any_open = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}
        )
        if any_open and any_open.get("punch_out") is not None:
            raise HTTPException(409, "already punched out")
        raise HTTPException(404, "no punch-in found")

    pin = rec["punch_in"]
    if ts <= pin:
        raise HTTPException(422, "punched_at must be after punch_in")
    if (ts - pin).total_seconds() > 24 * 3600:
        raise HTTPException(422, "punched_at more than 24h after punch_in")

    wh = compute_work_hours(pin, ts)
    ot = compute_overtime(ts, emp["shift_start"], emp["shift_end"], pin)
    hd = wh < 4.50

    res = db.attendance_logs.update_one(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, "work_hours": wh,
                  "overtime_minutes": ot, "half_day": hd}},
    )
    if res.modified_count != 1:
        raise HTTPException(409, "already punched out")

    updated = db.attendance_logs.find_one({"_id": rec["_id"]})
    return serialize_attendance(updated)


# ===========================================================================
# Attendance: list
# ===========================================================================
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and not DATE_RE.match(date_from):
        raise HTTPException(422, "date_from must be YYYY-MM-DD")
    if date_to and not DATE_RE.match(date_to):
        raise HTTPException(422, "date_to must be YYYY-MM-DD")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")
    if status and status not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
        raise HTTPException(422, "invalid status")

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (db.attendance_logs.find(q)
              .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
              .skip(skip).limit(page_size))
    items = [serialize_attendance(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ===========================================================================
# Attendance: regularize
# ===========================================================================
@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    if not DATE_RE.match(date):
        raise HTTPException(422, "date must be YYYY-MM-DD")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "no record for that date")

    new_status = body.status if body.status is not None else rec.get("status")
    new_punch_in = rec.get("punch_in")
    new_punch_out = rec.get("punch_out")

    if body.punch_in is not None:
        new_punch_in = truncate_seconds(epoch_ms_to_dt(body.punch_in))
    if body.punch_out is not None:
        new_punch_out = truncate_seconds(epoch_ms_to_dt(body.punch_out))

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")
        new_punch_in = None
        new_punch_out = None
    else:
        if new_punch_in is None:
            raise HTTPException(422, "presence status requires punch_in")

    if new_punch_in is not None:
        expected_date = attendance_date_for(new_punch_in, emp["shift_start"], emp["shift_end"])
        if expected_date != date:
            raise HTTPException(422, "punch_in does not match the record's attendance date")

    if new_punch_out is not None:
        if new_punch_in is None:
            raise HTTPException(422, "punch_out requires punch_in")
        if new_punch_out <= new_punch_in:
            raise HTTPException(422, "punch_out must be after punch_in")
        if (new_punch_out - new_punch_in).total_seconds() > 24 * 3600:
            raise HTTPException(422, "punch_out more than 24h after punch_in")

    late = 0
    if new_punch_in is not None:
        late = compute_late_minutes(new_punch_in, emp["shift_start"])

    wh = None
    ot = 0
    hd = False
    if new_punch_in is not None and new_punch_out is not None:
        wh = compute_work_hours(new_punch_in, new_punch_out)
        ot = compute_overtime(new_punch_out, emp["shift_start"], emp["shift_end"], new_punch_in)
        hd = wh < 4.50

    def same(a, b):
        return a == b

    changes: Dict[str, Any] = {}
    old_status = rec.get("status")
    old_pin = rec.get("punch_in")
    old_pout = rec.get("punch_out")
    old_wh = rec.get("work_hours")
    old_late = rec.get("late_minutes", 0)
    old_ot = rec.get("overtime_minutes", 0)
    old_hd = rec.get("half_day", False)

    if not same(old_status, new_status):
        changes["status"] = {"from": old_status, "to": new_status}
    if not same(old_pin, new_punch_in):
        changes["punch_in"] = {"from": old_pin, "to": new_punch_in}
    if not same(old_pout, new_punch_out):
        changes["punch_out"] = {"from": old_pout, "to": new_punch_out}
    if not same(old_wh, wh):
        changes["work_hours"] = {"from": old_wh, "to": wh}
    if not same(old_late, late):
        changes["late_minutes"] = {"from": old_late, "to": late}
    if not same(old_ot, ot):
        changes["overtime_minutes"] = {"from": old_ot, "to": ot}
    if not same(old_hd, hd):
        changes["half_day"] = {"from": old_hd, "to": hd}

    if not changes:
        raise HTTPException(422, "no changes")

    history_entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    update_doc = {
        "$set": {
            "status": new_status,
            "punch_in": new_punch_in,
            "punch_out": new_punch_out,
            "work_hours": wh,
            "late_minutes": late,
            "overtime_minutes": ot,
            "half_day": hd,
        },
        "$push": {"history": history_entry},
    }

    old_hist_len = len(rec.get("history") or [])
    filter_q = {"_id": rec["_id"]}
    if old_hist_len == 0:
        filter_q["$or"] = [{"history": {"$exists": False}}, {"history": {"$size": 0}}]
    else:
        filter_q["history"] = {"$size": old_hist_len}

    res = db.attendance_logs.update_one(filter_q, update_doc)
    if res.modified_count != 1:
        raise HTTPException(409, "concurrent modification, retry")

    updated = db.attendance_logs.find_one({"_id": rec["_id"]})
    return serialize_attendance(updated)


# ===========================================================================
# Analytics helpers
# ===========================================================================
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def month_bounds(month: str) -> tuple:
    y, m = map(int, month.split("-"))
    last_day = monthrange(y, m)[1]
    return f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{last_day:02d}"


def working_days_in_month_for(month: str, joined_on: str) -> int:
    y, m = map(int, month.split("-"))
    last_day = monthrange(y, m)[1]
    start = date_cls(y, m, 1)
    join_date = datetime.strptime(joined_on, "%Y-%m-%d").date()
    if join_date > date_cls(y, m, last_day):
        return 0
    if join_date > start:
        start = join_date
    count = 0
    d = start
    end = date_cls(y, m, last_day)
    while d <= end:
        if d.weekday() < 5:
            count += 1
        d += timedelta(days=1)
    return count


def is_weekday(date_str: str) -> bool:
    return datetime.strptime(date_str, "%Y-%m-%d").weekday() < 5


# ===========================================================================
# Analytics: employee monthly
# ===========================================================================
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(...)):
    if not MONTH_RE.match(month):
        raise HTTPException(422, "month must be YYYY-MM")
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")

    from_d, to_d = month_bounds(month)
    records = list(db.attendance_logs.find(
        {"emp_code": emp_code, "date": {"$gte": from_d, "$lte": to_d}}
    ))

    present_days = 0.0
    leave_days = 0
    late_count = 0
    total_late = 0
    total_ot = 0

    for r in records:
        st = r.get("status")
        d = r.get("date")
        if st in ("PRESENT", "WFH", "ON_DUTY") and is_weekday(d):
            present_days += 0.5 if r.get("half_day") else 1.0
        if st == "LEAVE":
            leave_days += 1
        lm = r.get("late_minutes", 0) or 0
        if lm > 0:
            late_count += 1
        total_late += lm
        total_ot += r.get("overtime_minutes", 0) or 0

    working_days = working_days_in_month_for(month, emp["joined_on"])
    attendance_pct = None
    if working_days > 0:
        attendance_pct = round_half_up(present_days / working_days * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


# ===========================================================================
# Analytics: department summary
# ===========================================================================
@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(...), department: Optional[str] = None):
    if not MONTH_RE.match(month):
        raise HTTPException(422, "month must be YYYY-MM")

    from_d, to_d = month_bounds(month)
    end_of_month = to_d

    emp_q: Dict[str, Any] = {"joined_on": {"$lte": end_of_month}}
    if department:
        emp_q["department"] = department

    emps = list(db.employees.find(emp_q, {"emp_code": 1, "department": 1, "joined_on": 1}))

    dept_headcount: Dict[str, int] = {}
    emp_dept: Dict[str, str] = {}
    for e in emps:
        dept = e["department"]
        emp_dept[e["emp_code"]] = dept
        dept_headcount[dept] = dept_headcount.get(dept, 0) + 1

    log_match: Dict[str, Any] = {"date": {"$gte": from_d, "$lte": to_d}}
    if department:
        allowed = list(emp_dept.keys())
        log_match["emp_code"] = {"$in": allowed}

    pipeline = [
        {"$match": log_match},
        {"$group": {
            "_id": "$emp_code",
            "present_records": {"$push": {
                "date": "$date",
                "status": "$status",
                "half_day": {"$ifNull": ["$half_day", False]},
                "work_hours": "$work_hours",
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
            }},
        }},
    ]

    per_emp = {doc["_id"]: doc for doc in db.attendance_logs.aggregate(pipeline)}

    summary: Dict[str, Dict[str, Any]] = {}
    for dept, hc in dept_headcount.items():
        summary[dept] = {
            "department": dept,
            "headcount": hc,
            "present_days": 0.0,
            "work_hours_sum": 0.0,
            "work_hours_count": 0,
            "late_count": 0,
            "total_late_minutes": 0,
            "leave_count": 0,
            "on_duty_count": 0,
        }

    for emp_code, doc in per_emp.items():
        dept = emp_dept.get(emp_code)
        if dept is None:
            continue
        s = summary.get(dept)
        if s is None:
            continue
        for r in doc["present_records"]:
            st = r["status"]
            d = r["date"]
            if st in ("PRESENT", "WFH", "ON_DUTY") and is_weekday(d):
                s["present_days"] += 0.5 if r["half_day"] else 1.0
            if st in ("PRESENT", "WFH", "ON_DUTY") and r["work_hours"] is not None:
                s["work_hours_sum"] += r["work_hours"]
                s["work_hours_count"] += 1
            lm = r["late_minutes"] or 0
            if lm > 0:
                s["late_count"] += 1
            s["total_late_minutes"] += lm
            if st == "LEAVE":
                s["leave_count"] += 1
            if st == "ON_DUTY":
                s["on_duty_count"] += 1

    items: List[Dict[str, Any]] = []
    for dept in sorted(summary.keys()):
        s = summary[dept]
        if s["headcount"] == 0:
            continue
        avg_wh = None
        if s["work_hours_count"] > 0:
            avg_wh = round_half_up(s["work_hours_sum"] / s["work_hours_count"], 2)
        items.append({
            "department": dept,
            "headcount": s["headcount"],
            "present_days": s["present_days"],
            "avg_work_hours": avg_wh,
            "late_count": s["late_count"],
            "total_late_minutes": s["total_late_minutes"],
            "leave_count": s["leave_count"],
            "on_duty_count": s["on_duty_count"],
        })

    return {"month": month, "items": items}


# ===========================================================================
# Analytics: leaderboard late
# ===========================================================================
@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(...),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    if not MONTH_RE.match(month):
        raise HTTPException(422, "month must be YYYY-MM")

    from_d, to_d = month_bounds(month)

    log_match: Dict[str, Any] = {"date": {"$gte": from_d, "$lte": to_d}}
    if department:
        emp_codes = [e["emp_code"] for e in db.employees.find(
            {"department": department}, {"emp_code": 1}
        )]
        log_match["emp_code"] = {"$in": emp_codes}

    pipeline = [
        {"$match": log_match},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {
                "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
            },
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$match": {"emp": {"$ne": []}}},
        {"$unwind": "$emp"},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))

    items: List[Dict[str, Any]] = []
    prev_total = None
    rank = 0
    idx = 0
    for r in rows:
        idx += 1
        tot = r["total_late_minutes"]
        if prev_total is None or tot != prev_total:
            rank = idx
            prev_total = tot
        if rank > limit:
            break
        emp = r["emp"]
        items.append({
            "rank": rank,
            "emp_code": r["_id"],
            "name": emp.get("name", ""),
            "department": emp.get("department", ""),
            "total_late_minutes": tot,
            "late_count": r["late_count"],
        })

    return {"month": month, "items": items}


# ===========================================================================
# Analytics: department trend
# ===========================================================================
@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str,
                     from_: str = Query(..., alias="from"),
                     to: str = Query(...)):
    if not DATE_RE.match(from_) or not DATE_RE.match(to):
        raise HTTPException(422, "from/to must be YYYY-MM-DD")
    if to < from_:
        raise HTTPException(422, "to must be >= from")

    d_from = datetime.strptime(from_, "%Y-%m-%d").date()
    d_to = datetime.strptime(to, "%Y-%m-%d").date()
    if (d_to - d_from).days > 92:
        raise HTTPException(422, "range exceeds 92 days")

    emps = list(db.employees.find({"department": department},
                                  {"emp_code": 1, "joined_on": 1}))
    if not emps:
        raise HTTPException(404, "unknown department")

    emp_codes = [e["emp_code"] for e in emps]
    emp_join = {e["emp_code"]: e["joined_on"] for e in emps}

    logs = list(db.attendance_logs.find({
        "emp_code": {"$in": emp_codes},
        "date": {"$gte": from_, "$lte": to},
    }))

    by_date: Dict[str, List[Dict[str, Any]]] = {}
    for lg in logs:
        by_date.setdefault(lg["date"], []).append(lg)

    days: List[Dict[str, Any]] = []
    cur = d_from
    while cur <= d_to:
        ds = cur.isoformat()
        is_wd = cur.weekday() < 5
        headcount = sum(1 for ec, jd in emp_join.items() if jd <= ds)
        day_logs = by_date.get(ds, [])
        present_count = 0.0
        late_count = 0
        for lg in day_logs:
            st = lg.get("status")
            if st in ("PRESENT", "WFH", "ON_DUTY"):
                present_count += 0.5 if lg.get("half_day") else 1.0
            if (lg.get("late_minutes") or 0) > 0:
                late_count += 1

        if is_wd and headcount > 0:
            rate = round_half_up(present_count / headcount, 4)
        else:
            rate = None

        days.append({
            "date": ds,
            "is_working_day": is_wd,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": rate,
        })
        cur += timedelta(days=1)

    for i, row in enumerate(days):
        window = days[max(0, i - 6): i + 1]
        vals = [w["attendance_rate"] for w in window if w["attendance_rate"] is not None]
        if vals:
            row["moving_avg_7d"] = round_half_up(sum(vals) / len(vals), 4)
        else:
            row["moving_avg_7d"] = None

    return {"department": department, "items": days}


# ===========================================================================
# Admin: explain
# ===========================================================================
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    if endpoint == "attendance_list":
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        explain = db.attendance_logs.find(q).sort(
            [("date", DESCENDING), ("emp_code", ASCENDING)]
        ).skip(skip).limit(page_size).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month required")
        from_d, to_d = month_bounds(month)
        explain = db.attendance_logs.find(
            {"emp_code": emp_code, "date": {"$gte": from_d, "$lte": to_d}}
        ).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month required")
        from_d, to_d = month_bounds(month)
        q = {"date": {"$gte": from_d, "$lte": to_d}}
        explain = db.attendance_logs.find(q).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month required")
        from_d, to_d = month_bounds(month)
        q = {"date": {"$gte": from_d, "$lte": to_d}}
        explain = db.attendance_logs.find(q).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(422, "department, from, to required")
        emps = list(db.employees.find({"department": department}, {"emp_code": 1}))
        emp_codes = [e["emp_code"] for e in emps]
        q = {"emp_code": {"$in": emp_codes}, "date": {"$gte": from_, "$lte": to}}
        explain = db.attendance_logs.find(q).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    raise HTTPException(422, "unknown endpoint")