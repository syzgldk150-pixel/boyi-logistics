# First-party automation plugin sources and digest lock

This directory contains the package-owned orchestration for every first-party
automation action. It contains no credentials. Each deterministic ZIP is built
from its signed manifest plus the corresponding `payload/action.py`, subprocess
entry point, embedded plugin SDK, and result-contract helper. Payload code may
use only the Python standard library, the embedded SDK, and explicitly declared
package dependencies; it must not import Agent or shared business modules.

Schedules, approval policy, project parameters, resource bindings, and business
account selection remain control-plane data. A package declares account roles,
while each installed automation instance binds those roles to accounts from the
system business-account pool. The same package can therefore back multiple
independent instances without receiving account identifiers in plugin JSON.

`digests.json` pins the canonical manifest and deterministic package archive
hashes. `MIGRATION_MATRIX.md` records which extracted actions have complete,
closed core primitives and which remain fail-closed pending authoritative source
or post-write evidence.

Production artifacts are intentionally narrower than this source inventory. The
release builder, bootstrap, catalog, Broker surface, and generation health admit
only server actions that are both marked `RUNNABLE` in the migration matrix and
listed in `agent/automation_plugins/release_scope.py`. A `BLOCKED` payload is not
an executable fallback merely because its files are present. The current scope
also defers both R7 check-in actions and the entire Windows Worker/Tray runtime;
those exclusions do not make otherwise healthy Linux/ECS actions fail release
health. Re-enabling either scope requires a reviewed code and matrix change with
the closed adapters and tests in the same commit.

`builtin_release` is a deterministic development and release-preflight trust
source tied to the reviewed release SHA and digest lock. Production bootstrap
does not trust it: production first-party ZIPs must pass the
`ED25519_FIRST_PARTY` verifier. Administrator-uploaded packages use their
applicable Ed25519 trust policy as well.
