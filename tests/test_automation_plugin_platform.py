from __future__ import annotations

import asyncio
import base64
import copy
import json
import subprocess
import sys
import time
import uuid
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from Crypto.PublicKey import ECC

from agent.automation_plugins.broker import LocalBrokerCapabilityIssuer
from agent.automation_plugins.catalog import PluginCatalog, project_contract_fragment
from agent.automation_plugins.configuration import normalize_project_schedule
from agent.automation_plugins.core_adapter import (
    AccountManagerSessionResolver,
    RegisteredCoreAutomationBrokerAdapter,
)
from agent.automation_plugins.errors import (
    PluginConflictError,
    PluginExecutionError,
    PluginManifestError,
    PluginPackageError,
    PluginSignatureError,
)
from agent.automation_plugins.first_party import (
    FIRST_PARTY_PACKAGE_VERSION,
    SourceFirstPartyPackageProvider,
    expected_first_party_automation_ids,
    expected_first_party_plugin_ids,
    first_party_instance_seeds,
    first_party_payload_files,
    preflight_signed_first_party_release,
    release_first_party_digest_snapshot,
    release_first_party_automation_ids,
    release_first_party_plugin_ids,
    resolve_release_first_party_manifests,
)
from agent.automation_plugins.manifest import AutomationPluginManifest, canonical_json_bytes
from agent.automation_plugins.models import (
    PluginInstanceRecord,
    PluginProjectState,
    PluginTrustSource,
    PluginVersionRecord,
)
from agent.automation_plugins.package import (
    Ed25519PackageSigner,
    Ed25519TrustStore,
    _file_statement,
    _sha256,
    _zip_bytes,
    build_signed_plugin_zip,
    extract_verified_package,
    verify_signed_plugin_zip,
)
from agent.automation_plugins.sandbox import BubblewrapPluginSandbox, FailClosedPluginSandbox
from agent.automation_plugins.release_config import load_production_plugin_release_config
from agent.automation_plugins.sdk import PLUGIN_SDK_SOURCE
from agent.automation_plugins.storage import FilesystemPluginStorage, LockedVirtualEnvironmentBuilder
from agent.tms_runtime.errors import TMSAuthStateError
from agent.tool_registry import ToolRegistry


@pytest.fixture(scope="module")
def core_catalog() -> ToolRegistry:
    return ToolRegistry()


def _uploaded_manifest(core_catalog: ToolRegistry) -> AutomationPluginManifest:
    source = resolve_release_first_party_manifests(core_catalog)[
        "sync_arrive_list"
    ].to_mapping()
    contract = copy.deepcopy(source["invocation_contracts"]["console"])
    source.update(
        {
            "plugin_id": "uploaded_scan_action",
            "name": "Uploaded scan action",
            "description": "Test-only signed browser action",
            "runtime": {
                "kind": "python_subprocess",
                "entrypoint": "payload/main.py",
            },
            "allowed_entrypoints": ["console"],
            "invocation_contracts": {"console": contract},
            "scheduling": {
                "supported": False,
                "allowed_kinds": [],
                "max_daily_times": 0,
            },
            "project_full_auto_allowed": False,
        }
    )
    tool = copy.deepcopy(source["tool_contract"])
    tool.update(
        {
            "name": "uploaded_scan_action",
            "executor": "payload/main.py",
            "input_schema": copy.deepcopy(contract["input_schema"]),
            "project_full_auto_allowed": False,
        }
    )
    source["tool_contract"] = tool
    source["governance_anchor"] = {
        field: copy.deepcopy(tool[field])
        for field in source["governance_anchor"]
    }
    roles = []
    for role in source["account_roles"]:
        roles.append({**role, "argument_field": None})
    source["account_roles"] = roles
    role_name = roles[0]["role"]
    source["runtime_permissions"] = {
        "network": False,
        "browser": True,
        "office": False,
        "file_roles": [],
        "broker_operations": [
            {
                "operation": "browser.invoke",
                "action": "scan.fetch",
                "roles": [role_name],
                "effect": "read",
            }
        ],
        "max_broker_calls": 3,
    }
    return AutomationPluginManifest.from_mapping(source)


def _signer_and_store() -> tuple[Ed25519PackageSigner, Ed25519TrustStore]:
    private_key = ECC.generate(curve="Ed25519")
    signer = Ed25519PackageSigner(key_id="test-release", private_key=private_key)
    public_key = private_key.public_key().export_key(format="raw")
    return signer, Ed25519TrustStore({"test-release": public_key})


