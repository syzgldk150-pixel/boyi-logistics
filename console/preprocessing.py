import re
import uuid
from pathlib import Path
from typing import Any

import cv2
import numpy as np


def sanitize_filename(filename: str) -> str:
    cleaned = re.sub(r"[^\w.\-() \u4e00-\u9fff]", "_", filename)
    cleaned = cleaned.strip().replace(" ", "_")
    return cleaned or "document.jpg"


def new_doc_token() -> str:
    return uuid.uuid4().hex[:12]


def write_bytes(target: Path, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as handle:
        handle.write(payload)


def load_image(image_path: Path) -> np.ndarray:
    buffer = np.fromfile(str(image_path), dtype=np.uint8)
    image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"Cannot decode image: {image_path}")
    return image


def save_image(target: Path, image: np.ndarray) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    suffix = target.suffix.lower()
    extension = ".png" if suffix == ".png" else ".jpg"
    encode_params: list[int] = []
    if extension == ".jpg":
        encode_params = [cv2.IMWRITE_JPEG_QUALITY, 85]
    success, buffer = cv2.imencode(extension, image, encode_params)
    if not success:
        raise ValueError(f"Cannot encode image: {target}")
    buffer.tofile(str(target))


def _coerce_target_size(template_spec: dict[str, Any]) -> tuple[int, int]:
    preprocess_spec = template_spec.get("preprocess", {})
    raw_size = preprocess_spec.get("target_size", [2688, 1512])
    if isinstance(raw_size, list) and len(raw_size) == 2:
        width = int(raw_size[0] or 2688)
        height = int(raw_size[1] or 1512)
        return max(width, 800), max(height, 600)
    return 2688, 1512


def _preprocess_options(template_spec: dict[str, Any]) -> dict[str, float | tuple[int, int]]:
    preprocess_spec = template_spec.get("preprocess", {})
    base_expand_ratio = float(preprocess_spec.get("document_expand_ratio", 0.03))
    return {
        "target_size": _coerce_target_size(template_spec),
        "blur_threshold": float(preprocess_spec.get("blur_threshold", 90.0)),
        "min_brightness": float(preprocess_spec.get("min_brightness", 55.0)),
        "max_brightness": float(preprocess_spec.get("max_brightness", 230.0)),
        "min_document_fill_ratio": float(preprocess_spec.get("min_document_fill_ratio", 0.42)),
        "crop_padding_ratio": float(preprocess_spec.get("crop_padding_ratio", 0.006)),
        "document_expand_top_ratio": float(preprocess_spec.get("document_expand_top_ratio", base_expand_ratio)),
        "document_expand_right_ratio": float(preprocess_spec.get("document_expand_right_ratio", base_expand_ratio)),
        "document_expand_bottom_ratio": float(preprocess_spec.get("document_expand_bottom_ratio", base_expand_ratio)),
        "document_expand_left_ratio": float(preprocess_spec.get("document_expand_left_ratio", base_expand_ratio)),
    }


def _pixel_rect_from_bbox(
    bbox: list[float],
    width: int,
    height: int,
) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = bbox
    left = max(0, min(width - 1, int(round(width * float(x0)))))
    top = max(0, min(height - 1, int(round(height * float(y0)))))
    right = max(left + 1, min(width, int(round(width * float(x1)))))
    bottom = max(top + 1, min(height, int(round(height * float(y1)))))
    return left, top, right, bottom


def _resize_for_detection(image: np.ndarray, max_side: int = 1800) -> tuple[np.ndarray, float]:
    height, width = image.shape[:2]
    scale = min(1.0, float(max_side) / float(max(height, width)))
    if scale >= 1.0:
        return image.copy(), 1.0
    resized = cv2.resize(
        image,
        (int(width * scale), int(height * scale)),
        interpolation=cv2.INTER_AREA,
    )
    return resized, scale


