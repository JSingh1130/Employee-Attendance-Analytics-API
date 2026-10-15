"""Employee Attendance & Analytics API starter."""
import json
import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PRESENT_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")
_indexes_ready = False


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def truncate_to_seconds(value: datetime) -> datetime:
    return as_utc(value).replace(microsecond=0)


def epoch_millis_to_datetime(value: int) -> datetime:
    return datetime.fromtimestamp(value // 1000, tz=UTC)


def datetime_to_epoch_millis(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    return int(as_utc(value).timestamp() * 1000)


def shift_time(value: str) -> time:
    hour, minute = map(int, value.split(":"))
    return time(hour, minute)


def shift_datetime(date_str: str, time_str: str) -> datetime:
    return datetime.combine(date.fromisoformat(date_str), shift_time(time_str), tzinfo=IST)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local_punch_in = as_utc(punch_in).astimezone(IST)
    result = local_punch_in.date()
    if shift_end <= shift_start and local_punch_in.time().replace(tzinfo=None) < shift_time(shift_end):
        result -= timedelta(days=1)
    return result.isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: str) -> int:
    local_punch_in = as_utc(punch_in).astimezone(IST)
    start = shift_datetime(date_str, shift_start)
    elapsed_seconds = int((local_punch_in - start).total_seconds())
    if elapsed_seconds <= 10 * 60:
        return 0
    return elapsed_seconds // 60


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((truncate_to_seconds(punch_out) - truncate_to_seconds(punch_in)).total_seconds())
    hours = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str, shift_start: str) -> int:
    end = shift_datetime(date_str, shift_end)
    if shift_end <= shift_start:
        end += timedelta(days=1)
    elapsed_seconds = int((as_utc(punch_out).astimezone(IST) - end).total_seconds())
    if elapsed_seconds < 30 * 60:
        return 0
    return elapsed_seconds // 60


def employee_to_api(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_epoch_millis(doc["created_at"]),
    }


def attendance_to_api(doc: dict) -> dict:
    history = []
    for entry in doc.get("history") or []:
        changes = {}
        for field, change in (entry.get("changes") or {}).items():
            changes[field] = {
                key: datetime_to_epoch_millis(value)
                if field in ("punch_in", "punch_out") and isinstance(value, datetime)
                else value
                for key, value in change.items()
            }
        history.append({
            "at": datetime_to_epoch_millis(entry.get("at")),
            "by": entry.get("by"),
            "reason": entry.get("reason"),
            "changes": changes,
        })
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch_millis(doc.get("punch_in")),
        "punch_out": datetime_to_epoch_millis(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        try:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError
        except ValueError as exc:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date") from exc
        return value

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @model_validator(mode="after")
    def reject_null_timestamp(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at must be omitted or an epoch-millisecond integer")
        return self


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)

    @model_validator(mode="after")
    def reject_null_timestamp(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at must be omitted or an epoch-millisecond integer")
        return self


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def reject_explicit_nulls(self):
        for field in ("status", "punch_in", "punch_out"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


def build_attendance_filter(
    emp_code: Optional[str],
    date_from: Optional[date],
    date_to: Optional[date],
    status: Optional[Status],
) -> dict:
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from.isoformat()
        if date_to is not None:
            query["date"]["$lte"] = date_to.isoformat()
    if status is not None:
        query["status"] = status
    return query


@app.on_event("startup")
def initialize_indexes():
    try:
        create_indexes()
    except PyMongoError:
        return


def create_indexes():
    global _indexes_ready
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING)])
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True
    )
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)])
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)]
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)]
    )
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING)])
    _indexes_ready = True


