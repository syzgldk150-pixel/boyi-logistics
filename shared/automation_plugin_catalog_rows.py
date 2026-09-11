"""Batch database reads for a single, module-scoped display transaction.

Rows remain untrusted until consumed by the existing per-instance validators.
The cache never supplies locks, execution authority or mutation operations.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence
from copy import deepcopy
from shared.automation_project_policy_repository import read_project_configuration_rows
from shared.automation_plugin_v2_repository import read_active_plugin_migration_pairs, unique_active_plugin_migration_pair

from shared.orchestration_repository_support import (
    OrchestrationPersistenceError, _decode_row, _required_text, _rows,
)


def project_config_with_schedule(config, schedule_rows, *, schedule_from_rows):
    if config is None:
        return None
    desired = config.get("desired_schedule_json")
    if not isinstance(desired, Mapping):
        raise OrchestrationPersistenceError("automation project desired schedule is invalid")
    return {**config, "schedule": dict(desired),
        "committed_schedule": schedule_from_rows(schedule_rows),
        "scheduled_task_ids": tuple(str(row["id"]) for row in schedule_rows)}


class CatalogDisplayRows:
    def __init__(self, repository, identities: Sequence[str], *,
                 schedule_from_rows: Callable, validate_generation_row: Callable):
        self._schedule_from_rows = schedule_from_rows
        self._validate_generation_row = validate_generation_row
        self.identities = frozenset(_required_text(item, "automation_id") for item in identities)
        self.projects: dict[str, dict[str, Any]] = {}
        self.configs: dict[str, dict[str, Any]] = {}
        self.generations: dict[tuple[str, int], dict[str, Any]] = {}
        self.schedules: dict[str, list[dict[str, Any]]] = {}
        self.migration_pairs: dict[str, list[dict[str, Any]]] = {}
        if not self.identities:
            return
        values = tuple(sorted(self.identities))
        markers = ",".join("%s" for _ in values)
        with repository.cursor() as cursor:
            cursor.execute(f"SELECT * FROM automation_projects WHERE automation_id IN ({markers})", values)
            self.projects = {str(row["automation_id"]): row for row in _rows(cursor)}
            cursor.execute(f"SELECT * FROM automation_project_configs WHERE automation_id IN ({markers})", values)
            self.configs = {str(row["automation_id"]): row for row in _rows(cursor)}
            for row in read_project_configuration_rows(cursor, values):
                self.schedules.setdefault(str(row["automation_id"]), []).append(row)
            cursor.execute(
                "SELECT g.*, t.transition_token AS activation_transition_token, "
                "t.phase AS activation_phase FROM automation_projects p "
                "JOIN automation_project_generations g ON g.automation_id=p.automation_id "
                "AND g.generation=p.committed_generation "
                "LEFT JOIN automation_project_generation_transitions t "
                "ON t.automation_id=g.automation_id AND t.generation=g.generation "
                f"WHERE p.automation_id IN ({markers})", values,
            )
            self.generations = {
                (str(row["automation_id"]), int(row["generation"])): row for row in _rows(cursor)
            }
            for row in read_active_plugin_migration_pairs(cursor, values):
                for identity in {row['source_automation_id'], row['target_automation_id']} & self.identities:
                    self.migration_pairs.setdefault(identity, []).append(row)

    def migration_pair(self, automation_id):
        return deepcopy(unique_active_plugin_migration_pair(self.migration_pairs.get(automation_id, [])))

    def generation(self, repository, automation_id: str, generation: int):
        row = self.generations.get((automation_id, generation))
        if row is None:
            return None
        return self._validate_generation_row(_decode_row(dict(row), repository._GENERATION_JSON_FIELDS))

    def config(self, repository, automation_id: str):
        raw = self.configs.get(automation_id)
        config = _decode_row(dict(raw), repository._CONFIG_JSON_FIELDS) if raw is not None else None
        return project_config_with_schedule(config, self.schedules.get(automation_id, []),
            schedule_from_rows=self._schedule_from_rows)