def _order_corners(points: np.ndarray) -> np.ndarray:
    pts = np.asarray(points, dtype=np.float32).reshape(4, 2)
    sums = pts.sum(axis=1)
    diffs = np.diff(pts, axis=1).reshape(4)
    return np.array(
        [
            pts[np.argmin(sums)],
            pts[np.argmin(diffs)],
            pts[np.argmax(sums)],
            pts[np.argmax(diffs)],
        ],
        dtype=np.float32,
    )


def _build_edge_mask(gray: np.ndarray) -> np.ndarray:
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 50, 160)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7))
    closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=2)
    return cv2.dilate(closed, kernel, iterations=1)


def _detect_document_quad(
    image: np.ndarray,
    min_document_fill_ratio: float,
) -> tuple[np.ndarray | None, float, str, np.ndarray]:
    resized, scale = _resize_for_detection(image)
    gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
    mask = _build_edge_mask(gray)
    contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    preview = resized.copy()
    total_area = float(resized.shape[0] * resized.shape[1])
    min_area = total_area * max(min_document_fill_ratio * 0.55, 0.18)

    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:20]:
        area = cv2.contourArea(contour)
        if area < min_area:
            continue
        perimeter = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
        if len(approx) != 4:
            continue
        cv2.drawContours(preview, [approx], -1, (0, 200, 0), 3)
        points = _order_corners(approx.reshape(4, 2) / scale)
        return points, round(area / total_area, 4), "contour_quad", preview

    if contours:
        largest = max(contours, key=cv2.contourArea)
        area = cv2.contourArea(largest)
        if area >= min_area:
            rect = cv2.minAreaRect(largest)
            box = cv2.boxPoints(rect)
            ordered = _order_corners(box / scale)
            cv2.drawContours(preview, [box.astype(np.int32)], -1, (0, 160, 255), 3)
            return ordered, round(area / total_area, 4), "min_area_rect", preview

    return None, 0.0, "not_found", preview


