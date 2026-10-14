from __future__ import annotations



import calendar

import os

import re

from datetime import date, datetime, time, timedelta, timezone

from decimal import Decimal, ROUND_HALF_UP

from typing import Any, Optional

from zoneinfo import ZoneInfo



from dotenv import load_dotenv

from fastapi import FastAPI, HTTPException, Query, Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument

from pymongo.errors import DuplicateKeyError, PyMongoError

from bson import json_util

from bson.json_util import CANONICAL_JSON_OPTIONS



load_dotenv()

IST = ZoneInfo("Asia/Kolkata")

UTC = timezone.utc

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")

MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000, tz_aware=True)

db = client[MONGO_DB]

employees = db["employees"]

logs = db["attendance_logs"]



app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}

STATUSES = PRESENCE | {"ABSENT", "LEAVE"}

EPOCH_MIN = 100_000_000_000

EPOCH_MAX = 4_102_444_800_000



def now_utc() -> datetime:

    return datetime.now(UTC).replace(microsecond=0)



def epoch_ms(dt: Optional[datetime]) -> Optional[int]:

    if dt is None:

        return None

    if dt.tzinfo is None:

        dt = dt.replace(tzinfo=UTC)

    return int(dt.timestamp() * 1000)



def parse_ms(value: Optional[int], *, optional: bool = False) -> Optional[datetime]:

    if value is None and optional:

        return None

    if isinstance(value, bool) or not isinstance(value, int) or not EPOCH_MIN <= value <= EPOCH_MAX:

        raise HTTPException(422, "Timestamp must be an integer epoch-millisecond value in range")

    return datetime.fromtimestamp(value // 1000, UTC)



def as_utc(dt: datetime) -> datetime:

    if dt.tzinfo is None:

        return dt.replace(tzinfo=UTC)

    return dt.astimezone(UTC)



def ist_dt(dt: datetime) -> datetime:

    return as_utc(dt).astimezone(IST)



def round_half_up(value: float | Decimal, places: int = 2) -> float:

    quantum = Decimal("1").scaleb(-places)

    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))



def month_bounds(month: str) -> tuple[str, str, date, date]:

    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):

        raise HTTPException(422, "month must be YYYY-MM")

    year, mon = map(int, month.split("-"))

    start = date(year, mon, 1)

    end = date(year, mon, calendar.monthrange(year, mon)[1])

    return start.isoformat(), end.isoformat(), start, end



def working_days(start: date, end: date) -> int:

    if end < start:

        return 0

    count = 0

    cur = start

    while cur <= end:

        if cur.weekday() < 5:

            count += 1

        cur += timedelta(days=1)

    return count



def time_minutes(s: str) -> int:

    h, m = map(int, s.split(":"))

    return h * 60 + m



def attendance_date(punch: datetime, emp: dict) -> str:

    local = ist_dt(punch)

    day = local.date()

    start = time_minutes(emp["shift_start"])

    end = time_minutes(emp["shift_end"])

    if end <= start and time_minutes(local.strftime("%H:%M")) < end:

        day -= timedelta(days=1)

    return day.isoformat()



def shift_end_datetime(emp: dict, attendance_day: str) -> datetime:

    d = date.fromisoformat(attendance_day)

    start = time_minutes(emp["shift_start"])

    end = time_minutes(emp["shift_end"])

    if end <= start:

        d += timedelta(days=1)

    hh, mm = divmod(end, 60)

    return datetime.combine(d, time(hh, mm), IST).astimezone(UTC)



def shift_start_datetime(emp: dict, attendance_day: str) -> datetime:

    d = date.fromisoformat(attendance_day)

    hh, mm = map(int, emp["shift_start"].split(":"))

    return datetime.combine(d, time(hh, mm), IST).astimezone(UTC)



def derive_late(punch_in: Optional[datetime], emp: dict, day: str) -> int:

    if punch_in is None:

        return 0

    delta = int((as_utc(punch_in) - shift_start_datetime(emp, day)).total_seconds())

    return delta // 60 if delta > 600 else 0



