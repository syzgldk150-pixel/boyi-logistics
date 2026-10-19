"""监控查询：按单号读取真实扫描时间，并代理仓库笔记本录像服务的录像片段。

扫描时间只经 Agent 直接业务读取 `warehouse-scan-times`；录像由服务器端经 Tailscale
请求仓库录像服务，令牌只在服务器环境变量中，不进入浏览器。录像服务离线时明确提示，
不猜测画面或位置。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http import HTTPStatus
from typing import Any
from urllib.parse import urlencode

from console.services.business_calls import call_business

# 录像机通道与现场位置（2026-10-19 按画面核对）；第一项为默认卸车视角。
WAREHOUSE_CHANNELS = (
    (3, "车尾"),
    (5, "车厢侧面"),
    (7, "仓库内·对车厢"),
    (2, "仓库内·纵深"),
    (4, "仓库另一侧"),
    (1, "院子一"),
    (6, "院子二"),
)
_CHANNEL_IDS = frozenset(channel for channel, _label in WAREHOUSE_CHANNELS)
_CODE_RE = re.compile(r"^[0-9A-Za-z]{6,32}$")
_CLIP_SECONDS = (30, 60, 120, 300)
_MAX_CLIP_AGE_DAYS = 60
_STATUS_TIMEOUT_SECONDS = 6
_CLIP_TIMEOUT_SECONDS = 150


class WarehouseMonitorServiceMixin:
    """Render the monitor page and proxy the two read-only lookups it needs."""

    def _render_warehouse_monitor(self, handler: Any, query: dict[str, list[str]]) -> None:
        template = self.template_env.get_template("warehouse_monitor.html")
        body = template.render(
            app_title=self.settings.app_title,
            initial_code=str(query.get("code", [""])[0] or "").strip()[:32],
            channels=[{"id": channel, "label": label} for channel, label in WAREHOUSE_CHANNELS],
            clip_seconds=_CLIP_SECONDS,
            video_configured=self._warehouse_video_configured(),
        )
        self._send_html(handler, body)

    def _warehouse_video_configured(self) -> bool:
        return bool(self.settings.warehouse_video_base_url and self.settings.warehouse_video_token)

    def _warehouse_monitor_context(self, handler: Any) -> dict[str, Any] | None:
        principal = self._mysql_console_principal(getattr(handler, "current_admin_user", None))
        if principal is None:
            self._send_json(handler, HTTPStatus.FORBIDDEN,
                            {"ok": False, "message": "监控查询仅允许已登录的管理员会话。"})
            return None
        return {"_console_principal": principal}

    def _handle_warehouse_monitor_lookup(self, handler: Any, query: dict[str, list[str]]) -> None:
        code = str(query.get("code", [""])[0] or "").strip()
        if not _CODE_RE.fullmatch(code):
            self._send_json(handler, HTTPStatus.UNPROCESSABLE_ENTITY,
                            {"ok": False, "message": "请输入 6–32 位字母或数字的单号。"})
            return
        context = self._warehouse_monitor_context(handler)
        if context is None:
            return
        result = call_business(self, "warehouse-scan-times", {"code": code}, trusted_context=context,
                               request_id=str(handler.headers.get("X-Browser-Request-UUID") or ""),
                               timeout_sec=30)
        if not result.get("ok"):
            self._send_json(handler, HTTPStatus.BAD_GATEWAY,
                            {"ok": False, "message": str(result.get("error") or "扫描记录查询失败。")})
            return
        self._send_json(handler, HTTPStatus.OK, {"ok": True, "data": result.get("data") or {}})

    def _warehouse_video_request(self, path: str, params: dict[str, Any] | None, timeout: int):
        url = self.settings.warehouse_video_base_url + path
        if params:
            url += "?" + urlencode(params)
        request = urllib.request.Request(url, headers={"X-WV-Token": self.settings.warehouse_video_token})
        return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 - fixed configured base URL

    def _handle_warehouse_monitor_video_status(self, handler: Any) -> None:
        if not self._warehouse_video_configured():
            self._send_json(handler, HTTPStatus.OK, {"ok": True, "data": {"status": "not_configured"}})
            return
        try:
            with self._warehouse_video_request("/health", None, _STATUS_TIMEOUT_SECONDS) as response:
                health = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError):
            self._send_json(handler, HTTPStatus.OK, {"ok": True, "data": {"status": "offline"}})
            return
        status = "online" if health.get("nvr_reachable") is True else "nvr_unreachable"
        self._send_json(handler, HTTPStatus.OK, {"ok": True, "data": {"status": status}})

    def _handle_warehouse_monitor_clip(self, handler: Any, query: dict[str, list[str]]) -> None:
        def first(name: str) -> str:
            return str(query.get(name, [""])[0] or "").strip()

        try:
            channel = int(first("channel"))
            seconds = int(first("seconds") or "60")
            start = datetime.strptime(first("start"), "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            self._send_json(handler, HTTPStatus.UNPROCESSABLE_ENTITY, {"ok": False, "message": "录像参数无效。"})
            return
        now = datetime.now()
        if channel not in _CHANNEL_IDS or seconds not in _CLIP_SECONDS or not (
            now - timedelta(days=_MAX_CLIP_AGE_DAYS) <= start <= now
        ):
            self._send_json(handler, HTTPStatus.UNPROCESSABLE_ENTITY, {"ok": False, "message": "录像参数超出范围。"})
            return
        if not self._warehouse_video_configured():
            self._send_json(handler, HTTPStatus.SERVICE_UNAVAILABLE,
                            {"ok": False, "message": "服务器未配置仓库录像服务。"})
            return
        params = {"channel": channel, "start": start.strftime("%Y-%m-%dT%H:%M:%S"), "seconds": seconds}
        try:
            with self._warehouse_video_request("/clip", params, _CLIP_TIMEOUT_SECONDS) as response:
                payload = response.read()
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = str(json.loads(exc.read().decode("utf-8")).get("detail") or "")
            except (ValueError, OSError):
                pass
            self._send_json(handler, HTTPStatus.BAD_GATEWAY,
                            {"ok": False, "message": detail or f"录像服务返回错误（{exc.code}）。"})
            return
        except (urllib.error.URLError, OSError):
            self._send_json(handler, HTTPStatus.SERVICE_UNAVAILABLE,
                            {"ok": False, "message": "仓库录像服务离线：笔记本可能关机、休眠或不在仓库网络。"})
            return
        if not content_type.startswith("video/mp4") or not payload:
            self._send_json(handler, HTTPStatus.BAD_GATEWAY, {"ok": False, "message": "录像服务返回的内容无效。"})
            return
        self._send_bytes(handler, HTTPStatus.OK, payload, "video/mp4", cache_control="no-store")