def _warp_document(
    image: np.ndarray,
    corners: np.ndarray,
    target_size: tuple[int, int],
) -> np.ndarray:
    target_width, target_height = target_size
    destination = np.array(
        [
            [0, 0],
            [target_width - 1, 0],
            [target_width - 1, target_height - 1],
            [0, target_height - 1],
        ],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(corners.astype(np.float32), destination)
    return cv2.warpPerspective(
        image,
        matrix,
        (target_width, target_height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )


def _expand_document_corners(
    corners: np.ndarray,
    image_shape: tuple[int, int, int],
    top_ratio: float,
    right_ratio: float,
    bottom_ratio: float,
    left_ratio: float,
) -> np.ndarray:
    if max(top_ratio, right_ratio, bottom_ratio, left_ratio) <= 0:
        return corners.astype(np.float32)

    height, width = image_shape[:2]
    pts = corners.astype(np.float32).reshape(4, 2)
    tl, tr, br, bl = _order_corners(pts)
    horizontal_axis = ((tr - tl) + (br - bl)) / 2.0
    vertical_axis = ((bl - tl) + (br - tr)) / 2.0
    horizontal_length = float(np.linalg.norm(horizontal_axis))
    vertical_length = float(np.linalg.norm(vertical_axis))
    if horizontal_length < 1.0 or vertical_length < 1.0:
        return pts.astype(np.float32)

    h_unit = horizontal_axis / horizontal_length
    v_unit = vertical_axis / vertical_length
    top_shift = v_unit * (vertical_length * max(0.0, float(top_ratio)))
    bottom_shift = v_unit * (vertical_length * max(0.0, float(bottom_ratio)))
    left_shift = h_unit * (horizontal_length * max(0.0, float(left_ratio)))
    right_shift = h_unit * (horizontal_length * max(0.0, float(right_ratio)))

    expanded = np.array(
        [
            tl - left_shift - top_shift,
            tr + right_shift - top_shift,
            br + right_shift + bottom_shift,
            bl - left_shift + bottom_shift,
        ],
        dtype=np.float32,
    )
    expanded[:, 0] = np.clip(expanded[:, 0], 0, width - 1)
    expanded[:, 1] = np.clip(expanded[:, 1], 0, height - 1)
    return expanded.astype(np.float32)


def _collect_segments(indices: np.ndarray) -> list[tuple[int, int]]:
    if indices.size == 0:
        return []
    segments: list[tuple[int, int]] = []
    start = int(indices[0])
    previous = int(indices[0])
    for raw_index in indices[1:]:
        index = int(raw_index)
        if index == previous + 1:
            previous = index
            continue
        segments.append((start, previous))
        start = index
        previous = index
    segments.append((start, previous))
    return segments


def _pick_segment_coordinate(segments: list[tuple[int, int]], selector: str) -> int | None:
    if not segments:
        return None
    selector_value = str(selector or "center").lower()
    if selector_value in {"left", "top", "first"}:
        start, end = segments[0]
    elif selector_value in {"right", "bottom", "last"}:
        start, end = segments[-1]
    elif selector_value == "largest":
        start, end = max(segments, key=lambda item: item[1] - item[0])
    else:
        centers = [int(round((start + end) / 2.0)) for start, end in segments]
        return int(round(float(np.median(centers))))
    return int(round((start + end) / 2.0))


def _detect_line_intersection_anchor(
    image: np.ndarray,
    anchor_spec: dict[str, Any],
) -> tuple[np.ndarray | None, dict[str, Any]]:
    height, width = image.shape[:2]
    left, top, right, bottom = _pixel_rect_from_bbox(anchor_spec["bbox"], width, height)
    crop = image[top:bottom, left:right]
    debug = {
        "name": anchor_spec.get("name", ""),
        "type": "line_intersection",
        "bbox": [left, top, right, bottom],
        "status": "missing",
    }
    if crop.size == 0:
        return None, debug

    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    enhanced = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
    binary = cv2.adaptiveThreshold(
        enhanced,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        31,
        9,
    )

    kernel_width = max(10, crop.shape[1] // 4)
    kernel_height = max(10, crop.shape[0] // 3)
    horizontal_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_width, 1))
    vertical_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, kernel_height))
    horizontal = cv2.morphologyEx(binary, cv2.MORPH_OPEN, horizontal_kernel)
    vertical = cv2.morphologyEx(binary, cv2.MORPH_OPEN, vertical_kernel)

    horizontal_scores = horizontal.sum(axis=1) / 255.0
    vertical_scores = vertical.sum(axis=0) / 255.0
    horizontal_threshold = max(8.0, crop.shape[1] * 0.18)
    vertical_threshold = max(8.0, crop.shape[0] * 0.18)
    horizontal_segments = _collect_segments(np.where(horizontal_scores >= horizontal_threshold)[0])
    vertical_segments = _collect_segments(np.where(vertical_scores >= vertical_threshold)[0])

    y_value = _pick_segment_coordinate(horizontal_segments, str(anchor_spec.get("horizontal_select", "center")))
    x_value = _pick_segment_coordinate(vertical_segments, str(anchor_spec.get("vertical_select", "center")))
    if x_value is None or y_value is None:
        debug["horizontal_segments"] = horizontal_segments
        debug["vertical_segments"] = vertical_segments
        return None, debug

    point = np.array([left + x_value, top + y_value], dtype=np.float32)
    debug["status"] = "matched"
    debug["point"] = point.tolist()
    debug["horizontal_segments"] = horizontal_segments
    debug["vertical_segments"] = vertical_segments
    return point, debug