def derive_values(punch_in: Optional[datetime], punch_out: Optional[datetime], emp: dict, day: str) -> dict:

    if punch_in is None:

        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}

    late = derive_late(punch_in, emp, day)

    if punch_out is None:

        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}

    seconds = int((as_utc(punch_out) - as_utc(punch_in)).total_seconds())

    hours = round_half_up(Decimal(seconds) / Decimal(3600), 2)

    overtime_seconds = int((as_utc(punch_out) - shift_end_datetime(emp, day)).total_seconds())

    overtime = overtime_seconds // 60 if overtime_seconds >= 1800 else 0

    return {"late_minutes": late, "work_hours": hours, "overtime_minutes": overtime, "half_day": hours < 4.50}



def public_employee(e: dict) -> dict:

    return {k: e[k] for k in ("emp_code","name","email","department","shift_start","shift_end","joined_on")} | {"created_at": epoch_ms(e["created_at"])}



def public_log(r: dict) -> dict:

    return {

        "emp_code": r["emp_code"], "date": r["date"], "status": r["status"],

        "punch_in": epoch_ms(r.get("punch_in")), "punch_out": epoch_ms(r.get("punch_out")),

        "work_hours": r.get("work_hours"), "late_minutes": int(r.get("late_minutes", 0)),

        "overtime_minutes": int(r.get("overtime_minutes", 0)), "half_day": bool(r.get("half_day", False)),

        "history": [{

            "at": epoch_ms(h["at"]), "by": h["by"], "reason": h["reason"],

            "changes": {k: {"from": epoch_ms(v["from"]) if k in ("punch_in","punch_out") else v["from"],

                            "to": epoch_ms(v["to"]) if k in ("punch_in","punch_out") else v["to"]}

                        for k,v in h.get("changes", {}).items()}

        } for h in r.get("history", [])]

    }



def clean_log(r: dict) -> dict:

    r.setdefault("punch_in", None); r.setdefault("punch_out", None)

    r.setdefault("work_hours", None); r.setdefault("late_minutes", 0)

    r.setdefault("overtime_minutes", 0); r.setdefault("half_day", False); r.setdefault("history", [])

    return r



class EmployeeCreate(BaseModel):

    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")

    name: str = Field(min_length=1, max_length=100)

    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

    department: str = Field(min_length=1, max_length=50)

    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")

    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")

    joined_on: date

    @model_validator(mode="after")

    def shift_differs(self):

        if self.shift_start == self.shift_end:

            raise ValueError("shift_start must differ from shift_end")

        return self



class PunchInRequest(BaseModel):

    model_config = ConfigDict(extra="ignore")

    emp_code: str

    punched_at: Optional[int] = None

    status: str = "PRESENT"

    @field_validator("punched_at")

    @classmethod

    def validate_time(cls, v):

        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not EPOCH_MIN <= v <= EPOCH_MAX):

            raise ValueError("punched_at must be epoch milliseconds")

        return v

    @field_validator("status")

    @classmethod

    def validate_status(cls, v):

        if v not in PRESENCE: raise ValueError("status must be PRESENT, WFH, or ON_DUTY")

        return v



class PunchOutRequest(BaseModel):

    model_config = ConfigDict(extra="ignore")

    emp_code: str

    punched_at: Optional[int] = None

    @field_validator("punched_at")

    @classmethod

    def validate_time(cls, v):

        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not EPOCH_MIN <= v <= EPOCH_MAX):

            raise ValueError("punched_at must be epoch milliseconds")

        return v



class RegularizeRequest(BaseModel):

    model_config = ConfigDict(extra="ignore")

    status: Optional[str] = None

    punch_in: Optional[int] = None

    punch_out: Optional[int] = None

    reason: str = Field(min_length=5, max_length=200)

    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")

    @classmethod

    def valid_status(cls, v):

        if v is not None and v not in STATUSES: raise ValueError("Invalid status")

        return v

    @field_validator("punch_in", "punch_out")

    @classmethod

    def valid_timestamp(cls, v):

        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not EPOCH_MIN <= v <= EPOCH_MAX):

            raise ValueError("Timestamp must be epoch milliseconds")

        return v



