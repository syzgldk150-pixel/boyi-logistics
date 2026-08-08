# -*- coding: utf-8 -*-
from __future__ import annotations

import re
from typing import List

SERVICE_HALL_TOKEN = "服务大厅"
TRAILING_NOTE_RE = re.compile(r"\s*[\(（【\[].*?[\)）】\]]\s*$")
WHITESPACE_RE = re.compile(r"\s+")
AREA_PREFIX_RE = re.compile(
    r"^[^\d,，（\(\)）]{1,24}?(?:新区|开发区|经济区|工业区|园区|街道|镇|乡|村|社区|办事处)"
)


def normalize_address_text(address: str) -> str:
    text = str(address or "").strip()
    if not text:
        return ""
    return WHITESPACE_RE.sub("", text)


def is_service_hall_name(name: str) -> bool:
    return SERVICE_HALL_TOKEN in normalize_address_text(name)


def strip_trailing_poi_notes(address: str) -> str:
    text = normalize_address_text(address)
    while text:
        updated = TRAILING_NOTE_RE.sub("", text).strip()
        if updated == text:
            break
        text = updated
    return text


def _display_city(province: str, city: str) -> str:
    province = normalize_address_text(province)
    city = normalize_address_text(city)
    if not city or city in {province, "市辖区"}:
        return ""
    return city


def _admin_tokens(province: str, city: str, district: str) -> List[str]:
    tokens: List[str] = []
    for token in (
        normalize_address_text(province),
        _display_city(province, city),
        normalize_address_text(district),
    ):
        if token and token not in tokens:
            tokens.append(token)
    return tokens


def _strip_leading_admin_tokens(address: str, province: str, city: str, district: str) -> str:
    body = normalize_address_text(address)
    tokens = _admin_tokens(province, city, district)
    changed = True
    while body and changed:
        changed = False
        for token in tokens:
            if token and body.startswith(token):
                body = body[len(token) :].lstrip()
                changed = True
    return body


def build_admin_prefixed_address(address: str, province: str, city: str, district: str) -> str:
    base = normalize_address_text(address)
    if not base:
        return ""
    prefix = "".join(_admin_tokens(province, city, district))
    if not prefix:
        return base
    body = _strip_leading_admin_tokens(base, province, city, district)
    return normalize_address_text(f"{prefix}{body}")


def strip_leading_area_descriptors(address: str) -> str:
    text = normalize_address_text(address)
    while text:
        updated = AREA_PREFIX_RE.sub("", text).strip()
        if updated == text:
            break
        text = updated
    return text


def refine_street_body(address: str, province: str = "", city: str = "", district: str = "") -> str:
    body = _strip_leading_admin_tokens(address, province, city, district)
    body = strip_leading_area_descriptors(body)
    body = _strip_leading_admin_tokens(body, province, city, district)
    return normalize_address_text(body)


def generate_address_candidates(address: str, province: str = "", city: str = "", district: str = "") -> List[str]:
    candidates: List[str] = []

    def _append(value: str) -> None:
        normalized = normalize_address_text(value)
        if normalized and normalized not in candidates:
            candidates.append(normalized)

    base = normalize_address_text(address)
    cleaned = strip_trailing_poi_notes(base)

    _append(cleaned)
    _append(build_admin_prefixed_address(cleaned, province, city, district))
    _append(refine_street_body(cleaned, province, city, district))
    _append(base)
    _append(build_admin_prefixed_address(base, province, city, district))
    return candidates


def generate_refined_address_candidates(
    address: str,
    *,
    province: str = "",
    city: str = "",
    district: str = "",
    town: str = "",
) -> List[str]:
    candidates: List[str] = []

    def _append(value: str) -> None:
        normalized = normalize_address_text(value)
        if normalized and normalized not in candidates:
            candidates.append(normalized)

    body = refine_street_body(address, province=province, city=city, district=district)
    if not body:
        return candidates

    _append(build_admin_prefixed_address(body, province, city, district))
    if town:
        _append(build_admin_prefixed_address(f"{town}{body}", province, city, district))
    return candidates
