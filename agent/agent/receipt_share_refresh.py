"""Refresh customer share links: read platform photos, archive them, list them per link.

For every active share: find the customer's waybills by its sender rules, ask
the platform for the sign-for and receipt photos of waybills that may still
gain pictures, store new pictures once on the NAS, then replace what the link
shows in one transaction. Nothing is written to the platforms and no image is
kept on the server disk.

All outside effects are injected by the composition root: the database
connection, the NAS store, the platform photo reader and the image fetcher.
A platform or NAS failure for one waybill is recorded and retried on a later
run; the link keeps showing what it showed before.
"""
from __future__ import annotations

import hashlib
import io
import logging
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping

from shared import receipt_share_repository as repository
from shared.receipt_nas_store import NasStoreError
from shared.receipt_share import nas_image_path

logger = logging.getLogger("agent.receipt_share")

PHOTO_KINDS = ("sign", "receipt")
# A waybill missing a kind of picture is asked again after this long, while it is recent.
RECHECK_AFTER = timedelta(hours=6)
RECHECK_WHILE_OPENED_WITHIN_DAYS = 90
# Platform reads per run; the rest waits for the next run so one run stays bounded.
DEFAULT_CHECK_BUDGET = 120
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_SIDE = 4000


class ImageRejected(ValueError):
    """The fetched bytes are not a usable picture."""


def normalize_image(payload: bytes) -> tuple[bytes, str, str]:
    """Return ``(bytes, mime, extension)``: JPEG and PNG as received, anything else as JPEG."""

    if not payload or len(payload) > MAX_IMAGE_BYTES:
        raise ImageRejected("empty or oversized image")
    from PIL import Image  # Agent dependency; imported here so rule tests need no imaging library

    try:
        with Image.open(io.BytesIO(payload)) as image:
            image.verify()
        with Image.open(io.BytesIO(payload)) as image:
            image_format = (image.format or "").upper()
            width, height = image.size
            if width < 8 or height < 8 or width > 20000 or height > 20000:
                raise ImageRejected("unreasonable image size")
            if image_format == "JPEG" and max(width, height) <= MAX_IMAGE_SIDE:
                return payload, "image/jpeg", "jpg"
            if image_format == "PNG" and max(width, height) <= MAX_IMAGE_SIDE:
                return payload, "image/png", "png"
            converted = image.convert("RGB")
            converted.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
            output = io.BytesIO()
            converted.save(output, format="JPEG", quality=90)
            return output.getvalue(), "image/jpeg", "jpg"
    except ImageRejected:
        raise
    except Exception as exc:  # noqa: BLE001 - any decoder failure means "not a picture"
        raise ImageRejected(type(exc).__name__) from None


@dataclass
class RefreshOutcome:
    share_id: int
    state: str = "ok"
    waybills: int = 0
    checked: int = 0
    new_images: int = 0
    items: int = 0
    backlog: int = 0
    failures: list[str] = field(default_factory=list)

    def message(self) -> str:
        parts = [f"运单 {self.waybills} 张", f"图片 {self.items} 张"]
        if self.new_images:
            parts.append(f"本次新增 {self.new_images} 张")
        if self.backlog:
            parts.append(f"还有 {self.backlog} 张运单待读取")
        if self.failures:
            parts.append("未完成：" + "；".join(self.failures[:3]))
        return "，".join(parts)


