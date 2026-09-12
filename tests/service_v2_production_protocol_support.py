"""Actual V2 ZIP, isolated CPython, Unix broker and production connectors."""
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import zipfile

from agent.automation_plugins.broker import LocalBrokerCapabilityIssuer, LocalCoreAutomationBroker
from agent.automation_plugins.capability_proxy_v2 import build_service_v2_capability_handler_map
from agent.automation_plugins.core_adapter import RegisteredCoreAutomationBrokerAdapter
from agent.automation_plugins.execution import GenerationBoundResult, PluginExecutionRouter
from agent.automation_plugins.models import GenerationVerificationContext, RuntimeLeaseOutcome
from agent.automation_plugins.host_capability_registry import CapabilityEffect, governance_for_effect
from agent.orchestration.models import OperationType, PlanStep, RiskLevel
from agent.orchestration.result_verifier import ResultVerifier
from agent.automation_plugins.manifest_v2 import AutomationPluginManifestV2
from agent.automation_plugins.sandbox import BubblewrapPluginSandbox
from agent.automation_plugins.service_v2_contract import ServiceV2ProjectContract
from agent.automation_plugins.storage import LockedVirtualEnvironmentBuilder
from service_v2_plugins._shared.build_zip import build_plugin_zip
from tests.test_automation_plugin_connector_runtime_v2 import _AccountResolver, _ResourceResolver


