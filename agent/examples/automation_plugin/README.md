---
module: automation-plugin-platform
type: template
status: active
updated: 2026-08-30
---

# Automation capability template

This is a copyable, non-production `ACTION_V1` compatibility example for adding
one automation action. New independently installable service capabilities should
use `SERVICE_V2` and follow `docs/plugin-platform-v2.md` instead.
It is deliberately outside `first_party_automation_plugins`, so it is never
discovered by bootstrap, release packaging, the Catalog, or the Broker.

## Use

1. Copy this directory to a temporary working directory.
2. Replace `example_compute_automation` consistently in `manifest.json` and
   `payload/action.py`.
3. Keep the first version read-only or compute-only. Add each external effect
   only after its exact Broker `(operation, action, role, effect)` contract and
   core handler have independent source and postcondition evidence.
4. Run the source preflight without a signing key:

   ```bash
   PYTHONPATH=agent:. python agent/scripts/validate_automation_plugin_source.py \
     --source-root agent/examples/automation_plugin
   ```

5. Add focused action tests. The example action accepts a closed list of labels,
   removes duplicates deterministically, and returns the unified result shape.
6. Request a separate review before changing the production first-party
   allowlist, migration matrix, digest lock, instance seeds, Broker handlers,
   signed release, or production installation state.

## Boundaries

- Do not put account IDs, resource IDs, credentials, cookies, tokens, file
  paths, or browser/session state in action arguments or result JSON.
- Do not import `agent`, `shared`, `tools`, `feishu`, or legacy runtime modules
  from payload code.
- Do not add a fallback to an old whole-tool implementation.
- Missing data, ambiguous matches, unknown write outcomes, or absent evidence
  must fail explicitly.
- Signing is a separate authorized release action. This example contains no
  key, signature, package archive, production instance, or database migration.

The existing offline signer is `agent/scripts/sign_automation_plugin.py`. Never
run it with production signing material as part of scaffolding or template
validation.
