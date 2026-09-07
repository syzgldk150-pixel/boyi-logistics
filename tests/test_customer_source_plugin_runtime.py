"""Customer collector real subprocess, local broker and generation verification."""
from __future__ import annotations

from tests.test_first_party_action_payloads import (
    AccountManagerSessionResolver,
    Actor,
    ActorType,
    Command,
    FirstPartyCoreHandlerPorts,
    GenerationBoundResult,
    LocalBrokerCapabilityIssuer,
    LocalCoreAutomationBroker,
    OperationType,
    Path,
    PilotProjectionService,
    PlanStep,
    PluginExecutionRouter,
    PluginTrustSource,
    RegisteredCoreAutomationBrokerAdapter,
    RegisteredToolExecutionAdapter,
    ResultVerifier,
    RiskLevel,
    RuntimeLeaseOutcome,
    _ActiveBindingDescriptorAlias,
    _Catalog,
    _NoCoreFallback,
    _NoopIntegrity,
    _PayloadSandbox,
    _ProjectionUow,
    _ReadGenerationLeases,
    _sha,
    _walk_keys,
    asyncio,
    build_builtin_release_package,
    build_first_party_core_handler_map,
    copy,
    first_party_payload_files,
    hashlib,
    json,
    manifests as _manifests_fixture,
    os,
    sys,
    uuid,
)


manifests = _manifests_fixture

def test_customer_problem_payload_calls_real_local_broker_without_account_ids(
    manifests,
    tmp_path: Path,
) -> None:
    manifest = manifests["sync_customer_service_problems"]
    package_root = tmp_path / "payload"
    package_root.mkdir()
    for relative, content in first_party_payload_files(manifest).items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    resolved_accounts: list[str] = []
    core_calls: list[tuple[str, tuple[str, ...], dict]] = []

    class Manager(_ActiveBindingDescriptorAlias):
        def require_authenticated_binding(self, account_id):
            resolved_accounts.append(account_id)
            assert account_id in {"account-a", "account-b"}
            return {
                "account_id": account_id,
                "system": "ronghui" if account_id == "account-a" else "yunda",
                "account_purpose": "customer_service",
                "session_profile": "not-exposed",
            }

    def list_page(context, arguments):
        assert context.automation_id == "customer-instance"
        assert context.operation == "browser.invoke"
        assert context.role == "customer_service_source"
        assert context.account_ids == ("account-a", "account-b")
        assert not any(key.endswith("account_id") for key in arguments)
        core_calls.append((context.action, context.account_ids, dict(arguments)))
        return {
            "items": [
                {
                    "platform": "ronghui",
                    "source_direction": "received",
                    "external_id": "p-1",
                }
            ],
            "pagination_complete": True,
            "next_cursor": None,
            "evidence_ref": "broker-evidence:page-1",
        }

    def detail(context, arguments):
        core_calls.append((context.action, context.account_ids, dict(arguments)))
        return {
            "dedupe_key": arguments["dedupe_key"],
            "resolved": False,
            "evidence_ref": "broker-evidence:detail-1",
        }

    async def run() -> tuple[int, bytes, bytes]:
        socket_path = tmp_path / "broker.sock"
        issuer = LocalBrokerCapabilityIssuer(socket_path)
        adapter = RegisteredCoreAutomationBrokerAdapter(
            handlers={
                ("browser.invoke", "customer_problem.list_page"): list_page,
                ("browser.invoke", "customer_problem.detail"): detail,
            },
            account_resolver=AccountManagerSessionResolver(Manager()),
        )
        broker = LocalCoreAutomationBroker(issuer=issuer, adapter=adapter)
        await broker.start()
        capability = issuer.issue(
            automation_id="customer-instance",
            plugin_version=manifest.version,
            tool_name=manifest.plugin_id,
            ttl_seconds=30,
            runtime_permissions=manifest.runtime_permissions,
            account_roles=manifest.account_roles,
            resource_roles=manifest.resource_roles,
            account_bindings={"customer_service_source": ("account-a", "account-b")},
            resource_bindings={},
        )
        environment = {
            **os.environ,
            "BOYI_PLUGIN_BROKER_ENDPOINT": issuer.broker_endpoint,
            "BOYI_PLUGIN_EXECUTION_CAPABILITY": capability,
            "BOYI_PLUGIN_ID": manifest.plugin_id,
            "BOYI_PLUGIN_BROKER_CALL_TIMEOUT": "30",
        }
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(package_root / "main.py"),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        request = {
            "schema_version": 1,
            "automation_id": "customer-instance",
            "plugin_id": manifest.plugin_id,
            "plugin_version": manifest.version,
            "arguments": {"direction": "both"},
        }
        stdout, stderr = await process.communicate(
            json.dumps(request, separators=(",", ":")).encode("utf-8")
        )
        await broker.stop()
        return int(process.returncode or 0), stdout, stderr

    returncode, stdout, stderr = asyncio.run(run())
    assert returncode == 0, stderr.decode("utf-8", errors="replace")
    assert stdout, stderr.decode("utf-8", errors="replace")
    result = json.loads(stdout)
    assert result["status"] == "SUCCESS"
    assert result["meta"]["pagination_complete"] is True
    assert result["meta"]["evidence_refs"] == ["broker-evidence:page-1"]
    assert core_calls == [
        (
            "customer_problem.list_page",
            ("account-a", "account-b"),
            {"cursor": None, "direction": "both", "page_size": 200},
        )
    ]
    assert resolved_accounts == ["account-a", "account-b"]
    assert not any(
        key.lower() == "account_id" or key.lower().endswith("_account_id")
        for key in _walk_keys(result)
    )


