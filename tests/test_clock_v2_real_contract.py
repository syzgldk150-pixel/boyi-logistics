"""Exercise real clock handlers, opaque codec, schemas and plugin together."""
from dataclasses import replace

import pytest

from agent.tool_registry import validate_schema_instance

from agent.automation_plugins.capability_proxy_v2 import _service_v2_clock_handler
from agent.automation_plugins.first_party_handlers import FirstPartyCoreHandlerPorts, build_first_party_core_handler_map
from agent.automation_plugins.host_capability_registry import HOST_CAPABILITY_API_VERSION, default_host_capability_registry
from service_v2_plugins._shared.clock_runtime import run_clock_service
from tests.test_first_party_core_handlers import _Manager, _SECRET, _context


@pytest.mark.parametrize("branch", ["邵阳分拨中心", "隔离测试" + "".join(chr(0x4e00 + i) for i in range(70))])
def test_full_host_operation_token_survives_submit_and_verify_contracts(branch):
    calls = []

    def platform(arguments):
        calls.append(arguments["action"])
        if arguments["action"] == "precheck":
            return {"ready": True}
        if arguments["action"] == "submit":
            return {"accepted": True, "submitted_at": "2026-09-12 15:50:04"}
        return {"confirmed": True, "clock_type": arguments["clock_type"],
                "observed_at": "2026-09-12 15:50:05", "record_id": "isolated-" + arguments["clock_type"]}

    handlers = build_first_party_core_handler_map(FirstPartyCoreHandlerPorts(
        describe_account=_Manager().require_authenticated_binding, clock_action=platform), cursor_secret=_SECRET)
    registry = default_host_capability_registry()
    lengths = []

    def broker(operation, *, action, role, arguments):
        descriptor = registry.resolve(api_version=HOST_CAPABILITY_API_VERSION, capability=operation, action=action)
        schemas = descriptor.to_mapping()
        validate_schema_instance("clock-input", arguments, schemas["input_schema"])
        context = replace(_context(tool_name="clock_in_dual", role=role, account_ids=("clock-rh",),
                                   action=action, operation=operation),
                          signed_effect=descriptor.governance.effect.value,
                          signed_broker_effect=descriptor.governance.broker_effect)
        result = _service_v2_clock_handler(action, handlers[("browser.invoke", action)])(context, arguments)
        if action == "ronghui.clock.submit":
            lengths.append(len(result["operation_id"]))
        validate_schema_instance("clock-output", result, schemas["output_schema"])
        return result

    result = run_clock_service({"sitecode": "7390004", "sitefbcode": "73901", "sitename": "邵阳大祥站",
                                "sitefbname": branch, "first_type": "交件到港", "second_type": "接件离港",
                                "delay_seconds": 0}, broker, expected_site_name="邵阳大祥站", service_name="isolated-clock")
    assert result["status"] == "SUCCESS", (result, lengths)
    assert calls == ["precheck", "submit", "verify", "submit", "verify"]
    assert len(lengths) == 2 and min(lengths) > 191
    if branch.startswith("隔离测试"):
        assert min(lengths) > 256
