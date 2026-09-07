"""Local source history and signed producer continuation in business modules."""

from http import HTTPStatus
from urllib.parse import quote

from shared.data_sources import DataSourceRepository


SOURCE_DISPLAY_FIELDS = (
    "source_id", "display_name", "status", "producer_instance_id",
    "producer_generation", "revision", "module", "provider", "dataset",
    "contract_version",
)


class ModuleDataSourcesServiceMixin:
    def _local_module_sources(self, module):
        with self.repository.connect() as connection:
            rows = DataSourceRepository(connection).list_sources(module)
        return [{key: row[key] for key in SOURCE_DISPLAY_FIELDS} for row in rows]

    def _handle_module_sources_get(self, handler, module):
        if self._control_plane_read_context(handler) is None:
            return
        try:
            sources = self._local_module_sources(module)
        except Exception:
            self._control_plane_error(handler, HTTPStatus.SERVICE_UNAVAILABLE,
                                      "SOURCE_CATALOG_UNAVAILABLE", "数据源记录暂时无法读取。")
            return
        self._send_json(handler, HTTPStatus.OK, {"ok": True, "data": {"sources": sources}})

    def _handle_source_continuation(self, handler, source_id):
        context = self._control_plane_write_context(handler)
        if context is None:
            return
        payload = self._read_control_plane_json(handler)
        if payload is None:
            return
        result = self._agent_request(
            "PUT", f"/internal/v1/automation/data-sources/{quote(source_id, safe='')}/producer",
            payload=payload, timeout=20, console_principal=context["_console_principal"],
        )
        if result.get("ok"):
            self._clear_automation_plugin_catalog_cache()
        self._send_json(handler, HTTPStatus.OK if result.get("ok") else HTTPStatus.CONFLICT, result)
