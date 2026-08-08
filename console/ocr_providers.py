import base64
import json
import mimetypes
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from config import Settings

DELIVERY_METHOD_OPTIONS = ("送货", "自提")
PAYMENT_METHOD_OPTIONS = (
    "现付",
    "到付",
    "提付",
    "月结",
    "回单付",
    "欠付",
    "现结",
    "提货付款",
    "发货人付",
    "收货人付",
)
SECOND_PASS_BLOCKS = (
    ("header", ("waybill_no", "destination_site", "open_date")),
    ("contact", ("receiver_address", "sender_name", "sender_phone")),
    ("goods", ("goods_name_lines", "package_type_lines", "quantity_lines", "weight_volume")),
    ("charges", ("delivery_method", "freight_fee", "pickup_fee", "delivery_fee", "transfer_fee", "payment_method", "remark")),
)
SPECIAL_REGION_RERUN_FIELDS = ("waybill_no", "freight_fee")
ENABLE_TARGETED_PAGE_RERUN = False
SECOND_PASS_OVERRIDE_FIELDS = {
    "waybill_no",
    "destination_site",
    "open_date",
    "delivery_method",
    "freight_fee",
    "pickup_fee",
    "delivery_fee",
    "transfer_fee",
    "payment_method",
    "remark",
}
SECOND_PASS_IF_BETTER_FIELDS = {
    "receiver_address",
    "sender_name",
    "goods_name_lines",
    "package_type_lines",
    "quantity_lines",
    "weight_volume",
}


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)


