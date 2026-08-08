import copy
import json
import re
from pathlib import Path
from typing import Any

from config import Settings


DEFAULT_TEMPLATE_NAME = "template_new"


def _safe_template_name(raw_name: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_-]+", "_", (raw_name or "").strip())
    value = value.strip("_")
    return value or DEFAULT_TEMPLATE_NAME


def _blank_template_spec() -> dict[str, Any]:
    return {
        "template_name": DEFAULT_TEMPLATE_NAME,
        "description": "",
        "preprocess": {
            "target_size": [2688, 1512],
            "crop_padding_ratio": 0.006,
            "document_expand_ratio": 0.12,
            "document_expand_top_ratio": 0.22,
            "document_expand_right_ratio": 0.05,
            "document_expand_bottom_ratio": 0.06,
            "document_expand_left_ratio": 0.05,
            "blur_threshold": 90.0,
            "min_brightness": 55.0,
            "max_brightness": 230.0,
            "min_document_fill_ratio": 0.42,
            "anchor_alignment": {
                "enabled": False,
                "anchors": [],
            },
        },
        "fields": [],
    }


class TemplateStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.templates_dir = settings.templates_dir
        self.state_path = settings.template_state_path
        self.templates_dir.mkdir(parents=True, exist_ok=True)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._bootstrap_templates()

    def _bootstrap_templates(self) -> None:
        existing = list(self.templates_dir.glob("*.json"))
        if not existing and self.settings.template_path.exists():
            legacy_spec = self.load_template_from_path(self.settings.template_path)
            name = _safe_template_name(legacy_spec.get("template_name", "logistics_waybill_v1"))
            self._write_template_file(name, legacy_spec)

        if not self.state_path.exists():
            templates = self.list_templates()
            active_name = templates[0]["template_name"] if templates else DEFAULT_TEMPLATE_NAME
            self.set_active_template_name(active_name)

    def list_templates(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        active_name = self._read_active_template_name()
        for path in sorted(self.templates_dir.glob("*.json")):
            spec = self.load_template_from_path(path)
            template_name = _safe_template_name(spec.get("template_name", path.stem))
            items.append(
                {
                    "template_name": template_name,
                    "description": str(spec.get("description", "") or ""),
                    "field_count": len(spec.get("fields", [])),
                    "path": str(path),
                    "active": template_name == active_name,
                }
            )
        return items

    def _read_active_template_name(self) -> str:
        if not self.state_path.exists():
            return ""
        return self.state_path.read_text(encoding="utf-8-sig").strip()

    def get_active_template_name(self) -> str:
        value = self._read_active_template_name()
        if value:
            try:
                self.get_template_spec(value)
                return value
            except FileNotFoundError:
                pass
        templates = self.list_templates()
        if templates:
            return templates[0]["template_name"]
        return DEFAULT_TEMPLATE_NAME

    def set_active_template_name(self, template_name: str) -> None:
        safe_name = _safe_template_name(template_name)
        self.get_template_spec(safe_name)
        self.state_path.write_text(safe_name, encoding="utf-8")

    def get_active_template_spec(self) -> dict[str, Any]:
        return self.get_template_spec(self.get_active_template_name())

    def get_template_spec(self, template_name: str) -> dict[str, Any]:
        safe_name = _safe_template_name(template_name)
        path = self.templates_dir / f"{safe_name}.json"
        if not path.exists():
            raise FileNotFoundError(f"Template {safe_name} not found.")
        spec = self.load_template_from_path(path)
        spec["template_name"] = safe_name
        spec.setdefault("description", "")
        spec.setdefault("preprocess", _blank_template_spec()["preprocess"])
        spec.setdefault("fields", [])
        return spec

    def build_new_template_spec(self, copy_from: str | None = None) -> dict[str, Any]:
        if copy_from:
            spec = copy.deepcopy(self.get_template_spec(copy_from))
            spec["template_name"] = DEFAULT_TEMPLATE_NAME
            spec["description"] = str(spec.get("description", "") or "")
            return spec
        return _blank_template_spec()

    def save_template_spec(self, spec: dict[str, Any], original_template_name: str | None = None) -> str:
        normalized = copy.deepcopy(spec)
        template_name = _safe_template_name(normalized.get("template_name", ""))
        normalized["template_name"] = template_name
        normalized["description"] = str(normalized.get("description", "") or "").strip()
        normalized["preprocess"] = normalized.get("preprocess", _blank_template_spec()["preprocess"])
        normalized["fields"] = normalized.get("fields", [])
        self._write_template_file(template_name, normalized)

        if original_template_name:
            old_name = _safe_template_name(original_template_name)
            if old_name != template_name:
                old_path = self.templates_dir / f"{old_name}.json"
                if old_path.exists():
                    old_path.unlink()
        return template_name

    def load_template_from_path(self, path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8-sig") as handle:
            return json.load(handle)

    def _write_template_file(self, template_name: str, spec: dict[str, Any]) -> None:
        safe_name = _safe_template_name(template_name)
        path = self.templates_dir / f"{safe_name}.json"
        with path.open("w", encoding="utf-8") as handle:
            json.dump(spec, handle, ensure_ascii=False, indent=2)
