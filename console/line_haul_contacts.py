import csv
import re
from io import StringIO
from typing import Any


PHONE_RE = re.compile(r"(?<!\d)(?:1[3-9]\d{9}|0\d{2,3}[-\s]?\d{7,8}|\d{7,8})(?!\d)")
SEPARATOR_RE = re.compile(r"[\s,，/、;；|]+")
CONTACT_WORDS = {"查询", "查货", "负责人", "专线负责人", "专线", "电话"}


def normalize_cell(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\u3000", " ")).strip()


def normalize_phone_numbers(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    matches = [re.sub(r"\s+", "", match.group(0)) for match in PHONE_RE.finditer(text)]
    if matches:
        return " / ".join(dict.fromkeys(matches))
    parts = [part.strip() for part in re.split(r"[/、,，;；\s]+", text) if part.strip()]
    return " / ".join(dict.fromkeys(parts))


def parse_line_haul_source_text(source_text: Any) -> dict[str, str]:
    raw = normalize_cell(source_text)
    matches = list(PHONE_RE.finditer(raw))
    phone_numbers = " / ".join(dict.fromkeys(re.sub(r"\s+", "", match.group(0)) for match in matches))
    if not matches:
        return {
            "address": raw,
            "contact_name": "",
            "phone_numbers": "",
            "remark": "",
            "source_text": raw,
        }

    address = raw[: matches[0].start()]
    address = SEPARATOR_RE.sub(" ", address).strip(" -:：")

    tail_parts: list[str] = []
    cursor = matches[0].end()
    for match in matches[1:]:
        fragment = raw[cursor : match.start()]
        if fragment.strip():
            tail_parts.append(fragment)
        cursor = match.end()
    if raw[cursor:].strip():
        tail_parts.append(raw[cursor:])

    contact_name = ""
    remarks: list[str] = []
    for fragment in tail_parts:
        for segment in _split_tail_fragment(fragment):
            if not segment:
                continue
            contact, remark = _split_contact_segment(segment)
            if contact and not contact_name:
                contact_name = contact
            if remark:
                remarks.append(remark)
            elif not contact:
                remarks.append(segment)

    return {
        "address": address,
        "contact_name": contact_name,
        "phone_numbers": phone_numbers,
        "remark": " / ".join(dict.fromkeys(remarks)),
        "source_text": raw,
    }


def parse_line_haul_paste(text: Any) -> dict[str, Any]:
    reader = csv.reader(StringIO(str(text or "")), delimiter="\t")
    rows: list[dict[str, str]] = []
    last_company = ""
    skipped_empty = 0

    for index, raw_row in enumerate(reader, start=1):
        columns = [normalize_cell(cell) for cell in raw_row]
        if not any(columns):
            skipped_empty += 1
            continue
        while len(columns) < 3:
            columns.append("")
        company_name = columns[0] or last_company
        service_area = columns[1]
        source_text = normalize_cell(" ".join(columns[2:]))
        if columns[0]:
            last_company = columns[0]
        if not company_name or not service_area or not source_text:
            skipped_empty += 1
            continue
        parsed = parse_line_haul_source_text(source_text)
        parsed.update(
            {
                "company_name": company_name,
                "service_area": service_area,
                "sort_order": str(index * 10),
            }
        )
        rows.append(parsed)

    return {
        "rows": rows,
        "skipped_empty": skipped_empty,
        "total_rows": len(rows) + skipped_empty,
    }


def _split_tail_fragment(fragment: str) -> list[str]:
    cleaned = fragment.replace("：", ":")
    if ":" in cleaned:
        left, right = cleaned.split(":", 1)
        values = [left.strip(), right.strip()]
    else:
        values = [cleaned.strip()]
    parts: list[str] = []
    for value in values:
        parts.extend(part.strip(" -:：") for part in SEPARATOR_RE.split(value) if part.strip(" -:："))
    return parts


def _split_contact_segment(segment: str) -> tuple[str, str]:
    value = segment.strip(" -:：")
    if not value:
        return "", ""
    if value in CONTACT_WORDS:
        return "", value
    suffix_match = re.fullmatch(r"([\u4e00-\u9fff]{1,4})(查询|查货|负责人|专线负责人)", value)
    if suffix_match:
        return suffix_match.group(1), suffix_match.group(2)
    if re.fullmatch(r"[\u4e00-\u9fff]{1,4}", value):
        return value, ""
    return "", value