class PlaceholderQwenProvider:
    name = "placeholder_qwen_ocr"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def extract_document(
        self,
        image_paths: Path | list[Path],
        template_spec: dict[str, Any],
        field_state: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        started_at = time.perf_counter()
        results = {}
        for field in template_spec["fields"]:
            results[field["name"]] = {
                "value": "",
                "confidence": 0.0,
                "source": self.name,
                "message": "Qwen-OCR API not configured yet.",
            }
        debug = {
            "provider": self.name,
            "mode": "placeholder",
            "model": self.settings.qwen_model,
            "image_paths": [str(path) for path in _normalize_image_paths(image_paths)],
            "timing": {
                "main_pass_ms": 0.0,
                "second_pass_ms": 0.0,
                "region_rerun_ms": 0.0,
                "postprocess_ms": 0.0,
                "total_ms": _elapsed_ms(started_at),
            },
        }
        return results, debug


class HttpJsonQwenProvider:
    name = "qwen_vl_ocr_gateway"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def extract_document(
        self,
        image_paths: Path | list[Path],
        template_spec: dict[str, Any],
        field_state: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        started_at = time.perf_counter()
        endpoint = _normalize_endpoint(self.settings.qwen_endpoint)
        if not endpoint:
            return PlaceholderQwenProvider(self.settings).extract_document(image_paths, template_spec, field_state)

        normalized_paths = _normalize_image_paths(image_paths)
        primary_paths = normalized_paths[-1:] if normalized_paths else []
        image_data_url_cache: dict[str, str] = {}
        payload, request_summary = _build_qwen_payload(
            image_paths=primary_paths,
            template_spec=template_spec,
            field_state=field_state,
            model=self.settings.qwen_model,
            image_data_url_cache=image_data_url_cache,
        )
        headers = {"Content-Type": "application/json"}
        if self.settings.qwen_api_key:
            headers["Authorization"] = f"Bearer {self.settings.qwen_api_key}"
        headers.update(self.settings.qwen_extra_headers)
        main_pass_started_at = time.perf_counter()
        response = _post_json(endpoint, payload, headers, self.settings.http_timeout_seconds)
        results, debug = _parse_qwen_response(
            response=response,
            template_spec=template_spec,
            request_summary=request_summary,
            default_confidence=self.settings.confidence_threshold,
        )
        main_pass_ms = _elapsed_ms(main_pass_started_at)
        second_pass_started_at = time.perf_counter()
        second_pass_results, second_pass_debug = self._extract_targeted_fields_from_pages(
            image_paths=primary_paths,
            template_spec=template_spec,
            current_results=results,
            endpoint=endpoint,
            headers=headers,
            image_data_url_cache=image_data_url_cache,
        )
        second_pass_ms = _elapsed_ms(second_pass_started_at)
        if second_pass_results:
            _merge_second_pass_results(results, second_pass_results)
        if second_pass_debug:
            debug["field_rerun"] = second_pass_debug
        region_rerun_started_at = time.perf_counter()
        region_results, region_debug = self._extract_targeted_fields_from_regions(
            image_paths=normalized_paths,
            template_spec=template_spec,
            current_results=results,
            endpoint=endpoint,
            headers=headers,
        )
        region_rerun_ms = _elapsed_ms(region_rerun_started_at)
        if region_results:
            _merge_region_results(results, region_results)
        if region_debug:
            debug["field_region_rerun"] = region_debug
        postprocess_started_at = time.perf_counter()
        _postprocess_results(results)
        postprocess_ms = _elapsed_ms(postprocess_started_at)
        debug["timing"] = {
            "main_pass_ms": main_pass_ms,
            "second_pass_ms": second_pass_ms,
            "region_rerun_ms": region_rerun_ms,
            "postprocess_ms": postprocess_ms,
            "total_ms": _elapsed_ms(started_at),
        }
        return results, debug

    def _extract_targeted_fields_from_pages(
        self,
        *,
        image_paths: list[Path],
        template_spec: dict[str, Any],
        current_results: dict[str, Any],
        endpoint: str,
        headers: dict[str, str],
        image_data_url_cache: dict[str, str],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not ENABLE_TARGETED_PAGE_RERUN:
            return {}, {"disabled": True, "reason": "speed_optimized"}
        if not image_paths:
            return {}, {}

        started_at = time.perf_counter()
        results: dict[str, Any] = {}
        debug: dict[str, Any] = {}
        field_map = {field["name"]: field for field in template_spec["fields"]}
        for block_name, field_names in SECOND_PASS_BLOCKS:
            selected_names = [
                name
                for name in field_names
                if name in field_map and _should_rerun_field(name, current_results)
            ]
            if not selected_names:
                continue

            block_started_at = time.perf_counter()
            payload, request_summary = _build_targeted_page_payload(
                image_paths=image_paths,
                field_specs=[field_map[name] for name in selected_names],
                current_results=current_results,
                model=self.settings.qwen_model,
                image_data_url_cache=image_data_url_cache,
            )
            response = _post_json(endpoint, payload, headers, self.settings.http_timeout_seconds)
            block_results, field_debug = _parse_subset_qwen_response(
                response=response,
                field_specs=[field_map[name] for name in selected_names],
                request_summary=request_summary,
                default_confidence=self.settings.confidence_threshold,
            )
            results.update(block_results)
            field_debug["elapsed_ms"] = _elapsed_ms(block_started_at)
            field_debug["field_count"] = len(selected_names)
            debug[block_name] = field_debug
        if debug:
            debug["_timing"] = {
                "total_ms": _elapsed_ms(started_at),
                "request_count": len([key for key in debug.keys() if not key.startswith("_")]),
            }
        return results, debug

    def _extract_targeted_fields_from_regions(
        self,
        *,
        image_paths: list[Path],
        template_spec: dict[str, Any],
        current_results: dict[str, Any],
        endpoint: str,
        headers: dict[str, str],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not image_paths:
            return {}, {}

        started_at = time.perf_counter()
        source_map = _build_image_source_map(image_paths)
        if not source_map:
            return {}, {}

        template_fields = {field["name"]: field for field in template_spec["fields"]}
        default_padding_ratio = float(template_spec.get("preprocess", {}).get("crop_padding_ratio", 0.006))
        results: dict[str, Any] = {}
        debug: dict[str, Any] = {}

        for field_name in _select_region_rerun_fields(template_spec, current_results):
            field_spec = template_fields.get(field_name)
            if not field_spec:
                continue
            field_started_at = time.perf_counter()
            crop_data_url, crop_meta = _build_field_crop_data_url(field_spec, source_map, default_padding_ratio)
            if not crop_data_url:
                continue
            payload, request_summary = _build_targeted_region_payload(
                field_spec=field_spec,
                crop_data_url=crop_data_url,
                model=self.settings.qwen_model,
            )
            response = _post_json(endpoint, payload, headers, self.settings.http_timeout_seconds)
            field_results, field_debug = _parse_subset_qwen_response(
                response=response,
                field_specs=[field_spec],
                request_summary=request_summary,
                default_confidence=self.settings.confidence_threshold,
            )
            if field_name in field_results:
                results[field_name] = field_results[field_name]
            field_debug["crop"] = crop_meta
            field_debug["elapsed_ms"] = _elapsed_ms(field_started_at)
            debug[field_name] = field_debug
        if debug:
            debug["_timing"] = {
                "total_ms": _elapsed_ms(started_at),
                "request_count": len([key for key in debug.keys() if not key.startswith("_")]),
            }
        return results, debug


def build_qwen_provider(settings: Settings) -> Any:
    if settings.qwen_provider_mode == "http_json":
        return HttpJsonQwenProvider(settings)
    return PlaceholderQwenProvider(settings)


def _normalize_endpoint(endpoint: str) -> str:
    value = endpoint.strip()
    if not value:
        return ""
    if value.endswith("/compatible-mode/v1"):
        return value + "/chat/completions"
    return value


def _build_qwen_payload(
    *,
    image_paths: list[Path],
    template_spec: dict[str, Any],
    field_state: dict[str, Any],
    model: str,
    image_data_url_cache: dict[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    min_pixels, max_pixels = _resolve_pixel_limits(model)
    field_instructions = [
        _build_field_instruction(field, field_state.get(field["name"], {}))
        for field in template_spec["fields"]
    ]
    output_schema = {field["name"]: "" for field in template_spec["fields"]}
    prompt = "\n".join(
        [
            "你是物流托运单结构化录入助手。请阅读整张托运单图片，只返回一个 JSON 对象。",
            "不要输出解释，不要输出 Markdown，不要输出额外字段。",
            "你会收到同一张托运单的 1 到 2 张图片。",
            "如果有两张图：第 1 张是旋转后的原图，保留完整版面和右上角红色运单号；第 2 张是对齐增强图，表格结构更稳定。",
            "除运单号外，其余字段优先参考对齐增强图；运单号优先参考原图右上角红色流水号。",
            "JSON 的键必须严格使用以下字段名：",
            *field_instructions,
            "",
            "请严格按这个 JSON 结构返回，键不要缺失：",
            json.dumps(output_schema, ensure_ascii=False),
            "",
            "输出规则：",
            "1. 所有字段都必须存在。",
            "2. 无法识别时填 JSON 空字符串 \"\"，不要填写“空字符串”“无”“未知”。",
            "3. goods_name_lines、package_type_lines、quantity_lines 如有多行，用换行符分隔。",
            "4. package_type_lines 只能写包装种类，不要混入件数。",
            "5. quantity_lines 只能写件数，不要混入包装种类。",
            "6. sender_name 只写发货人姓名，sender_phone 只写发货人电话。",
            "7. delivery_method 只能写送货或自提。",
            "8. freight_fee、pickup_fee、delivery_fee、transfer_fee 只保留金额或数字，不要带无关说明。",
            "9. payment_method 只能返回手写或勾选后的结算方式，不要返回印刷字“结算方式”。",
            "10. remark 只能返回备注内容，不要返回印刷字“备注”。",
            "11. waybill_no 只返回右上角红色运单号/流水号，不要返回发站编号。",
            "12. open_date 只返回右上角日期。",
            "13. destination_site 只返回“到达站:”后的站点名，不要抄到地址。",
            "14. 只返回 JSON 对象。",
        ]
    )
    content = [
        {
            "type": "text",
            "text": prompt,
        }
    ]
    for image_path in image_paths:
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": _image_to_data_url(image_path, image_data_url_cache),
                },
                "min_pixels": min_pixels,
                "max_pixels": max_pixels,
            }
        )

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": content,
            }
        ],
        "max_tokens": 1024,
        "temperature": 0.0,
    }
    request_summary = {
        "model": model,
        "endpoint_mode": "openai_compatible",
        "task": "prompted_json_extraction",
        "image_paths": [str(path) for path in image_paths],
        "field_names": [field["name"] for field in template_spec["fields"]],
        "prompt_preview": prompt[:2000],
        "image_transport": "base64_data_url_list",
        "min_pixels": min_pixels,
        "max_pixels": max_pixels,
    }
    return payload, request_summary