def _signed_package(
    core_catalog: ToolRegistry,
) -> tuple[AutomationPluginManifest, bytes, Ed25519TrustStore]:
    manifest = _uploaded_manifest(core_catalog)
    signer, trust_store = _signer_and_store()
    package = build_signed_plugin_zip(
        manifest,
        {
            "payload/main.py": b"import json,sys\nprint(json.dumps({'ok': True}))\n",
            "payload/boyi_plugin_sdk.py": PLUGIN_SDK_SOURCE.encode("utf-8"),
        },
        signer=signer,
    )
    return manifest, package, trust_store


def test_ed25519_rfc8032_known_vector_and_bad_signature() -> None:
    public_key = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
    signature = bytes.fromhex(
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
        "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
    )
    trust = Ed25519TrustStore({"rfc8032": public_key})
    trust.verify(key_id="rfc8032", message=b"", signature=signature)
    with pytest.raises(PluginSignatureError):
        trust.verify(key_id="rfc8032", message=b"x", signature=signature)
    private_key = ECC.generate(curve="Ed25519")
    with pytest.raises(PluginSignatureError):
        Ed25519TrustStore({"private": private_key.export_key(format="PEM")})


def test_signed_zip_round_trip_and_wrong_key_fail_closed(core_catalog: ToolRegistry) -> None:
    manifest, package, trust = _signed_package(core_catalog)
    verified = verify_signed_plugin_zip(package, verifier=trust)
    with zipfile.ZipFile(BytesIO(package)) as archive:
        signed_manifest_bytes = archive.read("manifest.json")
    assert manifest.to_signed_mapping() == manifest.to_mapping()
    assert signed_manifest_bytes == canonical_json_bytes(manifest.to_mapping())
    assert verified.manifest_sha256 == _sha256(signed_manifest_bytes)
    assert verified.manifest.to_mapping() == manifest.to_mapping()
    assert verified.signing_key_id == "test-release"
    assert {item.path for item in verified.files} >= {
        "manifest.json",
        "payload/main.py",
        "payload/boyi_plugin_sdk.py",
    }
    other_private = ECC.generate(curve="Ed25519")
    wrong = Ed25519TrustStore(
        {"test-release": other_private.public_key().export_key(format="raw")}
    )
    with pytest.raises(PluginSignatureError):
        verify_signed_plugin_zip(package, verifier=wrong)
    with pytest.raises(PluginPackageError):
        verify_signed_plugin_zip(package, verifier=trust, expected_package_sha256="0" * 64)


def test_signed_legacy_v1_zip_without_effect_verifies_untouched_bytes(
    core_catalog: ToolRegistry,
) -> None:
    source = _uploaded_manifest(core_catalog).to_mapping()
    source["runtime_permissions"]["broker_operations"][0].pop("effect")
    manifest_bytes = canonical_json_bytes(source)
    files = {
        "manifest.json": manifest_bytes,
        "payload/main.py": b"import json\nprint(json.dumps({'ok': True}))\n",
        "payload/boyi_plugin_sdk.py": PLUGIN_SDK_SOURCE.encode("utf-8"),
    }
    signer, trust = _signer_and_store()
    manifest_sha256 = _sha256(manifest_bytes)
    statement, _ = _file_statement(files, manifest_sha256)
    files["signature.json"] = canonical_json_bytes(
        {
            "schema_version": 1,
            "algorithm": "Ed25519",
            "key_id": signer.key_id,
            "manifest_sha256": manifest_sha256,
            "statement_sha256": _sha256(statement),
            "signature": base64.b64encode(signer.sign(statement)).decode("ascii"),
        }
    )

    verified = verify_signed_plugin_zip(_zip_bytes(files), verifier=trust)

    assert "effect" not in verified.manifest.to_signed_mapping()["runtime_permissions"][
        "broker_operations"
    ][0]
    assert verified.manifest.runtime_permissions["broker_operations"][0]["effect"] == "write"
    assert verified.manifest.manifest_sha256 == manifest_sha256


def test_zip_slip_and_frontend_payload_are_rejected(core_catalog: ToolRegistry) -> None:
    _, _, trust = _signed_package(core_catalog)
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("../payload/main.py", b"pass")
        archive.writestr("manifest.json", b"{}")
        archive.writestr("signature.json", b"{}")
    with pytest.raises(PluginPackageError, match="traversal"):
        verify_signed_plugin_zip(stream.getvalue(), verifier=trust)
    manifest = _uploaded_manifest(core_catalog)
    signer, _ = _signer_and_store()
    with pytest.raises(PluginPackageError, match="HTML/JavaScript"):
        build_signed_plugin_zip(
            manifest,
            {
                "payload/main.py": b"pass",
                "payload/boyi_plugin_sdk.py": PLUGIN_SDK_SOURCE.encode("utf-8"),
                "payload/ui/panel.js": b"alert(1)",
            },
            signer=signer,
        )


