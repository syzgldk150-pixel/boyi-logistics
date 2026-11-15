"""Handbook trial: which action would the model pick, with and without a candidate (agent layer P2).

Selection only. Each example is sent once with the current active handbook and
once with the candidate, using the production system prompt and the reviewer's
own tool list; the first chosen action is recorded and nothing is invoked.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Mapping, Sequence

from shared.agent_handbook import HandbookSnapshot

# (actor) -> (model tools, model tool name -> action id, action id -> title, clock hint)
ToolsProvider = Callable[[Any], tuple[list[dict[str, Any]], dict[str, str], dict[str, str], str]]
MAX_TRIAL_EXAMPLES = 10


class HandbookTrial:
    def __init__(self, llm: Any, tools_provider: ToolsProvider, *, active: Callable[[], HandbookSnapshot],
                 timeout_seconds: float = 30.0) -> None:
        self._llm, self._tools, self._active, self._timeout = llm, tools_provider, active, timeout_seconds

    async def _select(self, text: str, handbook: HandbookSnapshot, tools, names, titles, clock_hint) -> str:
        from agent.harness_online import ASK_USER, build_system_prompt

        system = build_system_prompt(clock_hint=clock_hint, query_context=None, with_plugins=True)
        system += handbook.prompt_section(titles)
        try:
            response = await asyncio.wait_for(self._llm.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": text}], tools=tools),
                timeout=self._timeout)
        except Exception:
            return "error"
        for call in response.get("tool_calls") or ():
            name = str((call.get("function") or {}).get("name") or "")
            return "ask_user" if name == ASK_USER else names.get(name, "unknown")
        return "none"

    async def __call__(self, examples: Sequence[Mapping[str, str]], candidate: HandbookSnapshot, actor: Any) -> dict[str, Any]:
        tools, names, titles, clock_hint = await asyncio.to_thread(self._tools, actor)
        active = self._active()
        rows = []
        for example in list(examples)[:MAX_TRIAL_EXAMPLES]:
            before = await self._select(example["utterance"], active, tools, names, titles, clock_hint)
            after = await self._select(example["utterance"], candidate, tools, names, titles, clock_hint)
            rows.append({"utterance": example["utterance"], "expected": example["action"],
                         "expected_title": titles.get(example["action"], example["action"]),
                         "before": before, "before_title": titles.get(before, before),
                         "after": after, "after_title": titles.get(after, after), "pass": after == example["action"]})
        return {"cases": len(rows), "passed": sum(row["pass"] for row in rows),
                "improved": sum(row["pass"] and row["before"] != row["expected"] for row in rows), "rows": rows}


__all__ = ["HandbookTrial", "MAX_TRIAL_EXAMPLES"]