def _build_targeted_page_payload(
    *,
    image_paths: list[Path],
    field_specs: list[dict[str, Any]],
    current_results: dict[str, Any],
    model: str,
    image_data_url_cache: dict[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    min_pixels, max_pixels = _resolve_pixel_limits(model)
    output_schema = {field["name"]: "" for field in field_specs}
    field_lines = [
        _build_targeted_field_instruction(field, current_results.get(field["name"], {}))
        for field in field_specs
    ]
    prompt = "\n".join(
        [
            "你是物流托运单结构化复核助手。",
            "请重新阅读整张托运单，只针对下面这些容易识别错误的字段再识别一次。",
            "不要参考上一次可能错误的结果，不要猜，不要补全图片里看不见的内容。",
            "无法确认时必须返回 JSON 空字符串 \"\"。",
            "只返回一个 JSON 对象，不要解释。",
            *field_lines,
            "",
            "返回 JSON 结构：",
            json.dumps(output_schema, ensure_ascii=False),
        ]
    )
    content = [{"type": "text", "text": prompt}]
    for image_path in image_paths:
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": _image_to_data_url(image_path, image_data_url_cache),
                },
                "min_pixels": min_pixels,
                "max_pixels": max_pixels,
            }
        )
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": content,
            }
        ],
        "max_tokens": 768,
        "temperature": 0.0,
    }
    request_summary = {
        "model": model,
        "task": "targeted_page_rerun",
        "field_names": [field["name"] for field in field_specs],
        "image_paths": [str(path) for path in image_paths],
        "prompt_preview": prompt[:2000],
    }
    return payload, request_summary