def test_subprocess_account_ids_never_enter_input_contract(core_catalog: ToolRegistry) -> None:
    manifest = _uploaded_manifest(core_catalog)
    assert all(role["argument_field"] is None for role in manifest.account_roles)
    assert "account_id" not in manifest.tool_contract["input_schema"]["properties"]
    bad = manifest.to_mapping()
    bad["tool_contract"]["input_schema"]["properties"]["account_id"] = {"type": "string"}
    with pytest.raises(PluginManifestError, match="cannot receive account IDs"):
        AutomationPluginManifest.from_mapping(bad)


def test_first_party_descriptors_are_16_actions_and_18_instances(
    core_catalog: ToolRegistry,
) -> None:
    manifests = resolve_release_first_party_manifests(core_catalog)
    seeds = first_party_instance_seeds()
    assert set(manifests) == release_first_party_plugin_ids()
    assert len(expected_first_party_plugin_ids()) == 16
    assert {seed.automation_id for seed in seeds} == expected_first_party_automation_ids()
    assert len(seeds) == 18
    assert all(manifest.runtime["kind"] == "python_subprocess" for manifest in manifests.values())
    assert all(manifest.runtime_permissions["max_broker_calls"] > 0 for manifest in manifests.values())
    assert all(manifest.runtime_permissions["broker_operations"] for manifest in manifests.values())
    assert manifests["sync_scan_codes"].version == "1.0.23"
    assert manifests["sync_arrival_stats"].version == "1.0.21"
    assert manifests["self_pickup_problem_upload"].version == "1.0.23"
    assert manifests["split_pending_problem_upload"].version == "1.0.23"
    assert manifests["sync_arrival_stats"].runtime_permissions["max_broker_calls"] == 1000
    assert {
        manifest.version
        for plugin_id, manifest in manifests.items()
        if plugin_id
        not in {
            "self_pickup_problem_upload",
            "split_pending_problem_upload",
            "sync_arrival_stats",
            "sync_scan_codes",
        }
    } == {FIRST_PARTY_PACKAGE_VERSION}
    assert {
        seed.version
        for seed in seeds
        if seed.plugin_id == "sync_scan_codes"
    } == {"1.0.23"}
    assert {
        seed.version
        for seed in seeds
        if seed.plugin_id == "sync_arrival_stats"
    } == {"1.0.21"}
    assert {
        seed.version
        for seed in seeds
        if seed.plugin_id == "self_pickup_problem_upload"
    } == {"1.0.23"}
    assert {
        seed.version
        for seed in seeds
        if seed.plugin_id == "split_pending_problem_upload"
    } == {"1.0.23"}
    customer = manifests["sync_customer_service_problems"]
    assert customer.account_roles[0]["collection"] is True
    assert customer.account_roles[0]["argument_field"] is None
    assert customer.scheduling == {
        "supported": True,
        "allowed_kinds": ["daily_times"],
        "max_daily_times": 96,
    }


def test_first_party_digest_lock_is_canonical_and_current(core_catalog: ToolRegistry) -> None:
    lock_path = Path("agent/first_party_automation_plugins/digests.json")
    raw = lock_path.read_bytes()
    value = json.loads(raw.decode("utf-8"))
    assert raw.rstrip(b"\n") == canonical_json_bytes(value)
    release_snapshot = release_first_party_digest_snapshot(core_catalog)
    assert value.get("schema_version") == release_snapshot["schema_version"]
    assert {
        plugin_id: value["plugins"][plugin_id]
        for plugin_id in release_first_party_plugin_ids()
    } == release_snapshot["plugins"]


def test_development_source_provider_returns_only_release_scoped_actions(
    core_catalog: ToolRegistry,
) -> None:
    records = SourceFirstPartyPackageProvider().load_versions(
        core_catalog=core_catalog,
        current_release_sha="a" * 40,
        expected_release_sha="a" * 40,
    )
    assert {record.plugin_id for record in records} == release_first_party_plugin_ids()
    assert not ({"r7_arrival_checkin", "r7_departure_checkin"} & {
        record.plugin_id for record in records
    })