class ReceiptShareRefresher:
    def __init__(
        self,
        *,
        connect: Callable[[], Any],
        nas: Any,
        read_photos: Callable[[str, str], Mapping[str, Any]],
        fetch_image: Callable[[str, str, str], bytes],
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._connect = connect
        self._nas = nas
        self._read_photos = read_photos
        self._fetch_image = fetch_image
        self._now = now

    def refresh(self, *, share_id: int | None = None, check_budget: int = DEFAULT_CHECK_BUDGET) -> list[RefreshOutcome]:
        with closing(self._connect()) as connection:
            shares = repository.list_refreshable_shares(connection, now=self._now(), share_id=share_id)
            connection.commit()
        outcomes = []
        budget = max(0, int(check_budget))
        nas_down = False
        for share in shares:
            outcome = self._refresh_one(share, budget, skip_platform=nas_down)
            budget = max(0, budget - outcome.checked)
            nas_down = nas_down or outcome.state == "nas_unavailable"
            outcomes.append(outcome)
        return outcomes

    def _refresh_one(self, share: Mapping[str, Any], budget: int, *, skip_platform: bool) -> RefreshOutcome:
        outcome = RefreshOutcome(share_id=int(share["id"]))
        try:
            with closing(self._connect()) as connection:
                waybills = repository.matching_waybills(connection, share)
                outcome.waybills = len(waybills)
                targets = self._targets(waybills)
                due = self._due(connection, targets)
                connection.commit()
                if skip_platform:
                    outcome.state, outcome.backlog = "nas_unavailable", len(due)
                    outcome.failures.append("NAS 暂时连不上")
                else:
                    for key in due[:budget]:
                        if not self._check_waybill(connection, key, targets[key], outcome):
                            break  # NAS is down: stop asking the platforms for pictures we cannot store
                    outcome.backlog = max(0, len(due) - outcome.checked)
                items = self._items(connection, share, waybills)
                repository.replace_share_items(connection, outcome.share_id, items)
                outcome.items = len(items)
                state = "ok" if outcome.state == "ok" and not outcome.failures else (
                    "partial" if outcome.state == "ok" else "error")
                repository.mark_share_refresh(connection, outcome.share_id, state=state,
                                              message=outcome.message(), now=self._now())
                connection.commit()
        except Exception as exc:  # noqa: BLE001 - one share's failure must not stop the others
            logger.warning("Receipt share refresh failed share=%s error=%s", outcome.share_id, type(exc).__name__)
            outcome.state = "error"
            outcome.failures.append(f"刷新失败（{type(exc).__name__}）")
            self._record_failure(outcome)
        return outcome

    def _record_failure(self, outcome: RefreshOutcome) -> None:
        try:
            with closing(self._connect()) as connection:
                repository.mark_share_refresh(connection, outcome.share_id, state="error",
                                              message=outcome.message(), now=self._now())
                connection.commit()
        except Exception:  # noqa: BLE001 - the next run reports again
            logger.warning("Receipt share refresh state not saved share=%s", outcome.share_id)

    @staticmethod
    def _targets(waybills: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
        """Platform waybills to read pictures from, with the newest matching row's facts."""

        targets: dict[tuple[str, str], dict[str, Any]] = {}
        for row in waybills:
            if row["photo_platform"] and row["photo_waybill_no"]:
                targets.setdefault((row["photo_platform"], row["photo_waybill_no"]), row)
        return targets

    def _due(self, connection, targets: Mapping[tuple[str, str], Mapping[str, Any]]) -> list[tuple[str, str]]:
        states = repository.waybill_states(connection, targets)
        now = self._now()
        recent_day = (now - timedelta(days=RECHECK_WHILE_OPENED_WITHIN_DAYS)).strftime("%Y-%m-%d")
        due = []
        for key, row in targets.items():
            # A platform waybill has pictures only once signed; a Boyi order's own row does not carry
            # the linked platform's status, so it is asked on the normal recheck rhythm.
            if row["platform"] != "boyi" and not row["signed"]:
                continue
            state = states.get(key)
            if state is None:
                due.append(key)
            elif (int(state["sign_count"]) == 0 or int(state["receipt_count"]) == 0) \
                    and row["open_day"] >= recent_day and now - state["last_checked_at"] >= RECHECK_AFTER:
                due.append(key)
        return due  # newest first, as matching_waybills returned them

    def _check_waybill(self, connection, key: tuple[str, str], row: Mapping[str, Any],
                       outcome: RefreshOutcome) -> bool:
        platform, waybill_no = key
        outcome.checked += 1
        counts = {"sign": 0, "receipt": 0}
        error = ""
        nas_ok = True
        try:
            photos = self._read_photos(platform, waybill_no)
            urls = {"sign": list(photos.get("sign_photos") or []), "receipt": list(photos.get("receipt_photos") or [])}
            error = str(photos.get("receipt_error") or "")
            known = repository.known_image_sources(connection, platform, waybill_no)
            for kind in PHOTO_KINDS:
                existing = sum(1 for known_kind, _key in known if known_kind == kind)
                counts[kind] = existing
                for url in urls[kind]:
                    source_key = hashlib.sha256(str(url).encode("utf-8")).hexdigest()
                    if (kind, source_key) in known:
                        continue
                    try:
                        content, mime_type, extension = normalize_image(self._fetch_image(platform, kind, str(url)))
                    except ImageRejected:
                        error = error or "有图片未能读取"
                        continue
                    digest = hashlib.sha256(content).hexdigest()
                    month = row["open_day"][:7] if len(row["open_day"]) >= 7 else self._now().strftime("%Y-%m")
                    path = nas_image_path(platform, waybill_no, kind, digest, extension, month)
                    self._nas.put_if_absent(path, content)
                    counts[kind] += 1
                    repository.insert_image(
                        connection, platform=platform, waybill_no=waybill_no, kind=kind, source_key=source_key,
                        seq=counts[kind], content_sha256=digest, nas_path=path, mime_type=mime_type,
                        file_size=len(content), doc_date=row["open_day"] or None)
                    known.add((kind, source_key))
                    outcome.new_images += 1
        except NasStoreError as exc:
            nas_ok = False
            outcome.state = "nas_unavailable"
            error = f"NAS 不可用（{exc.code}）"
            outcome.failures.append("NAS 暂时连不上")
        except Exception as exc:  # noqa: BLE001 - platform login, network or page errors: retry on a later run
            error = f"平台读取失败（{type(exc).__name__}）"
            if error not in outcome.failures:
                outcome.failures.append(error)
        if nas_ok:
            # A read that failed leaves the counts at what is already archived, so it is asked again.
            repository.record_waybill_check(connection, platform, waybill_no, sign_count=counts["sign"],
                                            receipt_count=counts["receipt"], error=error, now=self._now())
        connection.commit()
        return nas_ok

    def _items(self, connection, share: Mapping[str, Any], waybills: list[dict[str, Any]]) -> list[dict[str, Any]]:
        kinds = set(share["image_types"])
        rows = [row for row in waybills if row["photo_platform"] and row["photo_waybill_no"]]
        if share["scope"] == "approved":
            approved: set[tuple[str, str]] = set()
            for platform in {row["photo_platform"] for row in rows}:
                numbers = [row["photo_waybill_no"] for row in rows if row["photo_platform"] == platform]
                approved.update((platform, number)
                                for number in repository.approved_receipt_waybills(connection, platform, numbers))
            rows = [row for row in rows if (row["photo_platform"], row["photo_waybill_no"]) in approved]
        images = repository.images_of_waybills(connection, {(row["photo_platform"], row["photo_waybill_no"]) for row in rows})
        items, seen = [], set()
        for row in rows:
            for image in images.get((row["photo_platform"], row["photo_waybill_no"]), []):
                if image["kind"] not in kinds or image["id"] in seen:
                    continue
                seen.add(image["id"])
                # The customer sees the number of the waybill they know (the Boyi number for a linked order).
                items.append({"image_id": image["id"], "platform": row["platform"], "waybill_no": row["waybill_no"],
                              "shipment_no": row.get("shipment_no") or "", "kind": image["kind"], "seq": image["seq"],
                              "doc_date": image["doc_date"] or row["open_day"] or None})
        return number_by_shipment(items)


def number_by_shipment(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Number the pictures of one kind under one delivery note (the waybill number when there is none).

    A file name made of {发货单号} without {运单号} uses these, so two waybills
    of the same delivery note never get the same name. The order is fixed
    (waybill, then picture), so names stay the same between refreshes.
    """

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in items:
        groups.setdefault((item["shipment_no"] or item["waybill_no"], item["kind"]), []).append(item)
    for members in groups.values():
        members.sort(key=lambda item: (item["waybill_no"], int(item["seq"]), int(item["image_id"])))
        for index, item in enumerate(members, start=1):
            item["shipment_seq"], item["shipment_of"] = index, len(members)
    return items