def _build_targeted_region_payload(
    *,
    field_spec: dict[str, Any],
    crop_data_url: str,
    model: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    min_pixels, max_pixels = _resolve_pixel_limits(model)
    field_name = field_spec["name"]
    output_schema = {field_name: ""}
    prompt_lines = [
        "You are extracting one field from a logistics waybill crop.",
        "Return exactly one JSON object and nothing else.",
        f"If the crop is unclear, return {{\"{field_name}\": \"\"}}.",
    ]
    if field_name == "waybill_no":
        prompt_lines.extend(
            [
                "Field: waybill_no.",
                "Read only the printed serial number in the top-right corner.",
                "Return digits only.",
                "Do not return the departure code on the left.",
                "Do not return the date below the serial number.",
            ]
        )
    elif field_name == "freight_fee":
        prompt_lines.extend(
            [
                "Field: freight_fee.",
                "Read only the handwritten amount after the small '¥' label.",
                "Ignore the total amount on the left such as 90.41.",
                "Ignore the long diagonal stroke extending to the right of the amount.",
                "Return digits only, no currency symbol.",
            ]
        )
    elif field_name == "sender_phone":
        prompt_lines.extend(
            [
                "Field: sender_phone.",
                "Read only the sender phone on the left side next to 电话.",
                "Return digits only.",
                "Do not return the receiver phone on the right.",
                "Do not return any bottom service phone number.",
            ]
        )
    elif field_name == "sender_name":
        prompt_lines.extend(
            [
                "Field: sender_name.",
                "Read only the sender name after 发货人 on the upper-left side.",
                "If the box is blank, return an empty string.",
                "Do not return the goods name.",
            ]
        )
    elif field_name == "receiver_phone":
        prompt_lines.extend(
            [
                "Field: receiver_phone.",
                "Read only the receiver phone on the right side next to 电话.",
                "Return digits only.",
            ]
        )
    elif field_name == "receiver_name":
        prompt_lines.extend(
            [
                "Field: receiver_name.",
                "Read only the receiver name after 收货人.",
                "Do not include address or phone digits.",
            ]
        )
    elif field_name == "receiver_address":
        prompt_lines.extend(
            [
                "Field: receiver_address.",
                "Read only the handwritten address after 地址.",
                "Copy only visible text.",
                "Do not prepend province, city, or district that are not clearly visible.",
                "Do not include receiver name or phone number.",
            ]
        )
    elif field_name == "destination_site":
        prompt_lines.extend(
            [
                "Field: destination_site.",
                "Read only the handwritten station/site name after 到达站.",
                "Do not return address text.",
            ]
        )
    elif field_name == "open_date":
        prompt_lines.extend(
            [
                "Field: open_date.",
                "Read only the date in the top-right corner.",
                "Preserve the year-month-day form if visible.",
            ]
        )
    elif field_name == "goods_name_lines":
        prompt_lines.extend(
            [
                "Field: goods_name_lines.",
                "Read only the goods name text.",
                "Do not return dimensions such as 175×109×72.",
            ]
        )
    elif field_name == "package_type_lines":
        prompt_lines.extend(
            [
                "Field: package_type_lines.",
                "Read only the packaging type.",
                "Examples include 膜, 木架, 纸箱.",
                "Do not return numbers or dimensions.",
            ]
        )
    elif field_name == "quantity_lines":
        prompt_lines.extend(
            [
                "Field: quantity_lines.",
                "Read only the quantity value.",
                "Examples include 1辆, 2件.",
                "Do not return dimensions.",
            ]
        )
    elif field_name == "delivery_method":
        prompt_lines.extend(
            [
                "Field: delivery_method.",
                "Return only one of: 送货 or 自提.",
                "Use the marked or checked option only.",
            ]
        )
    elif field_name == "payment_method":
        prompt_lines.extend(
            [
                "Field: payment_method.",
                "Read only the actual settlement method in the settlement box.",
                "Do not return the printed label 结算方式.",
            ]
        )
    prompt_lines.append(json.dumps(output_schema, ensure_ascii=False))

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "\n".join(prompt_lines)},
                    {
                        "type": "image_url",
                        "image_url": {"url": crop_data_url},
                        "min_pixels": min_pixels,
                        "max_pixels": max_pixels,
                    },
                ],
            }
        ],
        "max_tokens": 256,
        "temperature": 0.0,
    }
    request_summary = {
        "model": model,
        "task": "targeted_region_rerun",
        "field_names": [field_name],
    }
    return payload, request_summary


def _build_targeted_field_instruction(field: dict[str, Any], current_result: dict[str, Any]) -> str:
    parts = [f"- {field['name']}: {field['label']}。{field.get('hint', '').strip()}"]
    if field["name"] == "receiver_address":
        parts.append("只逐字抄写图片里真正能看到的地址，不要补全省市区，不要混入收货人姓名。")
        parts.append("如果开头混入其他字段，宁可从看得清的地址段开始抄。")
    elif field["name"] == "destination_site":
        parts.append("只返回顶部“到达站:”后的站点名。")
        parts.append("不要读取地址栏里的城市名。")
    elif field["name"] == "open_date":
        parts.append("只返回右上角日期，保留年/月/日格式。")
    elif field["name"] == "sender_phone":
        parts.append("只返回左侧发货人电话，不是右侧收货人电话，也不是底部网点电话。")
    elif field["name"] == "sender_name":
        parts.append("只返回左上角发货人姓名，不要误写成货物名称。")
    elif field["name"] == "goods_name_lines":
        parts.append("只返回货物名称本身，不要误写尺寸或其他列内容。")
        parts.append("像175×109×72这类尺寸不是货物名称。")
    elif field["name"] == "package_type_lines":
        parts.append("只返回包装种类。")
        parts.append("像膜、木架、纸箱属于包装；纯数字和尺寸不是包装。")
    elif field["name"] == "quantity_lines":
        parts.append("只返回件数。")
        parts.append("件数通常是短数字加单位，如1辆、2件；175×109×72这类尺寸不是件数。")
    elif field["name"] == "weight_volume":
        parts.append("只返回重量/体积栏内容。")
        parts.append("175×109×72这类尺寸应写到这里，不要写到件数。")
    elif field["name"] == "delivery_method":
        parts.append("只返回送货或自提中的一个。")
        parts.append("查看右侧送货/自提区域，被勾选或有明显手写标记的才返回。")
    elif field["name"] == "freight_fee":
        parts.append("只返回小写运费金额，不要读取运费总计的大写金额，也不要补成 0.00。")
        parts.append("只看右下角小写¥后的数字，不要抄运费总计里的90.41。")
    elif field["name"] in {"pickup_fee", "delivery_fee", "transfer_fee"}:
        parts.append("只返回该费用格里的数字金额；若空白就返回空字符串。")
    elif field["name"] == "payment_method":
        parts.append("只返回实际结算方式，不要翻译成英文。")
        parts.append("不要把送货/自提误写成结算方式。")
    elif field["name"] == "remark":
        parts.append("只返回备注内容本身。")
    parts.append('无法确认时返回 ""。')
    return " ".join(part for part in parts if part)


