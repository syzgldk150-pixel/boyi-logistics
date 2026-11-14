"""Real MySQL: a share refresh archives each platform picture once and lists only the customer's."""
from __future__ import annotations

import io
import json
from datetime import datetime, timedelta

import pytest
from PIL import Image

from agent.receipt_share_refresh import ReceiptShareRefresher, normalize_image, ImageRejected
from shared.receipt_nas_store import NasStoreError
from tests.mysql_isolated_fixtures import repository, pytestmark  # noqa: F401

NOW = datetime(2026, 11, 13, 12, 0, 0)


def _jpeg(color) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (40, 30), color).save(output, format="JPEG")
    return output.getvalue()


class _Nas:
    def __init__(self):
        self.files, self.down = {}, False

    def put_if_absent(self, path, content):
        if self.down:
            raise NasStoreError("NAS_UNAVAILABLE", "暂时连不上 NAS。")
        if path in self.files:
            return False
        self.files[path] = bytes(content)
        return True


class _Platform:
    def __init__(self):
        self.photos, self.reads, self.fetches, self.fail = {}, [], [], set()
        self.images = {}

    def read(self, platform, waybill_no):
        self.reads.append((platform, waybill_no))
        if (platform, waybill_no) in self.fail:
            raise RuntimeError("login required")
        return self.photos.get((platform, waybill_no), {"sign_photos": [], "receipt_photos": []})

    def fetch(self, platform, kind, url):
        self.fetches.append(url)
        return self.images[url]


def _insert(connection, table, values):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COLUMN_NAME AS name, DATA_TYPE AS type FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
            "AND TABLE_NAME = %s AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL AND EXTRA NOT LIKE %s", (table, "%auto_increment%"))
        row = {}
        for column in cursor.fetchall():
            row[column["name"]] = NOW if column["type"] in {"datetime", "timestamp"} else (0 if column["type"] in {"int", "bigint", "tinyint", "decimal"} else "")
        row.update(values)
        names = ", ".join(row)
        cursor.execute(f"INSERT INTO {table} ({names}) VALUES ({', '.join(['%s'] * len(row))})", tuple(row.values()))
        return cursor.lastrowid


@pytest.fixture
def world(repository):
    connection = repository._connection_factory()
    with connection.cursor() as cursor:
        for table in ("receipt_share_items", "receipt_share_images", "receipt_share_waybill_state", "receipt_shares",
                      "receipt_share_customers"):
            cursor.execute(f"DELETE FROM {table}")
        cursor.execute("DELETE FROM ronghui_waybills WHERE waybill_no LIKE 'RS%'")
        cursor.execute("DELETE FROM yunda_waybills WHERE waybill_no LIKE 'YS%'")
        cursor.execute("DELETE FROM boyi_waybills WHERE waybill_no LIKE 'BYS%'")
    for number, sender, status, day in (("RS001", "勇胜", "signed", "2026-11-10"), ("RS002", "勇胜", "in_transit", "2026-11-12"),
                                        ("RS003", "别家公司", "signed", "2026-11-11"), ("RS004", "勇胜", "cancelled", "2026-11-09")):
        _insert(connection, "ronghui_waybills", {"waybill_no": number, "sender_name": sender, "status": status,
                                                 "open_date": day, "source": "ronghui",
                                                 "remark": "送货上门签回单 出货单1001-2600000001" if number == "RS001" else ""})
    _insert(connection, "yunda_waybills", {"waybill_no": "YS001", "sender_name": "勇胜", "status": "signed",
                                           "open_date": "2026/11/08", "source": "yunda", "remark": "1001 2600000001/订单S1002-2600000009"})
    _insert(connection, "boyi_waybills", {"waybill_no": "BYS01", "sender_name": "勇胜二部", "status": "in_transit",
                                          "open_date": "2026-11-07", "source": "manual", "linked_platform": "ronghui",
                                          "linked_waybill_no": "RS900"})
    customer = _insert(connection, "receipt_share_customers", {
        "code": "C001", "name": "勇胜贸易有限公司", "short_name": "勇胜",
        "match_json": json.dumps({"mode": "any", "conditions": [{"field": "sender_name", "op": "contains", "value": "勇胜"}]})})
    share = _insert(connection, "receipt_shares", {
        "customer_id": customer, "request_id": "00000000-0000-4000-8000-000000000001", "token_sha256": "a" * 64,
        "image_types_json": json.dumps(["sign", "receipt"]), "naming_template": "{客户简称}-{运单号}-{单据类型}.jpg"})
    connection.commit()
    platform, nas = _Platform(), _Nas()
    platform.photos = {
        ("ronghui", "RS001"): {"sign_photos": ["https://t/s1"], "receipt_photos": ["https://t/r1", "https://t/r2"]},
        ("yunda", "YS001"): {"sign_photos": ["https://y/s1"], "receipt_photos": []},
        ("ronghui", "RS900"): {"sign_photos": ["https://t/s9"], "receipt_photos": []},
        ("ronghui", "RS003"): {"sign_photos": ["https://t/other"], "receipt_photos": []},
    }
    platform.images = {url: _jpeg((index * 20, 10, 10)) for index, url in enumerate(
        ["https://t/s1", "https://t/r1", "https://t/r2", "https://y/s1", "https://t/s9", "https://t/other", "https://t/r3"])}
    clock = {"now": NOW}
    refresher = ReceiptShareRefresher(connect=repository._connection_factory, nas=nas, read_photos=platform.read,
                                      fetch_image=platform.fetch, now=lambda: clock["now"])
    yield type("World", (), {"connection": connection, "share": share, "platform": platform, "nas": nas,
                             "refresher": refresher, "clock": clock})
    connection.close()


