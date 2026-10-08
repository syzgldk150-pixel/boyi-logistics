"""R7 task HTTP primitives verified against the original page on 2026-10-08."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from agent.tms_runtime.scripts.r7_login_manager import R7SSOAuth
from agent.tms_runtime.sso_session_persistence import default_sso_state_path

ORIGIN = "https://r7.ronghuiwl.com"
PAGE = "/gateway/tms/public/lineTask/pageGet"
DETAIL = "/gateway/tms/public/lineTask/getById"
PUNCH = "/gateway/tms/public/lineTask/saveCenterPunch"
AUTH = "/gateway/public/aurora/auth"
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
    def __init__(self, session):
        self.session = session

    @classmethod
    def from_account(cls, account_id):
        if not isinstance(account_id, str) or not account_id.strip():
            fail("BLOCKED_LOGIN")
        auth = R7SSOAuth(config_path="", state_path=default_sso_state_path(account_id))
        if not auth.restore_persisted_session(
            validate=False, validator=auth._verify_authenticated, attach_bearer=False
        ):
            auth.session.close()
            fail("BLOCKED_LOGIN")
        auth.session.headers.pop("Authorization", None)
        auth.session.headers.update(
            {
                "aurora-token": auth.last_token,
                "Origin": ORIGIN,
                "Referer": ORIGIN + "/operateManage/vehicleSchedule/vehicleRegular",
            }
        )
        return cls(auth.session)

    def close(self):
        self.session.close()

    def request(self, path, body, *, write=False):
        try:
            response = self.session.post(ORIGIN + path, json=body, timeout=(10, 25), allow_redirects=False)
        except requests.RequestException:
            fail("WRITE_OUTCOME_UNKNOWN" if write else "R7_REQUEST_FAILED")
        if response.status_code in {301, 302, 303, 307, 308, 401, 403}:
            fail("WRITE_OUTCOME_UNKNOWN" if write else "BLOCKED_LOGIN")
        if response.status_code != 200:
            fail("WRITE_OUTCOME_UNKNOWN" if write else "R7_REQUEST_FAILED")
        try:
            result = response.json()
        except ValueError:
            fail("WRITE_OUTCOME_UNKNOWN" if write else "R7_SOURCE_INVALID")
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
        user = self.request(AUTH, {})
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
        now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
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
            return {
                "task_id": task_id,
                "task_number": task_number,
                "confirmed": True,
                "arrival_time": actual_time,
                "status": fresh["taskStatus"],
            }
        except (R7TaskError, KeyError, TypeError, AttributeError):
            fail("WRITE_OUTCOME_UNKNOWN")