def _build_field_instruction(field: dict[str, Any], field_state: dict[str, Any]) -> str:
    parts = [f"- {field['name']}: {field['label']}。"]
    hint = str(field.get("hint", "")).strip()
    if hint:
        parts.append(hint)
    if field["name"] in {"goods_name_lines", "package_type_lines", "quantity_lines"}:
        parts.append("如有多行内容，按从上到下顺序返回。")
    elif field["name"] == "sender_name":
        parts.append("只返回左上侧“发货人:”后的姓名，不是货物名称；若该格为空，返回空字符串。")
    elif field["name"] == "sender_phone":
        parts.append("只返回左侧发货人电话，不是右侧收货人电话，也不是底部网点电话。")
    elif field["name"] == "open_date":
        parts.append("只看顶部右上角日期，保留原始日期写法。")
    elif field["name"] == "waybill_no":
        parts.append("只看右上角红色流水号。")
    elif field["name"] == "destination_site":
        parts.append("只看顶部“到达站:”后的手写站点，不是底部发货点城市。")
    elif field["name"] == "receiver_phone":
        parts.append("只返回右侧收货人电话。")
    elif field["name"] in {"receiver_phone", "sender_phone", "waybill_no", "freight_fee", "pickup_fee", "delivery_fee", "transfer_fee"}:
        parts.append("优先输出清晰可见的数字或金额。")
    elif field["name"] == "delivery_method":
        parts.append("只返回送货或自提；如果只看到印刷标签而没有明确勾选/手写标记，则返回空字符串。")
    elif field["name"] == "freight_fee":
        parts.append("只返回右下角“小写¥:”后的运费金额，不要读取“运费总计”的大写金额。")
    elif field["name"] == "payment_method":
        parts.append("只返回实际结算方式，不要返回印刷标签“结算方式”。")
    elif field["name"] == "remark":
        parts.append("只返回备注内容，不要返回印刷标签“备注”。若备注为空，返回空字符串。")
    parts.append("识别不到时返回空字符串。")
    return " ".join(parts)


def _resolve_pixel_limits(model: str) -> tuple[int, int]:
    if model.strip() == "qwen-vl-ocr":
        return 3136, 3145728
    return 1024, 8388608


def _image_to_data_url(image_path: Path, cache: dict[str, str] | None = None) -> str:
    cache_key = str(image_path)
    if cache is not None and cache_key in cache:
        return cache[cache_key]
    mime_type, _ = mimetypes.guess_type(str(image_path))
    if not mime_type:
        mime_type = "image/jpeg"
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    data_url = f"data:{mime_type};base64,{encoded}"
    if cache is not None:
        cache[cache_key] = data_url
    return data_url


def _image_bytes_to_data_url(image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _load_image(image_path: Path) -> np.ndarray | None:
    try:
        buffer = np.fromfile(str(image_path), dtype=np.uint8)
    except OSError:
        return None
    if buffer.size == 0:
        return None
    return cv2.imdecode(buffer, cv2.IMREAD_COLOR)


def _normalize_image_paths(image_paths: Path | list[Path]) -> list[Path]:
    if isinstance(image_paths, Path):
        return [image_paths]
    normalized = [Path(path) for path in image_paths if Path(path).exists()]
    return normalized or []


def _build_image_source_map(image_paths: list[Path]) -> dict[str, Path]:
    if not image_paths:
        return {}
    source_map = {"processed": image_paths[-1]}
    if len(image_paths) >= 2:
        source_map["ocr_input"] = image_paths[0]
    return source_map


def _build_field_crop_data_url(
    field_spec: dict[str, Any],
    source_map: dict[str, Path],
    default_padding_ratio: float,
) -> tuple[str, dict[str, Any]]:
    source_name = str(field_spec.get("crop_source", "processed"))
    source_path = source_map.get(source_name, source_map.get("processed"))
    if source_path is None or not source_path.exists():
        return "", {}

    image = _load_image(source_path)
    if image is None:
        return "", {}
    height, width = image.shape[:2]
    x0, y0, x1, y1 = field_spec.get("crop_bbox", field_spec.get("bbox", [0, 0, 1, 1]))
    left = max(0, min(width - 1, int(width * float(x0))))
    top = max(0, min(height - 1, int(height * float(y0))))
    right = max(left + 1, min(width, int(width * float(x1))))
    bottom = max(top + 1, min(height, int(height * float(y1))))
    pad_x_ratio = float(field_spec.get("padding_ratio_x", field_spec.get("padding_ratio", default_padding_ratio)))
    pad_y_ratio = float(field_spec.get("padding_ratio_y", field_spec.get("padding_ratio", default_padding_ratio)))
    pad_x = max(2, int(width * pad_x_ratio))
    pad_y = max(2, int(height * pad_y_ratio))
    left = max(0, left - pad_x)
    top = max(0, top - pad_y)
    right = min(width, right + pad_x)
    bottom = min(height, bottom + pad_y)

    crop = image[top:bottom, left:right]
    crop_height, crop_width = crop.shape[:2]
    scale = max(1.0, 320 / max(crop_height, 1), 640 / max(crop_width, 1))
    if scale > 1.05:
        crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    ok, encoded = cv2.imencode(".jpg", crop)
    if not ok:
        return "", {}
    meta = {
        "source": source_name,
        "source_path": str(source_path),
        "bbox_pixels": [left, top, right, bottom],
        "bbox_norm": field_spec.get("crop_bbox", field_spec.get("bbox")),
        "output_shape": list(crop.shape[:2]),
    }
    return _image_bytes_to_data_url(encoded.tobytes()), meta


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout_seconds: int) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    retries = 3
    for attempt in range(1, retries + 1):
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:  # pragma: no cover - network integration
            detail = exc.read().decode("utf-8", errors="ignore")
            if exc.code in {408, 429, 500, 502, 503, 504} and attempt < retries:
                time.sleep(1.2 * attempt)
                continue
            return {"error": f"HTTP {exc.code}", "detail": detail}
        except urllib.error.URLError as exc:  # pragma: no cover - network integration
            if attempt < retries:
                time.sleep(1.2 * attempt)
                continue
            return {"error": "URL_ERROR", "detail": str(exc)}

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"error": "INVALID_JSON", "detail": raw}
    return {"error": "UNKNOWN", "detail": "Request failed after retries."}