def _detect_red_region_anchor(
    image: np.ndarray,
    anchor_spec: dict[str, Any],
) -> tuple[np.ndarray | None, dict[str, Any]]:
    height, width = image.shape[:2]
    left, top, right, bottom = _pixel_rect_from_bbox(anchor_spec["bbox"], width, height)
    crop = image[top:bottom, left:right]
    debug = {
        "name": anchor_spec.get("name", ""),
        "type": "red_region",
        "bbox": [left, top, right, bottom],
        "status": "missing",
    }
    if crop.size == 0:
        return None, debug

    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    lower_red_1 = np.array([0, 70, 50], dtype=np.uint8)
    upper_red_1 = np.array([12, 255, 255], dtype=np.uint8)
    lower_red_2 = np.array([160, 70, 50], dtype=np.uint8)
    upper_red_2 = np.array([180, 255, 255], dtype=np.uint8)
    mask = cv2.inRange(hsv, lower_red_1, upper_red_1) | cv2.inRange(hsv, lower_red_2, upper_red_2)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
    points = cv2.findNonZero(mask)
    if points is None or int(cv2.countNonZero(mask)) < 40:
        return None, debug

    x, y, box_width, box_height = cv2.boundingRect(points)
    mode = str(anchor_spec.get("target_point", "center")).lower()
    if mode == "top_left":
        px = x
        py = y
    elif mode == "top_right":
        px = x + box_width
        py = y
    elif mode == "bottom_left":
        px = x
        py = y + box_height
    elif mode == "bottom_right":
        px = x + box_width
        py = y + box_height
    else:
        px = x + (box_width / 2.0)
        py = y + (box_height / 2.0)
    point = np.array([left + px, top + py], dtype=np.float32)
    debug["status"] = "matched"
    debug["point"] = point.tolist()
    debug["red_bbox"] = [left + x, top + y, left + x + box_width, top + y + box_height]
    return point, debug


def _target_point_from_anchor(anchor_spec: dict[str, Any], width: int, height: int) -> np.ndarray:
    bbox = anchor_spec["bbox"]
    x0, y0, x1, y1 = [float(value) for value in bbox]
    mode = str(anchor_spec.get("target_point", "")).lower()
    if mode == "top_left":
        return np.array([x0 * width, y0 * height], dtype=np.float32)
    if mode == "top_right":
        return np.array([x1 * width, y0 * height], dtype=np.float32)
    if mode == "bottom_left":
        return np.array([x0 * width, y1 * height], dtype=np.float32)
    if mode == "bottom_right":
        return np.array([x1 * width, y1 * height], dtype=np.float32)
    if mode == "center":
        return np.array([((x0 + x1) / 2.0) * width, ((y0 + y1) / 2.0) * height], dtype=np.float32)

    vertical_select = str(anchor_spec.get("vertical_select", "center")).lower()
    horizontal_select = str(anchor_spec.get("horizontal_select", "center")).lower()
    target_x = x0 if vertical_select in {"left", "first"} else x1 if vertical_select in {"right", "last"} else (x0 + x1) / 2.0
    target_y = y0 if horizontal_select in {"top", "first"} else y1 if horizontal_select in {"bottom", "last"} else (y0 + y1) / 2.0
    return np.array([target_x * width, target_y * height], dtype=np.float32)