def _items(world):
    world.connection.commit()
    with world.connection.cursor() as cursor:
        cursor.execute("SELECT platform, waybill_no, kind, seq FROM receipt_share_items WHERE share_id = %s "
                       "ORDER BY waybill_no, kind, seq", (world.share,))
        return [(row["platform"], row["waybill_no"], row["kind"], row["seq"]) for row in cursor.fetchall()]


def _share_row(world):
    world.connection.commit()
    with world.connection.cursor() as cursor:
        cursor.execute("SELECT refresh_state, refresh_message, last_refreshed_at FROM receipt_shares WHERE id = %s", (world.share,))
        return cursor.fetchone()


def test_refresh_archives_and_lists_only_the_customers_signed_waybills(world):
    outcome = world.refresher.refresh()[0]
    assert (outcome.state, outcome.waybills, outcome.new_images, outcome.items) == ("ok", 4, 5, 5)
    # Other customers, unsigned and cancelled waybills are never asked for or shown.
    assert sorted(world.platform.reads) == [("ronghui", "RS001"), ("ronghui", "RS900"), ("yunda", "YS001")]
    assert _items(world) == [("boyi", "BYS01", "sign", 1), ("ronghui", "RS001", "receipt", 1), ("ronghui", "RS001", "receipt", 2),
                             ("ronghui", "RS001", "sign", 1), ("yunda", "YS001", "sign", 1)]
    assert sorted(path.split("/")[0:3] for path in world.nas.files)[0] == ["ronghui", "2026-11", "RS001"]
    assert all(path.startswith(("ronghui/2026-11/", "yunda/2026-11/")) for path in world.nas.files) and len(world.nas.files) == 5
    row = _share_row(world)
    assert row["refresh_state"] == "ok" and row["last_refreshed_at"] == NOW and "图片 5 张" in row["refresh_message"]


def test_items_carry_the_delivery_note_number_from_the_remark(world):
    world.refresher.refresh()
    with world.connection.cursor() as cursor:
        cursor.execute("SELECT waybill_no, shipment_no, kind, seq, shipment_seq, shipment_of FROM receipt_share_items "
                       "WHERE share_id = %s ORDER BY waybill_no, kind, seq", (world.share,))
        rows = [tuple(row.values()) for row in cursor.fetchall()]
    assert rows == [("BYS01", "", "sign", 1, 1, 1),
                    ("RS001", "1001-2600000001", "receipt", 1, 1, 2), ("RS001", "1001-2600000001", "receipt", 2, 2, 2),
                    # two waybills of one delivery note: numbered across both, waybill order
                    ("RS001", "1001-2600000001", "sign", 1, 1, 2), ("YS001", "1001-2600000001", "sign", 1, 2, 2)]