def _parse_qwen_response(
    *,
    response: dict[str, Any],
    template_spec: dict[str, Any],
    request_summary: dict[str, Any],
    default_confidence: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    provider_name = "qwen_vl_ocr_gateway"
    field_specs = template_spec["fields"]
    if "error" in response:
        results = {
            field["name"]: {
                "value": "",
                "confidence": 0.0,
                "source": provider_name,
                "message": response.get("detail", response["error"]),
            }
            for field in field_specs
        }
        return results, {"provider": provider_name, "request": request_summary, "response": response}

    raw_fields = _extract_structured_fields(response)
    response_text = _extract_response_text(response)
    if not raw_fields and response_text:
        raw_fields = _extract_json_from_text(response_text)

    results: dict[str, Any] = {}
    for field in field_specs:
        raw_item = raw_fields.get(field["name"], raw_fields.get(field["label"], ""))
        results[field["name"]] = _normalize_result_item(
            field_name=field["name"],
            raw_item=raw_item,
            provider_name=provider_name,
            default_confidence=default_confidence,
        )

    debug = {
        "provider": provider_name,
        "request": request_summary,
        "response": response,
    }
    if response_text:
        debug["response_text_preview"] = response_text[:2000]
    return results, debug


def _parse_subset_qwen_response(
    *,
    response: dict[str, Any],
    field_specs: list[dict[str, Any]],
    request_summary: dict[str, Any],
    default_confidence: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    provider_name = "qwen_vl_ocr_gateway"
    if "error" in response:
        results = {
            field["name"]: {
                "value": "",
                "confidence": 0.0,
                "source": provider_name,
                "message": response.get("detail", response["error"]),
            }
            for field in field_specs
        }
        return results, {"provider": provider_name, "request": request_summary, "response": response}

    raw_fields = _extract_structured_fields(response)
    response_text = _extract_response_text(response)
    if not raw_fields and response_text:
        raw_fields = _extract_json_from_text(response_text)

    results: dict[str, Any] = {}
    for field in field_specs:
        raw_item = raw_fields.get(field["name"], raw_fields.get(field["label"], ""))
        if not raw_item and response_text and not raw_fields:
            raw_item = response_text
        results[field["name"]] = _normalize_result_item(
            field_name=field["name"],
            raw_item=raw_item,
            provider_name=provider_name,
            default_confidence=default_confidence,
        )

    debug = {
        "provider": provider_name,
        "request": request_summary,
        "response": response,
    }
    if response_text:
        debug["response_text_preview"] = response_text[:2000]
    return results, debug


def _extract_structured_fields(response: dict[str, Any]) -> dict[str, Any]:
    candidates: list[Any] = [
        response.get("fields"),
        response.get("result", {}).get("fields") if isinstance(response.get("result"), dict) else None,
        response.get("data", {}).get("fields") if isinstance(response.get("data"), dict) else None,
        response.get("output", {}).get("choices", [{}])[0].get("message", {}).get("content", [{}])[0].get("ocr_result", {}).get("kv_result")
        if isinstance(response.get("output"), dict)
        else None,
    ]
    for candidate in candidates:
        normalized = _normalize_field_collection(candidate)
        if normalized:
            return normalized
    return {}


def _normalize_field_collection(candidate: Any) -> dict[str, Any]:
    if isinstance(candidate, dict):
        return candidate
    if isinstance(candidate, list):
        normalized = {}
        for item in candidate:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("key")
            if not name:
                continue
            normalized[str(name)] = item
        return normalized
    return {}


def _extract_response_text(response: dict[str, Any]) -> str:
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message", {})
        content = message.get("content", "")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                text = item.get("text") or item.get("content")
                if text:
                    parts.append(str(text))
                ocr_result = item.get("ocr_result")
                if isinstance(ocr_result, dict):
                    kv_result = ocr_result.get("kv_result")
                    if kv_result:
                        return json.dumps(kv_result, ensure_ascii=False)
            return "\n".join(parts).strip()

    output = response.get("output")
    if isinstance(output, dict):
        text = output.get("text")
        if text:
            return str(text).strip()
    for key in ("text", "output_text", "message"):
        value = response.get(key)
        if value:
            return str(value).strip()
    return ""


def _extract_json_from_text(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if not cleaned:
        return {}

    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, flags=re.DOTALL)
    candidates = fenced + [cleaned]
    for candidate in candidates:
        parsed = _try_parse_json_object(candidate)
        if parsed:
            return parsed
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        parsed = _try_parse_json_object(cleaned[start : end + 1])
        if parsed:
            return parsed
    return {}


def _try_parse_json_object(candidate: str) -> dict[str, Any]:
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return {}
    if isinstance(parsed, dict):
        return parsed
    return {}


def _normalize_result_item(
    *,
    field_name: str,
    raw_item: Any,
    provider_name: str,
    default_confidence: float,
) -> dict[str, Any]:
    message = ""
    confidence = 0.0
    source = provider_name

    if isinstance(raw_item, dict):
        value = raw_item.get("value", raw_item.get("text", raw_item.get("result", "")))
        confidence = float(raw_item.get("confidence", 0.0) or 0.0)
        source = str(raw_item.get("source", provider_name))
        message = str(raw_item.get("message", "") or "")
    else:
        value = raw_item

    normalized_value = _sanitize_field_value(field_name, _stringify_result_value(value))
    if normalized_value != _stringify_result_value(value) and not normalized_value and not message:
        message = "Filtered likely printed label or invalid OCR output."
    if normalized_value and confidence <= 0.0:
        confidence = float(default_confidence)
    if not normalized_value:
        confidence = 0.0

    return {
        "value": normalized_value,
        "confidence": round(confidence, 4),
        "source": source,
        "message": message,
    }


def _stringify_result_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value).strip()
    if isinstance(value, list):
        parts = [_stringify_result_value(item) for item in value]
        return "\n".join(item for item in parts if item).strip()
    if isinstance(value, dict):
        if "text" in value:
            return _stringify_result_value(value["text"])
        if "value" in value:
            return _stringify_result_value(value["value"])
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def _sanitize_field_value(field_name: str, value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value or "").strip()
    if cleaned in {"", '""', "“”", "''", "空字符串", "空白", "未填写", "无", "未知", "null", "NULL", "None"}:
        return ""

    if field_name in {"receiver_phone", "sender_phone"}:
        digits = re.sub(r"\D", "", cleaned)
        if len(digits) >= 7:
            return digits[:11] if len(digits) >= 11 else digits
        return ""

    if field_name == "waybill_no":
        if re.search(r"[A-Za-z]", cleaned):
            return ""
        match = re.search(r"\d{6,12}", cleaned)
        return match.group(0) if match else ""

    if field_name == "open_date":
        if not re.search(r"\d", cleaned):
            return ""
        if "年" in cleaned or "月" in cleaned or "日" in cleaned:
            return cleaned
        return ""

    if field_name == "destination_site":
        if any(token in cleaned for token in ("$", "\\", "alpha", "beta", "gamma")):
            return ""
        if any(token in cleaned for token in ("地址", "电话", "发货", "收货")):
            return ""
        if re.search(r"\d{5,}", cleaned):
            return ""
        if not re.search(r"[\u4e00-\u9fff]", cleaned):
            return ""
        return cleaned

    if field_name == "delivery_method":
        hits = [option for option in DELIVERY_METHOD_OPTIONS if option in cleaned]
        if len(hits) == 1:
            return hits[0]
        return ""

    if field_name == "payment_method":
        for option in PAYMENT_METHOD_OPTIONS:
            if option in cleaned:
                return option
        if "结算方式" in cleaned:
            return ""
        return ""

    if field_name in {"freight_fee", "pickup_fee", "delivery_fee", "transfer_fee"}:
        match = re.search(r"\d+(?:\.\d+)?", cleaned.replace(",", ""))
        return match.group(0) if match else ""

    if field_name == "remark":
        stripped = re.sub(r"^备注[:：]?\s*", "", cleaned)
        if stripped in {"", "备注", "备注:", "备注："}:
            return ""
        return stripped

    if field_name in {"goods_name_lines", "package_type_lines", "quantity_lines"}:
        return "\n".join(
            line.strip()
            for line in re.split(r"[\r\n]+", cleaned)
            if line.strip() and line.strip() not in {"货物名称", "包装种类", "件数"}
        ).strip()

    return cleaned


def _address_candidates_overlap(current_value: str, crop_value: str) -> bool:
    current = re.sub(r"\s+", "", current_value or "")
    crop = re.sub(r"\s+", "", crop_value or "")
    if not current or not crop:
        return False
    return current in crop or crop in current


def _is_zero_like(value: str) -> bool:
    cleaned = (value or "").strip()
    return cleaned in {"0", "0.0", "0.00", "￥0", "¥0", "￥0.00", "¥0.00"}


def _looks_like_dimension(value: str) -> bool:
    cleaned = (value or "").strip()
    if not cleaned:
        return False
    return bool(re.search(r"\d+\s*[×xX*]\s*\d+", cleaned))


def _should_rerun_field(field_name: str, current_results: dict[str, Any]) -> bool:
    current_value = str(current_results.get(field_name, {}).get("value", "") or "").strip()
    goods_value = str(current_results.get("goods_name_lines", {}).get("value", "") or "").strip()
    if field_name in {"destination_site", "open_date", "receiver_address", "delivery_method"}:
        return not current_value
    if field_name == "waybill_no":
        return not current_value
    if field_name == "freight_fee":
        return False
    if field_name == "sender_name":
        return not current_value or current_value == goods_value or _looks_like_dimension(current_value)
    if field_name == "sender_phone":
        return not current_value
    if field_name == "goods_name_lines":
        return not current_value or _looks_like_dimension(current_value)
    if field_name == "package_type_lines":
        return not current_value or bool(re.search(r"\d", current_value))
    if field_name == "quantity_lines":
        return not current_value or _looks_like_dimension(current_value)
    if field_name == "weight_volume":
        return not current_value or not _looks_like_dimension(current_value)
    if field_name in {"pickup_fee", "delivery_fee", "transfer_fee", "payment_method", "remark"}:
        return not current_value
    return False


def _should_region_rerun_field(field_name: str, current_results: dict[str, Any]) -> bool:
    current_value = str(current_results.get(field_name, {}).get("value", "") or "").strip()
    if field_name == "waybill_no":
        return not current_value
    if field_name == "freight_fee":
        return not current_value or current_value == "90.41"
    return False


def _should_required_region_rerun(field_spec: dict[str, Any], current_results: dict[str, Any]) -> bool:
    if not field_spec.get("required"):
        return False
    field_name = str(field_spec.get("name", ""))
    current_value = str(current_results.get(field_name, {}).get("value", "") or "").strip()
    return not current_value


def _select_region_rerun_fields(
    template_spec: dict[str, Any],
    current_results: dict[str, Any],
) -> list[str]:
    selected: list[str] = []
    seen: set[str] = set()
    for field_name in SPECIAL_REGION_RERUN_FIELDS:
        if _should_region_rerun_field(field_name, current_results):
            selected.append(field_name)
            seen.add(field_name)
    return selected


def _replace_result_item(target_item: dict[str, Any], candidate_item: dict[str, Any]) -> None:
    for key in ("value", "confidence", "source", "message"):
        target_item[key] = candidate_item.get(key, target_item.get(key))


def _merge_second_pass_results(results: dict[str, Any], second_pass_results: dict[str, Any]) -> None:
    goods_name_value = str(results.get("goods_name_lines", {}).get("value", "") or "").strip()
    for field_name, candidate_item in second_pass_results.items():
        current_item = results.get(field_name)
        if not isinstance(current_item, dict) or not isinstance(candidate_item, dict):
            continue

        current_value = str(current_item.get("value", "") or "").strip()
        candidate_value = str(candidate_item.get("value", "") or "").strip()
        if field_name == "freight_fee" and not candidate_value and current_value and not _is_zero_like(current_value):
            current_item["value"] = ""
            current_item["confidence"] = 0.0
            current_item["source"] = candidate_item.get("source", current_item.get("source"))
            current_item["message"] = ""
            continue
        if not candidate_value:
            continue

        if field_name in SECOND_PASS_OVERRIDE_FIELDS:
            _replace_result_item(current_item, candidate_item)
            continue

        if field_name == "receiver_address":
            if not current_value:
                _replace_result_item(current_item, candidate_item)
                continue
            if _address_candidates_overlap(current_value, candidate_value) and len(candidate_value) > len(current_value):
                _replace_result_item(current_item, candidate_item)
            continue

        if field_name == "sender_name":
            if (
                candidate_value != goods_name_value
                and not _looks_like_dimension(candidate_value)
                and not re.search(r"\d{3,}", candidate_value)
                and (not current_value or current_value == goods_name_value)
            ):
                _replace_result_item(current_item, candidate_item)
            continue

        if field_name == "sender_phone":
            if re.fullmatch(r"1\d{10}", candidate_value):
                _replace_result_item(current_item, candidate_item)
            continue

        if field_name == "goods_name_lines":
            if not _looks_like_dimension(candidate_value):
                _replace_result_item(current_item, candidate_item)
            continue

        if field_name == "package_type_lines":
            if not re.search(r"\d", candidate_value):
                _replace_result_item(current_item, candidate_item)
            continue

        if field_name == "quantity_lines":
            if not _looks_like_dimension(candidate_value):
                _replace_result_item(current_item, candidate_item)
            continue

        if field_name == "weight_volume":
            if _looks_like_dimension(candidate_value):
                _replace_result_item(current_item, candidate_item)
            continue

        if not current_value and candidate_value:
            _replace_result_item(current_item, candidate_item)


def _merge_region_results(results: dict[str, Any], region_results: dict[str, Any]) -> None:
    for field_name, region_item in region_results.items():
        current_item = results.get(field_name)
        if not isinstance(current_item, dict) or not isinstance(region_item, dict):
            continue
        region_value = str(region_item.get("value", "") or "").strip()
        if not region_value:
            continue
        if field_name == "waybill_no" and re.fullmatch(r"\d{6,12}", region_value):
            _replace_result_item(current_item, region_item)
        elif field_name == "freight_fee" and not _is_zero_like(region_value) and region_value != "90.41":
            _replace_result_item(current_item, region_item)
        elif field_name in {"sender_phone", "receiver_phone"} and re.fullmatch(r"1\d{10}", region_value):
            _replace_result_item(current_item, region_item)
        elif field_name == "sender_name" and not re.search(r"\d{3,}", region_value) and not _looks_like_dimension(region_value):
            _replace_result_item(current_item, region_item)
        elif field_name == "receiver_name" and not re.search(r"\d{3,}", region_value):
            _replace_result_item(current_item, region_item)
        elif field_name == "receiver_address":
            if len(region_value) >= len(str(current_item.get("value", "") or "").strip()):
                _replace_result_item(current_item, region_item)
        elif field_name in {"destination_site", "open_date"}:
            _replace_result_item(current_item, region_item)


def _postprocess_results(results: dict[str, Any]) -> None:
    receiver_name = str(results.get("receiver_name", {}).get("value", "") or "").strip()
    destination_site = str(results.get("destination_site", {}).get("value", "") or "").strip()
    receiver_address_item = results.get("receiver_address")
    if receiver_name and isinstance(receiver_address_item, dict):
        address_value = str(receiver_address_item.get("value", "") or "").strip()
        if address_value.startswith(receiver_name):
            stripped = address_value[len(receiver_name):].strip(" ：:，,")
            receiver_address_item["value"] = stripped
            if not stripped:
                receiver_address_item["confidence"] = 0.0
    if destination_site and isinstance(receiver_address_item, dict):
        address_value = str(receiver_address_item.get("value", "") or "").strip()
        if destination_site in address_value:
            prefix, _, suffix = address_value.partition(destination_site)
            if prefix and re.search(r"[\u4e00-\u9fff]{2,}(市|区|县)$", prefix):
                receiver_address_item["value"] = f"{destination_site}{suffix}".strip()