@app.on_event("startup")

def create_indexes():

    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")

    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined")

    employees.create_index(

    [("joined_on", ASCENDING)],

    name="employee_joined_on")

    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")

    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_emp")

    logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="attendance_employee_punchin")

    logs.create_index([("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)], name="attendance_date_status_emp")



@app.get("/health")

def health():

    try:

        client.admin.command("ping")

        return {"status": "ok"}

    except PyMongoError:

        raise HTTPException(503, "MongoDB unavailable")



@app.post("/employees", status_code=201)

def create_employee(body: EmployeeCreate):

    doc = body.model_dump()

    doc["joined_on"] = body.joined_on.isoformat()

    doc["created_at"] = now_utc()

    try:

        employees.insert_one(doc)

    except DuplicateKeyError:

        raise HTTPException(409, "emp_code already exists")

    return public_employee(doc)



@app.get("/employees")

def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):

    filt = {"department": department} if department is not None else {}

    total = employees.count_documents(filt)

    cur = employees.find(filt, {"_id": 0}).sort("emp_code", ASCENDING).skip((page-1)*page_size).limit(page_size)

    return {"items": [public_employee(e) for e in cur], "total": total, "page": page, "page_size": page_size}



@app.post("/attendance/punch-in", status_code=201)

def punch_in(body: PunchInRequest):

    emp = employees.find_one({"emp_code": body.emp_code}, {"_id": 0})

    if not emp: raise HTTPException(404, "Unknown employee")

    instant = parse_ms(body.punched_at) if body.punched_at is not None else now_utc()

    day = attendance_date(instant, emp)

    instant = instant.replace(microsecond=0)

    derived = derive_values(instant, None, emp, day)

    doc = {"emp_code": body.emp_code, "date": day, "status": body.status, "punch_in": instant,

           "punch_out": None, "work_hours": None, "late_minutes": derived["late_minutes"],

           "overtime_minutes": 0, "half_day": False, "history": []}

    try:

        logs.insert_one(doc)

    except DuplicateKeyError:

        raise HTTPException(409, "Already punched in for this attendance date")

    return public_log(doc)



@app.post("/attendance/punch-out")

def punch_out(body: PunchOutRequest):

    emp = employees.find_one({"emp_code": body.emp_code}, {"_id": 0})

    if not emp: raise HTTPException(404, "Unknown employee")

    out = parse_ms(body.punched_at) if body.punched_at is not None else now_utc()

    out = out.replace(microsecond=0)

    record = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": out}},

                           sort=[("punch_in", DESCENDING)])

    if not record: raise HTTPException(404, "No punch-in found")

    if record.get("punch_out") is not None: raise HTTPException(409, "Record already punched out")

    seconds = (out - as_utc(record["punch_in"])).total_seconds()

    if seconds <= 0 or seconds > 24*3600: raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")

    derived = derive_values(record["punch_in"], out, emp, record["date"])

    updated = logs.find_one_and_update(

        {"_id": record["_id"], "punch_out": None},

        {"$set": {"punch_out": out, **derived}},

        return_document=ReturnDocument.AFTER)

    if updated is None: raise HTTPException(409, "Record already punched out")

    return public_log(clean_log(updated))



@app.get("/attendance")

def list_attendance(emp_code: Optional[str] = None, date_from: Optional[date] = None, date_to: Optional[date] = None,

                    status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):

    if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must be <= date_to")

    if status is not None and status not in STATUSES: raise HTTPException(422, "Invalid status")

    filt: dict[str, Any] = {}

    if emp_code is not None: filt["emp_code"] = emp_code

    if date_from or date_to:

        filt["date"] = {}

        if date_from: filt["date"]["$gte"] = date_from.isoformat()

        if date_to: filt["date"]["$lte"] = date_to.isoformat()

    if status: filt["status"] = status

    total = logs.count_documents(filt)

    cur = logs.find(filt, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page-1)*page_size).limit(page_size)

    return {"items": [public_log(clean_log(r)) for r in cur], "total": total, "page": page, "page_size": page_size}