def _signed_first_party_artifacts(
    root: Path,
    core_catalog: ToolRegistry,
    *,
    release_sha: str,
) -> Ed25519TrustStore:
    private_key = ECC.generate(curve="Ed25519")
    signer = Ed25519PackageSigner(key_id="first-party-test", private_key=private_key)
    index = {"schema_version": 1, "release_sha": release_sha, "plugins": {}}
    for plugin_id, manifest in resolve_release_first_party_manifests(core_catalog).items():
        package = build_signed_plugin_zip(
            manifest,
            first_party_payload_files(manifest),
            signer=signer,
        )
        path = root / f"{plugin_id}-{manifest.version}.zip"
        path.write_bytes(package)
        verified = verify_signed_plugin_zip(
            package,
            verifier=Ed25519TrustStore(
                {"first-party-test": private_key.public_key().export_key(format="raw")}
            ),
        )
        index["plugins"][plugin_id] = {
            "version": manifest.version,
            "manifest_sha256": manifest.manifest_sha256,
            "package_sha256": verified.package_sha256,
        }
    (root / "release-index.json").write_bytes(canonical_json_bytes(index))
    return Ed25519TrustStore(
        {"first-party-test": private_key.public_key().export_key(format="raw")}
    )


def test_signed_first_party_release_preflight_is_complete_and_read_only(
    core_catalog: ToolRegistry,
    tmp_path: Path,
) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    trust = _signed_first_party_artifacts(
        artifacts,
        core_catalog,
        release_sha="abcdef1",
    )
    before = {item.name: item.read_bytes() for item in artifacts.iterdir()}
    result = preflight_signed_first_party_release(
        artifact_root=artifacts,
        signature_verifier=trust,
        core_catalog=core_catalog,
        release_sha="abcdef1",
    )
    after = {item.name: item.read_bytes() for item in artifacts.iterdir()}
    assert result.package_count == len(release_first_party_plugin_ids())
    assert result.instance_count == len(release_first_party_automation_ids())
    assert len(result.contracts_sha256) == 64
    assert before == after


