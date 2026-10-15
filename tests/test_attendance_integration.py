import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()
TEST_DB_NAME = os.getenv("ATTENDANCE_TEST_DB")
APP_DB_NAME = os.getenv("MONGO_DB", "attendance_db")
if not TEST_DB_NAME:
    raise RuntimeError("Set ATTENDANCE_TEST_DB to a disposable MongoDB database before running tests")
if TEST_DB_NAME == APP_DB_NAME:
    raise RuntimeError("ATTENDANCE_TEST_DB must differ from MONGO_DB; tests delete their fixture records")

os.environ["MONGO_DB"] = TEST_DB_NAME

from fastapi import HTTPException

from app.main import (
    IST,
    PunchInIn,
    PunchOutIn,
    RegularizeIn,
    compute_late_minutes,
    compute_work_hours,
    create_indexes,
    db,
    department_summary,
    department_trend,
    employee_monthly,
    late_leaderboard,
    punch_in,
    punch_out,
    regularize_attendance,
)


EMPLOYEE_CODES = [f"EMP98{number:02d}" for number in range(1, 13)]
UTC = timezone.utc


def timestamp_millis(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def utc_datetime(day: int, hour: int = 0, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 7, day, hour, minute, second, tzinfo=UTC)


def invoke_status(operation, expected_success: int) -> int:
    try:
        operation()
        return expected_success
    except HTTPException as exc:
        return exc.status_code


class AttendanceIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.command("ping")
        create_indexes()

    def setUp(self):
        self.clear_fixtures()

    def tearDown(self):
        self.clear_fixtures()

    @staticmethod
    def clear_fixtures():
        db.employees.delete_many({"emp_code": {"$in": EMPLOYEE_CODES}})
        db.attendance_logs.delete_many({"emp_code": {"$in": EMPLOYEE_CODES}})

    def add_employee(
        self,
        code: str,
        department: str = "Test",
        joined_on: str = "2026-07-01",
        shift_start: str = "09:30",
        shift_end: str = "18:30",
    ):
        db.employees.insert_one(
            {
                "emp_code": code,
                "name": code,
                "email": f"{code.lower()}@example.com",
                "department": department,
                "shift_start": shift_start,
                "shift_end": shift_end,
                "joined_on": joined_on,
                "created_at": utc_datetime(1),
            }
        )

    def add_log(
        self,
        code: str,
        day: int,
        status: str = "PRESENT",
        late_minutes: int = 0,
        work_hours=None,
        half_day=False,
        punch_in=None,
        punch_out=None,
        omit_half_day=False,
    ):
        doc = {
            "emp_code": code,
            "date": f"2026-07-{day:02d}",
            "status": status,
            "punch_in": punch_in,
            "punch_out": punch_out,
            "work_hours": work_hours,
            "late_minutes": late_minutes,
            "overtime_minutes": 0,
            "history": [],
        }
        if not omit_half_day:
            doc["half_day"] = half_day
        db.attendance_logs.insert_one(doc)

    def test_midmonth_joiner_and_employee_without_logs(self):
        self.add_employee(EMPLOYEE_CODES[0], department="Headcount", joined_on="2026-07-15")
        self.add_employee(EMPLOYEE_CODES[1], department="Headcount")
        self.add_employee(EMPLOYEE_CODES[2], department="Headcount", joined_on="2026-08-01")
        self.add_log(
            EMPLOYEE_CODES[0],
            15,
            work_hours=4.49,
            half_day=True,
            punch_in=utc_datetime(15, 3, 30),
            punch_out=utc_datetime(15, 7, 59, 24),
        )

        monthly = employee_monthly(EMPLOYEE_CODES[0], "2026-07")
        self.assertEqual(monthly["working_days"], 13)
        self.assertEqual(monthly["present_days"], 0.5)
        self.assertEqual(monthly["attendance_pct"], 3.8462)

        summary = department_summary("2026-07", "Headcount")
        self.assertEqual(len(summary["items"]), 1)
        self.assertEqual(summary["items"][0]["headcount"], 2)
        self.assertEqual(summary["items"][0]["present_days"], 0.5)
        self.assertEqual(summary["items"][0]["avg_work_hours"], 4.49)

    def test_tied_leaderboard_cutoff_returns_all_tied_rows(self):
        for code in EMPLOYEE_CODES[3:7]:
            self.add_employee(code, department="Ties")
        for code, minutes in zip(EMPLOYEE_CODES[3:7], (40, 30, 30, 10)):
            self.add_log(code, 20, late_minutes=minutes)

        result = late_leaderboard("2026-07", 2, "Ties")
        rows = result["items"]
        self.assertEqual([row["rank"] for row in rows], [1, 2, 2])
        self.assertEqual([row["emp_code"] for row in rows], EMPLOYEE_CODES[3:6])

    def test_trend_fills_missing_weekdays_and_weekends(self):
        self.add_employee(EMPLOYEE_CODES[7], department="Trend", joined_on="2026-07-20")
        self.add_employee(EMPLOYEE_CODES[8], department="Trend", joined_on="2026-07-23")
        self.add_log(EMPLOYEE_CODES[7], 20, work_hours=8.0, punch_in=utc_datetime(20, 3, 30))
        self.add_log(EMPLOYEE_CODES[7], 25, status="WFH", work_hours=8.0, punch_in=utc_datetime(25, 3, 30))

        result = department_trend("Trend", datetime(2026, 7, 20).date(), datetime(2026, 7, 26).date())
        rows = result["items"]
        self.assertEqual(len(rows), 7)
        self.assertEqual(rows[1]["date"], "2026-07-21")
        self.assertTrue(rows[1]["is_working_day"])
        self.assertEqual(rows[1]["headcount"], 1)
        self.assertEqual(rows[1]["present_count"], 0)
        self.assertEqual(rows[1]["attendance_rate"], 0.0)
        self.assertEqual(rows[5]["date"], "2026-07-25")
        self.assertFalse(rows[5]["is_working_day"])
        self.assertEqual(rows[5]["headcount"], 2)
        self.assertEqual(rows[5]["present_count"], 1.0)
        self.assertIsNone(rows[5]["attendance_rate"])
        self.assertEqual(rows[5]["moving_avg_7d"], 0.2)

    def test_overnight_shift_punches_out_next_calendar_day(self):
        code = EMPLOYEE_CODES[9]
        self.add_employee(code, department="Night", shift_start="22:00", shift_end="06:00")
        start = datetime(2026, 7, 20, 21, 55, tzinfo=IST)
        end = datetime(2026, 7, 21, 6, 40, tzinfo=IST)
        created = punch_in(PunchInIn(emp_code=code, punched_at=timestamp_millis(start)))
        closed = punch_out(PunchOutIn(emp_code=code, punched_at=timestamp_millis(end)))

        self.assertEqual(created["date"], "2026-07-20")
        self.assertEqual(closed["work_hours"], 8.75)
        self.assertEqual(closed["overtime_minutes"], 40)

    def test_rounding_and_threshold_boundaries(self):
        exact_grace = datetime(2026, 7, 20, 9, 40, tzinfo=IST)
        after_grace = exact_grace + timedelta(seconds=1)
        self.assertEqual(compute_late_minutes(exact_grace, "09:30", "2026-07-20"), 0)
        self.assertEqual(compute_late_minutes(after_grace, "09:30", "2026-07-20"), 10)
        self.assertEqual(
            compute_work_hours(utc_datetime(20), utc_datetime(20, 8, 0, 18)),
            8.01,
        )

        half_day_code = EMPLOYEE_CODES[10]
        self.add_employee(half_day_code, department="Rounding", shift_start="00:00", shift_end="23:59")
        start = datetime(2026, 7, 22, 0, 0, tzinfo=IST)
        punch_in( PunchInIn(emp_code=half_day_code, punched_at=timestamp_millis(start)))
        closed = punch_out(
            PunchOutIn(
                emp_code=half_day_code,
                punched_at=timestamp_millis(start + timedelta(seconds=16182)),
            )
        )
        self.assertEqual(closed["work_hours"], 4.5)
        self.assertFalse(closed["half_day"])

    def test_simultaneous_punches_and_regularizations_are_atomic(self):
        punch_code = EMPLOYEE_CODES[11]
        self.add_employee(punch_code, department="Races")
        start = datetime(2026, 7, 20, 9, 30, tzinfo=IST)
        punch_request = PunchInIn(emp_code=punch_code, punched_at=timestamp_millis(start))
        with ThreadPoolExecutor(max_workers=8) as executor:
            punch_statuses = list(
                executor.map(lambda _: invoke_status(lambda: punch_in(punch_request), 201), range(8))
            )
        self.assertEqual(punch_statuses.count(201), 1)
        self.assertEqual(punch_statuses.count(409), 7)

        end = start + timedelta(hours=9)
        punch_out_request = PunchOutIn(emp_code=punch_code, punched_at=timestamp_millis(end))
        with ThreadPoolExecutor(max_workers=8) as executor:
            out_statuses = list(
                executor.map(lambda _: invoke_status(lambda: punch_out(punch_out_request), 200), range(8))
            )
        self.assertEqual(out_statuses.count(200), 1)
        self.assertEqual(out_statuses.count(409), 7)

        correction_requests = [
            RegularizeIn(
                punch_in=timestamp_millis(start + timedelta(minutes=1)),
                reason="correction one",
                regularized_by="tester-one",
            ),
            RegularizeIn(
                punch_in=timestamp_millis(start + timedelta(minutes=2)),
                reason="correction two",
                regularized_by="tester-two",
            ),
        ]
        with ThreadPoolExecutor(max_workers=2) as executor:
            correction_statuses = list(
                executor.map(
                    lambda body: invoke_status(
                        lambda: regularize_attendance(punch_code, "2026-07-20", body), 200
                    ),
                    correction_requests,
                )
            )
        self.assertTrue(all(status in (200, 409) for status in correction_statuses))
        saved = db.attendance_logs.find_one({"emp_code": punch_code, "date": "2026-07-20"})
        self.assertEqual(len(saved["history"]), correction_statuses.count(200))


if __name__ == "__main__":
    unittest.main()