def test_second_refresh_reads_nothing_again_until_a_recheck_is_due_then_adds_only_new_pictures(world):
    world.refresher.refresh()
    world.platform.reads.clear(); world.platform.fetches.clear()
    world.refresher.refresh()
    assert world.platform.reads == [] and world.platform.fetches == []  # complete or checked recently
    world.platform.photos[("yunda", "YS001")]["receipt_photos"] = ["https://t/r3"]
    world.clock["now"] = NOW + timedelta(hours=7)
    outcome = world.refresher.refresh()[0]
    # Only waybills still missing a kind are asked again; pictures already archived are not fetched twice.
    assert sorted(world.platform.reads) == [("ronghui", "RS900"), ("yunda", "YS001")]
    assert world.platform.fetches == ["https://t/r3"] and outcome.new_images == 1 and outcome.items == 6


def test_platform_failure_keeps_the_link_and_is_retried(world):
    world.platform.fail.add(("ronghui", "RS001"))
    outcome = world.refresher.refresh()[0]
    assert outcome.state == "ok" and outcome.failures == ["平台读取失败（RuntimeError）"] and outcome.items == 2
    assert _share_row(world)["refresh_state"] == "partial"
    world.platform.fail.clear()
    world.clock["now"] = NOW + timedelta(hours=7)
    assert world.refresher.refresh()[0].items == 5


def test_nas_outage_stops_platform_reads_and_keeps_previous_items(world):
    world.refresher.refresh()
    before = _items(world)
    world.platform.photos[("yunda", "YS001")]["receipt_photos"] = ["https://t/r3"]
    world.clock["now"] = NOW + timedelta(hours=7)
    world.nas.down = True
    world.platform.reads.clear()
    outcome = world.refresher.refresh()[0]
    assert outcome.state == "nas_unavailable" and len(world.platform.reads) == 1  # stops at the first failed store
    assert _items(world) == before and _share_row(world)["refresh_state"] == "error"
    world.nas.down = False
    world.clock["now"] = NOW + timedelta(hours=8)
    assert world.refresher.refresh()[0].items == 6  # nothing was marked as read while the NAS was down


def test_budget_scope_and_image_types(world):
    outcome = world.refresher.refresh(check_budget=1)[0]
    assert outcome.checked == 1 and outcome.backlog == 2 and "待读取" in outcome.message()
    world.refresher.refresh()
    with world.connection.cursor() as cursor:
        cursor.execute("UPDATE receipt_shares SET image_types_json = %s, scope = 'by_date', date_from = '2026-11-09', "
                       "date_to = '2026-11-30' WHERE id = %s", (json.dumps(["receipt"]), world.share))
    world.connection.commit()
    world.refresher.refresh()
    assert _items(world) == [("ronghui", "RS001", "receipt", 1), ("ronghui", "RS001", "receipt", 2)]
    with world.connection.cursor() as cursor:
        cursor.execute("UPDATE receipt_shares SET status = 'disabled' WHERE id = %s", (world.share,))
    world.connection.commit()
    assert world.refresher.refresh() == []  # a disabled link is not refreshed


def test_normalize_image_accepts_pictures_only():
    assert normalize_image(_jpeg((1, 2, 3)))[1:] == ("image/jpeg", "jpg")
    webp = io.BytesIO()
    Image.new("RGB", (40, 30), (1, 2, 3)).save(webp, format="WEBP")
    converted = normalize_image(webp.getvalue())
    assert converted[1:] == ("image/jpeg", "jpg") and converted[0].startswith(b"\xff\xd8")
    for bad in (b"", b"<html>login</html>", b"\xff\xd8 not really"):
        with pytest.raises(ImageRejected):
            normalize_image(bad)