@app.patch("/attendance/{emp_code}/{date}")

def regularize(emp_code: str, date: date, body: RegularizeRequest):

    day = date.isoformat()

    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0})

    if not emp: raise HTTPException(404, "Unknown employee")

    old = logs.find_one({"emp_code": emp_code, "date": day})

    if not old: raise HTTPException(404, "Attendance record not found")

    old = clean_log(old)

    status = body.status if body.status is not None else old["status"]

    if status in {"ABSENT", "LEAVE"}:

        if body.punch_in is not None or body.punch_out is not None:

            raise HTTPException(422, "ABSENT/LEAVE cannot be supplied with punch times")

        pin = pout = None

    else:

        pin = parse_ms(body.punch_in) if body.punch_in is not None else old.get("punch_in")

        pout = parse_ms(body.punch_out) if body.punch_out is not None else old.get("punch_out")

        if pin is None: raise HTTPException(422, "Presence status requires punch_in")

        if attendance_date(pin, emp) != day: raise HTTPException(422, "punch_in must belong to attendance date")

        if pout is not None:

            sec = (as_utc(pout)-as_utc(pin)).total_seconds()

            if sec <= 0 or sec > 86400: raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    derived = derive_values(pin, pout, emp, day) if status in PRESENCE else {"late_minutes":0,"work_hours":None,"overtime_minutes":0,"half_day":False}

    new_values = {"status":status,"punch_in":pin,"punch_out":pout,**derived}

    changes = {}

    for key, new in new_values.items():

        prev = old.get(key)

        if key in ("punch_in","punch_out"):

            prev_norm = as_utc(prev) if prev else None

            new_norm = as_utc(new) if new else None

            if prev_norm != new_norm:

                changes[key] = {"from": prev, "to": new}

        elif prev != new:

            changes[key] = {"from": prev, "to": new}

    if not changes: raise HTTPException(422, "Request does not change the record")

    history_entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Compare-and-swap avoids silently losing concurrent history updates.

    result = logs.update_one({"_id": old["_id"], "history": old.get("history", [])},

                             {"$set": new_values, "$push": {"history": history_entry}})

    if result.modified_count != 1: raise HTTPException(409, "Concurrent regularization; retry")

    updated = logs.find_one({"_id": old["_id"]})

    return public_log(clean_log(updated))



@app.get("/analytics/employees/{emp_code}/monthly")

def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):

    emp = employees.find_one({"emp_code": emp_code}, {"_id":0})

    if not emp: raise HTTPException(404, "Unknown employee")

    first, last, start, end = month_bounds(month)

    joined = date.fromisoformat(emp["joined_on"])

    wd = working_days(max(start, joined), end) if joined <= end else 0

    pipeline = [

      {"$match":{"emp_code":emp_code,"date":{"$gte":first,"$lte":last}}},

      {"$group":{"_id":None,

        "present_days":{"$sum":{"$cond":[{"$in":["$status",list(PRESENCE)]},{"$cond":[{"$eq":[{"$ifNull":["$half_day",False]},True]},0.5,1]},0]}},

        "leave_days":{"$sum":{"$cond":[{"$eq":["$status","LEAVE"]},1,0]}},

        "late_count":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$late_minutes",0]},0]},1,0]}},

        "total_late_minutes":{"$sum":{"$ifNull":["$late_minutes",0]}},

        "total_overtime_minutes":{"$sum":{"$ifNull":["$overtime_minutes",0]}}}}

    ]

    agg = list(logs.aggregate(pipeline))

    a = agg[0] if agg else {}

    present = round_half_up(a.get("present_days",0),2)

    pct = round_half_up(present/wd*100,2) if wd else None

    return {"emp_code":emp_code,"month":month,"working_days":wd,"present_days":present,

      "leave_days":a.get("leave_days",0),"late_count":a.get("late_count",0),

      "total_late_minutes":a.get("total_late_minutes",0),"total_overtime_minutes":a.get("total_overtime_minutes",0),

      "attendance_pct":pct}



@app.get("/analytics/departments/summary")

