"""Forward the plugin dispatcher's fixed commands to a test's fake project service."""
from __future__ import annotations

from agent.orchestration.preview_entrypoints import PREVIEW_ROUTES
from feishu import message_handler


# Fixed routes and the commands their current plugin packages declare.
FIXED_COMMAND_ROUTES = {
    **{command: route for route, command in PREVIEW_ROUTES.items()},
    "统计到货数据": "builtin.arrival_stats",
    "arrive-list": "builtin.arrive_list",
}


class EntrypointDispatcher:
    """Stand in for ``ServiceV2FeishuDispatcher``; unknown commands do not match."""

    async def dispatch(self, *, command_text, selected_bill_codes=None, runtime_inputs=None,
                       conversation_target=None, **kwargs):
        del runtime_inputs
        route_key = FIXED_COMMAND_ROUTES.get(command_text)
        service = message_handler._AUTOMATION_PROJECT_ENTRYPOINTS
        if route_key is None or not hasattr(service, "invoke_feishu"):
            return None
        body = {} if selected_bill_codes is None else {"selected_bill_codes": selected_bill_codes}
        if conversation_target is not None:
            kwargs["conversation_target"] = conversation_target
        return await service.invoke_feishu(route_key=route_key, envelope={"body": body, "query": {}}, **kwargs)
