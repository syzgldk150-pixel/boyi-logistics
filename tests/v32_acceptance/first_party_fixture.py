"""Actual signed full release bootstrap with isolated in-memory signing material."""
from pathlib import Path
import subprocess

from agent.automation_plugins.first_party import (
    SignedFirstPartyPackageProvider, bootstrap_first_party_plugins,
    release_first_party_instance_seeds, resolve_release_first_party_manifests,
)
from agent.automation_plugins.package import Ed25519PackageSigner
from agent.tool_registry import ToolRegistry
from scripts.build_first_party_plugin_release import _write_release

ROOT = Path(__file__).resolve().parents[2]


def isolated_migration_accounts():
    manifests = resolve_release_first_party_manifests(ToolRegistry())
    return {seed.automation_id: {
        str(role['role']): ['synthetic-bootstrap-' + str(role['role'])]
        for role in manifests[seed.plugin_id].account_roles
    } for seed in release_first_party_instance_seeds()}


def bootstrap(management, *, private_key, trust, key_id):
    release_root = management.task_env / 'first-party-release'
    release_root.mkdir()
    release_sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    _write_release(release_root, release_sha=release_sha,
        signer=Ed25519PackageSigner(key_id=key_id, private_key=private_key), core_catalog=management.core_catalog)
    provider = SignedFirstPartyPackageProvider(artifact_root=release_root,
        signature_verifier=trust, storage=management.storage, environments=management.lifecycle._environments)
    result = bootstrap_first_party_plugins(management.packages, core_catalog=management.core_catalog,
        current_release_sha=release_sha, expected_release_sha=release_sha, package_provider=provider,
        superseded_automation_ids=management.packages.superseded_first_party_ids(tuple(
            seed.automation_id for seed in release_first_party_instance_seeds())))
    if result.rejected:
        raise RuntimeError('real signed first-party bootstrap rejected: ' + str(result.rejected))
    return {'created': result.created, 'existing': result.existing, 'superseded': result.superseded,
        'release_sha': release_sha, 'artifact_root': str(release_root)}
