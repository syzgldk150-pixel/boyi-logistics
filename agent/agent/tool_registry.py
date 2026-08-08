"""工具注册表：从 registry.yaml 加载工具定义，转换为 OpenAI function calling 格式"""

import os
import logging
import time

import yaml

logger = logging.getLogger("agent")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY_PATH = os.path.join(PROJECT_ROOT, "tools", "registry.yaml")


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, dict] = {}
        self._load_time: float = 0
        self.load()

    def load(self):
        """加载或重新加载 registry.yaml"""
        if not os.path.exists(REGISTRY_PATH):
            logger.warning("registry.yaml 不存在: %s", REGISTRY_PATH)
            return

        with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        tools = data.get("tools", [])
        self._tools.clear()
        for tool in tools:
            name = tool["name"]
            executor = os.path.join(PROJECT_ROOT, tool["executor"])
            if not os.path.exists(executor):
                logger.warning("工具 %s 的执行脚本不存在: %s", name, executor)
            self._tools[name] = tool

        self._load_time = time.time()
        logger.info("已加载 %d 个工具定义", len(self._tools))

    def reload_if_changed(self):
        """检查文件是否修改，热加载"""
        if not os.path.exists(REGISTRY_PATH):
            return
        mtime = os.path.getmtime(REGISTRY_PATH)
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