@app.get("/health")
def health():
    try:
        db.command("ping")
        if not _indexes_ready:
            create_indexes()
    except PyMongoError as exc:
        raise HTTPException(503, "MongoDB is unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    return employee_to_api(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    query = {}
    if department is not None:
        query["department"] = department
    total = db.employees.count_documents(query)
    cursor = (
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [employee_to_api(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    punched_at = truncate_to_seconds(
        epoch_millis_to_datetime(body.punched_at)
        if body.punched_at is not None
        else datetime.now(UTC)
    )
    record_date = attendance_date(punched_at, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": record_date,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punched_at, emp["shift_start"], record_date),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this date") from exc
    return attendance_to_api(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Status] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    query = build_attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    cursor = (
        db.attendance_logs.find(query, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [attendance_to_api(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    punched_at = truncate_to_seconds(
        epoch_millis_to_datetime(body.punched_at)
        if body.punched_at is not None
        else datetime.now(UTC)
    )
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "record is already punched out")
    punch_in_time = as_utc(record["punch_in"])
    if punched_at <= punch_in_time or punched_at - punch_in_time > timedelta(hours=24):
        raise HTTPException(422, "punched_at must be after punch-in and within 24 hours")

    work_hours = compute_work_hours(punch_in_time, punched_at)
    values = {
        "punch_out": punched_at,
        "work_hours": work_hours,
        "overtime_minutes": compute_overtime(
            punched_at, emp["shift_end"], record["date"], emp["shift_start"]
        ),
        "half_day": work_hours < 4.50,
    }
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "status": record["status"],
            "punch_in": record["punch_in"],
            "punch_out": None,
        },
        {"$set": values},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record changed or was already punched out")
    return attendance_to_api(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    try:
        if datetime.strptime(date, "%Y-%m-%d").date().isoformat() != date:
            raise ValueError
    except ValueError as exc:
        raise HTTPException(422, "date must be a valid YYYY-MM-DD date") from exc
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "attendance record not found")

    supplied = body.model_fields_set
    if ("punch_in" in supplied and body.punch_in is None) or (
        "punch_out" in supplied and body.punch_out is None
    ):
        raise HTTPException(422, "punch times cannot be null")
    status = body.status if "status" in supplied else record["status"]
    if status in ("ABSENT", "LEAVE"):
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(422, "ABSENT and LEAVE cannot include punch times")
        punch_in_time = None
        punch_out_time = None
    else:
        punch_in_time = (
            truncate_to_seconds(epoch_millis_to_datetime(body.punch_in))
            if "punch_in" in supplied
            else (as_utc(record["punch_in"]) if record.get("punch_in") is not None else None)
        )
        punch_out_time = (
            truncate_to_seconds(epoch_millis_to_datetime(body.punch_out))
            if "punch_out" in supplied
            else (as_utc(record["punch_out"]) if record.get("punch_out") is not None else None)
        )
        if punch_in_time is None:
            raise HTTPException(422, "presence statuses require punch_in")
        if attendance_date(punch_in_time, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(422, "punch_in must belong to the record attendance date")
        if punch_out_time is not None and (
            punch_out_time <= punch_in_time
            or punch_out_time - punch_in_time > timedelta(hours=24)
        ):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    if status in ("ABSENT", "LEAVE"):
        derived = {
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }
    else:
        work_hours = compute_work_hours(punch_in_time, punch_out_time) if punch_out_time else None
        derived = {
            "work_hours": work_hours,
            "late_minutes": compute_late_minutes(punch_in_time, emp["shift_start"], date),
            "overtime_minutes": compute_overtime(
                punch_out_time, emp["shift_end"], date, emp["shift_start"]
            ) if punch_out_time else 0,
            "half_day": work_hours < 4.50 if work_hours is not None else False,
        }

    final_values = {
        "status": status,
        "punch_in": punch_in_time,
        "punch_out": punch_out_time,
        **derived,
    }
    before_values = {
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }
    changes = {}
    for field, new_value in final_values.items():
        old_value = before_values[field]
        comparable_old = as_utc(old_value) if isinstance(old_value, datetime) else old_value
        if comparable_old != new_value:
            changes[field] = {"from": old_value, "to": new_value}
    if not changes:
        raise HTTPException(422, "regularization does not change the record")

    history_entry = {
        "at": datetime.now(UTC),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    expected = {"_id": record["_id"]}
    for field in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        expected[field] = record[field] if field in record else {"$exists": False}
    expected["history"] = record["history"] if "history" in record else {"$exists": False}
    updated = db.attendance_logs.find_one_and_update(
        expected,
        {"$set": final_values, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record changed during regularization; retry")
    return attendance_to_api(updated)


def month_bounds(month: str) -> tuple[str, str, date, date]:
    try:
        first_day = date.fromisoformat(f"{month}-01")
    except ValueError as exc:
        raise HTTPException(422, "month must be YYYY-MM") from exc
    if first_day.strftime("%Y-%m") != month:
        raise HTTPException(422, "month must be YYYY-MM")
    next_month = (first_day.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first_day.isoformat(), next_month.isoformat(), first_day, next_month - timedelta(days=1)


def round_half_up_expression(value, places: int):
    factor = 10 ** places
    return {
        "$divide": [
            {"$trunc": {"$add": [{"$multiply": [value, factor]}, 0.5]}},
            factor,
        ]
    }


def working_days_expression(start_expression, end_expression):
    return {
        "$let": {
            "vars": {"first_day": start_expression, "last_day": end_expression},
            "in": {
                "$let": {
                    "vars": {
                        "day_count": {
                            "$max": [
                                0,
                                {
                                    "$add": [
                                        {
                                            "$dateDiff": {
                                                "startDate": "$$first_day",
                                                "endDate": "$$last_day",
                                                "unit": "day",
                                            }
                                        },
                                        1,
                                    ]
                                },
                            ]
                        }
                    },
                    "in": {
                        "$size": {
                            "$filter": {
                                "input": {
                                    "$map": {
                                        "input": {"$range": [0, "$$day_count"]},
                                        "as": "offset",
                                        "in": {
                                            "$dateAdd": {
                                                "startDate": "$$first_day",
                                                "unit": "day",
                                                "amount": "$$offset",
                                            }
                                        },
                                    }
                                },
                                "as": "calendar_day",
                                "cond": {
                                    "$in": [
                                        {"$dayOfWeek": "$$calendar_day"},
                                        [2, 3, 4, 5, 6],
                                    ]
                                },
                            }
                        }
                    },
                }
            },
        }
    }


def record_weekday_expression(path: str):
    return {
        "$cond": [
            {"$ifNull": [f"{path}.date", False]},
            {
                "$in": [
                    {"$dayOfWeek": {"$dateFromString": {"dateString": f"{path}.date"}}},
                    [2, 3, 4, 5, 6],
                ]
            },
            False,
        ]
    }


def build_employee_monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    month_start, month_after, month_start_date, month_end_date = month_bounds(month)
    month_start_dt = datetime.combine(month_start_date, time.min, tzinfo=UTC)
    month_end_dt = datetime.combine(month_end_date, time.min, tzinfo=UTC)
    return [
        {"$match": {"emp_code": emp_code}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lt": ["$date", month_after]},
                                ]
                            }
                        }
                    }
                ],
                "as": "month_logs",
            }
        },
        {
            "$set": {
                "joined_date": {"$dateFromString": {"dateString": "$joined_on"}},
            }
        },
        {
            "$set": {
                "period_start": {
                    "$cond": [
                        {"$gt": ["$joined_date", month_start_dt]},
                        "$joined_date",
                        month_start_dt,
                    ]
                }
            }
        },
        {"$set": {"working_days": working_days_expression("$period_start", month_end_dt)}},
        {"$unwind": {"path": "$month_logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$emp_code",
                "working_days": {"$first": "$working_days"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$month_logs.status", list(PRESENT_STATUSES)]},
                                    record_weekday_expression("$month_logs"),
                                ]
                            },
                            {"$cond": [{"$ifNull": ["$month_logs.half_day", False]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$month_logs.status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$month_logs.late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$month_logs.late_minutes", 0]}},
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$month_logs.overtime_minutes", 0]}
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "month": {"$literal": month},
                "working_days": 1,
                "present_days": 1,
                "leave_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "total_overtime_minutes": 1,
                "attendance_pct": {
                    "$cond": [
                        {"$eq": ["$working_days", 0]},
                        None,
                        round_half_up_expression(
                            {"$multiply": [{"$divide": ["$present_days", "$working_days"]}, 100]},
                            4,
                        ),
                    ]
                },
            }
        },
    ]


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> list[dict]:
    month_start, month_after, _, month_end_date = month_bounds(month)
    employee_match = {"joined_on": {"$lte": month_end_date.isoformat()}}
    if department is not None:
        employee_match["department"] = department
    present_record = {"$in": ["$records.status", list(PRESENT_STATUSES)]}
    present_workday = {"$and": [present_record, record_weekday_expression("$records")]}
    work_hours_valid = {
        "$and": [present_record, {"$ne": ["$records.work_hours", None]}]
    }
    return [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lt": ["$date", month_after]},
                                ]
                            }
                        }
                    }
                ],
                "as": "month_logs",
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "employee_logs": {"$push": "$month_logs"},
            }
        },
        {
            "$project": {
                "headcount": 1,
                "records": {
                    "$reduce": {
                        "input": "$employee_logs",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]},
                    }
                },
            }
        },
        {"$unwind": {"path": "$records", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$_id",
                "headcount": {"$first": "$headcount"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            present_workday,
                            {"$cond": [{"$ifNull": ["$records.half_day", False]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {"$cond": [work_hours_valid, "$records.work_hours", 0]}
                },
                "work_hours_count": {
                    "$sum": {"$cond": [work_hours_valid, 1, 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$records.late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$records.late_minutes", 0]}},
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$records.status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$records.status", "ON_DUTY"]}, 1, 0]}
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$eq": ["$work_hours_count", 0]},
                        None,
                        round_half_up_expression(
                            {"$divide": ["$work_hours_sum", "$work_hours_count"]}, 2
                        ),
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": ASCENDING}},
    ]


def build_late_leaderboard_pipeline(
    month: str, limit: int, department: Optional[str] = None
) -> list[dict]:
    month_start, month_after, _, _ = month_bounds(month)
    pipeline = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lt": month_after},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": DESCENDING},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )
    return pipeline


def build_department_trend_pipeline(department: str, from_date: date, to_date: date) -> list[dict]:
    start_day = datetime.combine(from_date, time.min, tzinfo=UTC)
    end_exclusive = datetime.combine(to_date + timedelta(days=1), time.min, tzinfo=UTC)
    start_string = from_date.isoformat()
    end_string = (to_date + timedelta(days=1)).isoformat()
    return [
        {"$match": {"department": department}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", start_string]},
                                    {"$lt": ["$date", end_string]},
                                ]
                            }
                        }
                    },
                    {"$project": {"date": 1, "status": 1, "half_day": 1, "late_minutes": 1}},
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": {"$ifNull": ["$logs.date", start_string]},
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$in": [
                                    {"$ifNull": ["$logs.status", ""]},
                                    list(PRESENT_STATUSES),
                                ]
                            },
                            {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "day": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "day",
                "range": {"step": 1, "unit": "day", "bounds": [start_day, end_exclusive]},
            }
        },
        {
            "$set": {
                "day_string": {
                    "$dateToString": {"date": "$day", "format": "%Y-%m-%d", "timezone": "UTC"}
                },
                "is_working_day": {"$in": [{"$isoDayOfWeek": "$day"}, [1, 2, 3, 4, 5]]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"calendar_date": "$day_string"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$calendar_date"]},
                                ]
                            }
                        }
                    },
                    {"$count": "headcount"},
                ],
                "as": "daily_headcount",
            }
        },
        {
            "$set": {
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$daily_headcount.headcount", 0]}, 0]},
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        round_half_up_expression(
                            {"$divide": ["$present_count", "$headcount"]}, 4
                        ),
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"day": ASCENDING},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$set": {
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$moving_avg_7d", None]},
                        None,
                        round_half_up_expression("$moving_avg_7d", 4),
                    ]
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$day_string",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": ASCENDING}},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    month_bounds(month)
    result = list(db.employees.aggregate(build_employee_monthly_pipeline(emp_code, month)))
    if not result:
        raise HTTPException(404, "employee not found")
    return result[0]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    month_bounds(month)
    items = list(db.employees.aggregate(build_department_summary_pipeline(month, department)))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    month_bounds(month)
    items = list(
        db.attendance_logs.aggregate(build_late_leaderboard_pipeline(month, limit, department))
    )
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
):
    if to_date < from_date or (to_date - from_date).days + 1 > 92:
        raise HTTPException(422, "date range must be ordered and no longer than 92 days")
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "department not found")
    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    return {"department": department, "items": list(db.employees.aggregate(pipeline))}


