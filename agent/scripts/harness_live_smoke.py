"""Manual synthetic Harness LLM smoke test; never used by product or CI."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any

from openai import OpenAI

from agent.llm_settings import PROVIDERS


_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,190}$")


def _configure_windows_stdout() -> None:
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True, choices=tuple(sorted(PROVIDERS)))
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    if _MODEL_ID_RE.fullmatch(args.model) is None:
        parser.error("--model is invalid")
    return args


def _tool_call_projection(call: Any) -> dict[str, Any]:
    function = getattr(call, "function", None)
    if getattr(call, "type", None) != "function" or function is None:
        raise RuntimeError("LIVE_SMOKE_TOOL_SELECTION_INVALID")
    if getattr(function, "name", None) != "synthetic_read":
        raise RuntimeError("LIVE_SMOKE_TOOL_SELECTION_INVALID")
    try:
        arguments = json.loads(str(getattr(function, "arguments", "")))
    except json.JSONDecodeError as exc:
        raise RuntimeError("LIVE_SMOKE_TOOL_ARGUMENTS_INVALID") from exc
    if arguments != {}:
        raise RuntimeError("LIVE_SMOKE_TOOL_ARGUMENTS_INVALID")
    call_id = str(getattr(call, "id", "") or "")
    if not call_id:
        raise RuntimeError("LIVE_SMOKE_TOOL_SELECTION_INVALID")
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "synthetic_read", "arguments": "{}"},
    }


def main() -> int:
    _configure_windows_stdout()
    args = _arguments()
    provider = PROVIDERS[args.provider]
    environment_name = str(provider["env_key"])
    api_key = str(os.environ.get(environment_name) or "").strip()
    if not api_key:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "LIVE_SMOKE_KEY_MISSING",
                        "message": f"{environment_name} is not configured",
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2

    client = OpenAI(
        api_key=api_key,
        base_url=str(provider["base_url"]),
        timeout=float(provider["timeout"]),
    )
    tools = [
        {
            "type": "function",
            "function": {
                "name": "synthetic_read",
                "description": "Return one synthetic offline readiness value.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
            },
        }
    ]
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": "Call synthetic_read once, then summarize whether the synthetic value is ready.",
        }
    ]
    try:
        first = client.chat.completions.create(
            model=args.model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=0,
        )
        first_message = first.choices[0].message
        calls = list(first_message.tool_calls or ())
        if len(calls) != 1:
            raise RuntimeError("LIVE_SMOKE_TOOL_SELECTION_INVALID")
        projected_call = _tool_call_projection(calls[0])
        messages.extend(
            (
                {
                    "role": "assistant",
                    "content": first_message.content,
                    "tool_calls": [projected_call],
                },
                {
                    "role": "tool",
                    "tool_call_id": projected_call["id"],
                    "content": '{"synthetic_status":"ready"}',
                },
            )
        )
        final = client.chat.completions.create(
            model=args.model,
            messages=messages,
            temperature=0,
        )
        final_content = str(final.choices[0].message.content or "").strip()
        if not final_content:
            raise RuntimeError("LIVE_SMOKE_FINAL_RESPONSE_MISSING")
    except Exception:
        print(
            '{"error":{"code":"LIVE_SMOKE_FAILED","message":"Synthetic live smoke failed"},"ok":false}'
        )
        return 1

    print(
        json.dumps(
            {
                "ok": True,
                "provider": args.provider,
                "model": args.model,
                "tool_selected": True,
                "final_response_received": True,
                "business_data_used": False,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
