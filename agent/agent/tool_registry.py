"""工具注册表：从 registry.yaml 加载工具定义，转换为 OpenAI function calling 格式"""

import logging
import time
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("agent")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = PROJECT_ROOT / "tools" / "registry.yaml"
_JSON_SCHEMA_TYPES = frozenset({"string", "integer", "number", "boolean", "array", "object"})


def _validation_error(index: int, message: str) -> ValueError:
    return ValueError(f"Invalid tools registry entry #{index}: {message}")


def _validate_parameter_schema(index: int, schema: Any) -> None:
    if not isinstance(schema, dict):
        raise _validation_error(index, "parameters must be a mapping")
    if schema.get("type") != "object":
        raise _validation_error(index, "parameters.type must be object")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise _validation_error(index, "parameters.properties must be a mapping")
    required = schema.get("required", [])
    if not isinstance(required, list) or any(not isinstance(name, str) for name in required):
        raise _validation_error(index, "parameters.required must be a string list")
    unknown_required = set(required) - set(properties)
    if unknown_required:
        raise _validation_error(index, f"parameters.required has unknown properties: {sorted(unknown_required)}")
    for name, property_schema in properties.items():
        if not isinstance(name, str) or not name:
            raise _validation_error(index, "parameter property names must be non-empty strings")
        if not isinstance(property_schema, dict):
            raise _validation_error(index, f"parameters.properties.{name} must be a mapping")
        _validate_value_schema(index, property_schema, f"parameters.properties.{name}")


def _validate_value_schema(index: int, schema: dict[str, Any], path: str) -> None:
    property_type = schema.get("type")
    if property_type in _JSON_SCHEMA_TYPES:
        if property_type == "array":
            items = schema.get("items")
            if not isinstance(items, dict) or items.get("type") not in _JSON_SCHEMA_TYPES:
                raise _validation_error(index, f"{path}.items.type is invalid")
        return
    one_of = schema.get("oneOf")
    if not isinstance(one_of, list) or not one_of:
        raise _validation_error(index, f"{path}.type or oneOf is invalid")
    for candidate in one_of:
        if not isinstance(candidate, dict) or candidate.get("type") not in _JSON_SCHEMA_TYPES:
            raise _validation_error(index, f"{path}.oneOf has an invalid type")
        if candidate.get("type") == "array":
            items = candidate.get("items")
            if not isinstance(items, dict) or items.get("type") not in _JSON_SCHEMA_TYPES:
                raise _validation_error(index, f"{path}.oneOf array items.type is invalid")


def validate_registry(data: Any, *, project_root: Path = PROJECT_ROOT) -> list[dict[str, Any]]:
    """Validate the tool manifest completely before exposing any executor."""

    if not isinstance(data, dict):
        raise ValueError("Invalid tools registry: root must be a mapping")
    tools = data.get("tools")
    if not isinstance(tools, list):
        raise ValueError("Invalid tools registry: tools must be a list")

    seen_names: set[str] = set()
    validated: list[dict[str, Any]] = []
    resolved_root = project_root.resolve()
    for index, tool in enumerate(tools, start=1):
        if not isinstance(tool, dict):
            raise _validation_error(index, "entry must be a mapping")
        name = tool.get("name")
        if not isinstance(name, str) or not name.strip():
            raise _validation_error(index, "name must be a non-empty string")
        if name in seen_names:
            raise _validation_error(index, f"duplicate name: {name}")
        seen_names.add(name)
        if not isinstance(tool.get("description"), str) or not str(tool["description"]).strip():
            raise _validation_error(index, "description must be a non-empty string")
        executor_value = tool.get("executor")
        if not isinstance(executor_value, str) or not executor_value.strip():
            raise _validation_error(index, "executor must be a non-empty relative path")
        executor = (resolved_root / executor_value).resolve()
        if executor == resolved_root or resolved_root not in executor.parents:
            raise _validation_error(index, "executor must stay inside the Agent project root")
        if not executor.is_file():
            raise _validation_error(index, f"executor does not exist: {executor_value}")
        _validate_parameter_schema(index, tool.get("parameters", {"type": "object", "properties": {}}))
        validated.append(tool)
    return validated


class ToolRegistry:
    def __init__(self, registry_path: Path = REGISTRY_PATH, *, project_root: Path = PROJECT_ROOT):
        self._registry_path = Path(registry_path)
        self._project_root = Path(project_root)
        self._tools: dict[str, dict] = {}
        self._load_time: float = 0
        self.load()

    def load(self):
        """加载或重新加载 registry.yaml"""
        if not self._registry_path.is_file():
            raise RuntimeError(f"tools registry does not exist: {self._registry_path}")

        with self._registry_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        tools = validate_registry(data, project_root=self._project_root)
        self._tools.clear()
        for tool in tools:
            name = tool["name"]
            self._tools[name] = tool

        self._load_time = time.time()
        logger.info("已加载 %d 个工具定义", len(self._tools))

    def reload_if_changed(self):
        """检查文件是否修改，热加载"""
        if not self._registry_path.is_file():
            raise RuntimeError(f"tools registry does not exist: {self._registry_path}")
        mtime = self._registry_path.stat().st_mtime
        if mtime > self._load_time:
            logger.info("registry.yaml 已更新，重新加载")
            self.load()

    def get_openai_tools(self) -> list[dict]:
        """转换为 OpenAI function calling 格式"""
        self.reload_if_changed()
        result = []
        for tool in self._tools.values():
            result.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
                },
            })
        return result

    def get_tool(self, name: str) -> dict | None:
        self.reload_if_changed()
        return self._tools.get(name)

    def list_tools(self) -> list[str]:
        return list(self._tools.keys())