def _estimate_anchor_alignment(
    image: np.ndarray,
    template_spec: dict[str, Any],
) -> tuple[np.ndarray | None, dict[str, Any], np.ndarray]:
    preprocess_spec = template_spec.get("preprocess", {})
    alignment_spec = preprocess_spec.get("anchor_alignment", {})
    preview = image.copy()
    default_debug = {
        "enabled": bool(alignment_spec.get("enabled", False)),
        "status": "disabled",
        "matches": 0,
        "inliers": 0,
        "anchors": [],
    }
    if not alignment_spec.get("enabled"):
        return None, default_debug, preview

    height, width = image.shape[:2]
    anchors = alignment_spec.get("anchors", [])
    source_points: list[np.ndarray] = []
    target_points: list[np.ndarray] = []
    anchor_debug: list[dict[str, Any]] = []

    for anchor_spec in anchors:
        anchor_type = str(anchor_spec.get("type", "line_intersection"))
        if anchor_type == "red_region":
            point, debug = _detect_red_region_anchor(image, anchor_spec)
        else:
            point, debug = _detect_line_intersection_anchor(image, anchor_spec)

        left, top, right, bottom = debug["bbox"]
        color = (0, 90, 255) if point is None else (20, 180, 20)
        cv2.rectangle(preview, (left, top), (right, bottom), color, 2)
        cv2.putText(
            preview,
            str(anchor_spec.get("name", "anchor")),
            (left + 4, max(top - 6, 18)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            color,
            1,
            cv2.LINE_AA,
        )
        target_point = _target_point_from_anchor(anchor_spec, width, height)
        debug["target"] = target_point.tolist()
        cv2.circle(preview, (int(round(target_point[0])), int(round(target_point[1]))), 5, (255, 120, 0), -1)
        if point is not None:
            source_points.append(point)
            target_points.append(target_point)
            cv2.circle(preview, (int(round(point[0])), int(round(point[1]))), 5, (20, 220, 20), -1)
            cv2.line(
                preview,
                (int(round(point[0])), int(round(point[1]))),
                (int(round(target_point[0])), int(round(target_point[1]))),
                (255, 180, 60),
                1,
                cv2.LINE_AA,
            )
        anchor_debug.append(debug)

    min_matches = int(alignment_spec.get("min_matches", 4))
    if len(source_points) < min_matches:
        default_debug.update(
            {
                "status": "insufficient_matches",
                "matches": len(source_points),
                "anchors": anchor_debug,
            }
        )
        return None, default_debug, preview

    source = np.array(source_points, dtype=np.float32).reshape(-1, 1, 2)
    target = np.array(target_points, dtype=np.float32).reshape(-1, 1, 2)
    ransac_threshold = float(alignment_spec.get("ransac_threshold", 10.0))
    matrix, inlier_mask = cv2.estimateAffinePartial2D(
        source.reshape(-1, 2),
        target.reshape(-1, 2),
        method=cv2.RANSAC,
        ransacReprojThreshold=ransac_threshold,
    )
    inliers = int(inlier_mask.sum()) if inlier_mask is not None else 0
    min_inliers = int(alignment_spec.get("min_inliers", 4))
    sane_transform = False
    scale_value = 1.0
    rotation_deg = 0.0
    translation_x = 0.0
    translation_y = 0.0
    if matrix is not None:
        a_value = float(matrix[0, 0])
        b_value = float(matrix[0, 1])
        translation_x = float(matrix[0, 2])
        translation_y = float(matrix[1, 2])
        scale_value = float(np.sqrt((a_value * a_value) + (b_value * b_value)))
        rotation_deg = float(np.degrees(np.arctan2(b_value, a_value)))
        max_translation_ratio = float(alignment_spec.get("max_translation_ratio", 0.08))
        min_scale = float(alignment_spec.get("min_scale", 0.92))
        max_scale = float(alignment_spec.get("max_scale", 1.08))
        max_rotation_deg = float(alignment_spec.get("max_rotation_deg", 4.0))
        sane_transform = (
            min_scale <= scale_value <= max_scale
            and abs(rotation_deg) <= max_rotation_deg
            and abs(translation_x) <= (width * max_translation_ratio)
            and abs(translation_y) <= (height * max_translation_ratio)
        )
    debug = {
        "enabled": True,
        "status": "matched" if matrix is not None and inliers >= min_inliers and sane_transform else "rejected",
        "transform": "affine_partial",
        "matches": len(source_points),
        "inliers": inliers,
        "scale": round(scale_value, 4),
        "rotation_deg": round(rotation_deg, 4),
        "translation": [round(translation_x, 2), round(translation_y, 2)],
        "anchors": anchor_debug,
    }
    if matrix is None or inliers < min_inliers or not sane_transform:
        return None, debug, preview
    debug["matrix"] = matrix.tolist()
    return matrix, debug, preview


def _trim_to_content(
    image: np.ndarray,
    target_size: tuple[int, int],
) -> tuple[np.ndarray, list[int], float]:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    mask = _build_edge_mask(gray)
    points = cv2.findNonZero(mask)
    if points is None:
        resized = cv2.resize(image, target_size, interpolation=cv2.INTER_LINEAR)
        return resized, [0, 0, image.shape[1], image.shape[0]], 0.0

    x, y, width, height = cv2.boundingRect(points)
    pad_x = max(10, int(width * 0.03))
    pad_y = max(10, int(height * 0.03))
    left = max(0, x - pad_x)
    top = max(0, y - pad_y)
    right = min(image.shape[1], x + width + pad_x)
    bottom = min(image.shape[0], y + height + pad_y)
    cropped = image[top:bottom, left:right]
    resized = cv2.resize(cropped, target_size, interpolation=cv2.INTER_LINEAR)
    fill_ratio = round(float((right - left) * (bottom - top)) / float(image.shape[0] * image.shape[1]), 4)
    return resized, [left, top, right, bottom], fill_ratio


def _assess_quality(
    gray: np.ndarray,
    document_fill_ratio: float,
    document_method: str,
    options: dict[str, float | tuple[int, int]],
) -> dict[str, Any]:
    blur_score = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    brightness_mean = float(np.mean(gray))
    contrast_std = float(np.std(gray))

    blocking_issues: list[str] = []

    if blur_score < float(options["blur_threshold"]):
        blocking_issues.append("blurred")
    if brightness_mean < float(options["min_brightness"]):
        blocking_issues.append("too_dark")
    if brightness_mean > float(options["max_brightness"]):
        blocking_issues.append("too_bright")
    if document_fill_ratio < float(options["min_document_fill_ratio"]):
        blocking_issues.append("document_too_small")
    if document_method == "not_found":
        blocking_issues.append("document_not_detected")

    return {
        "blur_score": round(blur_score, 2),
        "brightness_mean": round(brightness_mean, 2),
        "contrast_std": round(contrast_std, 2),
        "document_fill_ratio": round(document_fill_ratio, 4),
        "blocking_issues": blocking_issues,
        "warning_issues": [],
        "should_route_to_review": bool(blocking_issues),
    }


def _enhance_document(image: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.2, tileGridSize=(8, 8))
    enhanced_gray = clahe.apply(gray)
    denoised = cv2.fastNlMeansDenoising(enhanced_gray, None, 12, 7, 21)
    return cv2.cvtColor(denoised, cv2.COLOR_GRAY2BGR)


def preprocess_document(
    original_path: Path,
    processed_path: Path,
    temp_dir: Path,
    template_spec: dict[str, Any],
) -> dict[str, Any]:
    temp_dir.mkdir(parents=True, exist_ok=True)
    options = _preprocess_options(template_spec)
    image = load_image(original_path)
    original_height, original_width = image.shape[:2]

    rotated = False
    if original_height > original_width:
        image = cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
        rotated = True

    if rotated:
        ocr_input_path = temp_dir / "04_ocr_input.jpg"
        save_image(ocr_input_path, image)
    else:
        ocr_input_path = original_path

    document_corners, document_fill_ratio, document_method, detection_preview = _detect_document_quad(
        image,
        float(options["min_document_fill_ratio"]),
    )

    if document_corners is not None:
        tight_aligned = _warp_document(image, document_corners, options["target_size"])
        expanded_corners = _expand_document_corners(
            document_corners,
            image.shape,
            float(options["document_expand_top_ratio"]),
            float(options["document_expand_right_ratio"]),
            float(options["document_expand_bottom_ratio"]),
            float(options["document_expand_left_ratio"]),
        )
        aligned = _warp_document(image, expanded_corners, options["target_size"])
        content_bbox = [0, 0, aligned.shape[1], aligned.shape[0]]
    else:
        aligned, content_bbox, document_fill_ratio = _trim_to_content(image, options["target_size"])
        tight_aligned = aligned.copy()

    anchor_matrix, anchor_debug, anchor_preview = _estimate_anchor_alignment(tight_aligned, template_spec)

    gray = cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY)
    processed = _enhance_document(aligned)
    save_image(processed_path, processed)

    height, width = processed.shape[:2]
    quality = _assess_quality(gray, document_fill_ratio, document_method, options)
    return {
        "rotated_to_landscape": rotated,
        "original_size": {"width": original_width, "height": original_height},
        "processed_size": {"width": width, "height": height},
        "document_detection": {
            "method": document_method,
            "content_bbox": content_bbox,
            "corners": document_corners.tolist() if document_corners is not None else [],
            "target_size": {"width": width, "height": height},
        },
        "anchor_alignment": anchor_debug,
        "ocr_input_path": str(ocr_input_path),
        "quality": quality,
        "temp_dir": str(temp_dir),
    }
