from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agent.tool_registry import ToolRegistry, validate_registry, validate_tool_parameters


def _tool(*, name: str = "valid") -> dict:
    return {
        "name": name,
        "description": "test tool",
        "executor": "tools/runner.py",
        "parameters": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
    }


class ToolRegistryValidationTests(unittest.TestCase):
    def _project_root(self) -> TemporaryDirectory[str]:
        temp_dir = TemporaryDirectory()
        root = Path(temp_dir.name)
        executor = root / "tools" / "runner.py"
        executor.parent.mkdir(parents=True)
        executor.write_text("# test executor\n", encoding="utf-8")
        return temp_dir

    def test_rejects_duplicate_tool_names(self):
        with self._project_root() as root:
            with self.assertRaisesRegex(ValueError, "duplicate name"):
                validate_registry({"tools": [_tool(), _tool()]}, project_root=Path(root))

    def test_rejects_property_definition_without_a_schema_type(self):
        with self._project_root() as root:
            manifest = {"tools": [_tool()]}
            manifest["tools"][0]["parameters"]["properties"]["value"] = {
                "nested": {"type": "string"},
            }
            with self.assertRaisesRegex(ValueError, "type or oneOf"):
                validate_registry(manifest, project_root=Path(root))

    def test_rejects_missing_executor_and_unknown_required_property(self):
        with self._project_root() as root:
            missing = _tool()
            missing["executor"] = "tools/missing.py"
            with self.assertRaisesRegex(ValueError, "executor does not exist"):
                validate_registry({"tools": [missing]}, project_root=Path(root))

            required = _tool()
            required["parameters"]["required"] = ["absent"]
            with self.assertRaisesRegex(ValueError, "unknown properties"):
                validate_registry({"tools": [required]}, project_root=Path(root))

    def test_loader_refuses_invalid_yaml_definition_at_startup(self):
        with self._project_root() as root:
            registry_path = Path(root) / "registry.yaml"
            registry_path.write_text("tools:\n  - name: missing-fields\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "description"):
                ToolRegistry(registry_path=registry_path, project_root=Path(root))

    def test_runtime_validation_enforces_required_type_and_numeric_bounds(self):
        tool = _tool()
        tool["parameters"] = {
            "type": "object",
            "properties": {
                "weight": {"type": "number", "exclusiveMinimum": 0},
                "labels": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["weight"],
        }

        with self.assertRaisesRegex(ValueError, "missing required"):
            validate_tool_parameters(tool, {})
        with self.assertRaisesRegex(ValueError, "greater than 0"):
            validate_tool_parameters(tool, {"weight": 0})
        with self.assertRaisesRegex(ValueError, "must be number"):
            validate_tool_parameters(tool, {"weight": True})
        with self.assertRaisesRegex(ValueError, r"labels\[0\] must be string"):
            validate_tool_parameters(tool, {"weight": 1, "labels": [3]})

        validate_tool_parameters(tool, {"weight": 1.5, "labels": ["ok"]})


if __name__ == "__main__":
    unittest.main()