def test_customer_payload_runs_through_router_and_result_verifier(
    manifests,
    tmp_path: Path,
    monkeypatch,
) -> None:
    manifest = manifests["sync_customer_service_problems"]
    install_root = tmp_path / "generation-1"
    package_root = install_root / "package"
    for relative, content in first_party_payload_files(manifest).items():
        target = package_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    python_marker = install_root / "venv" / "bin" / "python"
    python_marker.parent.mkdir(parents=True)
    python_marker.write_text("isolated-python", encoding="utf-8")

    manifest_mapping = manifest.to_mapping()
    capability = dict(manifest_mapping["tool_contract"])
    account_bindings = {
        "customer_service_source": ["account-a", "account-b"],
    }
    capability["_plugin_runtime"] = {
        "automation_id": "customer-instance",
        "plugin_id": manifest.plugin_id,
        "version": manifest.version,
        "generation": 1,
        "package_sha256": hashlib.sha256(
            build_builtin_release_package(manifest)
        ).hexdigest(),
        "manifest_sha256": manifest.manifest_sha256,
        "trust_source": PluginTrustSource.ED25519_FIRST_PARTY.value,
        "install_root": str(install_root),
        "runtime": manifest_mapping["runtime"],
        "install_metadata": {"python_relative": "venv/bin/python"},
        "runtime_permissions": manifest_mapping["runtime_permissions"],
        "account_roles": manifest_mapping["account_roles"],
        "resource_roles": manifest_mapping["resource_roles"],
        "account_bindings": account_bindings,
        "resource_bindings": {},
        "compiled_invocations": {
            "console": copy.deepcopy(manifest_mapping["invocation_contracts"]["console"]),
        },
        "governance_anchor": copy.deepcopy(manifest_mapping["governance_anchor"]),
    }
    leases = _ReadGenerationLeases(capability)
    resolved_accounts: list[str] = []

    class Manager(_ActiveBindingDescriptorAlias):
        def require_authenticated_binding(self, account_id):
            resolved_accounts.append(account_id)
            return {
                "account_id": account_id,
                "system": "ronghui" if account_id == "account-a" else "yunda",
                "account_purpose": "customer_service",
                "session_profile": f"profile-{account_id}",
            }

    def customer_action(arguments):
        assert arguments["account_id"] in {"account-a", "account-b"}
        assert arguments["action"] == "query"
        include = (
            arguments["platform"] == "ronghui"
            and arguments["direction"] == "received"
        )
        rows = (
            [
                {
                    "platform": "ronghui",
                    "account_id": arguments["account_id"],
                    "source_direction": "received",
                    "external_id": "p-1",
                    "status": "待处理",
                    "reply_text": "",
                }
            ]
            if include
            else []
        )
        return {
            "ok": True,
            "rows": rows,
            "source_site_code": "fixture-site",
            "stats": {
                "total": len(rows),
                "returned": len(rows),
                "total_authoritative": True,
            },
        }

    async def run():
        issuer = LocalBrokerCapabilityIssuer(tmp_path / "router-broker.sock")
        core_adapter = RegisteredCoreAutomationBrokerAdapter(
            handlers=build_first_party_core_handler_map(
                FirstPartyCoreHandlerPorts(
                    describe_account=Manager().require_authenticated_binding,
                    customer_action=customer_action,
                ),
                cursor_secret=b"router-projection-customer-secret-v1",
            ),
            account_resolver=AccountManagerSessionResolver(Manager()),
        )
        broker = LocalCoreAutomationBroker(issuer=issuer, adapter=core_adapter)
        await broker.start()
        try:
            router = PluginExecutionRouter(
                core_executor=_NoCoreFallback(),
                capability_issuer=issuer,
                integrity_verifier=_NoopIntegrity(),
                sandbox_launcher=_PayloadSandbox(),
                generation_leases=leases,
                release_hold_provider=lambda: False,
            )
            adapter = RegisteredToolExecutionAdapter(
                catalog=_Catalog(capability),
                executor=router,
            )
            step = PlanStep(
                step_key="customer-read",
                tool_name=str(capability["name"]),
                tool_version=str(capability["version"]),
                operation_type=OperationType.READ,
                arguments={"direction": "both"},
                account_id=None,
                depends_on=(),
                idempotency_key="customer-read-1",
                expected_evidence=(dict(capability["evidence"]),),
                postconditions=tuple(dict(item) for item in capability["postconditions"]),
                risk_level=RiskLevel.LOW,
                requires_approval=False,
            )
            raw = await adapter.execute_step(
                step,
                run_id=str(uuid.uuid4()),
                step_id=str(uuid.uuid4()),
                execution_context={
                    "source": "console",
                    "_automation_project_invocation": {
                        "schema_version": 1,
                        "automation_id": "customer-instance",
                        "automation_generation": 1,
                        "entrypoint": "console",
                        "contract_id": "console",
                        "contract_hash": "a" * 64,
                        "policy_version": 1,
                        "project_configuration_version": 1,
                        "request_id": str(uuid.uuid4()),
                    },
                },
            )
            verified = ResultVerifier(leases).verify(step, raw, capability)
            return raw, verified, step
        finally:
            await broker.stop()

    raw, verified, step = asyncio.run(run())
    assert isinstance(raw, GenerationBoundResult), raw
    assert raw["status"] == "SUCCESS"
    assert "account_id" not in raw["meta"]
    assert raw.generation_verification.account_ids == ("account-a", "account-b")
    assert len(raw["meta"]["evidence_refs"]) == 4
    assert all(
        value.startswith("broker-evidence:customer-list-page:")
        for value in raw["meta"]["evidence_refs"]
    )
    assert verified.accepted is True
    assert verified.code == "VERIFIED"
    assert verified.result is not None
    assert verified.result.meta["account_id"] == (
        f"binding-set:{_sha(account_bindings)}"
    )
    assert verified.generation_verification is raw.generation_verification
    projection_uow = _ProjectionUow()
    projection_uow.connection = object()
    publications = []
    monkeypatch.setattr("agent.orchestration.pilot_projection.publish_customer_collection",
        lambda connection, **kwargs: publications.append(kwargs))
    projection = PilotProjectionService().project_successful_step(
        uow=projection_uow,
        run={
            "run_id": "customer-run",
            "work_item_id": "gateway-item",
            "correlation_id": "customer-correlation",
        },
        step_row={"step_id": "customer-step", "attempt_count": 1},
        step=step,
        command=Command(
            command_type="tool.execute",
            source="console",
            actor=Actor(ActorType.CONSOLE_ADMIN, "admin-1"),
            parameters={"tool_name": "sync_customer_service_problems"},
            idempotency_key="customer-projection-command",
        ),
        result=verified.result,
        generation_verification=verified.generation_verification,
    )
    assert projection is not None
    assert len(publications) == 1
    assert publications[0]["verification"].host_call_observations
    assert len(projection_uow.work_items.items) == 1
    projected_key = next(iter(projection_uow.work_items.items))
    assert projected_key.startswith("problem:v2:")
    assert "account-a" not in json.dumps(raw, sort_keys=True)
    assert "account-b" not in json.dumps(raw, sort_keys=True)
    projected_customer_evidence = next(
        row
        for row in projection_uow.evidence.rows
        if row["source_record_type"] == "customer_problem"
    )
    assert projected_customer_evidence["account_id"] == "account-a"
    assert leases.released == [RuntimeLeaseOutcome.SUCCEEDED]
    assert set(resolved_accounts) == {"account-a", "account-b"}
