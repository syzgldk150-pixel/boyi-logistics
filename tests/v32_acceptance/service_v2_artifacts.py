"""Build isolated variants through the current first-party V2 packager.

Copies contain only reviewed source files. Production defaults and Host files
are never edited by a maintenance drill; all reported bytes come from the ZIP.
"""
from hashlib import sha256
import json
from pathlib import Path
import shutil
import zipfile

from service_v2_plugins._shared.build_zip import build_plugin_zip

ROOT = Path(__file__).resolve().parents[2]


def build_artifact(plugin_id, output_root, *, label="baseline", version=None, edits=None):
    original = ROOT / "agent" / "service_v2_plugins" / plugin_id
    manifest = json.loads((original / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["plugin_id"] == plugin_id and manifest["runtime_model"] == "service_v2"
    copy_root = output_root / label
    source = copy_root / "agent" / "service_v2_plugins" / plugin_id
    source.mkdir(parents=True)
    for directory in ("payload", "settings"):
        shutil.copytree(original / directory, source / directory,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if version is not None:
        manifest["version"] = version
    (source / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for relative, replacements in (edits or {}).items():
        target = source / relative
        contents = target.read_bytes()
        for old, new in replacements:
            assert contents.count(old) == 1, f"maintenance edit is not unique: {relative}"
            contents = contents.replace(old, new)
        target.write_bytes(contents)
    # The production builder includes these stable shared protocols by path.
    protocols = {"sync_finance_bills_v2": ("ronghui_finance_fields.py",),
                 "sync_customer_service_problems_v2": ("ronghui_customer_problem_fields.py", "customer_problem_policy.py")}
    for filename in protocols.get(plugin_id, ()):
        target = copy_root / "shared" / filename
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(ROOT / "shared" / filename, target)
    if plugin_id == 'split_pending_problem_upload_v2':
        for relative in ('service_v2_plugins/sync_daily_should_sign_v2/payload/business/daily_sign_rules.py',
                         'tools/daily_sign_values.py'):
            target = copy_root / 'agent' / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / 'agent' / relative, target)
    archive = output_root / f"{plugin_id}-{label}-{manifest['version']}.zip"
    build_plugin_zip(source, archive)
    package = archive.read_bytes()
    with zipfile.ZipFile(archive) as zipped:
        files = {name: sha256(zipped.read(name)).hexdigest() for name in zipped.namelist()}
        assert json.loads(zipped.read("manifest.json")) == manifest
        for relative in edits or {}:
            assert zipped.read(relative) == (source / relative).read_bytes()
    return {"plugin_id": plugin_id, "runtime_model": "SERVICE_V2", "version": manifest["version"],
            "bytes": package, "sha256": sha256(package).hexdigest(), "files": files,
            "archive": str(archive), "source_root": str(source),
            "manifest_sha256": files["manifest.json"]}


def install_artifact(management, artifact, *, actor, name, module=None):
    from uuid import uuid4
    installed = management.management.install_service_v2(
        artifact["bytes"], request_id=str(uuid4()), transport_package_sha256=artifact["sha256"],
        raw_intent=json.dumps({"instance_name": name, "permissions_confirmed": True}),
        actor=actor, module=module)
    identity = installed["automation_id"]
    entry = management.catalog.require(identity)
    assert entry.plugin_id == artifact["plugin_id"]
    return identity