def aggregation_explain(collection, pipeline: list[dict]) -> dict:
    return db.command(
        {
            "explain": {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
            "verbosity": "executionStats",
        }
    )


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Status] = None,
    from_date: Optional[date] = Query(default=None, alias="from"),
    to_date: Optional[date] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from is not None and date_to is not None and date_from > date_to:
            raise HTTPException(422, "date_from must be on or before date_to")
        command = {
            "find": "attendance_logs",
            "filter": build_attendance_filter(emp_code, date_from, date_to, status),
            "sort": {"date": DESCENDING, "emp_code": ASCENDING},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
        explain = db.command({"explain": command, "verbosity": "executionStats"})
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise HTTPException(422, "emp_code and month are required")
        month_bounds(month)
        explain = aggregation_explain(
            "employees", build_employee_monthly_pipeline(emp_code, month)
        )
        collection = "employees"
    elif endpoint == "department_summary":
        if month is None:
            raise HTTPException(422, "month is required")
        month_bounds(month)
        explain = aggregation_explain(
            "employees", build_department_summary_pipeline(month, department)
        )
        collection = "employees"
    elif endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(422, "month is required")
        month_bounds(month)
        explain = aggregation_explain(
            "attendance_logs", build_late_leaderboard_pipeline(month, limit, department)
        )
        collection = "attendance_logs"
    else:
        if department is None or from_date is None or to_date is None:
            raise HTTPException(422, "department, from, and to are required")
        if to_date < from_date or (to_date - from_date).days + 1 > 92:
            raise HTTPException(422, "date range must be ordered and no longer than 92 days")
        explain = aggregation_explain(
            "employees", build_department_trend_pipeline(department, from_date, to_date)
        )
        collection = "employees"
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": json.loads(json_util.dumps(explain)),
    }