def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):

    first,last,start,end = month_bounds(month)

    emp_match: dict[str,Any] = {"joined_on":{"$lte":last}}

    if department is not None: emp_match["department"] = department

    # Start from employees so zero-log employees contribute to headcount.

    pipeline = [

      {"$match":emp_match},

      {"$lookup":{"from":"attendance_logs","let":{"code":"$emp_code","dept":"$department"},

        "pipeline":[{"$match":{"$expr":{"$and":[{"$eq":["$emp_code","$$code"]},{"$gte":["$date",first]},{"$lte":["$date",last]}]}}}],

        "as":"logs"}},

      {"$unwind":{"path":"$logs","preserveNullAndEmptyArrays":True}},

      {"$group":{"_id":"$department","headcount":{"$addToSet":"$emp_code"},

        "present_days":{"$sum":{"$cond":[{"$in":["$logs.status",list(PRESENCE)]},{"$cond":[{"$eq":[{"$ifNull":["$logs.half_day",False]},True]},0.5,1]},0]}},

        "work_hours_sum":{"$sum":{"$cond":[{"$and":[{"$in":["$logs.status",list(PRESENCE)]},{"$ne":["$logs.work_hours",None]}]},"$logs.work_hours",0]}},

        "work_hours_n":{"$sum":{"$cond":[{"$and":[{"$in":["$logs.status",list(PRESENCE)]},{"$ne":["$logs.work_hours",None]}]},1,0]}},

        "late_count":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$logs.late_minutes",0]},0]},1,0]}},

        "total_late_minutes":{"$sum":{"$ifNull":["$logs.late_minutes",0]}},

        "leave_count":{"$sum":{"$cond":[{"$eq":["$logs.status","LEAVE"]},1,0]}},

        "on_duty_count":{"$sum":{"$cond":[{"$eq":["$logs.status","ON_DUTY"]},1,0]}}}},

      {"$project":{"_id":0,"department":"$_id","headcount":{"$size":"$headcount"},"present_days":1,

        "avg_work_hours":{"$cond":[{"$gt":["$work_hours_n",0]},{"$divide":["$work_hours_sum","$work_hours_n"]},None]},

        "late_count":1,"total_late_minutes":1,"leave_count":1,"on_duty_count":1}},

      {"$sort":{"department":1}}

    ]

    rows = list(employees.aggregate(pipeline, allowDiskUse=True))

    # Each employee is included only if joined by month-end; dedupe headcount via addToSet.

    return {"month":month,"items":[{**r,"present_days":round_half_up(r["present_days"],2),

      "avg_work_hours":round_half_up(r["avg_work_hours"],2) if r["avg_work_hours"] is not None else None} for r in rows if r["headcount"] > 0]}



@app.get("/analytics/leaderboard/late")

