"""监控查询：按单号读取真实扫描时间，定位到录像时间点；录像只在仓库局域网内播放。

扫描时间只经 Agent 直接业务读取 `warehouse-scan-times`。服务器不转发任何视频：查询结果附带
一个短时有效的签名链接，浏览器在仓库局域网内打开它，由仓库笔记本的录像服务直连录像机回放。
令牌只用于在服务器端计算签名，不进入浏览器。
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from http import HTTPStatus
from typing import Any
from urllib.parse import urlencode

from console.services.business_calls import call_business

_CODE_RE = re.compile(r"^[0-9A-Za-z]{6,32}$")
# 签名链接有效期：查询后在此时间内点击"看录像"即可，录像服务另发会话。
_VIDEO_LINK_SECONDS = 15 * 60


class WarehouseMonitorServiceMixin:
    """Render the monitor page and look up scan times for a waybill."""

    def _render_warehouse_monitor(self, handler: Any, query: dict[str, list[str]]) -> None:
        template = self.template_env.get_template("warehouse_monitor.html")
        body = template.render(
            app_title=self.settings.app_title,
            initial_code=str(query.get("code", [""])[0] or "").strip()[:32],
            video_configured=self._warehouse_video_configured(),
        )
        self._send_html(handler, body)

    def _warehouse_video_configured(self) -> bool:
        return bool(self.settings.warehouse_video_lan_url and self.settings.warehouse_video_token)

    def _warehouse_video_link(self) -> str:
        """局域网录像服务的签名入口；页面再追加 code 与 scan 参数。"""
        if not self._warehouse_video_configured():
            return ""
        exp = int(time.time()) + _VIDEO_LINK_SECONDS
        sig = hmac.new(self.settings.warehouse_video_token.encode(), f"open:{exp}".encode(), hashlib.sha256).hexdigest()
        return f"{self.settings.warehouse_video_lan_url}/open?{urlencode({'exp': exp, 'sig': sig})}"

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
        data = dict(result.get("data") or {})
        data["video_link"] = self._warehouse_video_link()
        self._send_json(handler, HTTPStatus.OK, {"ok": True, "data": data})
