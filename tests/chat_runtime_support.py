"""Compose the real common chat engine with explicit isolated model/read ports."""
import asyncio
from types import SimpleNamespace

from agent.chat_text_queries import ChatTextQueries
from agent.harness.catalog import HarnessToolCatalog
from agent.harness.sessions import InMemoryHarnessSessionRepository
from agent.harness_application import HarnessConversationService, TrustedHarnessInvocationAdapter, build_fixed_harness_tools
from agent.harness_online import OnlineHarnessSidecar
from agent.llm_client import LLMClient


def configure_chat(core, *, plugins=None, handlers=None, authority=None, include_fixed=False):
    if authority is not None:
        core.identity_access = authority
    model = LLMClient()
    model.chat = core.llm.chat
    model.public_status = lambda: {"configured": True}
    calls = []
    def reader(actor, name, params, request_id):
        result = asyncio.run(core.execute_tool(name, params, actor=actor,
            source="feishu" if actor.actor_type.value == "feishu_user" else "console", idempotency_key=request_id))
        calls.append({"tool_name": name, "params": params, "result": result})
        return result
    def sidecar(actor, request_id):
        adapter = TrustedHarnessInvocationAdapter(policy_service=SimpleNamespace(identity_access=authority),
            actor=actor, base_request_id=request_id, fixed_handlers=handlers or {})
        catalog = HarnessToolCatalog(invocation_port=adapter,
            fixed_tools=build_fixed_harness_tools() if include_fixed else (), tool_allowed=adapter.visible_tool_filter())
        return OnlineHarnessSidecar(catalog=catalog, llm=model,
            plugin_turn=plugins.turn(actor=actor, source="feishu" if actor.actor_type.value == "feishu_user" else "console", request_id=request_id) if plugins else None)
    service = HarnessConversationService(repository=InMemoryHarnessSessionRepository(), sidecar_factory=sidecar,
        timeout_seconds=30, identity_access=authority, text_queries=ChatTextQueries(reader, today=core._today_provider))
    core.configure_conversations(service)
    return service, calls