def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),

                     limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):

    first,last,_,_ = month_bounds(month)

    pipeline = [

      {"$match":{"date":{"$gte":first,"$lte":last}}},

      {"$group":{"_id":"$emp_code","total_late_minutes":{"$sum":{"$ifNull":["$late_minutes",0]}},

                 "late_count":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$late_minutes",0]},0]},1,0]}}}},

      {"$match":{"total_late_minutes":{"$gt":0}}},

      {"$lookup":{"from":"employees","localField":"_id","foreignField":"emp_code","as":"employee"}},

      {"$unwind":"$employee"},

    ]

    if department is not None: pipeline.append({"$match":{"employee.department":department}})

    pipeline += [

      {"$project":{"_id":0,"emp_code":"$_id","name":"$employee.name","department":"$employee.department","total_late_minutes":1,"late_count":1}},

      {"$sort":{"total_late_minutes":-1,"emp_code":1}},

      {"$setWindowFields":{"sortBy":{"total_late_minutes":-1},"output":{"rank":{"$rank":{}}}}},

      {"$match":{"rank":{"$lte":limit}}},

      {"$sort":{"total_late_minutes":-1,"emp_code":1}}

    ]

    rows = list(logs.aggregate(pipeline, allowDiskUse=True))

    return {"month":month,"items":rows}



@app.get("/analytics/departments/{department}/trend")

def department_trend(department: str, from_date: date = Query(..., alias="from"), to_date: date = Query(..., alias="to")):

    if to_date < from_date: raise HTTPException(422, "'to' must be >= 'from'")

    days = (to_date-from_date).days + 1

    if days > 92: raise HTTPException(422, "Date range cannot exceed 92 days")

    if employees.count_documents({"department":department}) == 0: raise HTTPException(404, "Unknown department")

    first, last = from_date.isoformat(), to_date.isoformat()

    pipeline = [

      {"$match":{"department":department,"joined_on":{"$lte":last}}},

      {"$lookup":{"from":"attendance_logs","let":{"code":"$emp_code"},

        "pipeline":[{"$match":{"$expr":{"$and":[{"$eq":["$emp_code","$$code"]},{"$gte":["$date",first]},{"$lte":["$date",last]}]}}}],

        "as":"logs"}},

      {"$unwind":{"path":"$logs","preserveNullAndEmptyArrays":True}},

      {"$group":{"_id":{"$ifNull":["$logs.date",None]},

        "headcount":{"$addToSet":"$emp_code"},

        "present_count":{"$sum":{"$cond":[{"$in":["$logs.status",list(PRESENCE)]},{"$cond":[{"$eq":[{"$ifNull":["$logs.half_day",False]},True]},0.5,1]},0]}},

        "late_count":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$logs.late_minutes",0]},0]},1,0]}}}},

      {"$project":{"_id":0,"date":"$_id","headcount":{"$size":"$headcount"},"present_count":1,"late_count":1}},

      {"$match":{"date":{"$ne":None}}},

      {"$set": {"date": {"$dateFromString": {"dateString": "$date"}}}},

      {"$densify": {"field": "date", "range": {"step": 1, "unit": "day", "bounds": [datetime.fromisoformat(first), datetime.fromisoformat(last)]}}},

      {"$set":{"date":{"$dateToString":{"format":"%Y-%m-%d","date":"$date","timezone":"UTC"}}}},

      {"$sort":{"date":1}},

      {"$set":{"is_working_day":{"$not":[{"$in":[{"$dayOfWeek":{"$dateFromString":{"dateString":"$date"}}},[1,7]]}]}}},

      {"$set":{"headcount":{"$ifNull":["$headcount",0]},"present_count":{"$ifNull":["$present_count",0]},"late_count":{"$ifNull":["$late_count",0]}}},

      {"$set":{"attendance_rate":{"$cond":[{"$and":["$is_working_day",{"$gt":["$headcount",0]}]},{"$round":[{"$divide":["$present_count","$headcount"]},4]},None]}}},

      {"$setWindowFields":{"sortBy":{"date":1},"output":{"moving_avg_7d":{"$avg":"$attendance_rate","window":{"documents":[-6,0]}}}}},

      {"$project":{"_id":0,"date":1,"is_working_day":1,"headcount":1,"present_count":1,"late_count":1,

        "attendance_rate":1,"moving_avg_7d":{"$cond":[{"$eq":["$moving_avg_7d",None]},None,{"$round":["$moving_avg_7d",4]}]}}},

      {"$sort":{"date":1}}

    ]

    rows = list(employees.aggregate(pipeline, allowDiskUse=True))

    # Densify's date type is converted to canonical date strings above.

    return {"department":department,"items":rows}



def plan_has_stage(obj: Any, stage: str) -> bool:

    if isinstance(obj, dict):

        if obj.get("stage") == stage or stage in obj:

            return True

        return any(plan_has_stage(v, stage) for v in obj.values())

    if isinstance(obj, list):

        return any(plan_has_stage(v, stage) for v in obj)

    return False



@app.get("/admin/explain/{endpoint}")

def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None,

                     department: Optional[str] = None, limit: int = Query(10, ge=1, le=50),

                     date_from: Optional[date] = None, date_to: Optional[date] = None,

                     status: Optional[str] = None, page: int = Query(1, ge=1),

                     page_size: int = Query(20, ge=1, le=100),

                     from_date: Optional[date] = Query(None, alias="from"), to_date: Optional[date] = Query(None, alias="to")):

    if endpoint == "attendance_list":
        # Match GET /attendance filters, with optional month support for Swagger.
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must be <= date_to")
        if status is not None and status not in STATUSES:
            raise HTTPException(422, "Invalid status")

        filt: dict[str, Any] = {}
        if emp_code is not None:
            filt["emp_code"] = emp_code

        if month:
            first, last, _, _ = month_bounds(month)
            filt["date"] = {"$gte": first, "$lte": last}
            if date_from:
                filt["date"]["$gte"] = max(first, date_from.isoformat())
            if date_to:
                filt["date"]["$lte"] = min(last, date_to.isoformat())
            if filt["date"]["$gte"] > filt["date"]["$lte"]:
                raise HTTPException(422, "The supplied date range does not overlap the month")
        elif date_from or date_to:
            filt["date"] = {}
            if date_from:
                filt["date"]["$gte"] = date_from.isoformat()
            if date_to:
                filt["date"]["$lte"] = date_to.isoformat()

        if status is not None:
            filt["status"] = status

        command = {
            "find": logs.name,
            "filter": filt,
            "sort": {"date": DESCENDING, "emp_code": ASCENDING},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
        exp = logs.database.command(
            "explain",
            command,
            verbosity="executionStats",
        )
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":

        if not emp_code or not month: raise HTTPException(422, "emp_code and month are required")

        first,last,_,_ = month_bounds(month)

        pipe = [{"$match":{"emp_code":emp_code,"date":{"$gte":first,"$lte":last}}},{"$group":{"_id":None,"n":{"$sum":1}}}]

        exp = logs.database.command(

    "explain",

    {

        "aggregate": logs.name,

        "pipeline": pipe,

        "cursor": {}

    },

    verbosity="executionStats"

)



        collection = "attendance_logs"

    elif endpoint == "department_summary":

        if not month: raise HTTPException(422, "month is required")

        first,last,_,_ = month_bounds(month)

        match = {"joined_on":{"$lte":last}}

        if department is not None: match["department"] = department

        pipe = [{"$match":match},{"$lookup":{"from":"attendance_logs","localField":"emp_code","foreignField":"emp_code","as":"logs"}},{"$unwind":{"path":"$logs","preserveNullAndEmptyArrays":True}},{"$group":{"_id":"$department","n":{"$sum":1}}}]

        exp = employees.database.command(

    "explain",

    {

        "aggregate": employees.name,

        "pipeline": pipe,

        "cursor": {}

    },

    verbosity="executionStats"

)



        collection = "employees"

    elif endpoint == "late_leaderboard":

        if not month: raise HTTPException(422, "month is required")

        first,last,_,_ = month_bounds(month)

        pipe = [{"$match":{"date":{"$gte":first,"$lte":last}}},{"$group":{"_id":"$emp_code","n":{"$sum":{"$ifNull":["$late_minutes",0]}}}}]

        exp = logs.database.command("explain",

    {

        "aggregate": logs.name,

        "pipeline": pipe,

        "cursor": {}

    },

    verbosity="executionStats"

)

        collection = "attendance_logs"

    elif endpoint == "department_trend":

        if not department or not from_date or not to_date: raise HTTPException(422, "department, from, and to are required")

        if to_date < from_date or (to_date-from_date).days+1 > 92: raise HTTPException(422, "Invalid date range")

        pipe = [{"$match":{"department":department,"joined_on":{"$lte":to_date.isoformat()}}},{"$lookup":{"from":"attendance_logs","localField":"emp_code","foreignField":"emp_code","as":"logs"}}]

        exp = employees.database.command("explain",

    {

        "aggregate": employees.name,

        "pipeline": pipe,

        "cursor": {}

    },

    verbosity="executionStats"

)

        collection = "employees"

    else:

        raise HTTPException(422, "Unknown endpoint")

    return {"endpoint":endpoint,"collection":collection,"explain":json_util.loads(json_util.dumps(exp))}