class PackagedConnectorHost:
    def __init__(self, path, plugin_id, registry, context, *, resource_resolver=None, account_resolver=None):
        self.root = path
        path.mkdir(parents=True,exist_ok=True)
        self.context = context
        self.registry = registry
        self.receipts = []
        self.observations = ()
        self.resource_resolver = resource_resolver or _ResourceResolver()
        self.account_resolver = account_resolver or _AccountResolver(system="ronghui")
        source = Path(__file__).resolve().parents[1]/"agent/service_v2_plugins"/plugin_id
        package = build_plugin_zip(source,path/"plugin.zip")
        with zipfile.ZipFile(package) as archive:
            self.raw = json.loads(archive.read("manifest.json"))
            archive.extractall(path/"package")
        self.manifest = AutomationPluginManifestV2.from_mapping(self.raw)
        self.contract = ServiceV2ProjectContract.from_manifest(self.manifest)
        self.python = LockedVirtualEnvironmentBuilder().build(path,self.manifest)

    @contextmanager
    def unit_of_work(self):
        def project(identifier):
            assert identifier == self.context.automation_id
            return {"automation_id":identifier,"plugin_id":self.manifest.plugin_id}
        def version(identifier, number):
            assert identifier == self.manifest.plugin_id and number == self.manifest.version
            return {"runtime_model":"SERVICE_V2","manifest_json":self.raw}
        yield SimpleNamespace(automation_plugins=SimpleNamespace(get_project=project,get_version=version))

    def execute(self, arguments, *, operation, entrypoint="console"):
        return asyncio.run(self._execute(arguments,operation=operation,entrypoint=entrypoint))

    def verify(self, result, arguments, target, effect, *, expect_success=True):
        capability = self.resolved_capability
        step = PlanStep(step_key="isolated-call", tool_name=capability["name"], tool_version=capability["version"],
            operation_type=OperationType(capability["operation_type"]), arguments=arguments, account_id=None,
            depends_on=(), idempotency_key="isolated-call", expected_evidence=(capability["evidence"],),
            postconditions=tuple(capability["postconditions"]), risk_level=RiskLevel(capability["risk_level"]))
        accounts = tuple(sorted({account for value in self.context.account_bindings.values()
            for account in ([value] if isinstance(value, str) else value)}))
        started = sum(row["write_started"] for row in self.observations)
        lease_outcome = PluginExecutionRouter._lease_outcome(
            capability, result, process_launched=True, started_mutating_call_count=started,
            verified_business_failure=PluginExecutionRouter._verified_business_failure(
                capability, result, {"started_mutating_call_count": started,
                    "host_call_observations": self.observations}))
        proof = GenerationVerificationContext(automation_id=self.context.automation_id, generation=1,
            lease_id=self.write_identity["lease_id"], invocation_id=self.write_identity["invocation_id"], account_ids=accounts,
            account_bindings_sha256="a" * 64, requires_write_verification=lease_outcome == RuntimeLeaseOutcome.VERIFYING,
            started_mutating_call_count=started,
            host_call_observations=self.observations, plugin_id=self.manifest.plugin_id)
        settled = []
        outcome = ResultVerifier(SimpleNamespace(finalize_generation_write=lambda **values: settled.append(values))).verify(
            step, GenerationBoundResult(result, verification=proof), capability)
        self.verification_settlements = settled
        if expect_success:
            assert outcome.accepted, (outcome.code, outcome.message)
        return outcome

    async def _execute(self, arguments, *, operation, entrypoint):
        identifier = self.manifest.plugin_id
        declarations = self.raw["contributes"][entrypoint]
        contribution = declarations[0]
        service = contribution["service"]
        effects = {item["name"]:item["effect"] for provided in self.raw["provides"] if provided["service"]==service for item in provided["operations"]}
        effect = effects[operation]
        base = {**self.contract.tool_contract, "_plugin_runtime": {"runtime_model": "SERVICE_V2",
            "plugin_id": self.manifest.plugin_id, "contributions": self.raw["contributes"],
            "service_contracts": {"provides": self.raw["provides"]},
            "compiled_invocations": {key: {"governance": value["governance"], "target": {
                "service": value["service"], "operation": value["operation"],
                "contribution_id": key, "contribution_kind": value["contribution_kind"]}}
                for key, value in self.contract.invocation_contracts.items()}}}
        self.resolved_capability = PluginExecutionRouter._service_contribution_capability(
            base, contribution_id=contribution["id"], arguments=arguments)
        assert self.resolved_capability["operation"] == operation
        assert self.resolved_capability["effect"] == effect
        issuer = LocalBrokerCapabilityIssuer(self.root/"broker.sock",write_attempt_recorder=self.receipts.append)
        adapter = RegisteredCoreAutomationBrokerAdapter(
            handlers=build_service_v2_capability_handler_map(self,connector_registry=self.registry),
            account_resolver=self.account_resolver,resource_resolver=self.resource_resolver,connector_registry=self.registry)
        local = LocalCoreAutomationBroker(issuer=issuer,adapter=adapter)
        await local.start()
        process = None
        capability = None
        try:
            self.write_identity = {"automation_id":self.context.automation_id,"plugin_id":identifier,
                "generation":1,"lease_id":str(uuid4()),"invocation_id":str(uuid4())}
            capability = issuer.issue(automation_id=self.context.automation_id,plugin_version=self.manifest.version,
                tool_name=f"service.{identifier}",ttl_seconds=60,
                runtime_permissions={**self.contract.runtime_permissions,"_service_effect_ceiling":effect},
                account_roles=self.contract.account_roles,resource_roles=self.contract.resource_roles,
                account_bindings=self.context.account_bindings,resource_bindings=self.context.resource_bindings,
                write_attempt_context=self.write_identity)
            process = await BubblewrapPluginSandbox(Path("/usr/bin/bwrap")).launch(
                install_root=self.root,python_relative=self.python.relative_to(self.root).as_posix(),
                entrypoint_relative="payload/main.py",broker_socket_path=self.root/"broker.sock",
                environment={"BOYI_PLUGIN_EXECUTION_CAPABILITY":capability,"BOYI_PLUGIN_ID":identifier,
                    "BOYI_PLUGIN_VERSION":self.manifest.version,"BOYI_AUTOMATION_ID":self.context.automation_id,
                    "BOYI_PLUGIN_BROKER_CALL_TIMEOUT":"30"})
            request = {"schema_version":2,"runtime_model":"SERVICE_V2","automation_id":self.context.automation_id,
                "plugin_id":identifier,"plugin_version":self.manifest.version,"entrypoint":entrypoint,
                "target":{"service":service,"operation":operation,"contribution_id":contribution["id"],"contribution_kind":entrypoint},
                "governance":governance_for_effect(CapabilityEffect(effect)).to_mapping(),"arguments":arguments}
            output, error = await asyncio.wait_for(process.communicate(json.dumps(request).encode()),timeout=45)
            assert process.returncode == 0, error.decode()
            self.observations = issuer.broker_call_observations(capability)
            result = json.loads(output)
            if result.get("status") == "SUCCESS":
                self.verified_outcome = self.verify(result, arguments, request["target"], effect)
            else:
                self.verified_outcome = self.verify(result, arguments, request["target"], effect, expect_success=False)
            return result
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            if capability is not None:
                await issuer.drain_host_calls(capability)
                issuer.revoke(capability)
            await local.stop()