def test_linux_venv_interpreter_stays_in_immutable_root(
    core_catalog: ToolRegistry,
    tmp_path: Path,
) -> None:
    manifest = resolve_release_first_party_manifests(core_catalog)["sync_arrive_list"]
    version_root = tmp_path / "version"
    (version_root / "package" / "payload").mkdir(parents=True)
    (version_root / "package" / "payload" / "entrypoint.py").write_text(
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    python_path = LockedVirtualEnvironmentBuilder().build(version_root, manifest)
    assert python_path.is_file()
    assert not python_path.is_symlink()
    assert python_path.is_relative_to(version_root.resolve())
    assert not (python_path.parent / "python3").exists()
    assert not (
        python_path.parent / f"python{sys.version_info.major}.{sys.version_info.minor}"
    ).exists()
    assert not (version_root / "venv" / "lib64").is_symlink()
    completed = subprocess.run(
        [str(python_path), "-I", "-c", "import sys; print(sys.prefix)"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert Path(completed.stdout.strip()).resolve().is_relative_to(version_root.resolve())


@pytest.mark.skipif(sys.platform == "win32", reason="Linux copied-venv layout")
def test_incomplete_venv_preserves_disk_error_and_cleans_staging(
    core_catalog: ToolRegistry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = resolve_release_first_party_manifests(core_catalog)["sync_arrive_list"]
    storage = FilesystemPluginStorage(tmp_path / "plugins")
    version_root = storage.create_staging_root(manifest.plugin_id, manifest.version)
    (version_root / "package" / "payload").mkdir(parents=True)

    def fail_after_interpreter_copy(_builder: object, env_dir: str | Path) -> None:
        venv_root = Path(env_dir)
        (venv_root / "bin").mkdir(parents=True)
        (venv_root / "lib").mkdir()
        (venv_root / "bin" / "python").write_bytes(b"python")
        (venv_root / "bin" / "python3").write_bytes(b"partial")
        (venv_root / "lib64").symlink_to("lib", target_is_directory=True)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(
        "agent.automation_plugins.storage.venv.EnvBuilder.create",
        fail_after_interpreter_copy,
    )
    with pytest.raises(OSError, match="No space left on device"):
        LockedVirtualEnvironmentBuilder().build(version_root, manifest)

    assert (version_root / "venv" / "bin" / "python").is_file()
    assert not (version_root / "venv" / "bin" / "python3").exists()
    assert not (version_root / "venv" / "lib64").is_symlink()
    storage.discard_staging_root(version_root)
    assert not version_root.exists()


def test_copied_venv_normalization_is_unchanged_on_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    venv_root = tmp_path / "venv"
    alias = venv_root / "Scripts" / "python3.exe"
    alias.parent.mkdir(parents=True)
    alias.write_bytes(b"unchanged")
    monkeypatch.setattr("agent.automation_plugins.storage.os.name", "nt")

    LockedVirtualEnvironmentBuilder._normalize_copied_venv(venv_root)

    assert alias.read_bytes() == b"unchanged"


def test_linux_materialize_and_bubblewrap_cannot_read_host_sibling(
    core_catalog: ToolRegistry,
    tmp_path: Path,
) -> None:
    bubblewrap = Path("/usr/bin/bwrap")
    if not bubblewrap.is_file():
        pytest.skip("bubblewrap is not installed")
    manifest, package, trust = _signed_package(core_catalog)
    verified = verify_signed_plugin_zip(package, verifier=trust)
    storage = FilesystemPluginStorage(tmp_path / "plugins")
    stage = storage.create_staging_root(manifest.plugin_id, manifest.version)
    package_root = stage / "package"
    extract_verified_package(verified, package_root)
    probe = tmp_path / "synthetic-sensitive.txt"
    probe.write_text("synthetic-only", encoding="utf-8")
    entrypoint = package_root / "payload" / "main.py"
    entrypoint.chmod(0o644)
    entrypoint.write_text(
        "import json,pathlib,sys\n"
        "request=json.load(sys.stdin)\n"
        "path=pathlib.Path(request['arguments']['probe_path'])\n"
        "try:\n path.read_text(encoding='utf-8')\n readable=True\n"
        "except (OSError,PermissionError):\n readable=False\n"
        "print(json.dumps({'readable':readable}))\n",
        encoding="utf-8",
    )
    python_path = LockedVirtualEnvironmentBuilder().build(stage, manifest)
    python_relative = python_path.relative_to(stage).as_posix()
    install_root = storage.commit_staging_root(
        stage,
        plugin_id=manifest.plugin_id,
        version=manifest.version,
        manifest_sha256=manifest.manifest_sha256,
    )

    async def run() -> tuple[int, bytes, bytes]:
        process = await BubblewrapPluginSandbox(bubblewrap).launch(
            install_root=install_root,
            python_relative=python_relative,
            entrypoint_relative="payload/main.py",
            environment={},
            broker_socket_path=None,
        )
        assert process.stdin is not None
        process.stdin.write(
            canonical_json_bytes(
                {"schema_version": 1, "arguments": {"probe_path": str(probe)}}
            )
        )
        await process.stdin.drain()
        process.stdin.close()
        stdout, stderr = await process.communicate()
        return int(process.returncode or 0), stdout, stderr

    returncode, stdout, stderr = asyncio.run(run())
    if returncode != 0 and b"namespace" in stderr.lower():
        pytest.skip("host kernel disabled unprivileged user namespaces")
    assert returncode == 0, stderr.decode("utf-8", errors="replace")
    assert json.loads(stdout.decode("utf-8")) == {"readable": False}


class _CatalogRepository:
    def __init__(self, instance: PluginInstanceRecord) -> None:
        self.instance = instance

    def list_instances(self) -> list[PluginInstanceRecord]:
        return [self.instance]

    def get_instance(self, automation_id: str) -> PluginInstanceRecord | None:
        return self.instance if automation_id == self.instance.automation_id else None


def test_catalog_fragment_binds_trust_source(core_catalog: ToolRegistry) -> None:
    manifest = resolve_release_first_party_manifests(core_catalog)["sync_arrive_list"]
    version = PluginVersionRecord(
        plugin_id=manifest.plugin_id,
        version=manifest.version,
        package_sha256="1" * 64,
        manifest_sha256=manifest.manifest_sha256,
        manifest=manifest.to_mapping(),
        trust_source=PluginTrustSource.ED25519_FIRST_PARTY,
        install_root="/srv/plugins/scan",
    )
    instance = PluginInstanceRecord(
        automation_id="scan_codes",
        display_name="scan",
        plugin_id=manifest.plugin_id,
        state=PluginProjectState.ENABLED,
        active_version=version,
    )
    catalog = PluginCatalog(_CatalogRepository(instance))
    fragment = project_contract_fragment(catalog.require("scan_codes"))
    assert fragment["trust_source"] == "ed25519_first_party"
    assert fragment["code_owned_plan_fields"] == []
    assert "trust_source" not in catalog.safe_projection()["instances"][0]
    assert catalog.safe_projection()["instances"][0][
        "code_owned_config_fields"
    ] == []


def test_catalog_projects_exact_first_party_code_owned_fields(
    core_catalog: ToolRegistry,
) -> None:
    manifest = resolve_release_first_party_manifests(core_catalog)[
        "sync_customer_service_problems"
    ]
    version = PluginVersionRecord(
        plugin_id=manifest.plugin_id,
        version=manifest.version,
        package_sha256="1" * 64,
        manifest_sha256=manifest.manifest_sha256,
        manifest=manifest.to_mapping(),
        trust_source=PluginTrustSource.ED25519_FIRST_PARTY,
        install_root="/srv/plugins/customer-problems",
    )
    instance = PluginInstanceRecord(
        automation_id="customer_problems_shadow",
        display_name="customer problems",
        plugin_id=manifest.plugin_id,
        state=PluginProjectState.ENABLED,
        active_version=version,
    )
    catalog = PluginCatalog(_CatalogRepository(instance))

    assert project_contract_fragment(catalog.require(instance.automation_id))[
        "code_owned_plan_fields"
    ] == ["recheck_items"]
    projected = catalog.safe_projection()["instances"][0]
    assert projected["code_owned_config_fields"] == ["recheck_items"]
    assert "recheck_items" not in projected["config_schema"]["properties"]


def test_catalog_projects_exact_scan_preview_code_owned_fields(
    core_catalog: ToolRegistry,
) -> None:
    manifest = resolve_release_first_party_manifests(core_catalog)["sync_scan_codes"]
    version = PluginVersionRecord(
        plugin_id=manifest.plugin_id,
        version=manifest.version,
        package_sha256="1" * 64,
        manifest_sha256=manifest.manifest_sha256,
        manifest=manifest.to_mapping(),
        trust_source=PluginTrustSource.ED25519_FIRST_PARTY,
        install_root="/srv/plugins/scan",
    )
    instance = PluginInstanceRecord(
        automation_id="scan_codes",
        display_name="scan",
        plugin_id=manifest.plugin_id,
        state=PluginProjectState.ENABLED,
        active_version=version,
    )
    catalog = PluginCatalog(_CatalogRepository(instance))

    assert project_contract_fragment(catalog.require(instance.automation_id))[
        "code_owned_plan_fields"
    ] == ["_scan_preview_binding", "dry_run"]
    projected = catalog.safe_projection()["instances"][0]
    assert projected["code_owned_config_fields"] == [
        "_scan_preview_binding",
        "dry_run",
    ]
    assert "_scan_preview_binding" not in projected["config_schema"]["properties"]
    assert "dry_run" not in projected["config_schema"]["properties"]


def test_broker_grant_is_bounded_and_request_replay_is_rejected(
    core_catalog: ToolRegistry,
    tmp_path: Path,
) -> None:
    manifest = _uploaded_manifest(core_catalog)
    role = manifest.account_roles[0]["role"]
    issuer = LocalBrokerCapabilityIssuer(tmp_path / "broker.sock")
    token = issuer.issue(
        automation_id="instance-1",
        plugin_version=manifest.version,
        tool_name=manifest.tool_contract["name"],
        ttl_seconds=60,
        runtime_permissions={**manifest.runtime_permissions, "max_broker_calls": 2},
        account_roles=manifest.account_roles,
        resource_roles=manifest.resource_roles,
        account_bindings={role: "acct-1"},
        resource_bindings={},
    )
    first_id = str(uuid.uuid4())
    first, binding = issuer.consume(
        token,
        request_id=first_id,
        operation="browser.invoke",
        action="scan.fetch",
        role=role,
    )
    assert first.automation_id == "instance-1"
    assert binding == "acct-1"
    with pytest.raises(PluginExecutionError) as replayed:
        issuer.consume(
            token,
            request_id=first_id,
            operation="browser.invoke",
            action="scan.fetch",
            role=role,
        )
    assert replayed.value.code == "BROKER_REQUEST_REPLAYED"
    issuer.consume(
        token,
        request_id=str(uuid.uuid4()),
        operation="browser.invoke",
        action="scan.fetch",
        role=role,
    )
    with pytest.raises(PluginExecutionError) as exhausted:
        issuer.consume(
            token,
            request_id=str(uuid.uuid4()),
            operation="browser.invoke",
            action="scan.fetch",
            role=role,
        )
    assert exhausted.value.code == "BROKER_CALL_LIMIT"


def test_registered_core_adapter_revalidates_exact_bound_account(
    core_catalog: ToolRegistry,
) -> None:
    manifest = _uploaded_manifest(core_catalog)
    role = manifest.account_roles[0]["role"]

    class Manager:
        def require_authenticated_binding(self, account_id: str) -> dict[str, str]:
            assert account_id == "acct-1"
            return {
                "account_id": account_id,
                "system": "ronghui",
                "account_purpose": "general",
                "session_profile": "unused",
            }

    calls = []

    async def handler(context, arguments):
        calls.append((context, arguments))
        return {"count": 1}

    adapter = RegisteredCoreAutomationBrokerAdapter(
        handlers={("browser.invoke", "scan.fetch"): handler},
        account_resolver=AccountManagerSessionResolver(Manager()),
    )
    issuer = LocalBrokerCapabilityIssuer(Path(".task_tmp") / "unused-broker.sock")
    token = issuer.issue(
        automation_id="instance-1",
        plugin_version=manifest.version,
        tool_name=manifest.tool_contract["name"],
        ttl_seconds=60,
        runtime_permissions=manifest.runtime_permissions,
        account_roles=manifest.account_roles,
        resource_roles=manifest.resource_roles,
        account_bindings={role: "acct-1"},
        resource_bindings={},
    )
    grant, binding = issuer.consume(
        token,
        request_id=str(uuid.uuid4()),
        operation="browser.invoke",
        action="scan.fetch",
        role=role,
    )
    result = asyncio.run(
        adapter.invoke(
            grant=grant,
            operation="browser.invoke",
            action="scan.fetch",
            role=role,
            binding=binding,
            arguments={"query": "x"},
        )
    )
    assert result == {"count": 1}
    assert calls[0][0].account_ids == ("acct-1",)


def test_registered_core_adapter_skips_unbound_optional_action_resource(
    tmp_path: Path,
) -> None:
    primary_role = "primary_sheet"
    optional_role = "optional_sheet"
    calls = []

    class Resources:
        @staticmethod
        def require_active(*, resource_id: str, allowed_kinds) -> dict[str, str]:
            assert resource_id == "primary-resource"
            assert allowed_kinds == ["feishu_sheet"]
            return {"resource_id": resource_id, "kind": "feishu_sheet"}

    async def handler(context, arguments):
        calls.append((context, arguments))
        return {"count": 1}

    permissions = {
        "network": True,
        "max_broker_calls": 1,
        "broker_operations": [
            {
                "operation": "network.request",
                "action": "feishu.sheet.replace",
                "effect": "write",
                "roles": [primary_role, optional_role],
            }
        ],
    }
    roles = [
        {
            "role": primary_role,
            "allowed_kinds": ["feishu_sheet"],
            "required": True,
        },
        {
            "role": optional_role,
            "allowed_kinds": ["feishu_sheet"],
            "required": False,
        },
    ]
    issuer = LocalBrokerCapabilityIssuer(tmp_path / "optional-resource.sock")
    token = issuer.issue(
        automation_id="optional-resource-instance",
        plugin_version="1.0.0",
        tool_name="sync_arrival_stats",
        ttl_seconds=60,
        runtime_permissions=permissions,
        account_roles=[],
        resource_roles=roles,
        account_bindings={},
        resource_bindings={primary_role: "primary-resource"},
    )
    request_id = str(uuid.uuid4())
    grant, binding = issuer.consume(
        token,
        request_id=request_id,
        operation="network.request",
        action="feishu.sheet.replace",
        role=primary_role,
    )
    adapter = RegisteredCoreAutomationBrokerAdapter(
        handlers={("network.request", "feishu.sheet.replace"): handler},
        resource_resolver=Resources(),
    )

    result = asyncio.run(
        adapter.invoke(
            grant=grant,
            operation="network.request",
            action="feishu.sheet.replace",
            role=primary_role,
            binding=binding,
            arguments={"records": []},
        )
    )

    assert result == {"count": 1}
    assert calls[0][0].resource_id == "primary-resource"
    assert calls[0][0].resource_bindings == {primary_role: "primary-resource"}


def test_registered_core_adapter_keeps_event_loop_responsive_for_sync_handler(
    core_catalog: ToolRegistry,
) -> None:
    manifest = _uploaded_manifest(core_catalog)
    role = manifest.account_roles[0]["role"]

    class Manager:
        @staticmethod
        def require_authenticated_binding(account_id: str) -> dict[str, str]:
            return {
                "account_id": account_id,
                "system": "ronghui",
                "account_purpose": "general",
            }

    def handler(_context, _arguments):
        time.sleep(0.25)
        return {"count": 1}

    adapter = RegisteredCoreAutomationBrokerAdapter(
        handlers={("browser.invoke", "scan.fetch"): handler},
        account_resolver=AccountManagerSessionResolver(Manager()),
    )
    issuer = LocalBrokerCapabilityIssuer(Path(".task_tmp") / "unused-broker.sock")
    token = issuer.issue(
        automation_id="instance-1",
        plugin_version=manifest.version,
        tool_name=manifest.tool_contract["name"],
        ttl_seconds=60,
        runtime_permissions=manifest.runtime_permissions,
        account_roles=manifest.account_roles,
        resource_roles=manifest.resource_roles,
        account_bindings={role: "acct-1"},
        resource_bindings={},
    )
    grant, binding = issuer.consume(
        token,
        request_id=str(uuid.uuid4()),
        operation="browser.invoke",
        action="scan.fetch",
        role=role,
    )

    async def invoke() -> float:
        started = time.monotonic()
        task = asyncio.create_task(
            adapter.invoke(
                grant=grant,
                operation="browser.invoke",
                action="scan.fetch",
                role=role,
                binding=binding,
                arguments={"query": "x"},
            )
        )
        await asyncio.sleep(0.02)
        elapsed = time.monotonic() - started
        assert await task == {"count": 1}
        return elapsed

    assert asyncio.run(invoke()) < 0.1


def test_registered_core_adapter_blocks_unauthenticated_bound_account(
    core_catalog: ToolRegistry,
) -> None:
    manifest = _uploaded_manifest(core_catalog)
    role = manifest.account_roles[0]["role"]

    class Manager:
        @staticmethod
        def require_authenticated_binding(account_id: str) -> dict[str, str]:
            assert account_id == "acct-1"
            raise TMSAuthStateError(
                "AUTH_REQUIRED",
                "The bound account session is unavailable.",
            )

    calls = []

    async def handler(context, arguments):
        calls.append((context, arguments))
        return {"count": 1}

    adapter = RegisteredCoreAutomationBrokerAdapter(
        handlers={("browser.invoke", "scan.fetch"): handler},
        account_resolver=AccountManagerSessionResolver(Manager()),
    )
    issuer = LocalBrokerCapabilityIssuer(Path(".task_tmp") / "unused-broker.sock")
    token = issuer.issue(
        automation_id="instance-1",
        plugin_version=manifest.version,
        tool_name=manifest.tool_contract["name"],
        ttl_seconds=60,
        runtime_permissions=manifest.runtime_permissions,
        account_roles=manifest.account_roles,
        resource_roles=manifest.resource_roles,
        account_bindings={role: "acct-1"},
        resource_bindings={},
    )
    grant, binding = issuer.consume(
        token,
        request_id=str(uuid.uuid4()),
        operation="browser.invoke",
        action="scan.fetch",
        role=role,
    )

    with pytest.raises(PluginExecutionError) as blocked:
        asyncio.run(
            adapter.invoke(
                grant=grant,
                operation="browser.invoke",
                action="scan.fetch",
                role=role,
                binding=binding,
                arguments={"query": "x"},
            )
        )

    assert blocked.value.code == "BLOCKED_LOGIN"
    assert calls == []


def test_schedule_capability_is_closed() -> None:
    capability = {
        "supported": True,
        "allowed_kinds": ["daily_times"],
        "max_daily_times": 2,
    }
    assert normalize_project_schedule(
        {"kind": "daily_times", "times": ["09:30", "08:00"], "enabled": True},
        capability,
    ) == {"kind": "daily_times", "times": ["08:00", "09:30"], "enabled": True}
    with pytest.raises(PluginConflictError):
        normalize_project_schedule(
            {"kind": "daily_times", "times": ["08:00", "09:00", "10:00"], "enabled": True},
            capability,
        )


def test_uploaded_python_runtime_defaults_to_fail_closed_sandbox() -> None:
    with pytest.raises(PluginExecutionError) as exc_info:
        asyncio.run(FailClosedPluginSandbox().launch())
    assert exc_info.value.code == "PLUGIN_SANDBOX_UNAVAILABLE"


def test_trust_source_enum_matches_migration_check() -> None:
    sql = Path("agent/migrations/018_automation_project_authorization.sql").read_text(
        encoding="utf-8"
    )
    marker = "CONSTRAINT chk_automation_plugin_trust_source CHECK"
    clause = sql[sql.index(marker) : sql.index(")\n    )", sql.index(marker))]
    assert {f"'{item.value}'" for item in PluginTrustSource} <= {
        token.strip().rstrip(",") for token in clause.replace("(", " ").split()
    }


def test_production_release_configuration_has_no_fallback(tmp_path: Path) -> None:
    release_sha = "abcdef1"
    artifact_root = tmp_path / "releases" / release_sha
    trust_root = tmp_path / "trust"
    artifact_root.mkdir(parents=True)
    trust_root.mkdir()
    values = {
        "BOYI_AUTOMATION_PLUGIN_ARTIFACT_ROOT": str(artifact_root),
        "BOYI_AUTOMATION_PLUGIN_TRUST_ROOT": str(trust_root),
        "BOYI_AUTOMATION_PLUGIN_VERIFIED_RELEASE_SHA": release_sha,
    }
    config = load_production_plugin_release_config(
        values,
        runtime_release_sha=release_sha,
    )
    assert config.artifact_root == artifact_root.resolve()
    with pytest.raises(PluginPackageError):
        load_production_plugin_release_config({}, runtime_release_sha=release_sha)
    with pytest.raises(PluginPackageError):
        load_production_plugin_release_config(values, runtime_release_sha="1234567")
