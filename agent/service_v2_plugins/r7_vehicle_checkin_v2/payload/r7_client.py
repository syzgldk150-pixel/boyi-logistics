"""R7 task HTTP primitives verified against the original page on 2026-10-08."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

PAGE = "task_page"
DETAIL = "task_detail"
PUNCH = "arrival_punch"
AUTH = "operator_context"
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


class R7TaskError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def fail(code):
    raise R7TaskError(code)


def text(value):
    if not isinstance(value, str) or not value.strip():
        fail("R7_SOURCE_INVALID")
    return value


def timestamp(value):
    try:
        return datetime.strptime(text(value), TIME_FORMAT)
    except ValueError:
        fail("R7_SOURCE_INVALID")


def task_identity(row):
    if not isinstance(row, dict):
        fail("R7_SOURCE_INVALID")
    return text(row.get("id")), text(row.get("taskNumber"))


def find_via(row, station):
    vias = row.get("taskViaList")
    if not isinstance(vias, list) or not vias:
        fail("R7_SOURCE_INVALID")
    matches = [v for v in vias if isinstance(v, dict) and v.get("viaStationCode") == station]
    # The page explicitly chooses the terminal occurrence on a returning route.
    if isinstance(vias[-1], dict) and vias[-1].get("viaStationCode") == station:
        selected = vias[-1]
    elif len(matches) == 1:
        selected = matches[0]
    else:
        fail("R7_OPERATION_SITE_AMBIGUOUS")
    text(selected.get("id"))
    return selected


class R7VehicleClient:
    def __init__(self, broker):
        self.broker = broker
        self.refs = []
        self.proofs = []
        self.user = None

    def request(self, name, body, *, write=False):
        response = self.broker(
            "http.request",
            action="write_json" if write else "read_json",
            role="r7_operator",
            arguments={"request": name, "body": body},
        )
        reference = getattr(response, "host_evidence_ref", None)
        if not isinstance(response, dict) or not isinstance(reference, str) or not reference:
            fail("R7_HOST_EVIDENCE_MISSING")
        self.refs.append(reference)
        result = response.get("response")
        if not isinstance(result, dict) or result.get("code") != 200 or "data" not in result:
            fail("WRITE_OUTCOME_UNKNOWN" if write else "R7_SOURCE_INVALID")
        return result["data"]

    def read_page(self, *, start_time, end_time, page):
        start, end = timestamp(start_time), timestamp(end_time)
        if start > end or (end - start).total_seconds() > 3 * 86400 or type(page) is not int or not 1 <= page <= 100:
            fail("R7_RANGE_INVALID")
        data = self.request(
            PAGE,
            {
                "queryType": 1,
                "pageSize": 200,
                "currentPage": page,
                "queryCount": True,
                "headPlanGoTime_CondStart": start_time,
                "headPlanGoTime_CondEnd": end_time,
                "publishStatus_CondList": ["20"],
            },
        )
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            fail("R7_SOURCE_INVALID")
        total = data.get("total")
        if type(total) is not int or total < 0 or data.get("currentPage") != page or data.get("pageSize") != 200:
            fail("R7_PAGINATION_INVALID")
        rows = data["data"]
        expected = min(200, max(0, total - (page - 1) * 200))
        if len(rows) != expected or (page > 1 and not rows):
            fail("R7_PAGINATION_INCOMPLETE")
        items = []
        for row in rows:
            task_id, number = task_identity(row)
            status, planned = row.get("taskStatus"), row.get("headPlanGoTime")
            if type(status) is not int or not start <= timestamp(planned) <= end:
                fail("R7_SOURCE_INVALID")
            items.append({"task_id": task_id, "task_number": number, "status": status, "planned_departure": planned})
        return {"items": items, "total": total, "page": page, "complete": page * 200 >= total}

    def arrive(self, *, task_id, task_number, start_time, end_time):
        start, end = timestamp(start_time), timestamp(end_time)
        if start > end or (end - start).total_seconds() > 3 * 86400:
            fail("R7_RANGE_INVALID")
        if self.user is None:
            self.user = self.request(AUTH, {})
        user = self.user
        if not isinstance(user, dict) or user.get("siteTypeCode") not in {110, 401}:
            fail("R7_OPERATION_SITE_REQUIRED")
        station = text(user.get("siteCode"))
        row = self.request(DETAIL, {"id": task_id})
        if task_identity(row) != (task_id, task_number) or row.get("taskStatus") != 55:
            fail("R7_TASK_CHANGED")
        if not start <= timestamp(row.get("headPlanGoTime")) <= end:
            fail("R7_TASK_CHANGED")
        if (
            row.get("departureStationCode") != row.get("arrivalStationCode")
            and row.get("departureStationCode") == station
        ):
            fail("R7_OPERATION_SITE_INVALID")
        selected = find_via(row, station)
        # Preserve extra-confirmation boundaries; never replace a record.
        if not selected.get("carHeadRealArriveTimeDriver"):
            fail("R7_DRIVER_ARRIVAL_CONFIRMATION_REQUIRED")
        if selected.get("carHeadRealArriveTimeCenter"):
            fail("R7_EXISTING_PUNCH_CONFIRMATION_REQUIRED")
        now = datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)
        if not row.get("needExtraCarType") and now < timestamp(row.get("taskReleaseTime")):
            fail("R7_TASK_TIME_INVALID")
        if "headAmount" not in row or "trunkAmount" not in row:
            fail("R7_SOURCE_INVALID")
        payload = deepcopy(row)
        at = now.strftime(TIME_FORMAT)
        find_via(payload, station)["carHeadRealArriveTimeCenter"] = at
        payload.update(
            changeReason="",
            amount=[row["headAmount"], row["trunkAmount"]],
            viaStationCode=station,
            clockTime=at,
            clockType=2,
            restockTag=False,
        )
        ack = self.request(PUNCH, payload, write=True)
        write_ref = self.refs[-1]
        # The server owns the final time; it can differ from the dialog time.
        try:
            actual = find_via(ack, station)
            actual_time = text(actual.get("carHeadRealArriveTimeCenter"))
            timestamp(actual_time)
            fresh = self.request(DETAIL, {"id": task_id})
            verified = find_via(fresh, station)
            if (
                task_identity(ack) != (task_id, task_number)
                or task_identity(fresh) != (task_id, task_number)
                or actual["id"] != selected["id"]
                or verified["id"] != selected["id"]
                or verified.get("carHeadRealArriveTimeCenter") != actual_time
            ):
                fail("WRITE_OUTCOME_UNKNOWN")
            write_index = ack["taskViaList"].index(actual)
            read_index = fresh["taskViaList"].index(verified)
            self.proofs.append(
                {
                    "write_ref": write_ref,
                    "read_ref": self.refs[-1],
                    "matches": [
                        {
                            "write_path": "/response/data/" + field,
                            "read_path": "/response/data/" + field,
                            "expected": value,
                        }
                        for field, value in (("id", task_id), ("taskNumber", task_number))
                    ]
                    + [
                        {
                            "write_path": f"/response/data/taskViaList/{write_index}/{field}",
                            "read_path": f"/response/data/taskViaList/{read_index}/{field}",
                            "expected": value,
                        }
                        for field, value in (("id", selected["id"]), ("carHeadRealArriveTimeCenter", actual_time))
                    ],
                }
            )
            return {
                "task_id": task_id,
                "task_number": task_number,
                "confirmed": True,
                "arrival_time": actual_time,
                "status": fresh["taskStatus"],
            }
        except (RuntimeError, KeyError, TypeError, AttributeError):
            fail("WRITE_OUTCOME_UNKNOWN")
